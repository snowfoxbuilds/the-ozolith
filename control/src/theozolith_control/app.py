"""The control-plane API: heartbeats/commands, typed events, claim
dispatch, and the secret store, under /api/v1.

Design rules enforced here:

- Channel invariant (restated by ADR-0016): a heartbeat response carries
  desired state and references only; telemetry payloads are advisory and
  size-capped at ingestion; the sole value payload anywhere is the
  secrets-pull response, and both secret endpoints refuse to serve unless
  the channel is TLS (or the operator explicitly started --insecure-dev).
- Coordination: GitHub owns coordination truth. The one write path is claim
  creation through the dispatch endpoint (ADR-0017, write-through); events
  are facts about the past; commands are node/docker lifecycle, which the
  Control Node does own.
- Unknown event types are accepted and stored (the typed-event extension
  point): custom Stacks get visibility without product changes.

Bodies are plain JSON validated by hand — the schemas are small, settled in
ADR-0015/0018, and shared with stdlib-only clients that cannot see pydantic.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from theozolith_control.configrepo import ConfigRepoError, DeployConfig, load_config
from theozolith_control.crypto import SecretBox
from theozolith_control.dispatch import Dispatcher
from theozolith_control.settings import ControlSettings
from theozolith_control.store import EVENT_PROGRESS, Store

COMMAND_VERBS = ("drain", "recycle", "update", "rebuild")

# Ingestion size caps (ADR-0016): the transcript tail inside a progress
# event, and any single event payload. Oversized tails are truncated (the
# tail end is the interesting part); anything still over the payload cap is
# refused — the control database is a bounded cache, never an archive.
PROGRESS_TAIL_LIMIT = 8_192
EVENT_PAYLOAD_LIMIT = 32_768

DISPATCH_ROLES = ("worker", "reviewer")


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _authorize(request: Request, expected: str, who: str) -> None:
    if not hmac.compare_digest(_bearer(request), expected):
        raise HTTPException(status_code=401, detail=f"{who} token required")


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body


def _require(body: dict[str, Any], key: str, kind: type) -> Any:
    value = body.get(key)
    if not isinstance(value, kind) or (kind is str and not value):
        raise HTTPException(status_code=400, detail=f"{key!r} must be a non-empty {kind.__name__}")
    return value


def _list_of_dicts(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = body.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HTTPException(status_code=400, detail=f"{key!r} must be a list of objects")
    return value


def _capped_event(body: dict[str, Any]) -> dict[str, Any]:
    """Apply the ingestion size caps; raises 413 past the payload cap."""
    if body.get("type") == EVENT_PROGRESS:
        tail = body.get("transcript_tail")
        if isinstance(tail, str) and len(tail) > PROGRESS_TAIL_LIMIT:
            body = {**body, "transcript_tail": tail[-PROGRESS_TAIL_LIMIT:]}
    if len(json.dumps(body)) > EVENT_PAYLOAD_LIMIT:
        raise HTTPException(
            status_code=413,
            detail=f"event payload exceeds {EVENT_PAYLOAD_LIMIT} bytes (ADR-0016 ingestion cap)",
        )
    return body


def create_app(
    settings: ControlSettings,
    store: Store,
    box: SecretBox,
    *,
    config_loader=None,
    github_client=None,
) -> FastAPI:
    load = config_loader or (lambda: load_config(settings.config_repo))
    app = FastAPI(title="TheOzolith Control Node", docs_url=None, redoc_url=None)
    # Shared with the CLI-driven sweeps (janitor --once against a live server
    # is still a separate process; these are for the in-process loops).
    app.state.settings = settings
    app.state.store = store

    if github_client is None and settings.coordination_jobs_enabled:
        from theozolith_worker.githubapi import GitHubClient

        github_client = GitHubClient(
            settings.repo or "", settings.github_token or "", settings.api_url
        )
    dispatcher = Dispatcher(store, github_client) if github_client is not None else None
    app.state.github_client = github_client

    def _config() -> DeployConfig:
        try:
            return load()
        except ConfigRepoError as exc:
            # A broken Config Repo must not take heartbeats down with it:
            # nodes keep their cached desired state (ADR-0006 degraded mode).
            raise HTTPException(status_code=500, detail=f"config repo error: {exc}") from exc

    def _secrets_channel_guard() -> None:
        if not settings.secrets_channel_ok:
            raise HTTPException(
                status_code=403,
                detail="secret values only transit TLS (start with TLS or --insecure-dev)",
            )

    @app.get("/api/v1/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    # -- node channel (node token) -----------------------------------------

    @app.post("/api/v1/nodes/register")
    async def register(request: Request) -> dict[str, Any]:
        _authorize(request, settings.node_token, "node")
        body = await _json_body(request)
        node = _require(body, "node", str)
        store.touch_node(node, str(body.get("version", "")))
        return {"ok": True}

    @app.post("/api/v1/heartbeats")
    async def heartbeat(request: Request) -> dict[str, Any]:
        _authorize(request, settings.node_token, "node")
        body = await _json_body(request)
        node = _require(body, "node", str)
        store.touch_node(node, str(body.get("version", "")))
        store.record_status(
            node,
            _list_of_dicts(body, "stacks"),
            _list_of_dicts(body, "run_containers"),
            _list_of_dicts(body, "images"),
        )
        completed = body.get("completed_commands", [])
        if isinstance(completed, list):
            store.complete_commands(node, [i for i in completed if isinstance(i, int)])
        # Queue-behind visibility: commands the daemon is holding behind an
        # in-flight Run ride the heartbeat as deferrals (NODE-SUBSTRATE).
        store.record_deferrals(node, _list_of_dicts(body, "deferred_commands"))
        return {
            "commands": store.pending_commands(node),
            "config": _config().desired_state_for(node),
        }

    @app.post("/api/v1/events")
    async def ingest_event(request: Request) -> dict[str, Any]:
        _authorize(request, settings.node_token, "node")
        body = await _json_body(request)
        _require(body, "type", str)
        store.record_event(_capped_event(body))
        return {"ok": True}

    @app.post("/api/v1/dispatch")
    async def dispatch(request: Request) -> dict[str, Any]:
        """The one claim path (ADR-0017): grant for Workers, discovery for
        the Reviewer. Requires the control PAT — without it the pipeline
        pauses (no second claim path exists)."""
        _authorize(request, settings.node_token, "node")
        if dispatcher is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "claim dispatch requires THEOZOLITH_REPO and CONTROL_GITHUB_TOKEN"
                    " on the Control Node (ADR-0017)"
                ),
            )
        body = await _json_body(request)
        role = _require(body, "role", str)
        if role not in DISPATCH_ROLES:
            raise HTTPException(
                status_code=400, detail=f"role must be one of {', '.join(DISPATCH_ROLES)}"
            )
        worker = _require(body, "worker", str)
        login = _require(body, "login", str)
        node = str(body.get("node", ""))
        if role == "worker":
            return dispatcher.grant_work(worker, node, login)
        return dispatcher.review_targets(worker, node, login)

    @app.post("/api/v1/secrets/pull")
    async def secrets_pull(request: Request) -> dict[str, Any]:
        _authorize(request, settings.node_token, "node")
        _secrets_channel_guard()
        body = await _json_body(request)
        node = _require(body, "node", str)
        names = body.get("names")
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            raise HTTPException(status_code=400, detail="'names' must be a list of strings")
        allowed = _config().secret_names_for(node)
        denied = sorted(set(names) - allowed)
        if denied:
            # Node-scoping: a node may pull only what its placed Stacks
            # reference — a node with no Stacks may pull nothing.
            raise HTTPException(
                status_code=403,
                detail=f"node {node!r} has no Stack referencing: {', '.join(denied)}",
            )
        values: dict[str, str] = {}
        for name in names:
            token = store.get_secret_token(name)
            if token is None:
                raise HTTPException(status_code=404, detail=f"secret {name!r} has no stored value")
            values[name] = box.decrypt(token)
        return {"secrets": values}

    # -- operator surface (admin token) --------------------------------------

    @app.put("/api/v1/secrets/{name}")
    async def secret_set(name: str, request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        _secrets_channel_guard()
        body = await _json_body(request)
        value = _require(body, "value", str)
        # Encrypted before it touches the store; write-only entry — no API
        # returns a stored value to an admin (values are pull-only, to nodes).
        store.put_secret(name, box.encrypt(value))
        return {"ok": True}

    @app.get("/api/v1/secrets")
    async def secret_names(request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        return {"names": store.secret_names()}

    @app.post("/api/v1/commands")
    async def queue_command(request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        body = await _json_body(request)
        node = _require(body, "node", str)
        verb = _require(body, "verb", str)
        if verb not in COMMAND_VERBS:
            raise HTTPException(
                status_code=400, detail=f"verb must be one of {', '.join(COMMAND_VERBS)}"
            )
        target = body.get("target")
        if target is not None and not isinstance(target, str):
            raise HTTPException(status_code=400, detail="'target' must be a string or absent")
        force = bool(body.get("force", False))
        command_id = store.queue_command(node, verb, target, force)
        if verb in ("recycle", "update") and store.release_quarantine(node):
            # Recycle/update is one of the two human quarantine releases
            # (ADR-0016; the other is the explicit unquarantine).
            store.record_janitor_action(
                0, "", "", f"node {node}: quarantine released by {verb} command"
            )
        return {"id": command_id}

    @app.post("/api/v1/nodes/{node}/quarantine/release")
    async def unquarantine(node: str, request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        released = store.release_quarantine(node)
        if released:
            store.record_janitor_action(0, "", "", f"node {node}: quarantine released by operator")
        return {"released": released}

    @app.get("/api/v1/state")
    async def state(request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        return store.fleet_state()

    @app.get("/api/v1/flags")
    async def flags(request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        return {
            "zombie_flags": store.zombie_flags(),
            "janitor_actions": store.janitor_actions(),
            "malformed_states": store.malformed_states(),
            "quarantines": store.quarantines(),
        }

    return app
