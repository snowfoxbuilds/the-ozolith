"""The zombie-claim janitor (ADR-0016): a silent claim flags the dashboard
without touching GitHub; only landed evidence releases and escalates it —
plus the release of never-activated dispatch grants (ADR-0017)."""

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


def test_silent_claim_is_flagged_but_github_is_untouched(control, github):
    """Acceptance 11, phase 1: silence flags the dashboard; the claim is
    NOT released and nothing is re-queued (evidence first, ADR-0016)."""
    claimed_issue(control, github)
    control.clock.advance(GRACE + 60)  # the node was killed: total silence

    assert sweep(control, github) == []
    assert github.writes == []
    flags = control.admin("GET", "/api/v1/flags").json()["zombie_flags"]
    assert flags[0]["issue"] == 5 and flags[0]["run_id"] == "r1"
    issue = github.get_issue(5)
    assert issue.assignees == ["ozolith-worker-a"] and "in_progress" in issue.labels


def test_swept_evidence_releases_and_escalates_the_claim(control, github):
    """Acceptance 11, phase 2: once the boot sweep's bundle lands, the claim
    is released and escalated failed + needs_human with the evidence link —
    never re-queued to plan_ready."""
    claimed_issue(control, github)
    control.clock.advance(GRACE + 60)
    assert sweep(control, github) == []  # no evidence yet: flag only

    github.evidence.add("runs/issue-5/r1/swept.json")
    assert sweep(control, github) == [5]
    issue = github.get_issue(5)
    assert issue.assignees == []
    assert "in_progress" not in issue.labels
    assert "failed" in issue.labels and "needs_human" in issue.labels
    assert "plan_ready" not in issue.labels  # no automatic re-queue
    comment = github.comments[5][0]
    assert "theozolith/evidence" in comment and "runs/issue-5" in comment
    # The flag resolved and the action is on record.
    assert control.admin("GET", "/api/v1/flags").json()["zombie_flags"] == []
    actions = control.store.janitor_actions()
    assert actions[0]["issue"] == 5 and "escalated" in actions[0]["reason"]


def test_live_pushed_evidence_also_counts(control, github):
    """A driver that died after its evidence push but before its labels:
    run.json is complete forensics too."""
    claimed_issue(control, github)
    control.clock.advance(GRACE + 60)
    github.evidence.add("runs/issue-5/r1/run.json")
    assert sweep(control, github) == [5]


def test_live_workers_are_left_alone(control, github):
    claimed_issue(control, github)
    control.clock.advance(GRACE - 30)
    assert sweep(control, github) == []
    assert github.writes == []
    assert control.store.zombie_flags() == []


def test_a_fresh_node_heartbeat_counts_as_liveness(control, github):
    claimed_issue(control, github)
    control.clock.advance(GRACE + 60)
    control.heartbeat(node="box1")  # the node is fine; the claim event is just old
    assert sweep(control, github) == []


def test_a_resurfaced_worker_clears_its_flag(control, github):
    claimed_issue(control, github)
    control.clock.advance(GRACE + 60)
    sweep(control, github)
    assert control.store.zombie_flags() != []

    control.heartbeat(node="box1")  # the node came back
    assert sweep(control, github) == []
    assert control.store.zombie_flags() == []


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


def test_pr_ready_pr_blocks_any_action(control, github):
    """A shipped PR awaiting the Reviewer means the claim must NOT be
    touched under it (ADR-0015): the Reviewer owns that path."""
    claimed_issue(control, github)
    github.add_pr(11, head_ref="ozolith/issue-5", labels={"pr_ready"})
    github.evidence.add("runs/issue-5/r1/swept.json")
    control.clock.advance(GRACE + 60)

    assert sweep(control, github) == []
    assert github.writes == []
    # The skip is recorded and final for this Run; the flag clears.
    actions = control.store.janitor_actions()
    assert "pr_ready" in actions[0]["reason"]
    assert control.store.zombie_flags() == []


