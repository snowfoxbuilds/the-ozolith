"""Driver-side Run/review event emission to the Control Node.

Events are the drivers' observability channel (NODE-SUBSTRATE.md typed event
API): namespaced facts about the past plus advisory run-progress telemetry
(ADR-0016), emitted best-effort. Every failure mode — node down, TLS
trouble, garbled answer — is a swallowed skip that never fails a Run;
``emit`` answers whether the event landed so the one delivery that matters
(the claimed event that activates a dispatch grant, ADR-0017) can be
retried and acted on.

The zombie-claim janitor, the quarantine gate, and the dashboard read these
events; the pipeline itself never does.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from theozolith_worker import jobdir
from theozolith_worker.config import DriverConfig

RUN_EVENT = "theozolith.run"
REVIEW_EVENT = "theozolith.review"
PROGRESS_EVENT = "theozolith.run.progress"

# The Worker Run phases (ADR-0015/0016): claimed and gate are "in flight"
# (what the janitor watches); pr-open, failed, and escalated are terminal.
# Every non-completed Run emits failed; a spent local-retry budget adds
# escalated for the claim.
PHASE_CLAIMED = "claimed"
PHASE_GATE = "gate"
PHASE_PR_OPEN = "pr-open"
PHASE_FAILED = "failed"
PHASE_ESCALATED = "escalated"

# Kept below the Control Node's ingestion cap (ADR-0016) with headroom.
TRANSCRIPT_TAIL_CHARS = 4_000


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> bool:
        """True when the event demonstrably landed on the Control Node."""
        ...


def ssl_context_for(url: str, ca: str | None) -> ssl.SSLContext | None:
    """The TLS context for a Control Node URL (shared by every stdlib
    client on the channel: sink, dispatch)."""
    if not url.startswith("https"):
        return None
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


def control_request(url: str, token: str, payload: dict[str, Any]) -> urllib.request.Request:
    """One Control Node POST, headers included (shared request builder)."""
    return urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "theozolith-worker",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )


class ControlNodeSink:
    """POSTs events; any failure is logged at most and always swallowed."""

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        ca: str | None = None,
        timeout: float = 3.0,
        log=None,
    ):
        self._url = url.rstrip("/") + "/api/v1/events"
        self._token = token
        self._ca = ca
        self._timeout = timeout
        self._log = log

    def emit(self, event: dict[str, Any]) -> bool:
        request = control_request(self._url, self._token, event)
        context = ssl_context_for(self._url, self._ca)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout, context=context) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if self._log:
                self._log(f"event emission skipped ({event.get('type')}): {exc}")
            return False
        return True


def make_sink(config: DriverConfig, log=None) -> EventSink:
    return ControlNodeSink(
        config.control_node_url, token=config.control_token, ca=config.control_ca, log=log
    )


def run_event(
    config: DriverConfig,
    *,
    issue: int,
    run_id: str,
    phase: str,
    attempt: int | None = None,
    pr: int | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": RUN_EVENT,
        "worker": config.worker_id,
        "node": config.node_name,
        "stack": config.stack,
        "issue": issue,
        "run_id": run_id,
        "phase": phase,
    }
    if attempt is not None:
        event["attempt"] = attempt
    if pr is not None:
        event["pr"] = pr
    return event


def review_event(
    config: DriverConfig, *, pr: int, issue: int, round_number: int, verdict: str
) -> dict[str, Any]:
    return {
        "type": REVIEW_EVENT,
        "reviewer": config.worker_id,
        "node": config.node_name,
        "stack": config.stack,
        "pr": pr,
        "issue": issue,
        "round": round_number,
        "verdict": verdict,
    }


# -- run-progress telemetry (ADR-0016) ------------------------------------------


def _count_hook_events(job: Path) -> dict[str, int]:
    try:
        lines = (job / jobdir.HOOK_EVENTS_FILE).read_text(encoding="utf-8").split()
    except OSError:
        lines = []
    counts: dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    return counts


def _transcript_snapshot(path: Path) -> tuple[int, str]:
    """(size in bytes, decoded tail) without reading the whole file — a
    multi-hour session transcript can be tens of MB and this runs every
    progress interval."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 4 * TRANSCRIPT_TAIL_CHARS))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return 0, ""
    return size, tail[-TRANSCRIPT_TAIL_CHARS:]


def progress_event(
    config: DriverConfig,
    job: Path,
    *,
    issue: int,
    run_id: str,
    attempt: int,
    elapsed_seconds: float,
) -> dict[str, Any]:
    """A snapshot of the job directory as advisory telemetry. The transcript
    tail is agent-authored text: untrusted wherever displayed, size-capped
    here and again at ingestion."""
    status = jobdir.read_status(job)
    transcript_bytes, tail = _transcript_snapshot(job / jobdir.TRANSCRIPT_FILE)
    counts = _count_hook_events(job)
    return {
        "type": PROGRESS_EVENT,
        "worker": config.worker_id,
        "node": config.node_name,
        "stack": config.stack,
        "issue": issue,
        "run_id": run_id,
        "attempt": attempt,
        "phase": status.phase if status else "starting",
        "elapsed_seconds": round(elapsed_seconds, 1),
        # Counters: tool calls and operator prompts from the adapter's hook
        # log; token counts need adapter support the claude adapter does not
        # have yet (recorded as remaining work in the M4 ADR).
        "tool_calls": counts.get("tool", 0),
        "prompts": counts.get("prompt", 0),
        "tokens": None,
        "transcript_bytes": transcript_bytes,
        "transcript_tail": tail,
    }


class ProgressReporter:
    """Emits a progress event immediately and then on an interval while a
    Run's agent phase is in flight. Best-effort by construction: the thread
    only reads the job directory and every emission failure is swallowed."""

    def __init__(
        self,
        config: DriverConfig,
        sink: EventSink,
        job: Path,
        *,
        issue: int,
        run_id: str,
        attempt: int,
    ):
        self._config = config
        self._sink = sink
        self._job = job
        self._issue = issue
        self._run_id = run_id
        self._attempt = attempt
        self._interval = config.progress_seconds
        self._started_at = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="run-progress")

    def _emit_once(self) -> None:
        self._sink.emit(
            progress_event(
                self._config,
                self._job,
                issue=self._issue,
                run_id=self._run_id,
                attempt=self._attempt,
                elapsed_seconds=time.monotonic() - self._started_at,
            )
        )

    def _loop(self) -> None:
        self._emit_once()
        while not self._stop.wait(self._interval):
            self._emit_once()

    def __enter__(self) -> ProgressReporter:
        if self._interval > 0:
            self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
