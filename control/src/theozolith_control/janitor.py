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

Base drift (ADR-0053) is the SECOND enumerated janitorial exception: a
dependent PR whose Based-on zone records its blocker's tip at checkout
silently rots when that blocker branch moves — nothing else watches the
pair. Only under the exact three-way trigger (dependent PR open with a
zone, the named blocker PR still open AND not itself blocked, blocker tip
!= recorded SHA) does the janitor apply blocked + needs_human with a
forensic comment naming the blocker and both SHAs; idempotent per drift
(SHA-pair keyed, interruption-recoverable: an act proven complete on
GitHub whose final recording was lost is healed into the ledger with no
new write), never an auto-repair. A merged-and-deleted blocker is the
healthy retarget path, not drift; a rejected or abandoned blocker — its
PR closed unmerged, or open but carrying blocked — is a HUMAN-OWNED
condition: a moved tip changes nothing automatically once the blocker is
blocked; its chained dependents are surfaced on the dashboard with no
automated GitHub write until the human resolves the blocker itself.
Everything is re-verified against GitHub at act time; ``LiveClaim`` is
untouched — this lane watches PRs, not claims.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from theozolith_worker import basedon
from theozolith_worker.bootstrap.vocabulary import (
    BLOCKED,
    FAILED,
    IN_PROGRESS,
    NEEDS_HUMAN,
    PLAN_READY,
    PR_READY,
)
from theozolith_worker.deps import branch_for
from theozolith_worker.evidence import EVIDENCE_BRANCH, issue_evidence_url, run_dir
from theozolith_worker.githubapi import GitHubClient, PullRequest

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
    if claim.driver:
        driver_seen = store.driver_last_seen(claim.driver)
        if driver_seen is not None:
            candidates.append(driver_seen)
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
        f"Run `{claim.run_id or 'unknown'}` (worker {claim.driver or 'unknown'}) went silent"
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
    store.flag_zombie(claim.issue, claim.run_id, claim.driver, claim.node)

    # Events are advisory: GitHub decides what actually needs releasing.
    issue = client.get_issue(claim.issue)
    if IN_PROGRESS not in issue.labels and not issue.assignees:
        store.record_janitor_action(
            claim.issue, claim.run_id, claim.driver, "claim already released on GitHub"
        )
        store.clear_zombie_flag(claim.issue, claim.run_id)
        return False
    pr = client.find_open_pr_by_head(branch_for(claim.issue))
    if pr is not None and PR_READY in pr.labels:
        # A shipped PR awaits the Reviewer; the Run did not die mid-flight
        # (or its death no longer matters). Never touch the claim under it.
        store.record_janitor_action(
            claim.issue, claim.run_id, claim.driver, f"skipped: PR #{pr.number} is pr_ready"
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
        f"worker {claim.driver or 'unknown'} silent {silence:.0f}s"
        f" (grace {grace_seconds:.0f}s); run {claim.run_id or 'unknown'} escalated"
        f" {FAILED} + {NEEDS_HUMAN} with swept evidence"
    )
    store.record_janitor_action(claim.issue, claim.run_id, claim.driver, reason)
    store.clear_zombie_flag(claim.issue, claim.run_id)
    log(f"janitor: issue #{claim.issue} escalated ({reason})")
    return True


def _drift_comment(based: basedon.BasedOn, blocker_pr: int, tip: str) -> str:
    return (
        f"Base drift (ADR-0053): this PR is based on issue #{based.issue}'s branch"
        f" (PR #{blocker_pr}), whose tip was `{based.sha}` when this work was checked"
        f" out — but the branch has since moved to `{tip}`. The diff and any review"
        " no longer frame against the base this PR was built on."
        f"\n\nNo auto-repair: a human decides — re-queue the dependent so a fresh Run"
        " rebuilds against the new base, or rule the drift immaterial and proceed."
        f"\n\nApplied `{BLOCKED}` + `{NEEDS_HUMAN}`."
    )


def sweep_base_drift(
    store: Store,
    client: GitHubClient,
    *,
    log=_log,
) -> list[int]:
    """One base-drift pass over every open PR carrying a Based-on zone
    (ADR-0053, the second enumerated janitorial exception — see the module
    docstring). Returns the dependent PRs escalated this pass. Also owns the
    chained-dependents display rows: recorded while the named blocker PR is
    closed-unmerged or blocked, cleared the moment the condition no longer
    holds — cache, never authority."""
    acted: list[int] = []
    seen: set[int] = set()
    try:
        prs = client.list_open_prs()
    except Exception as exc:
        # A GitHub outage must not abort the whole janitor pass (the
        # telemetry eviction runs after this sweep in _sweep_pass); the
        # lane simply waits for the next pass.
        log(f"janitor: base-drift listing failed: {exc}")
        return []
    for pr in prs:
        seen.add(pr.number)
        try:
            if _drift_one(store, client, pr, log):
                acted.append(pr.number)
        except Exception as exc:
            # One broken PR (deleted branch race, transient GitHub error)
            # must never starve the rest of the pass.
            log(f"janitor: base-drift check for PR #{pr.number} failed: {exc}")
    # Display rows for PRs that left the open pool (merged, closed) clear
    # here — the condition can no longer be re-verified, so it no longer
    # holds.
    for row in store.chained_dependents():
        if row["dependent_pr"] not in seen:
            store.clear_chained_dependent(row["dependent_pr"])
    return acted


