"""Driver-side Run/review event emission to the Control Node.

Events are the drivers' observability channel (NODE-SUBSTRATE.md typed event
API): namespaced facts about the past, emitted best-effort. The Control Node
is advisory (ADR-0002), so every failure mode — no CONTROL_NODE_URL, node
down, TLS trouble, garbled answer — is a clean skip that never delays or
fails a Run. The zombie-claim janitor and the retry auditor read these
events; the pipeline itself never does.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Protocol

from theozolith_worker.config import DriverConfig

RUN_EVENT = "theozolith.run"
REVIEW_EVENT = "theozolith.review"

# The Worker Run phases (ADR-0015): claimed and gate are "in flight" (what
# the janitor watches); pr-open, failed, and escalated are terminal.
PHASE_CLAIMED = "claimed"
PHASE_GATE = "gate"
PHASE_PR_OPEN = "pr-open"
PHASE_FAILED = "failed"
PHASE_ESCALATED = "escalated"


class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


class NullSink:
    """No Control Node configured: events go nowhere, silently."""

    def emit(self, event: dict[str, Any]) -> None:
        return None


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

    def emit(self, event: dict[str, Any]) -> None:
        request = urllib.request.Request(
            self._url,
            data=json.dumps(event).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "theozolith-worker",
                **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
            },
        )
        context = None
        if self._url.startswith("https"):
            context = (
                ssl.create_default_context(cafile=self._ca)
                if self._ca
                else ssl.create_default_context()
            )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout, context=context) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if self._log:
                self._log(f"event emission skipped ({event.get('type')}): {exc}")


def make_sink(config: DriverConfig, log=None) -> EventSink:
    if config.control_node_url:
        return ControlNodeSink(
            config.control_node_url, token=config.control_token, ca=config.control_ca, log=log
        )
    return NullSink()


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
