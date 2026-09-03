"""``theozolith status`` (ADR-0039): the stdlib-only fleet health one-shot.

A pure API consumer: two bearer reads — ``GET /api/v1/state`` and
``GET /api/v1/events`` for the recent-error window — and a verdict computed
client-side. No systemd, no docker, no subprocess of any kind, ever; on an
unreachable Control Node it prints the dial target, the error class, and
static hints, and exits 2. Exit codes: 0 healthy, 1 degraded (reason on
the first line), 2 the read failed.

This module's entire import closure is stdlib-only (test-enforced): the
Operator TUI milestone builds on the same discipline, and the URL/token/CA
resolution defined here is the one implementation the rest of the CLI
delegates to (``cli._admin_env``).

``--json`` is the only parsing contract: it emits the two raw read-model
documents verbatim plus the computed ``status``/``reasons`` — the human
table is explicitly not parseable surface.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from theozolith_control import bearerhttp
from theozolith_control.origin import OriginError, derive_origin
from theozolith_control.settings import ControlSettings, load_settings

# ~2.5 missed heartbeats — the same threshold the dashboard and attach
# machinery use (ADR-0022; views.STALE_AFTER_SECONDS, not imported: the
# web package is outside this module's stdlib-only closure).
STALE_AFTER_SECONDS = 150.0
# The recent-theozolith.error window (ADR-0039).
ERROR_WINDOW_SECONDS = 900.0
# tls.CA_FILE, not imported: tls pulls cryptography at module scope.
CA_FILE_NAME = "ca.pem"

UNREACHABLE_HINTS = (
    "on the Control Node: systemctl status theozolith-control.service",
    "compose flow: docker ps",
)


class TargetError(RuntimeError):
    """The dial target cannot be resolved from flags, env, or init state."""


class Unreachable(RuntimeError):
    """The read failed — connection, TLS, or a non-2xx answer."""

    def __init__(self, dial_target: str, error_class: str, detail: str):
        super().__init__(detail)
        self.dial_target = dial_target
        self.error_class = error_class
        self.detail = detail


def control_url(settings: ControlSettings) -> str:
    """The IP-based URL nodes and the CLI dial (ADR-0031/0034): persisted
    control IP + external https port. Empty until init has persisted it."""
    if not settings.control_ip:
        return ""
    try:
        return derive_origin(settings.control_ip, settings.control_port).origin
    except OriginError:
        return ""


def resolve_target(
    url_flag: str | None, ca_flag: str | None, environ: Mapping[str, str] | None = None
) -> tuple[str, str, str | None]:
    """(url, admin token, ca): flags beat env beats init's on-box artifacts
    — on the Control Node itself this works with no environment at all."""
    from theozolith_worker.config import env_value

    environ = os.environ if environ is None else environ
    settings = load_settings(environ)
    url = url_flag or env_value(environ, "CONTROL_NODE_URL") or control_url(settings)
    if not url:
        raise TargetError(
            "no Control Node URL — set CONTROL_NODE_URL, pass --url, or run"
            " 'theozolith init' on this box first"
        )
    token = settings.admin_token
    if not token:
        raise TargetError(
            "no admin token — on the Control Node run this under sudo"
            " (a root-mediated install keeps it in /var/lib/theozolith-control;"
            " ADR-0034); elsewhere set THEOZOLITH_ADMIN_TOKEN (or its _FILE"
            " form), or run 'theozolith init' first"
        )
    ca = ca_flag or env_value(environ, "THEOZOLITH_TLS_CA")
    if not ca and (settings.tls_dir / CA_FILE_NAME).is_file():
        ca = str(settings.tls_dir / CA_FILE_NAME)
    return url, token, ca


def _fetch(url: str, path: str, token: str, ca: str | None) -> Any:
    """One bearer GET; every failure mode folds into Unreachable with the
    class the operator needs (ADR-0039: reachable-but-refusing included —
    either way the fleet could not be assessed)."""
    request = urllib.request.Request(
        url.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "theozolith-status"},
    )
    try:
        _status, raw = bearerhttp.open_bearer(request, ca=ca, timeout=15)
        return json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise Unreachable(url, f"HTTP {exc.code}", detail) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        error_class = type(reason).__name__ if isinstance(reason, Exception) else type(exc).__name__
        raise Unreachable(url, error_class, str(reason)) from exc
    except (bearerhttp.BearerTransportError, OSError, ValueError) as exc:
        raise Unreachable(url, type(exc).__name__, str(exc)) from exc


def evaluate(state: dict[str, Any], errors: dict[str, Any]) -> list[str]:
    """Degraded reasons in precedence order (ADR-0039): quarantined >
    stale > off-pin > stack-off-desired > recent errors > incomplete error
    history — from 'a human already decided to halt' down to advisory
    telemetry and, last, the epistemic qualifier. Empty = healthy; a
    non-empty list is exit 1, so an incomplete recent-error window can
    never read as unqualified health. All staleness math uses the server
    clock shipped in the document."""
    now = float(state.get("now") or 0.0)
    reasons: list[str] = []

    for row in sorted(state.get("node_health") or [], key=lambda r: r.get("node") or ""):
        if row.get("quarantined"):
            reasons.append(f"node {row['node']} quarantined: {row.get('reason') or 'unspecified'}")

    for node in sorted(state.get("nodes") or [], key=lambda n: n.get("name") or ""):
        age = now - float(node.get("last_seen") or 0.0)
        if age > STALE_AFTER_SECONDS:
            reasons.append(
                f"node {node['name']} stale: last heartbeat {age:.0f}s ago"
                f" (>{STALE_AFTER_SECONDS:.0f}s)"
            )

    pin = state.get("product_pin")
    if pin:
        for node in sorted(state.get("nodes") or [], key=lambda n: n.get("name") or ""):
            version = node.get("version") or ""
            if version and version != pin:
                reasons.append(f"node {node['name']} off-pin: running {version}, pinned {pin}")

    # Off-hash (ADR-0042): a node's reported config distribution differs from
    # the recorded one — convergence state, dispatch-blocking. Below off-pin,
    # above stack-off-desired, mirroring the TUI's health precedence.
    drivers_hash = state.get("config_drivers_hash")
    if drivers_hash:
        for node in sorted(state.get("nodes") or [], key=lambda n: n.get("name") or ""):
            reported = node.get("drivers_hash") or ""
            # Presence, not truthiness: a current daemon that reports '' (no
            # verified tree) is off-hash; a heartbeat that omitted the field is
            # fail-open and never listed here (ADR-0042).
            if node.get("drivers_hash_reported") and reported != drivers_hash:
                running = f"{reported[:12]}" if reported else "none applied"
                reasons.append(
                    f"node {node['name']} off-hash: running config distribution"
                    f" {running}, recorded {drivers_hash[:12]}"
                )

    actual = {
        (row.get("node"), row.get("name")): row.get("state") or ""
        for row in state.get("stacks") or []
    }
    for desired in sorted(
        state.get("desired_stacks") or [], key=lambda d: (d.get("node") or "", d.get("name") or "")
    ):
        want = desired.get("state") or "running"
        have = actual.get((desired.get("node"), desired.get("name")))
        if have is None and want == "running":
            reasons.append(
                f"stack {desired['name']} on {desired['node']} off desired state:"
                f" desired running, not reported"
            )
        elif have is not None and have != want:
            reasons.append(
                f"stack {desired['name']} on {desired['node']} off desired state:"
                f" desired {want}, actual {have}"
            )

    # Per-repo dispatch pauses (ADR-0056): a Bound Workspace whose GitHub
    # reads failed degrades the verdict — repo-scoped coordination trouble,
    # below node-health conditions, above advisory telemetry.
    for row in sorted(state.get("dispatch_pauses") or [], key=lambda p: p.get("repo") or ""):
        reasons.append(
            f"dispatch paused for {row.get('repo')}: {row.get('reason') or 'unspecified'}"
        )

    recent = errors.get("events") or []
    if recent:
        latest = recent[0].get("payload") or {}
        where = latest.get("component") or latest.get("node") or "unknown"
        message = latest.get("message") or latest.get("error_class") or ""
        reasons.append(
            f"{len(recent)} recent theozolith.error event(s) in the last"
            f" {ERROR_WINDOW_SECONDS / 60:.0f}m (latest: {where}: {message})"
        )
    # The events response says whether the window itself is trustworthy
    # (ADR-0038): evicted history means this assessment may miss failures —
    # never an unqualified healthy verdict on incomplete evidence.
    if errors.get("evicted"):
        reasons.append(
            "recent-error history is incomplete: events inside the"
            f" {ERROR_WINDOW_SECONDS / 60:.0f}m window were evicted"
            " (cache-not-archive, ADR-0016) — this assessment may miss failures"
        )
    return reasons


def _age(now: float, then: float) -> str:
    seconds = max(0.0, now - then)
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _table(rows: list[list[str]], out) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        out(
            "  "
            + "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


def render(state: dict[str, Any], reasons: list[str], out) -> None:
    """The human report: verdict first (the degraded reason leads), then
    nodes and Stacks. Not a parsing surface — that is --json."""
    if reasons:
        out(f"degraded: {reasons[0]}")
        for reason in reasons[1:]:
            out(f"          {reason}")
    else:
        out("healthy")
    out("")
    now = float(state.get("now") or 0.0)
    quarantined = {r.get("node") for r in state.get("node_health") or [] if r.get("quarantined")}
    pin = state.get("product_pin")
    drivers_hash = state.get("config_drivers_hash")
    out(f"product pin: {pin or '(none)'}")
    # The Bound Workspaces this Control Node coordinates (ADR-0056), and any
    # per-repo dispatch pauses beneath them.
    repos = state.get("repos") or []
    out(f"coordinating: {', '.join(repos) if repos else '(no Bound Workspaces)'}")
    pauses = state.get("dispatch_pauses") or []
    if pauses:
        rows = [["REPO", "REASON", "SINCE"]]
        for row in sorted(pauses, key=lambda p: p.get("repo") or ""):
            rows.append(
                [
                    str(row.get("repo") or ""),
                    str(row.get("reason") or ""),
                    _age(now, float(row.get("first_seen") or 0.0)),
                ]
            )
        out("dispatch paused:")
        _table(rows, out)
    # Unfinished coordination in unbound repos (ADR-0056): what a repo the
    # Pinned Build no longer binds left behind — operator-owned, never a
    # GitHub cleanup the Control Node performs. Surfaced, not a health verdict.
    unbound = state.get("unbound_obligations") or []
    if unbound:
        rows = [["KIND", "REFERENCE", "DETAIL", "SINCE"]]
        for row in unbound:  # the store already sorts by (repo, kind, ref)
            rows.append(
                [
                    str(row.get("kind") or ""),
                    str(row.get("ref") or ""),
                    str(row.get("reason") or ""),
                    _age(now, float(row.get("since") or 0.0)),
                ]
            )
        out("unbound coordination obligations (repo no longer bound — operator-owned):")
        _table(rows, out)
    nodes = state.get("nodes") or []
    if nodes:
        rows = [["NODE", "HEALTH", "VERSION", "LAST SEEN"]]
        for node in sorted(nodes, key=lambda n: n.get("name") or ""):
            age = now - float(node.get("last_seen") or 0.0)
            reported_hash = node.get("drivers_hash") or ""
            if node.get("name") in quarantined:
                health = "quarantined"
            elif age > STALE_AFTER_SECONDS:
                health = "stale"
            elif pin and node.get("version") and node.get("version") != pin:
                health = "off-pin"
            elif (
                drivers_hash and node.get("drivers_hash_reported") and reported_hash != drivers_hash
            ):
                health = "off-hash"
            else:
                health = "ok"
            rows.append(
                [
                    str(node.get("name") or ""),
                    health,
                    str(node.get("version") or "?"),
                    _age(now, float(node.get("last_seen") or 0.0)),
                ]
            )
        out("nodes:")
        _table(rows, out)
    else:
        out("nodes: none registered")
    desired = state.get("desired_stacks") or []
    if desired:
        actual = {
            (row.get("node"), row.get("name")): row.get("state") or ""
            for row in state.get("stacks") or []
        }
        rows = [["NODE", "STACK", "DESIRED", "ACTUAL"]]
        for stack in sorted(desired, key=lambda d: (d.get("node") or "", d.get("name") or "")):
            rows.append(
                [
                    str(stack.get("node") or ""),
                    str(stack.get("name") or ""),
                    str(stack.get("state") or "running"),
                    actual.get((stack.get("node"), stack.get("name"))) or "not reported",
                ]
            )
        out("stacks:")
        _table(rows, out)


def run(args, *, environ: Mapping[str, str] | None = None, fetch=_fetch, out=print) -> int:
    """The subcommand body. ``fetch`` is injectable for tests — the default
    is the one urllib implementation above; nothing else does I/O."""
    json_output = bool(getattr(args, "json_output", False))
    try:
        url, token, ca = resolve_target(args.url, args.ca, environ)
    except TargetError as exc:
        if json_output:
            out(
                json.dumps(
                    {
                        "status": "unreachable",
                        "dial_target": "",
                        "error_class": "not-configured",
                        "hints": [str(exc)],
                    }
                )
            )
        else:
            out(f"cannot resolve the Control Node: {exc}")
        return 2
    try:
        state = fetch(url, "/api/v1/state", token, ca)
        since = float(state.get("now") or 0.0) - ERROR_WINDOW_SECONDS
        query = urllib.parse.urlencode({"type": "theozolith.error", "since": since})
        errors = fetch(url, f"/api/v1/events?{query}", token, ca)
    except Unreachable as exc:
        if json_output:
            out(
                json.dumps(
                    {
                        "status": "unreachable",
                        "dial_target": exc.dial_target,
                        "error_class": exc.error_class,
                        "hints": list(UNREACHABLE_HINTS),
                    }
                )
            )
        else:
            out(f"Control Node unreachable: {exc.dial_target} ({exc.error_class})")
            if exc.detail:
                out(f"  {exc.detail}")
            for hint in UNREACHABLE_HINTS:
                out(f"  hint: {hint}")
        return 2
    reasons = evaluate(state, errors)
    if json_output:
        out(
            json.dumps(
                {
                    "status": "degraded" if reasons else "healthy",
                    "reasons": reasons,
                    "state": state,
                    "errors": errors,
                },
                sort_keys=True,
            )
        )
    else:
        render(state, reasons, out)
    return 1 if reasons else 0
