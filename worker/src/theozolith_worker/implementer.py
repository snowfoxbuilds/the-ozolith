"""The Implementer worker type: the dispatch-run loop bound to one Agent.

The trusted, credentialed half of the Implementer (ADR-0013): it holds the
machine-user PAT, performs all non-claim GitHub I/O, and never executes
repository code or model output. Claims arrive through the Control Node's
dispatch endpoint (ADR-0017): the Control Node writes each claim to GitHub
itself and hands the issue over in the same response — the driver never
assigns, labels, or verifies a claim into existence. Runs execute
sequentially, one at a time, each in a fresh ephemeral run container, with
the ADR-0016 local-retry budget handled per claim (``execute_claim``).

Startup and idle passes run the boot-time evidence sweep: orphaned job
directories are pushed to the evidence branch (swept: true) and deleted only
after the push confirms. Failed pushes back off (a down evidence remote is
the common cause of kept dirs; re-cloning it every poll would hammer it for
nothing).

The shared loop, backoff, and error surfacing live in ``Worker`` (base.py);
this type fills the ADR-0020 seams — its work query is a dispatch claim, its
transition set is ``runner.execute_claim``, and its idle hook owns the
failed-push sweep-retry backoff.
"""

from __future__ import annotations

import time
from typing import ClassVar

from theozolith_worker.base import Worker
from theozolith_worker.githubapi import Issue
from theozolith_worker.runner import execute_claim
from theozolith_worker.sweep import sweep_orphans

SWEEP_RETRY_SECONDS = 900.0  # backoff between failed-evidence-push retries


def _granted_issue(granted: dict, login: str) -> Issue:
    """The dispatch answer as an Issue: the claim already exists on GitHub
    (assigned to this driver, in_progress applied) — no re-read needed."""
    return Issue(
        number=int(granted["number"]),
        title=str(granted.get("title", "")),
        body=str(granted.get("body", "")),
        labels=set(granted.get("labels", [])),
        assignees=[login],
        is_pr=False,
    )


class Implementer(Worker):
    """Requests claims from the Control Node and runs each in a container."""

    role: ClassVar[str] = "implementer"
    sleep_after_work: ClassVar[bool] = False  # drain the queue between grants
    # pass_label inherits the base "dispatch" default.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sweep_retry_at = 0.0

    def _startup_log(self) -> None:
        self.log(
            f"implementer driver {self.config.worker_id} ({self.me}) requesting work"
            f" for {self.config.repo} via dispatch"
        )

    def on_boot(self) -> None:
        _, kept = sweep_orphans(self.config, log=self.log)  # boot-time sweep (ADR-0016)
        self._arm_sweep_retry(kept)

    def on_idle(self) -> None:
        # An idle-pass sweep with nothing to push is one directory listing;
        # only failed pushes arm the sweep backoff.
        if time.monotonic() >= self._sweep_retry_at:
            _, kept = sweep_orphans(self.config, log=self.log)
            self._arm_sweep_retry(kept)

    def _arm_sweep_retry(self, kept: bool) -> None:
        self._sweep_retry_at = time.monotonic() + SWEEP_RETRY_SECONDS if kept else 0.0

    def fetch_work(self) -> list | None:
        granted = self.dispatch.request_work(self.config.worker_id, self.config.node_name, self.me)
        if granted is not None:
            return [granted]
        # request_work returns None for both "nothing eligible" and "Control
        # unreachable"; only the latter arms the backoff (ADR-0017).
        return None if getattr(self.dispatch, "last_unreachable", False) else []

    def execute(self, item: dict) -> int:
        issue = _granted_issue(item, self.me)
        self.log(f"granted #{issue.number} ({issue.title}); executing the claim")
        reports = execute_claim(
            self.config, self.client, issue, self.session_factory, log=self.log, sink=self.sink
        )
        for report in reports:
            self.log(
                f"run {report.run_id} finished: phase={report.phase} "
                f"pr={report.pr_number} round={report.round} "
                f"agent={report.agent_outcome or 'n/a'}"
            )
        return len(reports)
