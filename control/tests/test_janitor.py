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
    assert actions[0]["repo"] == "acme/sandbox"  # a repo-owned act names its repo


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
    assert control.store.granted_issues("acme/sandbox") == set()
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
    assert control.store.granted_issues("acme/sandbox") == {7}


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


# -- the base-drift lane (ADR-0053, the second enumerated exception) -----------

from theozolith_worker import basedon  # noqa: E402
from theozolith_worker.bootstrap.vocabulary import BLOCKED, NEEDS_HUMAN  # noqa: E402

RECORDED = "a" * 40
MOVED = "b" * 40


def drift_sweep(control: ControlRig, github: FakeGitHubLite) -> list[int]:
    return janitor.sweep_base_drift(control.store, github, log=lambda *_: None)


def _zone_body(blocker: int, sha: str) -> str:
    return basedon.upsert_zone("The dependent PR narrative.", basedon.BasedOn(blocker, sha))


def chained_pair(
    github: FakeGitHubLite,
    *,
    recorded: str = RECORDED,
    tip: str | None = RECORDED,
    blocker_labels: set[str] | None = None,
    blocker_state: str = "open",
    blocker_merged: bool = False,
) -> None:
    """Blocker issue #3 with PR #7 on its branch; dependent PR #9 based on
    that branch with a Based-on zone recording ``recorded``. ``tip`` is the
    blocker branch's current head (None = deleted)."""
    github.add_pr(
        7,
        "ozolith/issue-3",
        blocker_labels if blocker_labels is not None else {"pr_ready", "needs_human"},
        state=blocker_state,
        merged=blocker_merged,
    )
    github.add_pr(
        9,
        "ozolith/issue-5",
        {"pr_ready"},
        base_ref="ozolith/issue-3",
        body=_zone_body(3, recorded),
    )
    if tip is not None:
        github.branch_tips["ozolith/issue-3"] = tip


def test_base_drift_escalates_with_forensics_and_nothing_else(control, github):
    """The exact three-way trigger: dependent open with a zone, blocker PR
    still open, tip off the recorded SHA — one comment naming the blocker
    and both SHAs, then blocked + needs_human; the write log proves nothing
    else reached GitHub."""
    chained_pair(github, tip=MOVED)

    assert drift_sweep(control, github) == [9]
    assert github.writes == [
        ("add_comment", 9),
        ("add_labels", 9, BLOCKED, NEEDS_HUMAN),
    ]
    (comment,) = github.comments[9]
    assert "#3" in comment and "#7" in comment
    assert RECORDED in comment and MOVED in comment
    assert "No auto-repair" in comment


def test_the_same_drift_never_double_writes(control, github):
    """Idempotent per drift: the applied label short-circuits, and after a
    human unblock the SHA-pair key still remembers this exact drift."""
    chained_pair(github, tip=MOVED)
    assert drift_sweep(control, github) == [9]
    writes = list(github.writes)

    assert drift_sweep(control, github) == []  # the label guard
    github.pulls[9]["labels"].discard(BLOCKED)  # human unblocks, same drift
    github.pulls[9]["labels"].discard(NEEDS_HUMAN)
    assert drift_sweep(control, github) == []  # the SHA-pair key
    assert github.writes == writes


def test_a_new_drift_after_a_human_unblock_acts_again(control, github):
    chained_pair(github, tip=MOVED)
    assert drift_sweep(control, github) == [9]
    github.pulls[9]["labels"].discard(BLOCKED)
    github.pulls[9]["labels"].discard(NEEDS_HUMAN)
    github.branch_tips["ozolith/issue-3"] = "c" * 40  # the blocker moved AGAIN

    assert drift_sweep(control, github) == [9]
    assert github.writes[-1] == ("add_labels", 9, BLOCKED, NEEDS_HUMAN)
    assert len(github.comments[9]) == 2


