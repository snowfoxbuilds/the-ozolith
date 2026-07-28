"""The Control Node web surface (M4, hardened by ADR-0019): read-only fleet
dashboard, secret entry form, and the web terminal — Jinja + HTMX, no build
step.

Everything sits behind the one admin credential (AdminSessions); the
dashboard refreshes by HTMX polling of server-rendered fragments (ADR-0018
chose polling over SSE: one mechanism, no connection bookkeeping, and the
5s cadence is well inside the one-heartbeat acceptance bound). The secret
form writes through exactly the same store call as PUT /api/v1/secrets —
never displaying stored values.

M5 hardening: cookie-authenticated state changes and the terminal
websocket additionally pass the BrowserGuard (exact canonical Host +
Origin; bearer clients are exempt). The terminal derives its Stack from
the live container record's ``owner`` — the URL names only the node and
container, and stale heartbeats, unknown owners, and attach-less Stacks
are refused before the websocket is accepted. Concurrent terminal
sessions are capped; a refused session launches no process.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from theozolith_control import controltoml, joinstring, tls
from theozolith_control.crypto import SecretBox
from theozolith_control.origin import parse_public_origin
from theozolith_control.secretstore import SecretStore
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store
from theozolith_control.web import views
from theozolith_control.web.auth import (
    DEV_SESSION_COOKIE,
    SESSION_COOKIE,
    AdminSessions,
    BrowserGuard,
)
from theozolith_control.web.terminal import AttachError, audit, bridge, render_attach_argv

_HERE = Path(__file__).parent

FRAGMENT_POLL_SECONDS = 5  # well inside one heartbeat interval (acceptance 1)
# Config Repo parses (TOML + a git rev-parse subprocess) are cached briefly:
# the dashboard polls at 5s but desired state changes at commit cadence.
CONFIG_CACHE_SECONDS = 10.0
# Attach requires fresh heartbeat evidence: the same ~2.5-missed-beats
# bound the dashboard calls a stale node (ADR-0019).
ATTACH_FRESH_SECONDS = views.STALE_AFTER_SECONDS

# Terminal websocket close codes: auth, browser origin, no target, capacity.
WS_UNAUTHORIZED = 4401
WS_WRONG_ORIGIN = 4403
WS_NO_TARGET = 4404
WS_OVER_CAPACITY = 4429


def mount_web(
    app: FastAPI,
    settings: ControlSettings,
    store: Store,
    secret_store: SecretStore,
    box: SecretBox,
    config_loader,
) -> None:
    # The default environment autoescapes *.html — every agent-authored
    # string (transcript tails, event payloads) renders escaped.
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    def _password_record() -> str:
        # Read per login attempt: `theozolith-control set-password` takes
        # effect without a restart (it truncates the session table itself).
        try:
            return settings.admin_password_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    sessions = AdminSessions(
        store,
        password_reader=_password_record,
        admin_token=settings.admin_token,
        session_days=settings.session_days,
        secure_cookies=settings.serve_tls,
    )
    app.state.admin_sessions = sessions  # tests reach in to mint sessions
    # The exact Host/Origin contract derives from the parsed public origin
    # alone (never the bind host/port); a configured-but-invalid origin
    # raises OriginError here, so the app fails closed instead of arming a
    # guard with garbage expectations.
    guard = BrowserGuard(
        parse_public_origin(settings.public_origin) if settings.public_origin else None
    )
    app.state.browser_guard = guard  # tests assert the derived expectations

    cached_config: list = [0.0, None]  # [expires_at, DeployConfig]

    def _config():
        now = time.monotonic()
        if cached_config[1] is None or now >= cached_config[0]:
            cached_config[1] = config_loader()
            cached_config[0] = now + CONFIG_CACHE_SECONDS
        return cached_config[1]

    def _page(request: Request, name: str, context: dict) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {"poll_seconds": FRAGMENT_POLL_SECONDS, **context}
        )

    def _login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    def _origin_ok(request: Request | WebSocket) -> bool:
        """The M5 origin contract for state changes: bearer callers are
        non-browser clients and pass; everything else (cookie sessions and
        the login form itself) must carry the exact canonical Host+Origin."""
        return sessions.auth_mode(request) == "bearer" or guard.ok(request)

    def _wrong_origin() -> HTMLResponse:
        return HTMLResponse(
            "request rejected: Host/Origin does not match this deployment's canonical origin",
            status_code=403,
        )

    # -- session -----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return _page(request, "login.html", {"error": ""})

    @app.post("/login")
    async def login(request: Request):
        if not _origin_ok(request):
            return _wrong_origin()
        # Rate limit BEFORE the scrypt check: a throttled attempt must cost
        # the caller nothing but time (ADR-0023 login hardening).
        retry_after = sessions.throttled_for()
        if retry_after > 0:
            response = _page(
                request,
                "login.html",
                {"error": f"too many failed attempts — retry in {retry_after:.0f}s"},
            )
            response.status_code = 429
            response.headers["Retry-After"] = str(int(retry_after) + 1)
            return response
        if not sessions.configured:
            response = _page(
                request,
                "login.html",
                {"error": "no admin password is set — run 'theozolith-control init'"},
            )
            response.status_code = 503
            return response
        form = await request.form()
        cookie = sessions.login(str(form.get("password", "")))
        if cookie is None:
            response = _page(request, "login.html", {"error": "wrong password"})
            response.status_code = 401
            return response
        response = RedirectResponse("/", status_code=303)
        # Over TLS: the __Host- contract (Secure, Path=/, no Domain — browsers
        # refuse it anywhere but this exact host over TLS). Over plain HTTP
        # (--insecure-dev): the unprefixed name without Secure, so the dev
        # dashboard authenticates over http (a __Host-/Secure cookie would be
        # dropped by the browser and loop the login).
        response.set_cookie(
            sessions.cookie_name,
            cookie,
            httponly=True,
            samesite="strict",
            secure=sessions.secure,
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        if not _origin_ok(request):
            return _wrong_origin()
        # Server-side first: the row dies, so the cookie value is inert
        # everywhere immediately (stateful sessions, ADR-0023).
        sessions.logout(sessions.cookie_value(request))
        response = _login_redirect()
        # Clear whichever session cookie this scheme uses.
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)
        response.delete_cookie(DEV_SESSION_COOKIE, path="/", httponly=True)
        return response

    # -- dashboard ---------------------------------------------------------
    # Page and fragment handlers are deliberately plain def: FastAPI runs
    # them in the threadpool, keeping the store/config work (sync SQLite,
    # TOML parsing) off the event loop the terminal websockets live on.

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        return _page(request, "dashboard.html", {"repo": settings.repo})

    def _fragment(request: Request, name: str, context_for: dict | None = None):
        if not sessions.authorized(request):
            return HTMLResponse("admin session required", status_code=401)
        return _page(request, name, context_for or {})

    @app.get("/fragments/fleet", response_class=HTMLResponse)
    def fleet_fragment(request: Request):
        return _fragment(request, "_fleet.html", {"fleet": views.fleet_view(store, _config())})

    @app.get("/fragments/runs", response_class=HTMLResponse)
    def runs_fragment(request: Request):
        return _fragment(
            request,
            "_runs.html",
            {"runs": views.runs_view(store, settings.repo), "flags": views.flags_view(store)},
        )

    @app.get("/fragments/activity", response_class=HTMLResponse)
    def activity_fragment(request: Request):
        return _fragment(request, "_activity.html", {"events": views.activity_view(store)})

    @app.get("/fragments/errors", response_class=HTMLResponse)
    def errors_fragment(request: Request, node: str = "", component: str = ""):
        return _fragment(
            request,
            "_errors.html",
            {"errors": views.errors_view(store, node=node, component=component)},
        )

    # -- secret entry (writes through the same path as the CLI's API call) --

    @app.get("/secrets", response_class=HTMLResponse)
    def secrets_form(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        return _page(
            request,
            "secrets.html",
            {
                "names": secret_store.secret_names(),
                "stored": request.query_params.get("stored", ""),
                "channel_ok": settings.secrets_channel_ok,
            },
        )

    @app.post("/secrets")
    async def secrets_submit(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        if not _origin_ok(request):
            return _wrong_origin()
        if not settings.secrets_channel_ok:
            return HTMLResponse("secret values only transit TLS", status_code=403)
        form = await request.form()
        name = str(form.get("name", "")).strip()
        value = str(form.get("value", ""))
        if not name or not value:
            return HTMLResponse("both a name and a value are required", status_code=400)
        # The same write as PUT /api/v1/secrets/{name}: encrypted before it
        # touches the store; nothing ever reads it back out to a browser.
        secret_store.put_secret(name, box.encrypt(value))
        return RedirectResponse(f"/secrets?stored={name}", status_code=303)

    # -- settings (tier-2 tunables; ADR-0023) --------------------------------

    def _settings_context(saved: str = "", error: str = "") -> dict:
        current = controltoml.read_values(settings.config_repo)
        return {
            "origin": controltoml.read_public_origin(settings.config_repo)
            or settings.public_origin,
            "control_ip": controltoml.read_control_ip(settings.config_repo) or settings.control_ip,
            "rows": [
                {
                    "key": s.key,
                    "label": s.label,
                    "value": current[s.key],
                    "default": s.default,
                    "is_default": current[s.key] == s.default,
                }
                for s in controltoml.SETTINGS
            ],
            "saved": saved,
            "error": error,
        }

    @app.get("/settings", response_class=HTMLResponse)
    def settings_form(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        return _page(request, "settings.html", _settings_context())

    @app.post("/settings")
    async def settings_submit(request: Request):
        """The fixed-schema write path (ADR-0023): one known tier-2 key at a
        time, committed to control.toml in the Config Repo — the
        product.toml pin-bump precedent, never a free-form editor. The
        public origin is not a settings key: writes to it are refused."""
        if not sessions.authorized(request):
            return _login_redirect()
        if not _origin_ok(request):
            return _wrong_origin()
        form = await request.form()
        key = str(form.get("key", "")).strip()
        value = str(form.get("value", "")).strip()
        if key in ("public_origin", "control_ip"):
            return HTMLResponse(
                "the [control] fields are read-only — re-pointing a deployment is"
                " 'theozolith-control origin-init --force' / 'recover --ip', not a"
                " settings edit",
                status_code=403,
            )
        try:
            controltoml.set_value(settings.config_repo, key, value)
        except controltoml.ControlTomlError as exc:
            page = _page(request, "settings.html", _settings_context(error=str(exc)))
            page.status_code = 400
            return page
        return RedirectResponse(f"/settings?saved={key}", status_code=303)

    # -- join tokens (node provisioning; ADR-0023) ----------------------------

    def _join_context(minted: dict | None = None, notice: str = "") -> dict:
        return {
            "tokens": [
                {**t, "expires_in": views.elapsed(t["expires_at"] - time.time())}
                for t in store.join_tokens()
            ],
            "minted": minted,
            "notice": notice,
        }

    @app.get("/join", response_class=HTMLResponse)
    def join_page(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        return _page(request, "join.html", _join_context())

    @app.post("/join")
    async def join_create(request: Request):
        """Mint a join token and show the ready-to-paste line — the same
        machinery as `theozolith join-token create` (the API twin)."""
        if not sessions.authorized(request):
            return _login_redirect()
        if not _origin_ok(request):
            return _wrong_origin()
        form = await request.form()
        try:
            ttl = float(str(form.get("ttl_seconds") or "3600"))
            uses = int(str(form.get("uses") or "1"))
            if ttl <= 0 or uses < 1:
                raise ValueError("ttl and uses must be positive")
            if not settings.control_ip:
                raise ValueError(
                    "no persisted control IP — run 'theozolith-control init' (ADR-0031)"
                )
            fingerprint = tls.ca_fingerprint_sha256((settings.tls_dir / tls.CA_FILE).read_bytes())
        except (ValueError, OSError) as exc:
            page = _page(request, "join.html", _join_context(notice=f"cannot mint: {exc}"))
            page.status_code = 400
            return page
        # The persisted control IP (ADR-0031) — never detected at mint time.
        addr = f"{settings.control_ip}:{settings.bootstrap_port}"
        token_id, raw, expires_at = store.create_join_token(ttl_seconds=ttl, uses=uses)
        join = joinstring.compose(
            addr=addr, ca_sha256=fingerprint, token=raw, expires_at=expires_at
        )
        minted = {
            "id": token_id,
            "join_string": join,
            "provision_command": f"sudo theozolith-nodedaemon provision '{join}'",
            "install_command": f"curl -fsSL {settings.installer_url} | sudo bash -s -- '{join}'",
        }
        return _page(request, "join.html", _join_context(minted=minted))

    @app.post("/join/revoke")
    async def join_revoke(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        if not _origin_ok(request):
            return _wrong_origin()
        form = await request.form()
        revoked = store.revoke_join_token(str(form.get("id", "")))
        notice = "join token revoked" if revoked else "no such outstanding token"
        return _page(request, "join.html", _join_context(notice=notice))

    # -- the web terminal ---------------------------------------------------

    def _attach_target(node: str, container: str) -> tuple[list[str] | None, str, str]:
        """(attach argv, owning stack, error). The target is resolved from
        the live stack-container record and its Stack derived server-side
        (ADR-0022) — caller-supplied Stack identity is not an authority.
        Run containers are categorically refused (ADR-0019: Runs are
        headless; the Flight Deck and other configured container Stacks own
        interactivity). Remaining refusals, in order: unknown/wrong-node
        container, stale heartbeat, unknown owning Stack, non-container
        kind, no attach configuration, and identifier validation (forged
        heartbeat values die here, never in a shell)."""
        run_record = store.container_record(node, container)
        if run_record is not None:
            return (
                None,
                run_record["owner"],
                (
                    f"container {container!r} is a run container — run containers are"
                    " headless and never attach targets (ADR-0019)"
                ),
            )
        record = store.stack_container_record(node, container)
        if record is None:
            return None, "", f"container {container!r} is not live on {node!r} (per heartbeats)"
        if record["age_seconds"] > ATTACH_FRESH_SECONDS:
            return (
                None,
                "",
                (
                    f"heartbeat evidence for {container!r} on {node!r} is stale"
                    f" ({record['age_seconds']:.0f}s old) — refusing to attach"
                ),
            )
        owner = record["stack"]
        stack_def = next((s for s in _config().stacks if s.name == owner and s.node == node), None)
        if stack_def is None:
            return (
                None,
                owner,
                (
                    f"container {container!r} has no configured owning Stack on {node!r}"
                    f" (stack {owner!r})"
                ),
            )
        if stack_def.kind != "container":
            return (
                None,
                owner,
                f"stack {owner!r} on {node!r} is not a container-kind Stack (ADR-0019)",
            )
        if not stack_def.attach:
            return (
                None,
                owner,
                (f"stack {owner!r} on {node!r} exposes no terminal (no attach command)"),
            )
        try:
            argv = render_attach_argv(stack_def.attach, host=node, container=container)
        except AttachError as exc:
            return None, owner, str(exc)
        return argv, owner, ""

    active_terminals = [0]  # concurrent-session accounting (ADR-0019 cap)

    @app.get("/terminal", response_class=HTMLResponse)
    def terminal_page(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        node = request.query_params.get("node", "")
        container = request.query_params.get("container", "")
        argv, stack, error = _attach_target(node, container)
        return _page(
            request,
            "terminal.html",
            {
                "node": node,
                "stack": stack,
                "container": container,
                "error": error,
                "ok": argv is not None,
            },
        )

    @app.websocket("/terminal/ws")
    async def terminal_ws(websocket: WebSocket):
        mode = sessions.auth_mode(websocket)
        if mode is None:
            await websocket.close(code=WS_UNAUTHORIZED)
            return
        if mode == "cookie" and not guard.ok(websocket):
            await websocket.close(code=WS_WRONG_ORIGIN)
            return
        # Reserve a slot in one synchronous critical section — check and
        # increment with no await between them, so concurrent connects
        # cannot all pass the cap and launch excess processes (acceptance
        # 12). Every path past this point decrements in the finally.
        if active_terminals[0] >= settings.terminal_session_cap:
            await websocket.close(
                code=WS_OVER_CAPACITY,
                reason=f"terminal session cap ({settings.terminal_session_cap}) reached",
            )
            return
        active_terminals[0] += 1
        attached = False
        record: dict = {}
        reason = "error"
        try:
            node = websocket.query_params.get("node", "")
            container = websocket.query_params.get("container", "")
            argv, stack, error = _attach_target(node, container)
            if argv is None:
                await websocket.close(code=WS_NO_TARGET, reason=error[:120])
                return
            await websocket.accept()
            record = {
                "actor": "admin",
                "node": node,
                "stack": stack,
                "container": container,
                "command": argv,
            }
            audit(settings.terminal_audit_path, {"event": "attach", **record})
            attached = True
            reason = await bridge(websocket, argv)
        except asyncio.CancelledError:
            # The server cancelled this task at hang-up; the bridge already
            # killed the attach tree — record the reason, let it propagate.
            reason = "client-closed"
            raise
        finally:
            active_terminals[0] -= 1
            if attached:
                audit(
                    settings.terminal_audit_path,
                    {"event": "detach", "reason": reason, **record},
                )