def test_claim_already_released_on_github_records_and_skips(control, github):
    claimed_issue(control, github)
    github.issues[5] = {"labels": {"plan_ready"}, "assignees": []}  # human already fixed it
    control.clock.advance(GRACE + 60)
    assert sweep(control, github) == []
    assert github.writes == []
    assert control.store.zombie_flags() == []


def test_each_zombie_run_is_escalated_exactly_once(control, github):
    claimed_issue(control, github)
    control.clock.advance(GRACE + 60)
    github.evidence.add("runs/issue-5/r1/swept.json")
    assert sweep(control, github) == [5]

    # A human re-queued it; a new Run dies again: handled again (new run_id).
    github.issues[5] = {"labels": {"in_progress"}, "assignees": ["ozolith-worker-b"]}
    control.node_post(
        "/api/v1/events", run_event(5, "claimed", worker="worker-b", run_id="r9", attempt=None)
    )
    assert sweep(control, github) == []  # fresh event: not stale yet
    control.clock.advance(GRACE + 60)
    github.evidence.add("runs/issue-5/r9/swept.json")
    assert sweep(control, github) == [5]
    # But the SAME dead Run is never re-escalated.
    control.clock.advance(GRACE + 60)
    assert sweep(control, github) == []


# -- never-activated grants (ADR-0017) -----------------------------------------


def release(control: ControlRig, github: FakeGitHubLite) -> list[int]:
    return janitor.release_never_activated(
        control.store, github, window_seconds=60, log=lambda *_: None
    )


def test_a_grant_with_no_claimed_event_is_released_after_the_window(control, github):
    github.add_issue(7, labels={"plan_ready"}, assignees=[])
    control.dispatch()
    control.clock.advance(61)

    assert release(control, github) == [7]
    issue = github.get_issue(7)
    assert issue.assignees == []
    assert "in_progress" not in issue.labels and "plan_ready" in issue.labels
    assert control.store.granted_issues() == set()
    # Releasing makes the issue grantable again.
    assert control.dispatch(worker="worker-b").json()["issue"]["number"] == 7


def test_an_activated_grant_is_never_released(control, github):
    github.add_issue(7, labels={"plan_ready"}, assignees=[])
    control.dispatch()
    control.node_post("/api/v1/events", run_event(7, "claimed", run_id="r1"))
    control.clock.advance(3600)
    assert release(control, github) == []


def test_a_fresh_grant_is_left_alone(control, github):
    github.add_issue(7, labels={"plan_ready"}, assignees=[])
    control.dispatch()
    control.clock.advance(30)
    assert release(control, github) == []
    assert control.store.granted_issues() == {7}


def test_one_broken_claim_never_starves_the_sweep(control, github):
    """A deleted/transferred issue must not abort the whole pass."""
    claimed_issue(control, github, issue=5)
    github.add_issue(6, labels={"in_progress"}, assignees=["ozolith-worker-a"])
    control.node_post("/api/v1/events", run_event(6, "claimed", run_id="r6"))
    del github.issues[5]  # gone on GitHub: get_issue raises
    control.clock.advance(GRACE + 60)
    github.evidence.add("runs/issue-6/r6/swept.json")

    assert sweep(control, github) == [6]  # issue 6 still escalates


def test_a_driver_dead_between_failed_run_and_retry_stays_visible(control, github):
    """ADR-0016: the driver holds the claim through the local retry, so a
    failed-latest claim is still LIVE — a death in the retry window flags
    and (with the failed Run's own evidence) escalates."""
    claimed_issue(control, github)
    control.node_post("/api/v1/events", run_event(5, "failed", run_id="r1"))
    control.clock.advance(GRACE + 60)  # the driver never came back

    assert sweep(control, github) == []  # flagged, waiting on evidence
    assert control.store.zombie_flags() != []
    github.evidence.add("runs/issue-5/r1/run.json")  # the failed Run's live push
    assert sweep(control, github) == [5]
    labels = github.get_issue(5).labels
    assert "failed" in labels and "needs_human" in labels
