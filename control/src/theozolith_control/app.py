"""The control-plane API (ADR-0015): heartbeats/commands, typed events,
claim intents, and the secret store, under /api/v1.

Design rules enforced here:

- Channel invariant: a heartbeat response carries desired state and
  references only; the sole value payload anywhere is the secrets-pull
  response, and both secret endpoints refuse to serve unless the channel is
  TLS (or the operator explicitly started the server --insecure-dev).
- Advisory only (ADR-0002): nothing in this API creates coordination state.
  A claim-intent grant is not a claim; events are facts about the past;
  commands are node/docker lifecycle, which the Control Node does own.
- Unknown event types are accepted and stored (the typed-event extension
  point): custom Stacks get visibility without product changes.

Bodies are plain JSON validated by hand — the schemas are small, settled in
ADR-0015, and shared with stdlib-only clients that cannot see pydantic.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from theozolith_control.configrepo import ConfigRepoError, DeployConfig, load_config
from theozolith_control.crypto import SecretBox
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store

COMMAND_VERBS = ("drain", "recycle", "update", "rebuild")


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


def create_app(
    settings: ControlSettings,
    store: Store,
    box: SecretBox,
    *,
    config_loader=None,
) -> FastAPI:
    load = config_loader or (lambda: load_config(settings.config_repo))
    app = FastAPI(title="TheOzolith Control Node", docs_url=None, redoc_url=None)
    # Shared with the CLI-driven sweeps (janitor --once against a live server
    # is still a separate process; these are for the in-process loops).
    app.state.settings = settings
    app.state.store = store

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
        return {
            "commands": store.pending_commands(node),
            "config": _config().desired_state_for(node),
        }

    @app.post("/api/v1/events")
    async def ingest_event(request: Request) -> dict[str, Any]:
        _authorize(request, settings.node_token, "node")
        body = await _json_body(request)
        _require(body, "type", str)
        store.record_event(body)
        return {"ok": True}

    @app.post("/api/v1/claim-intents")
    async def claim_intent(request: Request) -> dict[str, Any]:
        _authorize(request, settings.node_token, "node")
        body = await _json_body(request)
        issue = _require(body, "issue", int)
        worker = _require(body, "worker", str)
        allow, holder = store.claim_intent(issue, worker, settings.claim_ttl_seconds)
        # Advisory answer, never a claim: GitHub assign-and-verify remains
        # the only authority (ADR-0002).
        return {"allow": allow, "holder": holder}

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
        command_id = store.queue_command(node, verb, target)
        return {"id": command_id}

    @app.get("/api/v1/state")
    async def state(request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        return store.fleet_state()

    @app.get("/api/v1/audits")
    async def audits(request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        return {
            "audit_findings": store.audit_findings(),
            "janitor_actions": store.janitor_actions(),
        }

    return app