def test_a_matching_tip_is_untouched(control, github):
    chained_pair(github, tip=RECORDED)
    assert drift_sweep(control, github) == []
    assert github.writes == []
    assert control.store.chained_dependents() == []


def test_a_merged_blocker_is_the_healthy_retarget_never_drift(control, github):
    """Blocker merged and branch deleted: GitHub retargets the dependent —
    no write, no drift, and no chained-dependents row (merged is not
    rejected)."""
    chained_pair(github, tip=None, blocker_state="closed", blocker_merged=True)

    assert drift_sweep(control, github) == []
    assert github.writes == []
    assert control.store.chained_dependents() == []


def test_a_deleted_branch_on_an_open_blocker_is_not_drift(control, github):
    chained_pair(github, tip=None)  # branch gone, retarget in flight
    assert drift_sweep(control, github) == []
    assert github.writes == []


def test_a_rejected_blocker_lists_the_chained_dependent_without_writes(control, github):
    """A blocker PR closed unmerged is a human act: the dependent is listed
    via /api/v1/flags and the dashboard — never a GitHub write — and the
    row clears when the condition no longer holds."""
    chained_pair(github, tip=RECORDED, blocker_state="closed")

    assert drift_sweep(control, github) == []
    assert github.writes == []
    (row,) = control.admin("GET", "/api/v1/flags").json()["chained_dependents"]
    assert row["dependent_pr"] == 9 and row["blocker_issue"] == 3
    assert row["blocker_pr"] == 7 and row["blocker_state"] == "closed unmerged"
    assert row["recorded_sha"] == RECORDED

    github.pulls[7]["state"] = "open"  # the human reopens the blocker
    assert drift_sweep(control, github) == []
    assert control.store.chained_dependents() == []


def test_a_blocked_blocker_also_lists_its_dependent(control, github):
    chained_pair(github, tip=RECORDED, blocker_labels={"pr_ready", "needs_human", BLOCKED})
    assert drift_sweep(control, github) == []
    assert github.writes == []
    (row,) = control.store.chained_dependents()
    assert row["blocker_state"] == "blocked"


def test_a_blocked_blocker_with_a_moved_tip_is_visibility_only(control, github):
    """A blocker carrying blocked is a HUMAN-OWNED condition: even with its
    tip off the recorded SHA the janitor records the visibility row and
    writes NOTHING to GitHub — the drift lane escalates only against an
    open, unblocked blocker. Once the human unblocks the blocker, ordinary
    drift escalation resumes."""
    chained_pair(github, tip=MOVED, blocker_labels={"pr_ready", "needs_human", BLOCKED})

    assert drift_sweep(control, github) == []
    assert github.writes == []
    assert github.comments.get(9, []) == []
    assert BLOCKED not in github.pulls[9]["labels"]
    (row,) = control.store.chained_dependents()
    assert row["dependent_pr"] == 9 and row["blocker_state"] == "blocked"

    github.pulls[7]["labels"].discard(BLOCKED)  # the human resolves the blocker
    assert drift_sweep(control, github) == [9]
    assert github.writes == [
        ("add_comment", 9),
        ("add_labels", 9, BLOCKED, NEEDS_HUMAN),
    ]
    assert control.store.chained_dependents() == []


def test_a_departed_dependent_clears_its_row(control, github):
    chained_pair(github, tip=RECORDED, blocker_state="closed")
    drift_sweep(control, github)
    assert control.store.chained_dependents() != []

    github.pulls[9]["state"] = "closed"  # the dependent leaves the pool
    assert drift_sweep(control, github) == []
    assert control.store.chained_dependents() == []


def test_zoneless_and_mangled_bodies_are_skipped_silently(control, github):
    github.add_pr(11, "ozolith/issue-8", {"pr_ready"}, body="No zone at all.")
    github.add_pr(
        12,
        "ozolith/issue-9",
        {"pr_ready"},
        body="<!-- theozolith:based-on\nnot json at all\n-->",
    )
    assert drift_sweep(control, github) == []
    assert github.writes == []
    assert control.store.chained_dependents() == []


