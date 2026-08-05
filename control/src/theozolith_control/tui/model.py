"""Pure derivations from the two read documents — no I/O, no Textual.

Everything ``theozolith top`` renders is computed here from the verbatim
``/api/v1/state`` and ``/api/v1/events`` responses, so the panels are unit-
testable without a terminal and the app layer holds no policy. The rules
mirror their owners exactly (each mirrored constant is pinned by test):

- health precedence per node — quarantined > stale > off-pin > ok — and the
  150 s staleness threshold on the SERVER clock (``state["now"]``), never
  the local one (ADR-0022/0039).
- Run states reduce from run + progress events the way the dashboard's
  ``store.run_states()`` does server-side: the latest run event per issue,
  joined with the latest progress telemetry per run_id.
- attach resolution follows the PTY bridge's refusal order (ADR-0022) over
  the state document's heartbeat evidence — but ends in a PRINTED command,
  never a process.
- the events follow mode advances by id past the newest already-seen row,
  walking the cursor only across the unseen gap — history is never
  re-fetched (ADR-0038).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ~2.5 missed heartbeats (ADR-0022) — the same threshold status, the
# dashboard, and the attach machinery use. Mirrors views.STALE_AFTER_SECONDS
# (not imported: the web package must stay outside the TUI's import tree).
STALE_AFTER_SECONDS = 150.0

# The Run timeout budget (M9 brief: "elapsed vs. timeout budget"): the
# worker's THEOZOLITH_AGENT_TIMEOUT_SECONDS, resolved from the Stack's env
# declarations in the state document exactly as the daemon injects it, with
# the worker's shipped default. Mirrors worker config (pinned by test).
AGENT_TIMEOUT_ENV = "THEOZOLITH_AGENT_TIMEOUT_SECONDS"
AGENT_TIMEOUT_DEFAULT_SECONDS = 3600.0

# Evidence bundles (ADR-0014/0016): the orphan branch and per-Run directory
# the worker pushes to. Mirrors theozolith_worker.evidence (pinned by test);
# not imported so the TUI's read surface stays the two API documents.
EVIDENCE_BRANCH = "theozolith/evidence"

# The attach identifier whitelists (ADR-0022): shell-inert by construction —
# no whitespace, no shell syntax, no leading '-' — so a forged heartbeat
# value cannot alter printed command structure. Mirror web.terminal's
# _HOST_RE/_CONTAINER_RE (pinned by test; the web surface is not imported).
_HOST_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# The attach-template placeholders (configrepo.ATTACH_PLACEHOLDERS' values).
ATTACH_HOST = "{host}"
ATTACH_CONTAINER = "{container}"

# Run phases (ADR-0015/0016): claimed/gate are live, the rest terminal.
LIVE_PHASES = ("claimed", "gate")
TERMINAL_PHASES = ("pr-open", "failed", "escalated")


def elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def ago(seconds: float) -> str:
    return f"{elapsed(seconds)} ago"


# -- fleet / nodes ----------------------------------------------------------


@dataclass(frozen=True)
class NodeRow:
    name: str
    health: str  # quarantined | stale | off-pin | ok
    version: str
    last_seen: str  # rendered age (server clock)
    quarantine_reason: str = ""


def node_rows(state: dict[str, Any]) -> list[NodeRow]:
    """One row per node, health at ADR-0039's per-row precedence."""
    now = float(state.get("now") or 0.0)
    pin = state.get("product_pin")
    quarantined = {
        r.get("node"): str(r.get("reason") or "unspecified")
        for r in state.get("node_health") or []
        if r.get("quarantined")
    }
    rows = []
    for node in sorted(state.get("nodes") or [], key=lambda n: n.get("name") or ""):
        name = str(node.get("name") or "")
        age = now - float(node.get("last_seen") or 0.0)
        version = str(node.get("version") or "")
        if name in quarantined:
            health = "quarantined"
        elif age > STALE_AFTER_SECONDS:
            health = "stale"
        elif pin and version and version != pin:
            health = "off-pin"
        else:
            health = "ok"
        rows.append(
            NodeRow(
                name=name,
                health=health,
                version=version or "?",
                last_seen=ago(age),
                quarantine_reason=quarantined.get(name, ""),
            )
        )
    return rows


# -- stacks -----------------------------------------------------------------


@dataclass(frozen=True)
class StackRow:
    node: str
    name: str
    kind: str
    desired: str
    actual: str  # "not reported" when the node has not reported it
    detail: str
    converged: bool


