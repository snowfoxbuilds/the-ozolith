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

import ipaddress
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from theozolith_worker import adapters, jobdir
from theozolith_worker.config import DriverConfig

RUN_EVENT = "theozolith.run"
REVIEW_EVENT = "theozolith.review"
PROGRESS_EVENT = "theozolith.run.progress"
ERROR_EVENT = "theozolith.error"

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

# theozolith.error size caps (2026-07-21 grilling: summaries pointing at the
# failing node/component — depth stays in the journal and evidence bundles).
ERROR_MESSAGE_CHARS = 2_000
ERROR_CONTEXT_CHARS = 8_000


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> bool:
        """True when the event demonstrably landed on the Control Node."""
        ...


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_REDIRECT_LIMIT = 5


class BearerTransportError(RuntimeError):
    """The target or a redirect would put the worker's node token on a
    plaintext non-loopback wire or across a cross-origin/downgrade hop."""


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


def _origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, effective port) of an exactly-parsed http(s) URL, or
    None — a total parse (a urlsplit ValueError becomes None, never a leak)
    and never a prefix check, so ``httpsevil`` cannot pass for https."""
    try:
        split = urllib.parse.urlsplit(url)
        hostname = split.hostname
        port = split.port
    except ValueError:
        return None
    if split.scheme not in ("https", "http") or not hostname or port == 0:
        return None
    if port is None:
        port = 443 if split.scheme == "https" else 80
    return split.scheme, hostname, port


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface every 3xx as an HTTPError; the loop in open_bearer owns the
    same-origin gate before any redirect carries the node token."""

    def _decline(self, req, fp, code, msg, headers):
        return None

    http_error_301 = http_error_302 = http_error_303 = _decline
    http_error_307 = http_error_308 = _decline


def open_bearer(
    request: urllib.request.Request, *, ca: str | None, timeout: float
) -> tuple[int, bytes]:
    """Issue one bearer request under the https-or-loopback floor, following
    only same-origin redirects (bounded). Returns ``(status, body)``.

    Mirrors, stdlib-only, ``ControlClient``'s transport and the control-side
    ``bearerhttp`` (kept in step by parallel invariant tests, not a shared
    import — the components install independently). The node token rides the
    Authorization header on every call, so a non-loopback http target or any
    cross-origin/downgrade redirect is refused rather than handed the token —
    including a Stack-authored ``CONTROL_NODE_URL`` pointing off-box.
    Re-raises HTTPError/URLError like urlopen; a policy refusal is
    ``BearerTransportError``."""
    origin = _origin(request.full_url)
    if origin is None:
        raise BearerTransportError(f"refusing bearer request to {request.full_url!r}: not http(s)")
    if origin[0] != "https" and not _is_loopback(origin[1]):
        raise BearerTransportError(
            f"refusing bearer request to {request.full_url!r}: the node token must ride"
            " https (plain http is allowed only to a loopback address)"
        )
    context = None
    if origin[0] == "https":
        context = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)

    method, body = request.get_method(), request.data
    headers = dict(request.header_items())
    target = request.full_url
    for _ in range(_REDIRECT_LIMIT):
        req = urllib.request.Request(target, data=body, method=method, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in _REDIRECT_STATUSES:
                raise
            location = exc.headers.get("Location")
            exc.close()
            try:
                target = urllib.parse.urljoin(target, location) if location else ""
            except ValueError:
                raise BearerTransportError(
                    f"refusing redirect: malformed Location {location!r}"
                ) from None
            if _origin(target) != origin:
                raise BearerTransportError(
                    f"refusing redirect to {target!r}: it leaves the node-token origin"
                    " (cross-origin or downgrade would hand the token to someone else)"
                ) from None
    raise BearerTransportError(f"{method} {request.full_url}: too many redirects")


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
        try:
            open_bearer(request, ca=self._ca, timeout=self._timeout)
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            BearerTransportError,
        ) as exc:
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
    failure_class: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": RUN_EVENT,
        "driver": config.worker_id,
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
    if failure_class:
        # Terminal failed/escalated events carry the canonical class the
        # driver already wrote to the evidence bundle's run.json — the same
        # value, never re-derived (ADR-0040 amendment). Absent on pr-open
        # (no failure) and on events predating the field.
        event["failure_class"] = failure_class
    return event


def review_event(
    config: DriverConfig, *, pr: int, issue: int, round_number: int, verdict: str
) -> dict[str, Any]:
    """A review round as a fact for the Control Node.

    The actor field is ``reviewer`` (not ``driver``): "reviewer" is accurate
    vocabulary, not the taxonomy drift ADR-0020 renamed (ruled 2026-08-09),
    so this sweep left it. Unifying the actor field across run and review
    events is deliberately deferred; because this shape is frozen into
    ``theozolith_worker.api``, that unification would be a release-note api
    change (ADR-0042)."""
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


# -- error events (2026-07-21 grilling; NODE-SUBSTRATE.md) ---------------------


def driver_component(config: DriverConfig) -> str:
    """The dashboard-facing component name (ADR-0020 terminology): the role
    is already the worker-type name, so ``<role>-driver`` names it directly."""
    return f"{config.role}-driver"


def error_event(
    config: DriverConfig,
    *,
    error_class: str,
    message: str,
    context: str = "",
    component: str | None = None,
) -> dict[str, Any]:
    return {
        "type": ERROR_EVENT,
        "node": config.node_name,
        "component": component or driver_component(config),
        "stack": config.stack,
        "error_class": error_class,
        "message": str(message)[:ERROR_MESSAGE_CHARS],
        # Keep the tail: with a traceback, the innermost frames matter most.
        "context": str(context)[-ERROR_CONTEXT_CHARS:],
    }


def emit_error(
    sink: EventSink,
    config: DriverConfig,
    *,
    error_class: str,
    message: str,
    context: str = "",
    component: str | None = None,
) -> None:
    """Best-effort error summary to the Control Node; never raises. (The
    sink's own emission failures stay log-only — an error event about a
    failed error event would recurse.)"""
    sink.emit(
        error_event(
            config,
            error_class=error_class,
            message=message,
            context=context,
            component=component,
        )
    )


# -- run-progress telemetry (ADR-0016) ------------------------------------------


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
    stats = adapters.stream_stats(config.adapter, job / jobdir.TRANSCRIPT_FILE)
    return {
        "type": PROGRESS_EVENT,
        "driver": config.worker_id,
        "node": config.node_name,
        "stack": config.stack,
        "issue": issue,
        "run_id": run_id,
        "attempt": attempt,
        "phase": status.phase if status else "starting",
        "elapsed_seconds": round(elapsed_seconds, 1),
        # Counters from the headless session's structured output stream
        # (ADR-0019); tokens stay None only for adapters whose stream
        # carries no usage (the claude stream does — ADR-0018's gap closed).
        "tool_calls": stats.tool_calls,
        "tokens": stats.tokens,
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
