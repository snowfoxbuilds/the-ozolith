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

import asyncio
import contextlib
import hmac
import json
import os
import re
import tempfile
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

from theozolith_control import configdist, configrepo, controltoml, joinstring, product, tls
from theozolith_control.configrepo import ConfigRepoError, DeployConfig, load_config
from theozolith_control.crypto import SecretBox
from theozolith_control.dispatch import Dispatcher
from theozolith_control.secretstore import SecretStore
from theozolith_control.settings import ControlSettings
from theozolith_control.store import COMMAND_VERBS, EVENT_ERROR, EVENT_PROGRESS, Store

# Ingestion size caps (ADR-0016): the transcript tail inside a progress
# event, and any single event payload. Oversized tails are truncated (the
# tail end is the interesting part); anything still over the payload cap is
# refused — the control database is a bounded cache, never an archive.
# theozolith.error context/message are likewise truncated, never refused
# (2026-07-21 grilling: an error summary must not itself be droppable for
# being too verbose).
PROGRESS_TAIL_LIMIT = 8_192
ERROR_CONTEXT_LIMIT = 8_192
ERROR_MESSAGE_LIMIT = 2_048
EVENT_PAYLOAD_LIMIT = 32_768

DISPATCH_ROLES = ("implementer", "reviewer")

# Events read view (ADR-0038): default and maximum page size.
EVENTS_PAGE_DEFAULT = 100
EVENTS_PAGE_MAX = 500

# Join-token defaults (ADR-0023): short-lived and single-use unless the
# operator widens them for a batch provisioning session.
JOIN_TOKEN_TTL_SECONDS = 3600.0
JOIN_TOKEN_USES = 1

JOIN_TOKEN_REJECTED = "join token rejected (expired, consumed, or revoked)"


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _authorize(request: Request, expected: str, who: str) -> None:
    if not expected or not hmac.compare_digest(_bearer(request), expected):
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
    if body.get("type") == EVENT_ERROR:
        context = body.get("context")
        if isinstance(context, str) and len(context) > ERROR_CONTEXT_LIMIT:
            # Keep the tail: with a traceback the innermost frames matter.
            body = {**body, "context": context[-ERROR_CONTEXT_LIMIT:]}
        message = body.get("message")
        if isinstance(message, str) and len(message) > ERROR_MESSAGE_LIMIT:
            body = {**body, "message": message[:ERROR_MESSAGE_LIMIT]}
    if len(json.dumps(body)) > EVENT_PAYLOAD_LIMIT:
        raise HTTPException(
            status_code=413,
            detail=f"event payload exceeds {EVENT_PAYLOAD_LIMIT} bytes (ADR-0016 ingestion cap)",
        )
    return body