def stack_rows(state: dict[str, Any]) -> list[StackRow]:
    actual = {(row.get("node"), row.get("name")): row for row in state.get("stacks") or []}
    rows = []
    for desired in sorted(
        state.get("desired_stacks") or [],
        key=lambda d: (d.get("node") or "", d.get("name") or ""),
    ):
        key = (desired.get("node"), desired.get("name"))
        have = actual.get(key)
        want = str(desired.get("state") or "running")
        have_state = str(have.get("state") or "") if have else "not reported"
        rows.append(
            StackRow(
                node=str(desired.get("node") or ""),
                name=str(desired.get("name") or ""),
                kind=str(desired.get("kind") or ""),
                desired=want,
                actual=have_state,
                detail=str(have.get("detail") or "") if have else "",
                converged=have_state == want,
            )
        )
    return rows


# -- commands (queue-behind visibility) -------------------------------------


@dataclass(frozen=True)
class CommandRow:
    id: int
    node: str
    verb: str
    target: str
    state: str  # pending | deferred
    deferred_reason: str


def command_rows(state: dict[str, Any]) -> list[CommandRow]:
    """Uncompleted commands, deferrals surfaced (the queue-behind state the
    daemon reports over heartbeats — NODE-SUBSTRATE 2026-07-17)."""
    rows = []
    for command in state.get("commands") or []:
        if command.get("completed_at") is not None:
            continue
        reason = str(command.get("deferred_reason") or "")
        rows.append(
            CommandRow(
                id=int(command.get("id") or 0),
                node=str(command.get("node") or ""),
                verb=str(command.get("verb") or ""),
                target=str(command.get("target") or ""),
                state="deferred" if reason else "pending",
                deferred_reason=reason,
            )
        )
    return rows


# -- runs -------------------------------------------------------------------


@dataclass(frozen=True)
class RunRow:
    issue: int
    worker: str
    node: str
    stack: str
    run_id: str
    attempt: int | None
    phase: str
    terminal: bool
    pr: int | None
    last_event_at: float
    # Live telemetry (the latest progress event for this run_id, ADR-0016).
    progress_phase: str = ""
    elapsed_seconds: float | None = None
    tool_calls: int | None = None
    transcript_bytes: int | None = None
    transcript_tail: str = ""
    # Terminal facts. failure_class renders honestly absent when the run
    # event does not carry it — the channel gap recorded in ADR-0040.
    failure_class: str = ""
    issue_url: str = ""
    pr_url: str = ""
    evidence_ref: str = ""
    evidence_url: str = ""


def _first_per_key(events: list[dict[str, Any]], key: Callable[[dict], Any]) -> dict[Any, dict]:
    """Newest-first pages: the first occurrence per key IS the latest."""
    latest: dict[Any, dict] = {}
    for event in events:
        k = key(event)
        if k is not None and k not in latest:
            latest[k] = event
    return latest


def run_rows(
    runs_page: dict[str, Any],
    progress_page: dict[str, Any],
    repo: str | None,
) -> list[RunRow]:
    """The client-side twin of ``store.run_states()``: latest run event per
    issue joined with the latest progress telemetry per run_id."""
    latest_runs = _first_per_key(
        runs_page.get("events") or [], lambda e: (e.get("payload") or {}).get("issue")
    )
    latest_progress = _first_per_key(
        progress_page.get("events") or [], lambda e: (e.get("payload") or {}).get("run_id")
    )
    base = f"https://github.com/{repo}" if repo else ""
    rows = []
    for issue in sorted(latest_runs):
        event = latest_runs[issue]
        payload = event.get("payload") or {}
        run_id = str(payload.get("run_id") or "")
        progress = (latest_progress.get(run_id) or {}).get("payload") or {}
        phase = str(payload.get("phase") or "")
        pr = payload.get("pr")
        pr = int(pr) if isinstance(pr, (int, float)) else None
        attempt = payload.get("attempt")
        attempt = int(attempt) if isinstance(attempt, (int, float)) else None
        tool_calls = progress.get("tool_calls")
        transcript_bytes = progress.get("transcript_bytes")
        elapsed_s = progress.get("elapsed_seconds")
        evidence_path = f"runs/issue-{issue}/{run_id}" if run_id else ""
        rows.append(
            RunRow(
                issue=int(issue),
                worker=str(payload.get("worker") or ""),
                node=str(payload.get("node") or ""),
                stack=str(payload.get("stack") or progress.get("stack") or ""),
                run_id=run_id,
                attempt=attempt,
                phase=phase,
                terminal=phase in TERMINAL_PHASES,
                pr=pr,
                last_event_at=float(event.get("received_at") or 0.0),
                progress_phase=str(progress.get("phase") or ""),
                elapsed_seconds=(float(elapsed_s) if isinstance(elapsed_s, (int, float)) else None),
                tool_calls=int(tool_calls) if isinstance(tool_calls, (int, float)) else None,
                transcript_bytes=(
                    int(transcript_bytes) if isinstance(transcript_bytes, (int, float)) else None
                ),
                transcript_tail=str(progress.get("transcript_tail") or ""),
                failure_class=str(payload.get("failure_class") or ""),
                issue_url=f"{base}/issues/{issue}" if base else "",
                pr_url=f"{base}/pull/{pr}" if base and pr else "",
                evidence_ref=f"{EVIDENCE_BRANCH}: {evidence_path}" if evidence_path else "",
                evidence_url=(
                    f"{base}/tree/{EVIDENCE_BRANCH}/{evidence_path}"
                    if base and evidence_path
                    else ""
                ),
            )
        )
    return rows


