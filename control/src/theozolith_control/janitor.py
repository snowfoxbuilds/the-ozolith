"""The zombie-claim janitor: liveness-based claim restoration (ADR-0002).

A Worker that died mid-Run leaves an issue assigned + in_progress with no
process to finish it. The janitor watches the only signal the Control Node
has — Run events and node heartbeats — and, once a claim's Worker has been
silent past the grace period, restores the issue to the claimable pool:
assignees stripped, in_progress removed, plan_ready back on.

This is the one restorative direction in which the Control Node ever writes
claim state. Every action re-verifies GitHub first (events may be stale) and
skips issues whose open PR carries pr_ready — that PR is the Reviewer's to
drive, and re-queueing under it would fork a duplicate Run (ADR-0015).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from theozolith_worker.bootstrap.vocabulary import IN_PROGRESS, PLAN_READY, PR_READY
from theozolith_worker.githubapi import GitHubClient
from theozolith_worker.runner import branch_for

from theozolith_control.store import LiveClaim, Store


def _log(message: str) -> None:
    print(message, flush=True)


def _last_seen(store: Store, claim: LiveClaim) -> float:
    """A Worker's liveness: the freshest of its node's heartbeat and its own
    last event (either alone can lag; a dead box stops both)."""
    candidates = [claim.last_event_at]
    if claim.node:
        node_seen = store.node_last_seen(claim.node)
        if node_seen is not None:
            candidates.append(node_seen)
    if claim.worker:
        worker_seen = store.worker_last_seen(claim.worker)
        if worker_seen is not None:
            candidates.append(worker_seen)
    return max(candidates)


def sweep(
    store: Store,
    client: GitHubClient,
    *,
    grace_seconds: float,
    clock: Callable[[], float] = time.time,
    log=_log,
) -> list[int]:
    """One janitor pass; returns the issues restored to plan_ready."""
    restored: list[int] = []
    for claim in store.live_claims():
        if store.janitor_acted(claim.issue, claim.run_id):
            continue  # this Run's zombie claim was already cleaned
        silence = clock() - _last_seen(store, claim)
        if silence <= grace_seconds:
            continue

        # Events are advisory: GitHub decides what actually needs restoring.
        issue = client.get_issue(claim.issue)
        if IN_PROGRESS not in issue.labels and not issue.assignees:
            store.record_janitor_action(
                claim.issue, claim.run_id, claim.worker, "claim already released on GitHub"
            )
            continue
        pr = client.find_open_pr_by_head(branch_for(claim.issue))
        if pr is not None and PR_READY in pr.labels:
            # A shipped PR awaits the Reviewer; the Run did not die mid-flight
            # (or its death no longer matters). Never re-queue under it.
            store.record_janitor_action(
                claim.issue, claim.run_id, claim.worker, f"skipped: PR #{pr.number} is pr_ready"
            )
            continue

        for login in issue.assignees:
            client.remove_assignee(claim.issue, login)
        client.remove_label(claim.issue, IN_PROGRESS)
        client.add_labels(claim.issue, PLAN_READY)
        reason = (
            f"worker {claim.worker or 'unknown'} silent {silence:.0f}s"
            f" (grace {grace_seconds:.0f}s); run {claim.run_id or 'unknown'}"
        )
        store.record_janitor_action(claim.issue, claim.run_id, claim.worker, reason)
        restored.append(claim.issue)
        log(f"janitor: issue #{claim.issue} returned to {PLAN_READY} ({reason})")
    return restored
