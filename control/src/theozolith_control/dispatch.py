"""Claim dispatch: the Control Node as the single writer of claim creation.

ADR-0017 write-through: a Worker driver asks for work; the Control Node
selects an eligible plan_ready issue, writes the claim to GitHub itself —
assigns the driver's GitHub login, adds in_progress, removes plan_ready —
and returns the issue in the same response. Grants are serialized on one
lock, so claim races are structurally impossible; assign-and-verify is
gone. The Reviewer side of the same path is discovery-only (no claim label
exists on PRs).

Gates applied before any grant (ADR-0016 + the queue-behind rule):

- a quarantined node gets nothing until a human releases it;
- a node with a pending drain/recycle/update gets nothing, which bounds a
  queued-behind command by the current Run;
- an issue carrying ``failed`` is refused even with plan_ready present and
  surfaced on the dashboard as a malformed state — a visibly stalled grant
  beats a laundered failure loop;
- issues already granted (awaiting activation) or with a live Run are
  skipped.

GitHub remains the sole source of coordination truth: everything here
reconciles to what GitHub answers at grant time, never the reverse.
"""

from __future__ import annotations

import threading
from typing import Any

from theozolith_worker.bootstrap.vocabulary import (
    BLOCKED,
    FAILED,
    IN_PROGRESS,
    NEEDS_HUMAN,
    PLAN_READY,
    PR_READY,
)
from theozolith_worker.githubapi import GitHubClient

from theozolith_control.store import Store


def _log(message: str) -> None:
    print(message, flush=True)


class Dispatcher:
    """One serialized grant path for the whole fleet (ADR-0017)."""

    def __init__(self, store: Store, client: GitHubClient, *, log=_log):
        self._store = store
        self._client = client
        self._log = log
        self._lock = threading.Lock()

    def grant_work(self, worker: str, node: str, login: str) -> dict[str, Any]:
        """Grant one issue to ``worker`` (write-through), or explain why not.

        Returns ``{"issue": {...}}`` on a grant and ``{"issue": None}``
        (with an optional ``reason``) otherwise.
        """
        with self._lock:
            self._store.upsert_driver(worker, node, login, "worker")
            quarantine = self._store.node_quarantine(node)
            if quarantine is not None:
                return {"issue": None, "reason": f"node {node!r} quarantined: {quarantine}"}
            pending = self._store.pending_lifecycle_commands(node)
            if pending:
                return {
                    "issue": None,
                    "reason": f"node {node!r} has pending {'/'.join(pending)}; no new work",
                }

            granted = self._store.granted_issues()
            live = {claim.issue for claim in self._store.live_claims()}
            for issue in self._client.list_open_issues(PLAN_READY):
                if FAILED in issue.labels:
                    detail = "carries failed + plan_ready; dispatch refuses to grant (ADR-0016)"
                    self._store.record_malformed(issue.number, detail)
                    self._log(f"dispatch: issue #{issue.number} {detail}")
                    continue
                self._store.clear_malformed(issue.number)
                if issue.assignees or IN_PROGRESS in issue.labels:
                    continue  # spoken for on GitHub (hand-edited labels stay meaningful)
                if issue.number in granted or issue.number in live:
                    continue

                # Write-through: the claim exists on GitHub before the driver
                # ever sees the issue (ADR-0017).
                self._client.add_assignees(issue.number, login)
                self._client.add_labels(issue.number, IN_PROGRESS)
                self._client.remove_label(issue.number, PLAN_READY)
                self._store.record_grant(issue.number, worker, node, login)
                self._log(f"dispatch: issue #{issue.number} granted to {worker} ({login})")
                return {
                    "issue": {
                        "number": issue.number,
                        "title": issue.title,
                        "body": issue.body,
                        "labels": sorted((issue.labels | {IN_PROGRESS}) - {PLAN_READY}),
                    }
                }
            return {"issue": None}

    def review_targets(self, worker: str, node: str, login: str) -> dict[str, Any]:
        """Discovery for the Reviewer: reviewable pr_ready PRs, no writes."""
        self._store.upsert_driver(worker, node, login, "reviewer")
        numbers = [
            candidate.number
            for candidate in self._client.list_open_prs_by_label(PR_READY)
            if NEEDS_HUMAN not in candidate.labels and BLOCKED not in candidate.labels
        ]
        return {"prs": numbers}
