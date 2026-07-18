"""The zombie-claim janitor: two-phase, evidence-first escalation (ADR-0016),
plus the release of never-activated dispatch grants (ADR-0017).

A Worker that died mid-Run leaves an issue assigned + in_progress with no
process to finish it. The janitor watches the only signals the Control Node
has — Run events and node heartbeats — and acts in two phases:

1. Silence past the grace period flags the claim on the dashboard. Nothing
   touches GitHub: a down node stalls its zombie issues until it returns or
   a human intervenes (stability and token efficiency over uptime).
2. Only once the Run's evidence bundle is on the evidence branch (the
   returned driver's boot sweep pushed it, or a live push landed before
   death) does the janitor release the claim and apply failed + needs_human
   with the resolving evidence link. There is no automatic re-queue and no
   escalate-before-evidence.

Janitorial liveness corrections are the enumerated exception to the Control
Node never originating coordination (ADR-0017). Every action re-verifies
GitHub first (events may be stale) and skips issues whose open PR carries
pr_ready — that PR is the Reviewer's to drive.

Grant release is the other reaper here: a grant the Control Node wrote whose
driver never emitted a claimed event (lost response, death before pickup) is
invisible to every other mechanism, so after the activation window the
claim is unwound and the issue returned to plan_ready.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from theozolith_worker.bootstrap.vocabulary import (
    FAILED,
    IN_PROGRESS,
    NEEDS_HUMAN,
    PLAN_READY,
    PR_READY,
)
from theozolith_worker.evidence import EVIDENCE_BRANCH, issue_evidence_url, run_dir
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


def _evidence_landed(client: GitHubClient, claim: LiveClaim) -> bool:
    """Is this Run's bundle on the evidence branch? Either the returned
    driver's boot sweep pushed it (swept.json) or a live push landed before
    the driver died (run.json) — both are complete forensics (ADR-0016).
    The sweeps/ path is the fallback for a job dir swept before its issue
    metadata could be read (ADR-0018)."""
    prefix = run_dir(claim.issue, claim.run_id)
    return any(
        client.path_exists(path, ref=EVIDENCE_BRANCH)
        for path in (
            f"{prefix}/swept.json",
            f"{prefix}/run.json",
            f"sweeps/{claim.run_id}/swept.json",
        )
    )


def _escalation_comment(claim: LiveClaim, repo: str, silence: float) -> str:
    return (
        f"Run `{claim.run_id or 'unknown'}` (worker {claim.worker or 'unknown'}) went silent"
        f" mid-flight ({silence:.0f}s past its last signal) and its evidence bundle has landed."
        f" The claim is released and escalated: `{FAILED}` + `{NEEDS_HUMAN}` (ADR-0016)."
        f"\n\nEvidence: {issue_evidence_url(repo, claim.issue)}"
        f"\n\nA human re-queues by removing `{FAILED}` and restoring `{PLAN_READY}`."
    )


def sweep(
    store: Store,
    client: GitHubClient,
    *,
    grace_seconds: float,
    clock: Callable[[], float] = time.time,
    log=_log,
) -> list[int]:
    """One zombie pass; returns the issues escalated with evidence."""
    escalated: list[int] = []
    for claim in store.live_claims():
        try:
            if _sweep_one(store, client, claim, grace_seconds, clock, log):
                escalated.append(claim.issue)
        except Exception as exc:
            # One broken claim (deleted issue, transient GitHub error) must
            # never starve the rest of the pass.
            log(f"janitor: issue #{claim.issue} sweep failed: {exc}")
    return escalated


def _sweep_one(
    store: Store,
    client: GitHubClient,
    claim: LiveClaim,
    grace_seconds: float,
    clock: Callable[[], float],
    log,
) -> bool:
    if store.janitor_acted(claim.issue, claim.run_id):
        return False  # this Run's zombie claim was already handled
    silence = clock() - _last_seen(store, claim)
    if silence <= grace_seconds:
        # The Worker resurfaced (or never left): drop any stale flag.
        store.clear_zombie_flag(claim.issue, claim.run_id)
        return False

    # Phase 1: dashboard flag only — GitHub is untouched.
    store.flag_zombie(claim.issue, claim.run_id, claim.worker, claim.node)

    # Events are advisory: GitHub decides what actually needs releasing.
    issue = client.get_issue(claim.issue)
    if IN_PROGRESS not in issue.labels and not issue.assignees:
        store.record_janitor_action(
            claim.issue, claim.run_id, claim.worker, "claim already released on GitHub"
        )
        store.clear_zombie_flag(claim.issue, claim.run_id)
        return False
    pr = client.find_open_pr_by_head(branch_for(claim.issue))
    if pr is not None and PR_READY in pr.labels:
        # A shipped PR awaits the Reviewer; the Run did not die mid-flight
        # (or its death no longer matters). Never touch the claim under it.
        store.record_janitor_action(
            claim.issue, claim.run_id, claim.worker, f"skipped: PR #{pr.number} is pr_ready"
        )
        store.clear_zombie_flag(claim.issue, claim.run_id)
        return False

    # Phase 2: evidence first. No bundle yet = the driver has not
    # returned to sweep; the claim stays flagged for a human call.
    if not _evidence_landed(client, claim):
        return False

    # The forensics and the human-facing labels land before the claim is
    # stripped, so a mid-sequence failure can never leave the issue bare.
    client.add_labels(claim.issue, FAILED, NEEDS_HUMAN)
    client.add_comment(claim.issue, _escalation_comment(claim, client.repo, silence))
    for login in issue.assignees:
        client.remove_assignee(claim.issue, login)
    client.remove_label(claim.issue, IN_PROGRESS)
    reason = (
        f"worker {claim.worker or 'unknown'} silent {silence:.0f}s"
        f" (grace {grace_seconds:.0f}s); run {claim.run_id or 'unknown'} escalated"
        f" {FAILED} + {NEEDS_HUMAN} with swept evidence"
    )
    store.record_janitor_action(claim.issue, claim.run_id, claim.worker, reason)
    store.clear_zombie_flag(claim.issue, claim.run_id)
    log(f"janitor: issue #{claim.issue} escalated ({reason})")
    return True


def release_never_activated(
    store: Store,
    client: GitHubClient,
    *,
    window_seconds: float,
    log=_log,
) -> list[int]:
    """Unwind grants with no claimed event inside the activation window
    (ADR-0017): the Control Node wrote these claims, so it reverts them."""
    released: list[int] = []
    live = {claim.issue for claim in store.live_claims()}
    for grant in store.expired_grants(window_seconds):
        number = grant["issue"]
        if number in live:
            # A claimed event exists after all (activation raced or a
            # retried release re-recorded the grant): the claim is real,
            # only the bookkeeping row goes.
            store.release_grant(number)
            continue
        try:
            # Deleting the grant row FIRST decides the race against a
            # late-landing activation: whoever removes the row owns the
            # issue's fate, so an activated claim is never unwound.
            if not store.release_grant(number):
                continue
            issue = client.get_issue(number)
            for login in issue.assignees:
                client.remove_assignee(number, login)
            client.remove_label(number, IN_PROGRESS)
            client.add_labels(number, PLAN_READY)
        except Exception as exc:
            # Put the row back so the next pass retries: a half-unwound
            # claim with no grant row would be invisible to every reaper.
            store.record_grant(number, grant["worker"], grant["node"], grant["login"])
            log(f"janitor: releasing grant for #{number} failed (will retry): {exc}")
            continue
        store.record_janitor_action(
            number,
            "(never-activated)",
            grant["worker"],
            f"released never-activated grant to {grant['worker']}"
            f" (no claimed event within {window_seconds:.0f}s)",
        )
        released.append(number)
        log(f"janitor: issue #{number} grant to {grant['worker']} never activated; released")
    return released
