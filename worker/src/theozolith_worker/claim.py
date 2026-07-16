"""The Claim Protocol: exclusive ownership of a plan_ready issue.

Self-assign plus the in_progress label, then re-read to verify sole assignee;
back off otherwise (CONTEXT.md). A tie — two Workers whose self-assigns land
before either verifies — is broken deterministically by the issue event
timeline: the earliest-assigned login keeps the claim, everyone else removes
itself. The optional Control Node pre-filter runs first and is skipped
cleanly when absent; GitHub assign-and-verify is the only authority.
"""

from __future__ import annotations

from theozolith_worker.bootstrap.vocabulary import IN_PROGRESS, PLAN_READY
from theozolith_worker.githubapi import GitHubClient, Issue
from theozolith_worker.prefilter import ClaimPrefilter, NullPrefilter


def claimable(issue: Issue) -> bool:
    """Worth attempting: plan_ready, unassigned, no Run already in flight."""
    return PLAN_READY in issue.labels and IN_PROGRESS not in issue.labels and not issue.assignees


def attempt_claim(
    client: GitHubClient,
    issue: Issue,
    prefilter: ClaimPrefilter | None = None,
) -> bool:
    """Try to claim ``issue``; True when this Worker holds the sole claim."""
    me = client.viewer_login()
    if not claimable(issue):
        return False
    if not (prefilter or NullPrefilter()).allows(issue.number, me):
        return False

    client.add_assignees(issue.number, me)
    client.add_labels(issue.number, IN_PROGRESS)

    fresh = client.get_issue(issue.number)
    if fresh.assignees == [me]:
        client.remove_label(issue.number, PLAN_READY)
        return True

    # Contested: earliest assigned (by event timeline) keeps the claim.
    order = client.assign_order(issue.number)
    still_assigned = [login for login in order if login in fresh.assignees]
    if still_assigned and still_assigned[0] == me:
        client.remove_label(issue.number, PLAN_READY)
        return True

    # Back off: undo the self-assign; the surviving claimant owns the rest.
    client.remove_assignee(issue.number, me)
    return False