def _drift_one(store: Store, client: GitHubClient, pr: PullRequest, log) -> bool:
    based = basedon.parse_zone(pr.body)
    if based is None:
        # No zone (main-based, or a retargeted round removed it): nothing
        # to watch; drop any stale display row.
        store.clear_chained_dependent(pr.number)
        return False
    blocker_branch = branch_for(based.issue)
    blocker_pr = client.find_pr_by_head(blocker_branch, state="all")

    # The visibility lane (no GitHub write): a rejected or abandoned
    # blocker — its PR closed without merging, or open but blocked — is a
    # human act; the dashboard lists the chained dependent, nothing more.
    rejected = ""
    if blocker_pr is not None:
        if blocker_pr.state != "open" and not blocker_pr.merged:
            rejected = "closed unmerged"
        elif blocker_pr.state == "open" and BLOCKED in blocker_pr.labels:
            rejected = "blocked"
    if rejected:
        store.record_chained_dependent(
            pr.number, based.issue, blocker_pr.number, rejected, based.sha
        )
        # A rejected or blocked blocker is a HUMAN-OWNED condition: once a
        # human (or the one-strike lane) has put the blocker itself on
        # hold, its tip moving changes nothing automatically — the
        # dependent's fate rides the human's call on the blocker, and
        # stacking a drift escalation on top would only bury that call
        # under machine noise. Visibility only, no GitHub write, until the
        # blocker is open and unblocked again.
        return False
    store.clear_chained_dependent(pr.number)

    # The drift lane's trigger (ADR-0053): dependent open with a zone
    # (established above), blocker PR still OPEN and NOT blocked (ruled out
    # above), and the blocker tip off the recorded SHA. A closed blocker PR
    # is never drift; a deleted branch is the healthy retarget in flight.
    if blocker_pr is None or blocker_pr.state != "open":
        return False
    tip = client.branch_head(blocker_branch)
    if tip is None or tip == based.sha:
        return False

    # Idempotency: this exact drift already completed its act — the
    # SHA-pair key lets a NEW drift after a human unblock act again while
    # the same drift never double-writes.
    key = f"base-drift-{based.sha[:12]}-{tip[:12]}"
    if store.janitor_acted(pr.number, key):
        return False
    comment_recorded = store.janitor_acted(pr.number, f"{key}-comment")
    if BLOCKED in pr.labels:
        if comment_recorded:
            # Interruption recovery: GitHub state (the label is on) plus
            # the recorded comment sub-key (the forensics for exactly this
            # SHA pair landed) prove the escalation completed, but the
            # final recording was lost — a crash between the label write
            # and the ledger write. Heal the ledger with NO GitHub write,
            # so a later human unblock is final for this pair.
            store.record_janitor_action(
                pr.number,
                key,
                "",
                f"base-drift escalation on PR #{pr.number} recovered as already"
                f" complete ({based.sha[:12]} -> {tip[:12]}): label present and"
                " forensic comment recorded; ledger healed, no write",
            )
            log(f"janitor: base-drift act on PR #{pr.number} recovered as complete")
        return False

    # The comment lands first: no observable blocked state without the
    # forensics that explain it (the reviewer's publish order). The comment
    # is recorded under its own sub-key the moment it lands, so a label
    # write that fails mid-sequence retries next pass WITHOUT re-posting
    # the forensics; the full key is recorded only once the labels landed —
    # a later human unblock is then final for this drift.
    if not comment_recorded:
        client.add_comment(pr.number, _drift_comment(based, blocker_pr.number, tip))
        store.record_janitor_action(
            pr.number, f"{key}-comment", "", f"base-drift forensic comment on PR #{pr.number}"
        )
    client.add_labels(pr.number, BLOCKED, NEEDS_HUMAN)
    reason = (
        f"base drift on PR #{pr.number}: blocker #{based.issue} (PR"
        f" #{blocker_pr.number}) moved {based.sha[:12]} -> {tip[:12]};"
        f" escalated {BLOCKED} + {NEEDS_HUMAN} (ADR-0053, no auto-repair)"
    )
    store.record_janitor_action(pr.number, key, "", reason)
    log(f"janitor: {reason}")
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
