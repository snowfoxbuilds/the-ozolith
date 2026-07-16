"""The zombie-claim janitor (acceptance 2): a killed Worker's claim returns
to plan_ready after the grace period — and only then, and only safely."""

from __future__ import annotations

from controlrig import ControlRig, FakeGitHubLite, run_event
from theozolith_control import janitor

GRACE = 600.0


def sweep(control: ControlRig, github: FakeGitHubLite) -> list[int]:
    return janitor.sweep(
        control.store, github, grace_seconds=GRACE, clock=control.clock, log=lambda *_: None
    )


def claimed_issue(control: ControlRig, github: FakeGitHubLite, issue: int = 5) -> None:
    github.add_issue(issue, labels={"in_progress"}, assignees=["ozolith-worker-a"])
    control.heartbeat(node="box1")
    control.node_post("/api/v1/events", run_event(issue, "claimed", attempt=None))


def test_zombie_claim_is_returned_to_plan_ready_after_grace(control, github):
    claimed_issue(control, github)
    control.clock.advance(GRACE + 60)  # the node was killed: total silence

    assert sweep(control, github) == [5]
    issue = github.get_issue(5)
    assert issue.assignees == []
    assert "in_progress" not in issue.labels
    assert "plan_ready" in issue.labels
    # The restorative writes are the ONLY writes.
    assert {w[0] for w in github.writes} == {"remove_assignee", "remove_label", "add_labels"}
    actions = control.admin("GET", "/api/v1/audits").json()["janitor_actions"]
    assert actions[0]["issue"] == 5


def test_live_workers_are_left_alone(control, github):
    claimed_issue(control, github)
    control.clock.advance(GRACE - 30)
    assert sweep(control, github) == []
    assert github.writes == []


def test_a_fresh_node_heartbeat_counts_as_liveness(control, github):
    claimed_issue(control, github)
    control.clock.advance(GRACE + 60)
    control.heartbeat(node="box1")  # the node is fine; the claim event is just old
    assert sweep(control, github) == []


def test_a_late_worker_event_counts_as_liveness(control, github):
    claimed_issue(control, github)
    control.clock.advance(GRACE - 10)
    control.node_post("/api/v1/events", run_event(6, "claimed", run_id="r2"))  # same worker
    github.add_issue(6, labels={"in_progress"}, assignees=["ozolith-worker-a"])
    control.clock.advance(60)  # issue 5's event is now stale, but the worker spoke
    assert sweep(control, github) == []


def test_terminal_phases_are_not_live_claims(control, github):
    claimed_issue(control, github)
    control.node_post("/api/v1/events", run_event(5, "pr-open", pr=11))
    control.clock.advance(GRACE + 60)
    assert sweep(control, github) == []


def test_pr_ready_pr_blocks_requeue(control, github):
    """A shipped PR awaiting the Reviewer means the issue must NOT be
    re-queued under it (ADR-0015): the Reviewer owns that path."""
    claimed_issue(control, github)
    github.add_pr(11, head_ref="ozolith/issue-5", labels={"pr_ready"})
    control.clock.advance(GRACE + 60)

    assert sweep(control, github) == []
    assert github.writes == []
    # The skip is recorded and final for this Run.
    actions = control.store.janitor_actions()
    assert "pr_ready" in actions[0]["reason"]


def test_claim_already_released_on_github_records_and_skips(control, github):
    claimed_issue(control, github)
    github.issues[5] = {"labels": {"plan_ready"}, "assignees": []}  # human already fixed it
    control.clock.advance(GRACE + 60)
    assert sweep(control, github) == []
    assert github.writes == []


def test_each_zombie_run_is_cleaned_exactly_once(control, github):
    claimed_issue(control, github)
    control.clock.advance(GRACE + 60)
    assert sweep(control, github) == [5]

    # The issue gets claimed again by a new Run and dies again: cleaned again.
    github.issues[5] = {"labels": {"in_progress"}, "assignees": ["ozolith-worker-b"]}
    control.node_post(
        "/api/v1/events", run_event(5, "claimed", worker="worker-b", run_id="r9", attempt=None)
    )
    assert sweep(control, github) == []  # fresh event: not stale yet
    control.clock.advance(GRACE + 60)
    assert sweep(control, github) == [5]
    # But the SAME dead Run is never re-cleaned.
    control.clock.advance(GRACE + 60)
    assert sweep(control, github) == []
