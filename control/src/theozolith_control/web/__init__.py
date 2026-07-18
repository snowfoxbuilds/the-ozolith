"""The Control Node web surface (M4): read-only fleet dashboard, secret
entry form, and the web terminal — Jinja + HTMX, no build step.

Everything sits behind the one admin credential (AdminSessions); the
dashboard refreshes by HTMX polling of server-rendered fragments (ADR-0018
chose polling over SSE: one mechanism, no connection bookkeeping, and the
5s cadence is well inside the one-heartbeat acceptance bound). The secret
form writes through exactly the same store call as PUT /api/v1/secrets —
never displaying stored values — and the terminal websocket hands off to
the PTY bridge after the same auth check plus the config/liveness gates.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from theozolith_control.crypto import SecretBox
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store
from theozolith_control.web import views
from theozolith_control.web.auth import SESSION_COOKIE, AdminSessions
from theozolith_control.web.terminal import audit, bridge

_HERE = Path(__file__).parent

FRAGMENT_POLL_SECONDS = 5  # well inside one heartbeat interval (acceptance 1)
# Config Repo parses (TOML + a git rev-parse subprocess) are cached briefly:
# the dashboard polls at 5s but desired state changes at commit cadence.
CONFIG_CACHE_SECONDS = 10.0


def mount_web(
    app: FastAPI,
    settings: ControlSettings,
    store: Store,
    box: SecretBox,
    config_loader,
) -> None:
    # The default environment autoescapes *.html — every agent-authored
    # string (transcript tails, event payloads) renders escaped.
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    sessions = AdminSessions(settings.admin_token)
    app.state.admin_sessions = sessions  # tests reach in to mint sessions

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

    # -- session -----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return _page(request, "login.html", {"error": ""})

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        cookie = sessions.login(str(form.get("token", "")))
        if cookie is None:
            response = _page(request, "login.html", {"error": "wrong admin credential"})
            response.status_code = 401
            return response
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        response = _login_redirect()
        response.delete_cookie(SESSION_COOKIE)
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

    # -- secret entry (writes through the same path as the CLI's API call) --

    @app.get("/secrets", response_class=HTMLResponse)
    def secrets_form(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        return _page(
            request,
            "secrets.html",
            {
                "names": store.secret_names(),
                "stored": request.query_params.get("stored", ""),
                "channel_ok": settings.secrets_channel_ok,
            },
        )

    @app.post("/secrets")
    async def secrets_submit(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        if not settings.secrets_channel_ok:
            return HTMLResponse("secret values only transit TLS", status_code=403)
        form = await request.form()
        name = str(form.get("name", "")).strip()
        value = str(form.get("value", ""))
        if not name or not value:
            return HTMLResponse("both a name and a value are required", status_code=400)
        # The same write as PUT /api/v1/secrets/{name}: encrypted before it
        # touches the store; nothing ever reads it back out to a browser.
        store.put_secret(name, box.encrypt(value))
        return RedirectResponse(f"/secrets?stored={name}", status_code=303)

    # -- the web terminal ---------------------------------------------------

    def _attach_target(node: str, stack: str, container: str) -> tuple[str | None, str]:
        """(attach command, error). Enforces the two gates: an attach
        template must be configured and the container must be live."""
        stack_def = next((s for s in _config().stacks if s.name == stack and s.node == node), None)
        if stack_def is None or not stack_def.attach:
            return None, f"stack {stack!r} on {node!r} exposes no terminal (no attach command)"
        live = any(
            c["node"] == node and c["name"] == container
            for c in store.fleet_state()["run_containers"]
        )
        if not live:
            return None, f"container {container!r} is not live on {node!r} (per heartbeats)"
        return stack_def.attach.format(host=node, container=container), ""

    @app.get("/terminal", response_class=HTMLResponse)
    def terminal_page(request: Request):
        if not sessions.authorized(request):
            return _login_redirect()
        node = request.query_params.get("node", "")
        stack = request.query_params.get("stack", "")
        container = request.query_params.get("container", "")
        command, error = _attach_target(node, stack, container)
        return _page(
            request,
            "terminal.html",
            {
                "node": node,
                "stack": stack,
                "container": container,
                "error": error,
                "ok": command is not None,
            },
        )

    @app.websocket("/terminal/ws")
    async def terminal_ws(websocket: WebSocket):
        if not sessions.authorized(websocket):
            await websocket.close(code=4401)
            return
        node = websocket.query_params.get("node", "")
        stack = websocket.query_params.get("stack", "")
        container = websocket.query_params.get("container", "")
        command, error = _attach_target(node, stack, container)
        if command is None:
            await websocket.close(code=4404, reason=error[:120])
            return
        await websocket.accept()
        record = {
            "actor": "admin",
            "node": node,
            "stack": stack,
            "container": container,
            "command": command,
        }
        audit(settings.terminal_audit_path, {"event": "attach", **record})
        try:
            await bridge(websocket, command)
        finally:
            audit(settings.terminal_audit_path, {"event": "detach", **record})
