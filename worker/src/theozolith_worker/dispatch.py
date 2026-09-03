"""The drivers' claim-dispatch client (ADR-0017).

Workers and the Reviewer request work from the Control Node instead of
polling GitHub: one POST to /api/v1/dispatch with the driver's identity and
GitHub login (the request doubles as driver registration). For a Worker the
answer carries an issue the Control Node has already claimed on GitHub
(write-through — assigned to this driver's login, in_progress applied); for
the Reviewer it is discovery only, a list of reviewable PR numbers.

There is no second claim path: an unreachable or unconfigured Control Node
means new claims and new review rounds pause, while anything already in
flight finishes and publishes (the drivers hold their own PATs for all
non-claim GitHub writes).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from theozolith_worker.events import BearerTransportError, control_request, open_bearer

# Unreachable-Control-Node backoff cap (ADR-0015 revision): driver polling
# doubles its delay per consecutive failure up to this, then snaps back to
# the configured poll interval the moment the Control Node answers.
BACKOFF_CAP_SECONDS = 300.0


def backoff_delay(base: float, streak: int, cap: float = BACKOFF_CAP_SECONDS) -> float:
    """The poll delay after ``streak`` consecutive unreachable passes.

    The exponent is clamped: the cap dominates long before 2**32 for any
    plausible base, and an unbounded streak (a driver latched or unreachable
    for days) must never overflow the int-to-float conversion and crash the
    loop it paces."""
    if streak <= 1:
        return base
    return min(cap, base * 2 ** min(streak - 1, 32))


class WorkDispatch(Protocol):
    """What the drivers need from dispatch. Tests provide fakes."""

    def request_work(self, worker: str, node: str, login: str) -> dict[str, Any] | None:
        """A granted issue payload, or None (nothing eligible / paused)."""
        ...

    def review_targets(self, worker: str, node: str, login: str) -> list[int] | None:
        """Reviewable PR numbers; None = Control Node unreachable (pause)."""
        ...


class DispatchClient:
    """POSTs /api/v1/dispatch; every failure mode is a clean pause.

    Every request names ``repo`` (the repository this Driver will check out)
    and ``stack`` (the Stack it runs as) — the Claim Protocol is keyed by
    repository and the Control Node verifies the pair against the Pinned
    Build (ADR-0056). Adding the required keyword-only constructor arguments
    is a release-note-class ``theozolith_worker.api`` change (ADR-0042).

    ``on_error(error_class, message)`` is the theozolith.error hook (2026-07-21
    grilling): the drivers wire it to their event sink so dispatch failures
    surface on the dashboard. Best-effort — when the Control Node itself is
    unreachable the event goes nowhere, which is fine (the dashboard is on
    the same box that is down).
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        repo: str,
        stack: str,
        ca: str | None = None,
        timeout: float = 15.0,
        log=None,
        on_error=None,
    ):
        self._url = url.rstrip("/") + "/api/v1/dispatch"
        self._token = token
        self._repo = repo
        self._stack = stack
        self._ca = ca
        self._timeout = timeout
        self._log = log
        self._on_error = on_error
        # True after a pass that could not reach the Control Node at all —
        # the drivers' backoff signal (a refusal is not unreachability).
        self.last_unreachable = False

    def _error(self, error_class: str, message: str) -> None:
        if self._log:
            self._log(message)
        if self._on_error:
            self._on_error(error_class, message)

    def _post(self, body: dict[str, Any]) -> dict[str, Any] | None:
        request = control_request(self._url, self._token, body)
        try:
            _status, raw = open_bearer(request, ca=self._ca, timeout=self._timeout)
            answer = json.loads(raw or b"{}")
        except urllib.error.HTTPError as exc:
            self.last_unreachable = False  # it answered; it refused
            detail = exc.read().decode(errors="replace")[:200]
            self._error("dispatch-refused", f"dispatch refused (HTTP {exc.code}): {detail}")
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            self.last_unreachable = True
            self._error("control-unreachable", f"control node unreachable; dispatch paused ({exc})")
            return None
        except BearerTransportError as exc:
            # A misconfigured CONTROL_NODE_URL (e.g. off-box http): pause
            # rather than hand the node token over. Not transient, but the
            # driver pausing + surfacing beats crashing or leaking.
            self.last_unreachable = True
            self._error("control-url-refused", f"dispatch paused: {exc}")
            return None
        self.last_unreachable = False
        return answer if isinstance(answer, dict) else None

    def request_work(self, worker: str, node: str, login: str) -> dict[str, Any] | None:
        answer = self._post(
            {
                "role": "implementer",
                "driver": worker,
                "node": node,
                "login": login,
                "repo": self._repo,
                "stack": self._stack,
            }
        )
        if answer is None:
            return None
        issue = answer.get("issue")
        if issue is None and self._log and answer.get("reason"):
            self._log(f"dispatch: no grant ({answer['reason']})")
        if not isinstance(issue, dict):
            return None
        if not isinstance(issue.get("number"), int):
            # A malformed grant must not crash the driver — the claim is
            # already on GitHub; the activation-window release unwinds it.
            self._error(
                "malformed-grant", f"dispatch: malformed grant payload ignored: {issue!r:.200}"
            )
            return None
        return issue

    def review_targets(self, worker: str, node: str, login: str) -> list[int] | None:
        answer = self._post(
            {
                "role": "reviewer",
                "driver": worker,
                "node": node,
                "login": login,
                "repo": self._repo,
                "stack": self._stack,
            }
        )
        if answer is None:
            return None
        prs = answer.get("prs")
        if not isinstance(prs, list):
            return None
        return [int(n) for n in prs if isinstance(n, int)]