def timeout_budget_seconds(state: dict[str, Any], stack: str) -> float:
    """The Run timeout budget for a Stack: its THEOZOLITH_AGENT_TIMEOUT_SECONDS
    env declaration when one is set (Stack names are file stems — unique
    fleet-wide), else the worker's shipped default."""
    for desired in state.get("desired_stacks") or []:
        if desired.get("name") == stack:
            raw = (desired.get("env") or {}).get(AGENT_TIMEOUT_ENV)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return AGENT_TIMEOUT_DEFAULT_SECONDS
            return value if value > 0 else AGENT_TIMEOUT_DEFAULT_SECONDS
    return AGENT_TIMEOUT_DEFAULT_SECONDS


# -- attach assistance ------------------------------------------------------


@dataclass(frozen=True)
class AttachResult:
    """Either a pastable command or the reason there is none — never both."""

    command: str = ""
    stack: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.command)


def attach_command(state: dict[str, Any], node: str, container: str) -> AttachResult:
    """Resolve the pastable attach command from heartbeat evidence in the
    state document — the PTY bridge's refusal order (ADR-0022), ending in a
    printed string instead of a process. Freshness is judged on the SERVER
    clock (``state["now"]`` vs the row's ``updated_at``)."""
    now = float(state.get("now") or 0.0)
    for record in state.get("run_containers") or []:
        if record.get("node") == node and record.get("name") == container:
            return AttachResult(
                stack=str(record.get("owner") or ""),
                reason=(
                    f"container {container!r} is a run container — run containers are"
                    " headless and never attach targets (ADR-0019)"
                ),
            )
    record = next(
        (
            r
            for r in state.get("stack_containers") or []
            if r.get("node") == node and r.get("name") == container
        ),
        None,
    )
    if record is None:
        return AttachResult(
            reason=f"container {container!r} is not live on {node!r} (per heartbeats)"
        )
    age = now - float(record.get("updated_at") or 0.0)
    if age > STALE_AFTER_SECONDS:
        return AttachResult(
            reason=(
                f"heartbeat evidence for {container!r} on {node!r} is stale"
                f" ({age:.0f}s old on the server clock, threshold"
                f" {STALE_AFTER_SECONDS:.0f}s) — refusing to print an attach command"
            )
        )
    owner = str(record.get("stack") or "")
    stack_def = next(
        (
            s
            for s in state.get("desired_stacks") or []
            if s.get("name") == owner and s.get("node") == node
        ),
        None,
    )
    if stack_def is None:
        return AttachResult(
            stack=owner,
            reason=(
                f"container {container!r} has no configured owning Stack on {node!r}"
                f" (stack {owner!r})"
            ),
        )
    if stack_def.get("kind") != "container":
        return AttachResult(
            stack=owner,
            reason=f"stack {owner!r} on {node!r} is not a container-kind Stack (ADR-0019)",
        )
    template = [str(part) for part in stack_def.get("attach") or []]
    if not template:
        return AttachResult(
            stack=owner,
            reason=f"stack {owner!r} on {node!r} exposes no terminal (no attach command)",
        )
    if len(node) > 253 or _HOST_RE.match(node) is None:
        return AttachResult(stack=owner, reason=f"invalid attach host {node!r}")
    if _CONTAINER_RE.match(container) is None:
        return AttachResult(stack=owner, reason=f"invalid container name {container!r}")
    substitutions = {ATTACH_HOST: node, ATTACH_CONTAINER: container}
    argv = [substitutions.get(part, part) for part in template]
    return AttachResult(command=shlex.join(argv), stack=owner)


# -- the infrastructure-command write flow ----------------------------------

