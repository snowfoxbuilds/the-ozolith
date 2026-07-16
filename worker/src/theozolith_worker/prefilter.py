"""Control Node claim pre-filter: advisory, optional, never authoritative.

When a Control Node is configured and reachable it can veto a claim attempt
before the GitHub round-trip (a race pre-filter, ADR-0002). When it is not
configured, unreachable, or answers anything unexpected, the pre-filter is
cleanly skipped: GitHub assign-and-verify remains the only authority, and
GitHub-only operation is the permanent degraded mode.

The M3 wire format is not settled; this module owns the interface and treats
every non-committal answer as "allow".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol


class ClaimPrefilter(Protocol):
    def allows(self, issue_number: int, worker_id: str) -> bool: ...


class NullPrefilter:
    """No Control Node configured: every claim goes straight to GitHub."""

    def allows(self, issue_number: int, worker_id: str) -> bool:
        return True


class ControlNodePrefilter:
    """POSTs a claim intent; only an explicit {"allow": false} vetoes."""

    def __init__(self, url: str, timeout: float = 3.0):
        self._url = url.rstrip("/") + "/api/v1/claim-intents"
        self._timeout = timeout

    def allows(self, issue_number: int, worker_id: str) -> bool:
        body = json.dumps({"issue": issue_number, "worker": worker_id}).encode()
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "theozolith-worker"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                answer = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return True  # unreachable or garbled: skip the pre-filter
        return not (isinstance(answer, dict) and answer.get("allow") is False)


def make_prefilter(control_node_url: str | None) -> ClaimPrefilter:
    if control_node_url:
        return ControlNodePrefilter(control_node_url)
    return NullPrefilter()
