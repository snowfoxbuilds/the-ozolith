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
  joined with the latest progress telemetry per run_id — kept COMPLETE
  across event-page boundaries by ``RunIndex`` (bootstrap cursor walk, then
  incremental head advances; never a one-page snapshot).
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
    desired: str  # "(unplaced)" when only a heartbeat reports the Stack
    actual: str  # "not reported" when the node has not reported it
    detail: str
    converged: bool


def stack_rows(state: dict[str, Any]) -> list[StackRow]:
    """One row per (node, stack) in the UNION of desired and reported state
    — the frozen web surface's drift behavior. Desired-only rows render
    "not reported"; actual-only rows (a heartbeat reports a Stack the Config
    Repo no longer places here) render desired "(unplaced)" with the
    reported kind and detail preserved, and are never converged — an
    actual-only running Stack is off desired, not silently fine."""
    desired_by = {
        (row.get("node"), row.get("name")): row for row in state.get("desired_stacks") or []
    }
    actual_by = {(row.get("node"), row.get("name")): row for row in state.get("stacks") or []}
    rows = []
    for key in sorted(
        set(desired_by) | set(actual_by), key=lambda k: (str(k[0] or ""), str(k[1] or ""))
    ):
        desired = desired_by.get(key)
        have = actual_by.get(key)
        want = str(desired.get("state") or "running") if desired else "(unplaced)"
        have_state = str(have.get("state") or "") if have else "not reported"
        kind = (have.get("kind") if have else "") or (desired.get("kind") if desired else "")
        rows.append(
            StackRow(
                node=str(key[0] or ""),
                name=str(key[1] or ""),
                kind=str(kind or ""),
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
    driver: str  # the worker_id that emitted the Run event (ADR-0020 field)
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


# Defensive ceiling on event pages walked in one index pass. Crossing it is
# never a silent drop: the index marks itself incomplete and the Runs panel
# says so (ADR-0040 amendment — the one-page reduction was not equivalent to
# ``store.run_states()``; this bound is on EVENTS walked, not on issues).
INDEX_MAX_PAGES = 40


@dataclass
class RunIndex:
    """The client-side twin of ``store.run_states()``, complete across event
    pages: the latest ``theozolith.run`` event per issue and the latest
    progress telemetry per run_id. Bootstrapped once by a bounded cursor
    walk over the full retained history (run events are durable cache
    records — never evicted), then advanced incrementally from new head
    events each poll; an unclosed advance gap triggers a re-bootstrap
    instead of pretending continuity. Progress is evictable advisory
    telemetry: a missing record renders as unavailable, never as a missing
    Run."""

    latest_run_by_issue: dict[Any, dict[str, Any]] = field(default_factory=dict)
    latest_progress_by_run: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_run_id: int | None = None
    max_progress_id: int | None = None
    bootstrapped: bool = False
    # True when a bootstrap walk hit INDEX_MAX_PAGES with history remaining:
    # issues whose latest event lies beyond the walked window are missing
    # and the panel shows an incomplete-data notice (never a silent drop).
    truncated: bool = False

    def refresh(
        self,
        fetch_runs: Callable[[str | None], dict[str, Any]],
        fetch_progress: Callable[[str | None], dict[str, Any]],
    ) -> None:
        """One poll: bootstrap on first use (or when a previous walk saw no
        events at all — an id-less index cannot advance safely), otherwise
        advance both indexes past their newest known ids. Cursor overlap is
        handled by id filtering; an unclosed gap re-bootstraps."""
        if not self.bootstrapped or self.max_run_id is None:
            self._bootstrap_runs(fetch_runs)
        else:
            fresh, _evicted, gap = advance_events(
                fetch_runs, self.max_run_id, max_pages=INDEX_MAX_PAGES
            )
            if gap:
                self._bootstrap_runs(fetch_runs)
            else:
                self._apply_runs(fresh)
        if self.max_progress_id is None:
            self._bootstrap_progress(fetch_progress)
        else:
            fresh, _evicted, gap = advance_events(
                fetch_progress, self.max_progress_id, max_pages=INDEX_MAX_PAGES
            )
            if gap:
                self._bootstrap_progress(fetch_progress)
            else:
                self._apply_progress(fresh)
        self.bootstrapped = True
        self._prune_progress()

    def _bootstrap_runs(self, fetch: Callable[[str | None], dict[str, Any]]) -> None:
        latest: dict[Any, dict[str, Any]] = {}
        max_id: int | None = None
        cursor: str | None = None
        truncated = True
        for _ in range(INDEX_MAX_PAGES):
            page = fetch(cursor)
            for event in page.get("events") or []:
                if isinstance(event.get("id"), int):
                    max_id = event["id"] if max_id is None else max(max_id, event["id"])
                issue = (event.get("payload") or {}).get("issue")
                # Newest-first pages: the first occurrence per issue IS the
                # latest.
                if issue is not None and issue not in latest:
                    latest[issue] = event
            cursor = page.get("next_cursor")
            if cursor is None:
                truncated = False
                break
        # Assign only after a completed walk: a failed fetch raises out and
        # leaves the previous (still coherent) index in place.
        self.latest_run_by_issue = latest
        self.max_run_id = max_id
        self.truncated = truncated

    def _apply_runs(self, fresh: list[dict[str, Any]]) -> None:
        # Oldest-first application: the newest event per issue lands last.
        for event in reversed(fresh):
            issue = (event.get("payload") or {}).get("issue")
            if issue is not None:
                self.latest_run_by_issue[issue] = event
            if isinstance(event.get("id"), int):
                self.max_run_id = max(self.max_run_id or 0, event["id"])

    def _live_run_ids(self) -> set[str]:
        return {
            str((event.get("payload") or {}).get("run_id") or "")
            for event in self.latest_run_by_issue.values()
            if str((event.get("payload") or {}).get("phase") or "") not in TERMINAL_PHASES
        } - {""}

    def _bootstrap_progress(self, fetch: Callable[[str | None], dict[str, Any]]) -> None:
        """Walk progress pages until every live Run has telemetry or the
        retained history ends (bounded). Progress is evictable: a live Run
        with no surviving record simply renders as telemetry-unavailable."""
        wanted = self._live_run_ids()
        latest: dict[str, dict[str, Any]] = {}
        max_id: int | None = None
        cursor: str | None = None
        for _ in range(INDEX_MAX_PAGES):
            page = fetch(cursor)
            for event in page.get("events") or []:
                if isinstance(event.get("id"), int):
                    max_id = event["id"] if max_id is None else max(max_id, event["id"])
                run_id = (event.get("payload") or {}).get("run_id")
                if run_id and str(run_id) not in latest:
                    latest[str(run_id)] = event
            cursor = page.get("next_cursor")
            if cursor is None or wanted <= set(latest):
                break
        self.latest_progress_by_run = latest
        self.max_progress_id = max_id

    def _apply_progress(self, fresh: list[dict[str, Any]]) -> None:
        for event in reversed(fresh):
            run_id = (event.get("payload") or {}).get("run_id")
            if run_id:
                self.latest_progress_by_run[str(run_id)] = event
            if isinstance(event.get("id"), int):
                self.max_progress_id = max(self.max_progress_id or 0, event["id"])

    def _prune_progress(self) -> None:
        """Drop telemetry for run_ids no longer referenced by any latest run
        event, so the join stays bounded by the issue count."""
        referenced = {
            str((event.get("payload") or {}).get("run_id") or "")
            for event in self.latest_run_by_issue.values()
        }
        self.latest_progress_by_run = {
            run_id: event
            for run_id, event in self.latest_progress_by_run.items()
            if run_id in referenced
        }


def runs_notice(truncated: bool) -> str:
    """The Runs panel's incomplete-data line: shown exactly when the index's
    bootstrap walk hit its defensive page bound with history remaining. The
    bound is on event pages walked, never on issues — and crossing it is
    disclosed, never a silent drop."""
    if not truncated:
        return ""
    return (
        "run history exceeded the bounded index walk — Runs whose latest event"
        " lies beyond the walked window are missing from this listing"
        " (incomplete data)"
    )


def run_rows(index: RunIndex, repo: str | None) -> list[RunRow]:
    """Rows from the complete index: latest run event per issue joined with
    the latest progress telemetry per run_id (``store.run_states()``'s
    shape). A Run whose progress was evicted keeps its row — the durable
    run event is the row's existence; telemetry is advisory."""
    latest_runs = index.latest_run_by_issue
    latest_progress = index.latest_progress_by_run
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
                driver=str(payload.get("driver") or ""),
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


def attach_command(
    state: dict[str, Any], node: str, container: str, *, snapshot_fresh: bool = True
) -> AttachResult:
    """Resolve the pastable attach command from heartbeat evidence in the
    state document — the PTY bridge's refusal order (ADR-0022), ending in a
    printed string instead of a process. Freshness is judged on the SERVER
    clock (``state["now"]`` vs the row's ``updated_at``) — and only while
    that clock is itself provably current: ``snapshot_fresh=False`` (the
    caller's last refresh failed, so ``state["now"]`` is frozen at some past
    instant) refuses before any heartbeat judgment. The local wall clock is
    never a substitute — fail closed (ADR-0040 amendment)."""
    if not snapshot_fresh:
        return AttachResult(
            reason=(
                "the Control Node is unreachable and the retained state document is"
                " frozen — current server-clock freshness of the heartbeat evidence"
                " cannot be established; refusing to print an attach command until a"
                " state refresh succeeds"
            )
        )
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


FOLLOW_GAP_NOTICE = (
    "follow mode fell behind: more new matching events arrived than the bounded"
    " cursor walk could cover, so intermediate matching events were skipped"
    " client-side (not server eviction) — the feed resumed from the newest rows"
)


def continuity_notice(page_evicted: bool, follow_gap: bool) -> str:
    """One combined per-panel "history incomplete" line naming each cause
    distinctly: server-side eviction (query-relative, ADR-0038) and a
    client-side follow overflow are different facts and are never conflated.
    Both accumulate while the panel's filter conjunction stands and reset
    when it changes — a follow gap is only "restored" by a new query,
    because the skipped events never re-enter the retained feed (history is
    never re-fetched); a later overlapping head poll proves nothing about
    them."""
    causes = []
    if page_evicted:
        causes.append(eviction_notice(True))
    if follow_gap:
        causes.append(FOLLOW_GAP_NOTICE)
    if not causes:
        return ""
    return "history incomplete — " + "; ".join(causes)


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

    @property
    def degraded(self) -> bool:
        """True while the retained documents cannot prove current server-
        clock freshness: before any successful refresh, and from a failed
        refresh until the next success. Actions that present heartbeat-
        derived data as CURRENT (attach assistance) must fail closed on
        this — the retained ``state["now"]`` is frozen at some past instant
        and the local wall clock is never a substitute (ADR-0040
        amendment)."""
        return bool(self.error) or self.last_success_at is None

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