# The TUI's infrastructure verbs (M9 brief) — restart stays CLI-only (it is
# the escalation machinery's verb, not a routine operation).
COMMAND_VERBS = ("drain", "recycle", "update", "rebuild")
# Destructive: they kill or replace running work (queue-behind still
# applies server-side; --force is deliberately not offered in the TUI).
DESTRUCTIVE_VERBS = ("recycle", "update", "rebuild")


def command_refusal(verb: str, node: str, target: str, typed: str) -> str | None:
    """Why the command flow refuses to queue, or None when it may proceed.
    Destructive verbs demand the target's name typed back — the Stack or
    image name when one is targeted, the node name for whole-node verbs
    (acceptance 3: a wrong name refuses)."""
    if verb not in COMMAND_VERBS:
        return f"verb must be one of {', '.join(COMMAND_VERBS)}"
    if not node:
        return "a node is required"
    if verb in DESTRUCTIVE_VERBS:
        expected = target or node
        if typed != expected:
            return (
                f"{verb} is destructive — type the target name {expected!r}"
                " exactly to confirm (refused)"
            )
    return None


# -- events: follow mode and eviction honesty -------------------------------


def max_event_id(events: list[dict[str, Any]]) -> int | None:
    ids = [e["id"] for e in events if isinstance(e.get("id"), int)]
    return max(ids) if ids else None


def advance_events(
    fetch_page: Callable[[str | None], dict[str, Any]],
    known_max_id: int | None,
    *,
    max_pages: int = 5,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Follow mode (ADR-0038): fetch the head page and keep only rows newer
    than ``known_max_id``, walking the cursor across the unseen gap — never
    back through history already held. Returns ``(new events newest-first,
    query-relative evicted, gap_remains)``; ``gap_remains`` is True only
    when ``max_pages`` full pages were all unseen (the caller resyncs).
    """
    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    evicted = False
    for _ in range(max_pages):
        page = fetch_page(cursor)
        evicted = evicted or bool(page.get("evicted"))
        events = page.get("events") or []
        fresh = [
            e
            for e in events
            if known_max_id is None or (isinstance(e.get("id"), int) and e["id"] > known_max_id)
        ]
        collected.extend(fresh)
        cursor = page.get("next_cursor")
        if known_max_id is None or len(fresh) < len(events) or cursor is None:
            return collected, evicted, False
        # Ids are AUTOINCREMENT-monotonic and never reused (ADR-0038): a
        # page ending exactly one above the newest seen row proves the gap
        # is closed without fetching the boundary page.
        if fresh and fresh[-1]["id"] == known_max_id + 1:
            return collected, evicted, False
    return collected, evicted, True


def eviction_notice(page_evicted: bool) -> str:
    """The per-panel honesty line (ADR-0038's split contract): shown exactly
    when THIS query's ``evicted`` is true — the global ``any_evicted`` fact
    alone never flags an unaffected panel (ADR-0039 precedent)."""
    if not page_evicted:
        return ""
    return (
        "older matching events may be evicted (cache-not-archive,"
        " ADR-0016/0038) — this listing is not complete history"
    )


# -- settings (read-only) ---------------------------------------------------


def settings_rows(state: dict[str, Any]) -> list[tuple[str, str]]:
    """The read-only control.toml view (ADR-0040): address fields first,
    then the effective tier-2 values serve is running with. Editing stays
    git-native — nothing here writes."""
    view = state.get("control_toml") or {}
    rows = [
        ("control_ip", str(view.get("control_ip") or "(not initialized)")),
        ("control_port", str(view.get("control_port") or "")),
        ("browser_origin", str(view.get("browser_origin") or "(browser disabled — origin-init)")),
    ]
    settings = view.get("settings") or {}
    rows.extend((key, str(settings[key])) for key in sorted(settings))
    return rows


# -- degraded banner --------------------------------------------------------


@dataclass
class Freshness:
    """Tracks whether the documents on screen are live or stale after a
    failed refresh — the degraded-mode state (ADR-0040: banner + stale
    marking, never blocking)."""

    last_success_at: float | None = None
    error: str = ""
    dial_target: str = ""
    consecutive_failures: int = field(default=0)

    def succeed(self, at: float) -> None:
        self.last_success_at = at
        self.error = ""
        self.dial_target = ""
        self.consecutive_failures = 0

    def fail(self, dial_target: str, error_class: str) -> None:
        self.error = error_class
        self.dial_target = dial_target
        self.consecutive_failures += 1

    def banner(self, now: float) -> str:
        if not self.error:
            return ""
        age = (
            f"data is {elapsed(now - self.last_success_at)} stale"
            if self.last_success_at is not None
            else "no data received yet"
        )
        return (
            f"CONTROL UNREACHABLE: {self.dial_target} ({self.error}) — {age};"
            " retrying on the refresh cadence"
        )