def create_app(
    settings: ControlSettings,
    store: Store,
    secret_store: SecretStore,
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
    app.state.secret_store = secret_store

    if github_client is None and settings.coordination_jobs_enabled:
        from theozolith_worker.githubapi import GitHubClient

        github_client = GitHubClient(
            settings.repo or "", settings.github_token or "", settings.api_url
        )
    dispatcher = Dispatcher(store, github_client) if github_client is not None else None
    app.state.github_client = github_client

    # Lint warnings are printed once per distinct config commit, not on every
    # heartbeat — the journal stays readable and a repo edit re-surfaces them.
    warned_commit = ""

    def _config() -> DeployConfig:
        nonlocal warned_commit
        try:
            config = load()
        except ConfigRepoError as exc:
            # A broken Config Repo must not take heartbeats down with it:
            # nodes keep their cached desired state (ADR-0006 degraded mode).
            raise HTTPException(status_code=500, detail=f"config repo error: {exc}") from exc
        if config.warnings and config.commit != warned_commit:
            for warning in config.warnings:
                print(f"config warning: {warning}", flush=True)
            warned_commit = config.commit
        return config

    def _secrets_channel_guard() -> None:
        if not settings.secrets_channel_ok:
            raise HTTPException(
                status_code=403,
                detail="secret values only transit TLS (start with TLS or --insecure-dev)",
            )

    @app.get("/api/v1/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    # -- node channel (per-node tokens, ADR-0023) ---------------------------

    def _node_auth(request: Request, declared: str | None = None) -> str:
        """Resolve the per-node bearer token to its node. Provisioning is
        registration: an unknown or revoked token never creates anything —
        401. A declared node name that is not the token's node is refused:
        per-node tokens exist exactly so no node can speak as another."""
        node = secret_store.node_for_token(_bearer(request))
        if node is None:
            raise HTTPException(status_code=401, detail="per-node token required (provision first)")
        if declared and declared != node:
            raise HTTPException(
                status_code=403,
                detail=f"token belongs to node {node!r}, request claims {declared!r}",
            )
        return node

    @app.post("/api/v1/heartbeats")
    async def heartbeat(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        node = _require(body, "node", str)
        if secret_store.node_for_token(_bearer(request)) is None:
            # Rejected, and never a node record — but surfaced: the sighting
            # (self-declared name, source address, last seen) is the
            # unregistered-nodes view, the re-provision worklist after a
            # stale-backup recovery (ADR-0023/0024). Advisory only.
            client = request.client
            store.record_unregistered(node, client.host if client else "")
            raise HTTPException(status_code=401, detail="per-node token required (provision first)")
        _node_auth(request, node)
        # A valid heartbeat settles any stale sightings under this name
        # (e.g. the window between provisioning and the first heartbeat).
        store.clear_unregistered(node)
        # Field presence is load-bearing (ADR-0042): a heartbeat that OMITS
        # drivers_hash is a legacy/daemon-less client (fail-open eligible); a
        # heartbeat that includes it — even as '' — is a current daemon reporting
        # its verified applied tree (or the explicit absence of one). Carry the
        # nullable value, never a truthiness collapse.
        reported_drivers = str(body["drivers_hash"]) if "drivers_hash" in body else None
        reported_built_against = str(body.get("drivers_built_against", ""))
        store.touch_node(
            node,
            str(body.get("version", "")),
            drivers_hash=reported_drivers,
            drivers_built_against=reported_built_against,
        )
        store.record_status(
            node,
            _list_of_dicts(body, "stacks"),
            _list_of_dicts(body, "run_containers"),
            _list_of_dicts(body, "images"),
            stack_containers=_list_of_dicts(body, "stack_containers"),
        )
        completed = body.get("completed_commands", [])
        if isinstance(completed, list):
            store.complete_commands(node, [i for i in completed if isinstance(i, int)])
        # Queue-behind visibility: commands the daemon is holding behind an
        # in-flight Run ride the heartbeat as deferrals (NODE-SUBSTRATE).
        store.record_deferrals(node, _list_of_dicts(body, "deferred_commands"))
        config_doc = _config().desired_state_for(node)
        # Registry pull credentials (ADR-0049): names-only references so a node
        # building a private-base recipe knows which stored credential feeds
        # `docker build`. Derived from the running recipes' bases (the images
        # already scoped into config_doc), filtered to credentials ACTUALLY
        # STORED — a public-base fleet sees no new key, and the channel
        # invariant holds (the value rides the node-scoped secrets pull, never
        # the heartbeat). Scoping for the pull is secret_names_for's union.
        registry_secrets: dict[str, str] = {}
        for image in config_doc.get("images", []):
            base = str(image.get("base", ""))
            if not base:
                continue
            host = configrepo.registry_host(base)
            secret_name = configrepo.REGISTRY_SECRET_PREFIX + host
            if secret_store.get_secret_token(secret_name) is not None:
                registry_secrets[host] = secret_name
        if registry_secrets:
            config_doc["registry_secrets"] = registry_secrets
        # Tier-2 cadence settings ride desired state (ADR-0023: control.toml
        # replaces per-node env files) — declarations, never values.
        config_doc["heartbeat_seconds"] = settings.heartbeat_seconds
        config_doc["stop_grace_seconds"] = settings.stop_grace_seconds
        pin = config_doc.get("product_version") or ""
        if pin:
            # Developer-path builds (ADR-0015 amendment 2026-07-22): when the
            # pinned version has a served artifact set, the update installs
            # those wheels instead of pulling a published release — the
            # heartbeat carries filename REFERENCES only (channel invariant).
            version_dir = settings.artifacts_dir / pin
            if version_dir.is_dir():
                config_doc["product_artifacts"] = sorted(
                    p.name for p in version_dir.iterdir() if p.name.endswith(".whl")
                )
        # Pin-convergence watch (ADR-0015 revision): keyed on the REPORTED
        # version, never command acks. Persistent off-pin escalates: one
        # restart command at the threshold, a theozolith.error after that —
        # the node stays dispatch-ineligible until a human intervenes.
        # Deferring is NOT diverging (issue #8): while the daemon reports its
        # update queued behind an in-flight Run, the ladder resets — counting
        # resumes the moment the deferral clears, so a genuinely stuck node
        # still climbs to restart and escalation.
        reported = str(body.get("version", ""))
        action = store.observe_version(
            node, reported, pin, settings.offpin_beats, deferred=bool(body.get("update_deferred"))
        )
        if action == "escalated":
            store.record_event(
                {
                    "type": EVENT_ERROR,
                    "node": node,
                    "component": "control-node",
                    "error_class": "update-not-converging",
                    "message": (
                        f"node {node} still reports product version {reported} after a"
                        f" restart; the pin is {pin} — the node stays ineligible for"
                        " dispatch until a human intervenes"
                    ),
                }
            )
        # Config-distribution convergence watch (ADR-0042): the exact analog of
        # the pin watch — keyed on the REPORTED drivers-hash, same restart-then-
        # error ladder, same knob. The reference desired hash rode config_doc.
        desired_drivers = str(config_doc.get("drivers_hash") or "")
        drivers_action = store.observe_drivers(
            node,
            reported_drivers,
            desired_drivers,
            settings.offpin_beats,
            deferred=bool(body.get("drivers_deferred")),
        )
        if drivers_action == "escalated":
            store.record_event(
                {
                    "type": EVENT_ERROR,
                    "node": node,
                    "component": "control-node",
                    "error_class": "config-dist-not-converging",
                    "message": (
                        f"node {node} still reports config distribution"
                        f" {reported_drivers or '(none)'} after a restart; the"
                        f" deployment is {desired_drivers} — the node stays ineligible"
                        " for dispatch until a human intervenes"
                    ),
                }
            )
        return {
            "commands": store.pending_commands(node),
            "config": config_doc,
        }

    @app.post("/api/v1/events")
    async def ingest_event(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        # An event declaring a node must come from that node's token —
        # events are facts about the past, and the past has an author.
        _node_auth(request, str(body.get("node", "")) or None)
        _require(body, "type", str)
        store.record_event(_capped_event(body))
        return {"ok": True}

    @app.post("/api/v1/dispatch")
    async def dispatch(request: Request) -> dict[str, Any]:
        """The one claim path (ADR-0017): grant for Workers, discovery for
        the Reviewer. Requires the control PAT — without it the pipeline
        pauses (no second claim path exists)."""
        if dispatcher is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "claim dispatch requires THEOZOLITH_REPO and CONTROL_GITHUB_TOKEN"
                    " on the Control Node (ADR-0017)"
                ),
            )
        body = await _json_body(request)
        # Drivers are node-resident (ADR-0013): they present the token of
        # the node that supervises them, injected by its daemon.
        node = _node_auth(request, str(body.get("node", "")) or None)
        role = _require(body, "role", str)
        if role not in DISPATCH_ROLES:
            raise HTTPException(
                status_code=400, detail=f"role must be one of {', '.join(DISPATCH_ROLES)}"
            )
        worker = _require(body, "driver", str)
        login = _require(body, "login", str)
        # The pin-eligibility gate (ADR-0015 revision) needs the recorded
        # pin. Fail-open on a broken Config Repo: an unreadable repo must
        # not silence dispatch — it is loudly visible everywhere else.
        # The same fail-open-on-broken-repo posture covers the config-
        # distribution gate (ADR-0042): an unreadable repo must not silence
        # dispatch — it is loudly visible everywhere else.
        try:
            config = _config()
            pin = config.product_version
            drivers_hash = config.drivers_hash
        except HTTPException:
            pin = ""
            drivers_hash = ""
        # Off the event loop: the grant path does real GitHub round-trips
        # (with rate-limit sleeps) that must never stall heartbeats or the
        # terminal websockets. The dispatcher's own lock still serializes.
        if role == "implementer":
            return await asyncio.to_thread(
                lambda: dispatcher.grant_work(
                    worker, node, login, pin=pin, drivers_hash=drivers_hash
                )
            )
        return await asyncio.to_thread(
            lambda: dispatcher.review_targets(
                worker, node, login, pin=pin, drivers_hash=drivers_hash
            )
        )

    @app.post("/api/v1/secrets/pull")
    async def secrets_pull(request: Request) -> dict[str, Any]:
        _secrets_channel_guard()
        body = await _json_body(request)
        node = _require(body, "node", str)
        # The node-scoping seam was self-declared identity (ADR-0015's
        # accepted risk); per-node tokens close it — the pull filter now
        # keys on a cryptographically attributed node name.
        _node_auth(request, node)
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
            token = secret_store.get_secret_token(name)
            if token is None:
                raise HTTPException(status_code=404, detail=f"secret {name!r} has no stored value")
            values[name] = box.decrypt(token)
        return {"secrets": values}

    # -- provisioning: the join-token exchange (ADR-0023) --------------------

    @app.post("/api/v1/join/exchange")
    async def join_exchange(request: Request) -> dict[str, Any]:
        """The sole way a node comes to exist. The join token IS the
        credential (no bearer header); it is consumed here, over verified
        TLS only — the response carries the freshly minted per-node token."""
        _secrets_channel_guard()
        body = await _json_body(request)
        node = _require(body, "node", str)
        if not product.safe_segment(node) or len(node) > 64:
            raise HTTPException(status_code=400, detail=f"node name {node!r} is not usable")
        token_hex = _require(body, "token", str)
        try:
            raw = bytes.fromhex(token_hex)
        except ValueError:
            raise HTTPException(status_code=400, detail="'token' must be hex") from None
        if not store.redeem_join_token(raw):
            # One deliberate answer for expired, consumed, revoked, and
            # never-existed: nothing is persisted, node-side text stays put.
            raise HTTPException(status_code=401, detail=JOIN_TOKEN_REJECTED)
        # Re-provisioning an already-known node ROTATES its token (mint
        # replaces) — the IP-change recovery path is one re-paste per node
        # (ADR-0023 § node channel addressing).
        node_token = secret_store.mint_node_token(node)
        store.touch_node(node)
        store.clear_unregistered(node)
        # The node channel is IP-only (ADR-0023 as amended 2026-07-28): the
        # answer echoes the IP-based URL the node just verified — scheme +
        # the address it dialed (its Host header) — never the browser-only
        # slug origin. Nodes have zero DNS dependency.
        dialed = request.headers.get("host", "")
        return {
            "node": node,
            "node_token": node_token,
            "control_url": f"https://{dialed}" if dialed else "",
            "heartbeat_seconds": settings.heartbeat_seconds,
        }

    # -- operator surface (admin token) --------------------------------------

    @app.put("/api/v1/secrets/{name}")
    async def secret_set(name: str, request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        _secrets_channel_guard()
        body = await _json_body(request)
        value = _require(body, "value", str)
        # A managed registry pull credential (ADR-0049) must be shape-valid —
        # a plausible host in the name, a `<user>:<token>` value — or ingest
        # and the node cannot use it; every other name stays shape-blind.
        try:
            configrepo.validate_registry_secret(name, value)
        except ConfigRepoError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Encrypted before it touches the store; write-only entry — no API
        # returns a stored value to an admin (values are pull-only, to nodes).
        secret_store.put_secret(name, box.encrypt(value))
        return {"ok": True}

    @app.get("/api/v1/secrets")
    async def secret_names(request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        return {"names": secret_store.secret_names()}

    # -- join tokens + node revocation (admin; ADR-0023) ----------------------

    def _join_material(explicit_addr: str) -> tuple[str, str]:
        """(addr, ca fingerprint) for a join string. The CA must exist —
        a join string pins its fingerprint or it does not exist — and the
        default address is the init-persisted control IP (ADR-0031): never
        detected at mint time, so a containerized Control Node can never
        leak its bridge address into a paste."""
        try:
            fingerprint = tls.ca_fingerprint_sha256((settings.tls_dir / tls.CA_FILE).read_bytes())
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"no usable CA at {settings.tls_dir} — run 'theozolith init' first ({exc})",
            ) from exc
        addr = explicit_addr
        if not addr:
            if not settings.control_ip:
                raise HTTPException(
                    status_code=409,
                    detail="no persisted control IP — run 'theozolith init'"
                    " (ADR-0031), or pass an explicit 'addr'",
                )
            addr = f"{settings.control_ip}:{settings.bootstrap_port}"
        return addr, fingerprint

    @app.post("/api/v1/join-tokens")
    async def join_token_create(request: Request) -> dict[str, Any]:
        """Mint a join token and answer the complete paste (ADR-0023: the
        operator never composes the line). Default 1h TTL, single use;
        ttl_seconds/uses widen it for batch provisioning sessions."""
        _authorize(request, settings.admin_token, "admin")
        body = await _json_body(request)
        ttl = body.get("ttl_seconds", JOIN_TOKEN_TTL_SECONDS)
        uses = body.get("uses", JOIN_TOKEN_USES)
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
            raise HTTPException(status_code=400, detail="'ttl_seconds' must be a positive number")
        if not isinstance(uses, int) or isinstance(uses, bool) or uses < 1:
            raise HTTPException(status_code=400, detail="'uses' must be a positive integer")
        explicit_addr = body.get("addr", "")
        if not isinstance(explicit_addr, str):
            raise HTTPException(status_code=400, detail="'addr' must be a string or absent")
        addr, fingerprint = _join_material(explicit_addr)
        token_id, raw, expires_at = store.create_join_token(ttl_seconds=float(ttl), uses=uses)
        join = joinstring.compose(
            addr=addr, ca_sha256=fingerprint, token=raw, expires_at=expires_at
        )
        return {
            "id": token_id,
            "join_string": join,
            # Node already installed: one paste. Fresh box: the installer
            # rides a pre-trusted channel (GitHub release HTTPS) and hands
            # off to provision as its final step — never the bootstrap
            # listener (ADR-0023: code never rides plaintext).
            "provision_command": f"sudo theozolith-nodedaemon provision '{join}'",
            "install_command": (f"curl -fsSL {settings.installer_url} | sudo bash -s -- '{join}'"),
            "expires_at": expires_at,
            "uses": uses,
            # The CA fingerprint the node verifies (it is inside the opaque
            # join string too). Surfaced so an operator has one trusted print
            # to check a browser-downloaded ca.pem against (OZ-01).
            "ca_sha256": fingerprint,
        }

    @app.get("/api/v1/join-tokens")
    async def join_token_list(request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        return {"tokens": store.join_tokens()}

    @app.delete("/api/v1/join-tokens/{token_id}")
    async def join_token_revoke(token_id: str, request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        return {"revoked": store.revoke_join_token(token_id)}

    @app.post("/api/v1/nodes/{node}/revoke")
    async def node_revoke(node: str, request: Request) -> dict[str, Any]:
        """Remove a node from the fleet: per-node tokens never expire, so
        leaving the fleet is explicit revocation (ADR-0023). Its heartbeats
        401 from now on and surface as unregistered sightings."""
        _authorize(request, settings.admin_token, "admin")
        return {"revoked": secret_store.revoke_node_token(node)}

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

    # -- the two update paths, one machinery (ADR-0015, 2026-07-22) ----------

    @app.post("/api/v1/product/update")
    async def product_update(request: Request) -> dict[str, Any]:
        """Pin ``version`` in the Config Repo (committed) and fan the update
        out to every known node. Control is never a Stack (ADR-0035), so no
        fan-out ordering exists for it: the Control Node updates itself
        last through its own os.execv path (ADR-0015) after the fan-out is
        queued. Re-running with a previous version re-pins and redeploys
        (rollback)."""
        _authorize(request, settings.admin_token, "admin")
        body = await _json_body(request)
        version = _require(body, "version", str)
        previous = product.read_pin(settings.config_repo)
        try:
            product.write_pin(settings.config_repo, version)
        except product.ProductError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Cache, not archive: at most the pinned + previous artifact sets
        # survive a pin change (the previous set is the rollback path).
        product.prune_artifacts(settings.artifacts_dir, {version, previous} - {""})
        nodes = sorted(n["name"] for n in store.fleet_state()["nodes"])
        for node in nodes:
            store.queue_command(node, "update", None, False)
            if store.release_quarantine(node):
                store.record_janitor_action(
                    0, "", "", f"node {node}: quarantine released by update fan-out"
                )
        return {"version": version, "queued": nodes}

    @app.put("/api/v1/product/artifacts/{version}/{filename}")
    async def upload_artifact(version: str, filename: str, request: Request) -> dict[str, Any]:
        """`theozolith build` uploads its wheels here; nodes pull them back
        via the GET twin — nodes never pull source and never build."""
        _authorize(request, settings.admin_token, "admin")
        if not product.safe_segment(version) or not product.safe_segment(filename):
            raise HTTPException(status_code=400, detail="unsafe artifact path segment")
        if not filename.endswith(".whl"):
            raise HTTPException(status_code=400, detail="artifacts are wheels (*.whl)")
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="empty artifact body")
        target_dir = settings.artifacts_dir / version
        target_dir.mkdir(parents=True, exist_ok=True)
        # Atomic: a node pulling mid-upload must never receive a torn wheel.
        fd, tmp = tempfile.mkstemp(dir=str(target_dir), prefix=f".{filename}.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, target_dir / filename)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return {"ok": True, "bytes": len(data)}

    @app.get("/api/v1/product/artifacts/{version}/{filename}")
    async def pull_artifact(version: str, filename: str, request: Request):
        _node_auth(request)
        if not product.safe_segment(version) or not product.safe_segment(filename):
            raise HTTPException(status_code=400, detail="unsafe artifact path segment")
        path = settings.artifacts_dir / version / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no artifact {filename} for {version}")
        return FileResponse(path, media_type="application/octet-stream")

    def _config_built_against() -> str:
        """The product version a config-distribution artifact is stamped with
        (ADR-0042): the recorded pin when set, else control's own installed
        version. Advisory only — the stamp never gates anything."""
        pin = product.read_pin(settings.config_repo)
        if pin:
            return pin
        # No pin recorded yet: control's own stamped version (a source build
        # stamps __version__ alongside the pin bump, so this matches the wheel).
        from theozolith_control import __version__

        return __version__

    @app.get("/api/v1/config/artifacts/{drivers_hash}")
    async def pull_config_artifact(drivers_hash: str, request: Request):
        """Serve the config distribution by hash (ADR-0042). Node-authenticated;
        the hash is validated stricter than ``safe_segment``. A cached
        ``<hash>.zip`` is served only after it unpacks and recomputes to the
        requested hash — a corrupted or truncated cache entry is discarded and
        rebuilt rather than served forever. Absent (or discarded) cache builds
        on demand from the current working Config Repo; a built hash that
        differs from the requested one means the repo moved on, so answer 409
        (converge to the fresh hash next heartbeat), never 500. Every published
        artifact is verified by unpack-and-recompute before it lands under
        ``<hash>.zip`` (never trusted by filename or bytes). The response is an
        immutable byte snapshot verified as bytes, so a concurrent request
        pruning the cache entry (keep-two) cannot break an in-flight response.
        There is no PUT twin: control packages from its own repo."""
        _node_auth(request)
        if not re.fullmatch(r"[0-9a-f]{64}", drivers_hash):
            raise HTTPException(
                status_code=400, detail="drivers_hash must be 64 lowercase hex chars"
            )

        def _resolve_artifact() -> bytes:
            """Verify-the-cache-or-build-on-demand, off the event loop: cache
            verification unpacks-and-recomputes and on-demand packaging both do
            real disk work (unzip, sha256 a tree, deflate) that must never stall
            heartbeats or the terminal websockets (ADR-0042). Cache publication
            stays atomic (tempfile + os.replace inside build_artifact), so two
            concurrent builds each publish a valid <hash>.zip and the last
            os.replace wins — a torn entry is impossible.

            Returns an immutable BYTE SNAPSHOT, never a pathname: the cache is
            prunable (keep-two), so a concurrent request building a third hash
            may unlink this entry between verification and response send — the
            response must own what it verified through completion. BOTH exit
            branches read the snapshot first and verify that exact snapshot
            (recomputing to the requested hash) — the cached branch on its
            read, the build branch on its post-publication re-read, since a
            concurrent replace inside the publish-to-read window could hand
            back bytes build_artifact never verified. Nothing after that point
            can touch what is returned. No cross-request lock: the
            fd-lifetime/snapshot mechanism is sufficient, so artifact requests
            never serialize."""
            cached = settings.config_artifacts_dir / f"{drivers_hash}.zip"
            for _ in range(3):
                snapshot: bytes | None
                try:
                    snapshot = cached.read_bytes()
                except OSError:
                    snapshot = None
                if snapshot is not None:
                    # Never serve a cached artifact on the strength of its
                    # filename: a corrupted or truncated <hash>.zip must not be
                    # served forever (ADR-0042). Verify the snapshot unpacks and
                    # recomputes to this hash; if not, discard the cache entry
                    # and fall through to an on-demand rebuild.
                    try:
                        recomputed, _ = configdist.verify_artifact_bytes(snapshot)
                    except configdist.ConfigDistError:
                        recomputed = None
                    if recomputed == drivers_hash:
                        return snapshot
                    with contextlib.suppress(OSError):
                        cached.unlink()
                try:
                    built, path = configdist.build_artifact(
                        settings.config_repo,
                        settings.config_artifacts_dir,
                        built_against=_config_built_against(),
                    )
                except configdist.ConfigDistError as exc:
                    raise HTTPException(
                        status_code=500, detail=f"config distribution build failed: {exc}"
                    ) from exc
                if built != drivers_hash or path is None:
                    # The working repo no longer packages to this hash — no
                    # background build machinery, convergence by retry (ADR-0042).
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "config distribution changed; converge to the hash on the next"
                            " heartbeat"
                        ),
                    )
                try:
                    snapshot = path.read_bytes()
                except OSError:
                    # Concurrent requests built enough newer hashes to prune this
                    # one inside the publish-to-read window; rebuild and retry.
                    continue
                # What is sent must be what was VERIFIED: build_artifact verified
                # the tempfile it published, but THESE bytes were read after
                # publication — a concurrent replace or corruption inside the
                # publish-to-read window could hand back different bytes. Verify
                # this exact snapshot and require it to recompute to the requested
                # hash before it can be the response; on any disagreement drop the
                # suspect cache entry (best effort) and retry in the bounded loop.
                try:
                    recomputed, _ = configdist.verify_artifact_bytes(snapshot)
                except configdist.ConfigDistError:
                    recomputed = None
                if recomputed != drivers_hash:
                    with contextlib.suppress(OSError):
                        cached.unlink()
                    continue
                configdist.prune_config_artifacts(settings.config_artifacts_dir, keep=2)
                return snapshot
            raise HTTPException(
                status_code=503,
                detail="config artifact cache is churning; retry on the next heartbeat",
            )

        data = await asyncio.to_thread(_resolve_artifact)
        return Response(content=data, media_type="application/zip")

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
        config = _config()
        return {
            **store.fleet_state(),
            "provisioned_nodes": secret_store.provisioned_nodes(),
            "unregistered_nodes": store.unregistered_nodes(),
            # The status/TUI read model (ADR-0039): the server clock every
            # staleness computation uses (client clocks never enter the
            # math), the product pin, and desired Stack state — so a pure
            # API consumer needs no Config Repo or cache.db access.
            "now": store.now(),
            "product_pin": config.product_version or None,
            # The recorded config-distribution hash (ADR-0042), null when no
            # drivers/ — the off-hash convergence input for the read models.
            "config_drivers_hash": config.drivers_hash or None,
            # M9 (ADR-0040): entries carry the Stack's attach argv and its
            # non-secret env declarations too — the Operator TUI resolves
            # attach commands and the Run timeout budget client-side. This
            # is the admin read model, not the node channel: attach still
            # never rides a heartbeat response.
            "desired_stacks": [
                {
                    "node": s.node,
                    "name": s.name,
                    "kind": s.kind,
                    "state": s.state,
                    "env": dict(s.env),
                    "attach": list(s.attach),
                }
                for s in config.stacks
            ],
            # The coordination target (owner/name, null when dispatch is
            # off): the TUI builds issue/PR/evidence links from it exactly
            # as the dashboard does server-side (ADR-0040).
            "repo": settings.repo or None,
            # The read-only settings view (ADR-0040): the address fields and
            # the EFFECTIVE tier-2 values this serve is running with
            # (control.toml overlaid with env overrides — what is live, not
            # what the file says). Editing stays git-native.
            "control_toml": {
                "control_ip": settings.control_ip,
                "control_port": settings.control_port,
                "browser_origin": settings.browser_origin or None,
                "settings": {s.key: getattr(settings, s.key) for s in controltoml.SETTINGS},
            },
        }

    @app.get("/api/v1/events")
    async def events_read(request: Request) -> dict[str, Any]:
        """The events read view (ADR-0038, amending ADR-0015): newest-first
        stored rows — known and unknown types alike, payloads verbatim —
        with node/component/type filters, ``since``, cursor pagination, and
        the honest eviction indicator. Query params are hand-parsed like
        every other endpoint: stdlib clients share the schema."""
        _authorize(request, settings.admin_token, "admin")
        params = request.query_params

        def _bad(detail: str) -> None:
            raise HTTPException(status_code=400, detail=detail)

        since: float | None = None
        if params.get("since"):
            try:
                since = float(params["since"])
            except ValueError:
                _bad("'since' must be unix seconds")
        before_id: int | None = None
        if params.get("cursor"):
            try:
                before_id = int(params["cursor"])
            except ValueError:
                _bad("malformed cursor — pass a response's next_cursor back unchanged")
        limit = EVENTS_PAGE_DEFAULT
        if params.get("limit"):
            try:
                limit = int(params["limit"])
            except ValueError:
                _bad("'limit' must be an integer")
            limit = max(1, min(limit, EVENTS_PAGE_MAX))
        return store.events_page(
            node=params.get("node") or None,
            component=params.get("component") or None,
            type=params.get("type") or None,
            since=since,
            before_id=before_id,
            limit=limit,
        )

    @app.get("/api/v1/flags")
    async def flags(request: Request) -> dict[str, Any]:
        _authorize(request, settings.admin_token, "admin")
        return {
            "zombie_flags": store.zombie_flags(),
            "janitor_actions": store.janitor_actions(),
            "malformed_states": store.malformed_states(),
            "quarantines": store.quarantines(),
            # Advisory config-distribution stamp skew (ADR-0042): a node's
            # applied distribution was built against a product version other
            # than the one it now runs. Never a health downgrade — surfaced.
            "drivers_skew": store.drivers_skew(),
        }

    # -- the web surface: dashboard, secret form, terminal (M4) ---------------

    from theozolith_control.web import mount_web

    mount_web(app, settings, store, secret_store, box, _config)

    return app