def test_one_broken_pr_never_starves_the_drift_pass(control, github):
    """A throwing read on one dependent is logged and skipped; the drifted
    sibling still escalates."""
    chained_pair(github, tip=MOVED)
    github.add_pr(21, "ozolith/issue-13", {"pr_ready", "needs_human"})
    github.branch_tips["ozolith/issue-13"] = MOVED
    github.add_pr(
        23,
        "ozolith/issue-14",
        {"pr_ready"},
        base_ref="ozolith/issue-13",
        body=_zone_body(13, RECORDED),
    )
    original = github.branch_head

    def flaky(branch: str):
        if branch == "ozolith/issue-3":
            raise RuntimeError("boom")
        return original(branch)

    github.branch_head = flaky
    assert drift_sweep(control, github) == [23]
    assert ("add_labels", 23, BLOCKED, NEEDS_HUMAN) in github.writes
    assert not any(w[1] == 9 for w in github.writes)


def test_a_failed_label_write_retries_without_reposting_the_comment(control, github):
    """The comment is recorded under its own sub-key the moment it lands:
    a label write that fails mid-sequence retries next pass and completes
    the escalation with exactly one forensic comment ever posted."""
    chained_pair(github, tip=MOVED)
    original = github.add_labels
    calls = {"n": 0}

    def flaky(number: int, *labels: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("502 from GitHub")
        original(number, *labels)

    github.add_labels = flaky
    assert drift_sweep(control, github) == []  # the label write failed
    assert len(github.comments[9]) == 1

    assert drift_sweep(control, github) == [9]  # completes, no re-comment
    assert len(github.comments[9]) == 1
    assert BLOCKED in github.pulls[9]["labels"]


def test_an_interrupted_recording_heals_without_another_write(control, github):
    """The completed-act failure window: comment and labels land on GitHub
    but the final SHA-pair recording is lost (a crash mid-sequence). The
    next pass proves completion from GitHub state plus the recorded comment
    sub-key and heals the acted ledger with NO new write; a later human
    unblock stays final for that exact pair, and only a genuinely new
    blocker tip escalates again."""
    chained_pair(github, tip=MOVED)
    key = f"base-drift-{RECORDED[:12]}-{MOVED[:12]}"
    escalation = [("add_comment", 9), ("add_labels", 9, BLOCKED, NEEDS_HUMAN)]
    record = control.store.record_janitor_action

    def lossy(repo: str, issue: int, run_id: str, worker: str, reason: str) -> None:
        if run_id == key:
            raise RuntimeError("store lost mid-sequence")
        record(repo, issue, run_id, worker, reason)

    control.store.record_janitor_action = lossy
    assert drift_sweep(control, github) == []  # escalation landed, recording lost
    assert github.writes == escalation
    assert BLOCKED in github.pulls[9]["labels"]
    assert control.store.janitor_acted(github.repo, 9, f"{key}-comment")
    assert not control.store.janitor_acted(github.repo, 9, key)

    control.store.record_janitor_action = record  # the store recovers
    assert drift_sweep(control, github) == []  # healed: recognized complete
    assert control.store.janitor_acted(github.repo, 9, key)
    assert github.writes == escalation  # no second comment, no second label
    assert len(github.comments[9]) == 1

    github.pulls[9]["labels"].discard(BLOCKED)  # the human unblocks
    github.pulls[9]["labels"].discard(NEEDS_HUMAN)
    assert drift_sweep(control, github) == []  # the healed pair is final
    assert github.writes == escalation

    github.branch_tips["ozolith/issue-3"] = "c" * 40  # a genuinely NEW drift
    assert drift_sweep(control, github) == [9]
    assert len(github.comments[9]) == 2
    assert github.writes[-1] == ("add_labels", 9, BLOCKED, NEEDS_HUMAN)
