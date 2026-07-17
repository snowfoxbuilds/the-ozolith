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
import ssl
import urllib.error
import urllib.request
from typing import Any, Protocol


class WorkDispatch(Protocol):
    """What the drivers need from dispatch. Tests provide fakes."""

    def request_work(self, worker: str, node: str, login: str) -> dict[str, Any] | None:
        """A granted issue payload, or None (nothing eligible / paused)."""
        ...

    def review_targets(self, worker: str, node: str, login: str) -> list[int] | None:
        """Reviewable PR numbers; None = Control Node unreachable (pause)."""
        ...


class DispatchClient:
    """POSTs /api/v1/dispatch; every failure mode is a clean pause."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        ca: str | None = None,
        timeout: float = 15.0,
        log=None,
    ):
        self._url = url.rstrip("/") + "/api/v1/dispatch"
        self._token = token
        self._ca = ca
        self._timeout = timeout
        self._log = log

    def _post(self, body: dict[str, Any]) -> dict[str, Any] | None:
        request = urllib.request.Request(
            self._url,
            data=json.dumps(body).encode(),
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
                answer = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            if self._log:
                self._log(f"dispatch refused (HTTP {exc.code}): {detail}")
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            if self._log:
                self._log(f"control node unreachable; dispatch paused ({exc})")
            return None
        return answer if isinstance(answer, dict) else None

    def request_work(self, worker: str, node: str, login: str) -> dict[str, Any] | None:
        answer = self._post({"role": "worker", "worker": worker, "node": node, "login": login})
        if answer is None:
            return None
        issue = answer.get("issue")
        if issue is None and self._log and answer.get("reason"):
            self._log(f"dispatch: no grant ({answer['reason']})")
        return issue if isinstance(issue, dict) else None

    def review_targets(self, worker: str, node: str, login: str) -> list[int] | None:
        answer = self._post({"role": "reviewer", "worker": worker, "node": node, "login": login})
        if answer is None:
            return None
        prs = answer.get("prs")
        if not isinstance(prs, list):
            return None
        return [int(n) for n in prs if isinstance(n, int)]
