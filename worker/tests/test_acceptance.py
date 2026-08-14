"""The M2 acceptance criteria, end to end.

Each test drives the real Worker/Reviewer driver code paths (real
GitHubClient, real git remote, real job directories); only the GitHub
transport and the run containers are substituted — the fake session speaks
the same job-dir protocol at the same seam and executes gate jobs in a real
shell under the container's environment.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from conftest import (
    REVIEWER_LOGIN,
    WORKER_LOGIN,
    Harness,
    IdentityFailure,
    behavior_write,
    make_harness,
    write_decisions,
)
from fakegithub import rate_limited_response
from theozolith_worker import decisions, verdict
from theozolith_worker import jobdir as jobdir_module
from theozolith_worker.bootstrap.vocabulary import (
    ATTEMPT_PREFIX,
    BLOCKED,
    FAILED,
    IN_PROGRESS,
    NEEDS_HUMAN,
    PLAN_READY,
    PR_READY,
)
from theozolith_worker.containers import EngineError
from theozolith_worker.gitops import GitError
from theozolith_worker.jobdir import AgentOutcome
from theozolith_worker.runner import branch_for
from theozolith_worker.sessions import SessionError
from theozolith_worker.sweep import pending_dir

CRITERIA_BODY = "## Acceptance criteria\n- change.txt exists on the branch\n"


def approve_reply(**over) -> dict:
    return {
        "verdict": "approve",
        "deviation": "low",
        "risk": "low",
        "evidence": "change.txt satisfies the stated acceptance criteria.",
        **over,
    }


def revise_reply(plan: str, resume: str = "", cherry_pick: list[str] | None = None) -> dict:
    return {
        "verdict": "revise",
        "deviation": "medium",
        "risk": "medium",
        "evidence": "The diff diverges from the plan.",
        "revised_plan": plan,
        "resume_commit": resume,
        "cherry_pick": cherry_pick or [],
    }


def escalate_reply(evidence: str) -> dict:
    return {"verdict": "escalate", "deviation": "high", "risk": "high", "evidence": evidence}


# -- 1. happy path ------------------------------------------------------------


def test_happy_path(harness: Harness):
    number = harness.file_issue("Add change.txt", CRITERIA_BODY, risk="medium")

    assert harness.worker_once() == 1

    (pr_number,) = harness.fake.open_pr_numbers()
    assert PR_READY in harness.fake.labels_of(pr_number)
    body = harness.fake.issues[pr_number]["body"]
    assert f"Closes #{number}" in body
    assert decisions.parse(body) is not None  # mandatory Decisions Section
    assert harness.fake.assignees_of(number) == [WORKER_LOGIN]
    assert harness.fake.labels_of(number) == {IN_PROGRESS, "risk:medium"}
    assert harness.remote_file(branch_for(number), "change.txt") == "run 1"

    harness.reviewer_replies.append(approve_reply())
    assert harness.reviewer_once() == 1

    labels = harness.fake.labels_of(pr_number)
    assert {PR_READY, NEEDS_HUMAN, "deviation:low", "risk:low"} <= labels
    comment = harness.fake.comments[pr_number][-1]["body"]
    assert comment.startswith("### Reviewer verdict: approve")
    assert "acceptance criteria" in comment  # evidence-citing
    assert f"tree/theozolith/evidence/runs/issue-{number}" in comment

    # The bundle link resolves to a real git ref holding this Run's evidence —
    # the transcript is the headless session's structured output stream
    # (ADR-0019), and run.json carries the usage the stream reported.
    paths = harness.evidence_paths()
    run_dirs = [p for p in paths if p.startswith(f"runs/issue-{number}/") and "/reviews/" not in p]
    (run_json_path,) = [p for p in run_dirs if p.endswith("/run.json")]
    run_json = json.loads(harness.evidence_file(run_json_path))
    assert run_json["tokens"] == 180  # extracted from the stream's usage
    (transcript_path,) = [p for p in run_dirs if p.endswith("/transcript.txt")]
    transcript = harness.evidence_file(transcript_path)
    first_event = json.loads(transcript.splitlines()[0])
    assert first_event["type"] == "system"  # line-per-event JSON stream
    assert any(f"runs/issue-{number}/reviews/round-1" in p for p in paths)

    # Containers: run + review round, correctly named, none left behind.
    names = harness.record.launched
    assert any(name.startswith("ozolith-run-") for name in names)
    assert f"ozolith-review-{pr_number}-round-1" in names
    assert harness.record.alive == set()


def test_process_issues_render_everywhere_and_change_no_state(harness: Harness):
    """2026-07-22 grilling: a populated process_issues[] renders as a
    Process issues block in the PR body and the verdict comment, and
    provably changes no verdict, label, or gate outcome — the label sets
    are exactly the ones the plain happy path produces."""
    number = harness.file_issue("Add change.txt", CRITERIA_BODY, risk="medium")
    harness.worker_behaviors.append(
        behavior_write(
            {"change.txt": "run 1\n"},
            decisions=[{"what": "made the change", "why": "the issue asked"}],
            process_issues=[
                {"friction": "the gate ran twice", "suggested_fix": "dedupe gate steps"}
            ],
        )
    )
    assert harness.worker_once() == 1

    (pr_number,) = harness.fake.open_pr_numbers()
    body = harness.fake.issues[pr_number]["body"]
    assert "### Process issues" in body
    assert "the gate ran twice — suggested fix: dedupe gate steps" in body
    section = decisions.parse(body)
    assert section.process_issues == [
        decisions.ProcessIssue("the gate ran twice", "dedupe gate steps")
    ]
    # Identical PR/issue state to the plain happy path: advisory only.
    assert PR_READY in harness.fake.labels_of(pr_number)
    assert harness.fake.labels_of(number) == {IN_PROGRESS, "risk:medium"}

    harness.reviewer_replies.append(
        approve_reply(
            process_issues=[{"friction": "diff lacked context", "suggested_fix": "use -U10"}]
        )
    )
    assert harness.reviewer_once() == 1

    comment = harness.fake.comments[pr_number][-1]["body"]
    assert comment.startswith("### Reviewer verdict: approve")
    assert "#### Process issues" in comment
    assert "diff lacked context — suggested fix: use -U10" in comment
    # The exact label set an approve without process_issues produces.
    assert harness.fake.labels_of(pr_number) == {
        PR_READY,
        NEEDS_HUMAN,
        "deviation:low",
        "risk:low",
    }


# -- 2. claims (write-through dispatch, ADR-0017) ------------------------------


def test_worker_never_writes_claim_state(harness: Harness):
    """The Control Node is the single writer of claim creation: the driver
    acts on an already-claimed issue and never assigns, labels a claim, or
    dequeues plan_ready itself."""
    number = harness.file_issue("Dispatched", CRITERIA_BODY)
    assert harness.worker_once() == 1

    # The claim exists (written by dispatch), the Run shipped on top of it.
    assert harness.fake.assignees_of(number) == [WORKER_LOGIN]
    assert harness.fake.labels_of(number) == {IN_PROGRESS, "risk:medium"}
    claim_writes = [
        (actor, method, path)
        for actor, method, path, _ in harness.fake.write_log
        if path.endswith("/assignees")
        or (path.endswith("/labels") and f"/issues/{number}/" in path)
        or path.endswith(f"/labels/{PLAN_READY}")
    ]
    assert claim_writes == [], f"driver wrote claim state itself: {claim_writes}"


def test_no_grant_means_no_run(harness: Harness):
    """Issues spoken for on GitHub are never granted; the driver idles."""
    number = harness.file_issue("Busy", CRITERIA_BODY)
    harness.fake.force_assign(number, "ozolith-worker-b")  # someone else owns it
    assert harness.worker_once() == 0
    assert harness.record.work_launched == []
    assert harness.fake.assignees_of(number) == ["ozolith-worker-b"]


def test_failed_label_is_refused_at_dispatch(harness: Harness):
    """ADR-0016: failed overrides plan_ready — the issue is never granted."""
    number = harness.file_issue("Broken state", CRITERIA_BODY)
    harness.fake.issues[number]["labels"].append({"name": FAILED})
    assert harness.worker_once() == 0
    assert harness.record.work_launched == []
    assert PLAN_READY in harness.fake.labels_of(number)  # never laundered


# -- 3. review loop -----------------------------------------------------------


def test_review_loop_revise_resumes_same_pr(harness: Harness):
    number = harness.file_issue("Feature", CRITERIA_BODY)
    branch = branch_for(number)
    harness.worker_behaviors.append(
        behavior_write(
            {"feature.txt": "flawed\n"},
            decisions=[{"what": "took a shortcut", "why": "speed"}],
        )
    )
    harness.worker_once()
    (pr_number,) = harness.fake.open_pr_numbers()
    c1 = harness.remote_sha(branch)

    harness.reviewer_replies.append(revise_reply("1. Replace 'flawed' with 'fixed' in feature.txt"))
    harness.reviewer_once()

    labels = harness.fake.labels_of(pr_number)
    assert f"{ATTEMPT_PREFIX}1" in labels
    assert PR_READY not in labels
    parsed = verdict.parse_comment(harness.fake.comments[pr_number][-1]["body"])
    assert parsed is not None and parsed.verdict == verdict.REVISE
    assert parsed.resume_commit == c1  # designated resume state
    assert parsed.revised_plan
    assert harness.fake.assignees_of(number) == []  # claim stripped
    issue_labels = harness.fake.labels_of(number)
    assert PLAN_READY in issue_labels and IN_PROGRESS not in issue_labels

    harness.worker_behaviors.append(behavior_write({"feature.txt": "fixed\n"}))
    harness.worker_once()

    assert harness.fake.open_pr_numbers() == [pr_number]  # PR count == 1
    assert harness.remote_file(branch, "feature.txt") == "fixed"
    assert PR_READY in harness.fake.labels_of(pr_number)
    resumed_prompt = harness.worker_calls[-1][0]
    assert "Replace 'flawed' with 'fixed'" in resumed_prompt  # revised plan injected


def test_reviewer_designated_reset_and_cherry_pick(harness: Harness):
    number = harness.file_issue("Layered", CRITERIA_BODY)
    branch = branch_for(number)
    harness.worker_behaviors.append(behavior_write({"a.txt": "a\n"}))
    harness.worker_once()
    c1 = harness.remote_sha(branch)

    harness.reviewer_replies.append(revise_reply("1. add b.txt"))
    harness.reviewer_once()
    harness.worker_behaviors.append(behavior_write({"b.txt": "b\n"}))
    harness.worker_once()
    c2 = harness.remote_sha(branch)

    # Round 3: reset the branch to c1, keep c2's change by cherry-pick.
    harness.reviewer_replies.append(revise_reply("1. add fix.txt", resume=c1, cherry_pick=[c2]))
    harness.reviewer_once()
    harness.worker_behaviors.append(behavior_write({"fix.txt": "fix\n"}))
    harness.worker_once()

    history = subprocess.run(
        ["git", "--git-dir", str(harness.remote), "log", "--format=%H %s%n%b", branch],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert c2 not in history.split()  # c2 itself was dropped by the reset
    assert f"cherry picked from commit {c2}" in history
    assert harness.remote_file(branch, "b.txt") == "b"  # ...but its change survived
    assert harness.remote_file(branch, "fix.txt") == "fix"
    assert len(harness.fake.open_pr_numbers()) == 1


# -- 4. escalation on a human-only decision -----------------------------------


def test_escalation_and_human_decision_round(harness: Harness):
    number = harness.file_issue(
        "Contradictory", "## Acceptance criteria\n- flag A is on\n- flag A is off\n"
    )
    harness.worker_behaviors.append(
        behavior_write(
            {"flag.txt": "on\n"},
            decisions=[{"what": "implemented the 'on' reading", "why": "had to pick one"}],
            open_questions=["The acceptance criteria contradict: flag A on vs off."],
        )
    )
    harness.worker_once()
    (pr_number,) = harness.fake.open_pr_numbers()
    assert "criteria contradict" in harness.fake.issues[pr_number]["body"]

    harness.reviewer_replies.append(
        escalate_reply("The Decisions Section flags contradictory criteria; only a human can pick.")
    )
    harness.reviewer_once()
    labels = harness.fake.labels_of(pr_number)
    assert {BLOCKED, NEEDS_HUMAN} <= labels and PR_READY not in labels
    assert (
        f"tree/theozolith/evidence/runs/issue-{number}"
        in (harness.fake.comments[pr_number][-1]["body"])
    )

    # The human answers on the PR and re-queues the issue (human authority).
    harness.human_comment(pr_number, "Decision: flag A must be ON; drop the off criterion.")
    harness.human_requeue(number, pr_number)

    def compliant(prompt: str, cwd: Path) -> None:
        assert "Decision: flag A must be ON" in prompt  # the answer reaches the Run
        behavior_write({"flag.txt": "on, per human decision\n"})(prompt, cwd)

    harness.worker_behaviors.append(compliant)
    harness.worker_once()
    assert harness.fake.open_pr_numbers() == [pr_number]  # same PR completes

    harness.reviewer_replies.append(approve_reply())
    harness.reviewer_once()
    assert {PR_READY, NEEDS_HUMAN} <= harness.fake.labels_of(pr_number)


def test_reviewer_identity_failure_blocks_the_pr_once(harness: Harness):
    """ADR-0045 Reviewer lifecycle: a review session killed by the identity
    gate escalates through the one-strike lane — evidence (identity.json +
    transcript) survives with failure_class identity, the PR turns blocked +
    needs_human in the same pass, and the next poll launches NO new review
    instead of spinning against the same policy forever."""
    harness.file_issue("Feature", CRITERIA_BODY)
    harness.worker_once()
    (pr_number,) = harness.fake.open_pr_numbers()

    harness.reviewer_replies.append(IdentityFailure())
    assert harness.reviewer_once() == 1  # the escalation verdict was applied

    labels = harness.fake.labels_of(pr_number)
    assert {BLOCKED, NEEDS_HUMAN} <= labels and PR_READY not in labels
    comment = harness.fake.comments[pr_number][-1]["body"]
    parsed = verdict.parse_comment(comment)
    assert parsed is not None and parsed.verdict == verdict.ESCALATE
    assert "identity" in comment.lower()

    # Evidence first, and it survives the job dir cleanup: the record
    # carries the Implementer lane's failure-class vocabulary plus the
    # harness's own identity verdict; the transcript rode along.
    paths = harness.evidence_paths()
    (record_path,) = [p for p in paths if p.endswith("-identity.json")]
    record = json.loads(harness.evidence_file(record_path))
    assert record["failure_class"] == "identity"
    assert record["verdict"] is None
    assert record["identity"]["category"] == "substituted"
    assert record["identity"]["checks"] == "passed"
    assert record["identity"]["violation"]
    assert "[substituted]" in record["error"]
    assert any(p.endswith("-identity-transcript.txt") for p in paths)

    # The next poll: the PR is out of the reviewable pool — no session
    # starts (an unscripted review round would assert inside FakeSession).
    assert harness.reviewer_once() == 0
    assert {BLOCKED, NEEDS_HUMAN} <= harness.fake.labels_of(pr_number)


def test_reviewer_non_identity_harness_failure_keeps_current_behavior(harness: Harness):
    """A SessionError WITHOUT the anchored identity marker keeps the existing
    lane: the pass-level error summary, no verdict, no label changes — the
    one-strike identity path never widens into a general escalation path."""
    harness.file_issue("Feature", CRITERIA_BODY)
    harness.worker_once()
    (pr_number,) = harness.fake.open_pr_numbers()

    def broken_session(prompt: str, cwd: Path):
        raise SessionError("run container exited before the agent phase completed")

    harness.reviewer_replies.append(broken_session)
    assert harness.reviewer_once() == 0
    assert any("review pass failed" in line for line in harness.logs)
    labels = harness.fake.labels_of(pr_number)
    assert PR_READY in labels and BLOCKED not in labels  # unchanged behavior


# -- 5. round budget and the final-round rule ---------------------------------


def review_containers(harness: Harness) -> list[str]:
    return [n for n in harness.record.launched if n.startswith("ozolith-review-")]


def test_final_round_revise_escalates_immediately(harness: Harness):
    """A revise on the last budgeted round is an invalid verdict: one strike,
    straight to a human — never a retry, never a Run without a review round."""
    number = harness.file_issue("Forever failing", CRITERIA_BODY)
    for round_number in (1, 2):
        harness.worker_once()
        harness.reviewer_replies.append(revise_reply(f"try again ({round_number})"))
        harness.reviewer_once()
        (pr_number,) = harness.fake.open_pr_numbers()
        assert f"{ATTEMPT_PREFIX}{round_number}" in harness.fake.labels_of(pr_number)

    harness.worker_once()  # the round-3 Run ships on the same PR
    assert PR_READY in harness.fake.labels_of(pr_number)

    calls_before = len(review_containers(harness))
    harness.reviewer_replies.append(revise_reply("one more try"))
    assert harness.reviewer_once() == 1  # the escalation IS the verdict

    # Exactly one model call was spent on the invalid round.
    assert len(review_containers(harness)) == calls_before + 1
    labels = harness.fake.labels_of(pr_number)
    assert {BLOCKED, NEEDS_HUMAN} <= labels and PR_READY not in labels
    assert f"{ATTEMPT_PREFIX}3" not in labels  # invalid verdicts never spend rounds
    assert PLAN_READY not in harness.fake.labels_of(number)  # no re-queue
    last = harness.fake.comments[pr_number][-1]["body"]
    parsed = verdict.parse_comment(last)
    assert parsed is not None and parsed.verdict == verdict.ESCALATE
    assert "final-round" in last  # the raw validation error is in the comment
    assert f"tree/theozolith/evidence/runs/issue-{number}" in last  # bundle link
    assert "-invalid-verdict.json" in last  # evidence path of the bad file
    assert any("-invalid-verdict.json" in p for p in harness.evidence_paths())

    # No re-poll of the same PR: the next pass launches nothing.
    assert harness.reviewer_once() == 0
    assert len(review_containers(harness)) == calls_before + 1
    assert harness.fake.open_pr_numbers() == [pr_number]  # one PR through it all


def test_exhausted_attempt_labels_escalate_deterministically(harness: Harness):
    """A PR already bearing attempt-3 is escalated without a model call —
    the budget check cannot be argued with."""
    harness.file_issue("Exhausted", CRITERIA_BODY)
    harness.worker_once()
    (pr_number,) = harness.fake.open_pr_numbers()
    harness.fake.issues[pr_number]["labels"] += [
        {"name": f"{ATTEMPT_PREFIX}{n}"} for n in (1, 2, 3)
    ]
    containers_before = len(harness.record.work_launched)

    assert harness.reviewer_once() == 1  # no scripted reply: no container ran

    assert len(harness.record.work_launched) == containers_before  # no review container
    labels = harness.fake.labels_of(pr_number)
    assert {BLOCKED, NEEDS_HUMAN} <= labels and PR_READY not in labels
    parsed = verdict.parse_comment(harness.fake.comments[pr_number][-1]["body"])
    assert parsed is not None and parsed.verdict == verdict.ESCALATE
    assert "budget" in parsed.evidence.lower()
    # Cosmetic contract: never "round 4" of a 3-round budget.
    assert "(round 3)" in harness.fake.comments[pr_number][-1]["body"]


# -- 6. statelessness ---------------------------------------------------------


def test_statelessness_between_runs(harness: Harness):
    first = harness.file_issue("First", CRITERIA_BODY)
    second = harness.file_issue("Second", CRITERIA_BODY)

    assert harness.worker_once() == 1
    assert harness.worker_once() == 1

    # No filesystem state survives a Run: the job dirs are gone.
    jobs_root = Path(harness.worker_config.jobs_dir)
    assert not jobs_root.exists() or list(jobs_root.iterdir()) == []
    # Each Run got a fresh checkout in a fresh job dir, in a fresh container.
    (_, cwd_one), (_, cwd_two) = harness.worker_calls
    assert cwd_one != cwd_two
    assert len(set(harness.record.work_launched)) == 2
    assert harness.record.alive == set()  # no ozolith-run-* containers remain
    # The agent-side decisions file never lands on the branch.
    for number in (first, second):
        paths = harness.remote_paths(branch_for(number))
        assert "change.txt" in paths
        assert ".theozolith/decisions.json" not in paths
        assert ".claude/settings.local.json" not in paths
    # Fresh context: the second prompt carries nothing from the first issue.
    assert "First" not in harness.worker_calls[1][0]


def test_container_crash_retries_locally_and_the_retry_ships(harness: Harness):
    """ADR-0016 local retry: the driver keeps the claim and launches one
    full second Run — new run_id, fresh clone, fresh container — with no
    GitHub label churn in between."""
    number = harness.file_issue("Shaky", CRITERIA_BODY)

    def boom(prompt: str, cwd: Path) -> None:
        raise SessionError("run container exited before the agent phase completed")

    harness.worker_behaviors.append(boom)  # first Run crashes; retry succeeds
    assert harness.worker_once() == 2

    (pr_number,) = harness.fake.open_pr_numbers()
    assert PR_READY in harness.fake.labels_of(pr_number)
    # The claim never left this Worker and nothing touched the issue labels.
    assert harness.fake.assignees_of(number) == [WORKER_LOGIN]
    assert harness.fake.labels_of(number) == {IN_PROGRESS, "risk:medium"}
    # The local-retry lane on the channel: the crashed Run's failed event
    # carries its canonical class; the shipping retry's pr-open carries none.
    (crashed,) = (e for e in harness.sink.run_events(number) if e["phase"] == "failed")
    assert crashed["failure_class"] == "harness"
    (shipped,) = (e for e in harness.sink.run_events(number) if e["phase"] == "pr-open")
    assert "failure_class" not in shipped
    assert harness.fake.comments[number] == []  # no marker comments exist
    # Two distinct fresh Runs, two containers, none left behind.
    run_containers = [n for n in harness.record.launched if n.startswith("ozolith-run-")]
    assert len(set(run_containers)) == 2
    assert harness.record.alive == set()
    # Both Runs pushed their own evidence bundle (the failed one included).
    run_dirs = {
        p.split("/")[2]
        for p in harness.evidence_paths()
        if p.startswith(f"runs/issue-{number}/") and p.endswith("/run.json")
    }
    assert len(run_dirs) == 2
    # The events tell the story: claimed, failed, claimed, gate, pr-open.
    assert harness.sink.run_phases(number) == ["claimed", "failed", "claimed", "gate", "pr-open"]
    jobs_root = Path(harness.worker_config.jobs_dir)
    assert not jobs_root.exists() or list(jobs_root.iterdir()) == []


# -- run-outcome classification (ADR-0014, failure lane per ADR-0016) ---------


def _no_op(prompt: str, cwd: Path) -> None:
    """Completed session that neither edits nor records anything."""


def _empty_decisions(prompt: str, cwd: Path) -> None:
    write_decisions(cwd)  # a skeleton file with no content is not reasoning


def _timeout_with_changes(prompt: str, cwd: Path):
    (cwd / "half-done.txt").write_text("partial\n")
    return AgentOutcome(timed_out=True)


def _session_death(prompt: str, cwd: Path):
    return AgentOutcome(session_died=True)


def _assert_escalated(
    harness: Harness, number: int, failure_class: str, reason_fragment: str
) -> None:
    """The local-retry budget is spent: claim released, failed + needs_human,
    both evidence links and each failure's class in the comment."""
    assert harness.fake.assignees_of(number) == []
    labels = harness.fake.labels_of(number)
    assert {FAILED, NEEDS_HUMAN} <= labels
    assert PLAN_READY not in labels and IN_PROGRESS not in labels and BLOCKED not in labels
    (comment,) = harness.fake.comments[number]
    body = comment["body"]
    assert "local-retry budget is spent" in body
    assert failure_class in body and reason_fragment in body
    run_ids = re.findall(r"Run `([^`]+)`", body)
    assert len(run_ids) == 2 and run_ids[0] != run_ids[1]  # two distinct Runs
    for run_id in run_ids:
        assert f"tree/theozolith/evidence/runs/issue-{number}/{run_id}" in body
        record = json.loads(harness.evidence_file(f"runs/issue-{number}/{run_id}/run.json"))
        assert record["phase"] == "failed"
        assert record["failure_class"] == failure_class
        assert reason_fragment in record["reason"]
    # The channel carries the same canonical class as each run.json (ADR-0040
    # amendment): every failed event names its Run's class verbatim, and the
    # escalated event carries the final Run's.
    events_by_phase: dict[str, list[dict]] = {}
    for event in harness.sink.run_events(number):
        events_by_phase.setdefault(event["phase"], []).append(event)
    failed_by_run = {e["run_id"]: e for e in events_by_phase["failed"]}
    assert set(failed_by_run) == set(run_ids)
    for run_id in run_ids:
        record = json.loads(harness.evidence_file(f"runs/issue-{number}/{run_id}/run.json"))
        assert failed_by_run[run_id]["failure_class"] == record["failure_class"]
    (escalated,) = events_by_phase["escalated"]
    assert escalated["failure_class"] == failure_class
    # Non-failure phases never carry the field — a successful Run has no
    # failure class, not an empty one.
    for phase in ("claimed", "gate", "pr-open"):
        for event in events_by_phase.get(phase, []):
            assert "failure_class" not in event


def test_run_outcome_classification_matrix(harness: Harness):
    """commits / no-commits-with-reasoning / no-commits-no-reasoning /
    timeout / session death → normal PR / empty PR / failed / failed /
    failed, with the uniform local-retry budget on every failure class."""
    # commits → normal PR (default behavior writes change.txt).
    with_commits = harness.file_issue("With commits", CRITERIA_BODY)
    assert harness.worker_once() == 1
    (pr_normal,) = harness.fake.open_pr_numbers()
    assert PR_READY in harness.fake.labels_of(pr_normal)
    assert harness.remote_file(branch_for(with_commits), "change.txt")

    # no commits + reasoning → empty PR (asserted in depth in its own test).
    no_change = harness.file_issue("No change needed", CRITERIA_BODY)
    harness.worker_behaviors.append(
        lambda p, cwd: write_decisions(
            cwd, decisions=[{"what": "no change needed", "why": "already handled"}]
        )
    )
    assert harness.worker_once() == 1
    assert len(harness.fake.open_pr_numbers()) == 2
    assert PLAN_READY not in harness.fake.labels_of(no_change)

    # no commits + no reasoning → failed (both no-file and empty-file forms).
    silent = harness.file_issue("Silent", CRITERIA_BODY)
    harness.worker_behaviors.extend([_no_op, _no_op])
    assert harness.worker_once() == 2  # the original and the one local retry
    _assert_escalated(harness, silent, "no-changes", "no no-change reasoning")

    hollow = harness.file_issue("Hollow", CRITERIA_BODY)
    harness.worker_behaviors.extend([_empty_decisions, _empty_decisions])
    harness.worker_once()
    _assert_escalated(harness, hollow, "no-changes", "no no-change reasoning")

    # timeout → failed, even with changes in the tree.
    timed = harness.file_issue("Timed out", CRITERIA_BODY)
    harness.worker_behaviors.extend([_timeout_with_changes, _timeout_with_changes])
    harness.worker_once()
    _assert_escalated(harness, timed, "timeout", "timed out")

    # session death → failed.
    died = harness.file_issue("Died", CRITERIA_BODY)
    harness.worker_behaviors.extend([_session_death, _session_death])
    harness.worker_once()
    _assert_escalated(harness, died, "session-died", "session died")

    # Only the two shipping Runs opened PRs.
    assert len(harness.fake.open_pr_numbers()) == 2


def test_empty_pr_carries_reasoning_and_allow_empty_commit(harness: Harness):
    number = harness.file_issue("Already implemented", CRITERIA_BODY)
    harness.worker_behaviors.append(
        lambda p, cwd: write_decisions(
            cwd,
            decisions=[{"what": "no change needed", "why": "util.py already covers this"}],
        )
    )
    assert harness.worker_once() == 1

    (pr_number,) = harness.fake.open_pr_numbers()
    assert PR_READY in harness.fake.labels_of(pr_number)
    body = harness.fake.issues[pr_number]["body"]
    assert "no code change is needed" in body  # the reasoning, in the PR body
    assert "util.py already covers this" in body
    section = decisions.parse(body)
    assert section is not None and section.decisions  # plus the standard section

    # One driver-synthesized allow-empty commit: new head, identical tree.
    branch = branch_for(number)
    assert harness.remote_sha(branch) != harness.remote_sha("main")
    assert harness.remote_paths(branch) == harness.remote_paths("main")
    history = subprocess.run(
        ["git", "--git-dir", str(harness.remote), "log", "--format=%s", branch],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "no changes required" in history

    # Evidence records the empty-PR outcome; the Reviewer judges it normally.
    paths = harness.evidence_paths()
    run_json = next(
        p for p in paths if p.startswith(f"runs/issue-{number}/") and p.endswith("run.json")
    )
    assert json.loads(harness.evidence_file(run_json))["phase"] == "empty-pr"
    harness.reviewer_replies.append(
        approve_reply(evidence="Justified no-change: util.py already covers the criteria.")
    )
    assert harness.reviewer_once() == 1
    assert NEEDS_HUMAN in harness.fake.labels_of(pr_number)


def test_failed_run_budget_is_one_local_retry_then_escalation(harness: Harness):
    """Acceptance 10: a Run killed mid-flight retries locally exactly once
    (same claim, new run_id, both evidence bundles pushed); a second kill
    releases the claim and applies failed + needs_human with both evidence
    links and each failure's class — mixed classes stay one uniform budget."""
    number = harness.file_issue("Cursed", CRITERIA_BODY)

    harness.worker_behaviors.extend([_no_op, _session_death])
    assert harness.worker_once() == 2  # both Runs happened under ONE claim

    assert harness.fake.assignees_of(number) == []
    labels = harness.fake.labels_of(number)
    assert {FAILED, NEEDS_HUMAN} <= labels
    assert PLAN_READY not in labels and IN_PROGRESS not in labels
    (comment,) = harness.fake.comments[number]  # one escalation, no markers
    assert "no-changes" in comment["body"] and "session-died" in comment["body"]
    # Two distinct fresh Runs: different run ids, different containers.
    run_ids = re.findall(r"Run `([^`]+)`", comment["body"])
    assert len(set(run_ids)) == 2
    assert len({n for n in harness.record.launched if n.startswith("ozolith-run-")}) == 2
    # The first Run completed its session (so the gate ran) but classified
    # failed; the retry died before its gate.
    assert harness.sink.run_phases(number) == [
        "claimed",
        "gate",
        "failed",
        "claimed",
        "failed",
        "escalated",
    ]
    # Mixed classes ride the channel per-Run (ADR-0040 amendment): each
    # failed event names ITS Run's canonical class; escalated carries the
    # final Run's, exactly what its run.json records.
    run_events = harness.sink.run_events(number)
    assert [e["failure_class"] for e in run_events if e["phase"] == "failed"] == [
        "no-changes",
        "session-died",
    ]
    (escalated,) = (e for e in run_events if e["phase"] == "escalated")
    assert escalated["failure_class"] == "session-died"
    final_record = json.loads(
        harness.evidence_file(f"runs/issue-{number}/{escalated['run_id']}/run.json")
    )
    assert final_record["failure_class"] == escalated["failure_class"]

    # failed + needs_human is not claimable: nothing left to do.
    assert harness.worker_once() == 0


def test_evidence_push_failure_is_logged_never_fatal(harness: Harness, monkeypatch):
    def down(*args, **kwargs):
        raise GitError("evidence remote down")

    monkeypatch.setattr("theozolith_worker.evidence.push_bundle", down)

    harness.file_issue("Traceability", CRITERIA_BODY)
    assert harness.worker_once() == 1  # the Run still ships its PR
    (pr_number,) = harness.fake.open_pr_numbers()
    assert PR_READY in harness.fake.labels_of(pr_number)
    worker_failures = [line for line in harness.logs if "evidence push failed" in line]
    assert worker_failures and "evidence remote down" in worker_failures[-1]
    # ADR-0019: the failure is a structured record naming the bundle…
    structured = json.loads(worker_failures[-1].partition("evidence push failed: ")[2])
    assert structured["event"] == "theozolith.evidence-push-failed"
    assert structured["bundle"].startswith("runs/issue-") and structured["attempts"] >= 1
    # …and the retained job dir is parked in the -pending sibling, where the
    # boot sweep retries it and queue-behind never reads it as a live Run.
    jobs = harness.worker_config.jobs_dir
    assert [p for p in jobs.iterdir() if p.is_dir()] == []
    parked = [p.name for p in pending_dir(harness.worker_config).iterdir()]
    assert parked and structured["run_id"] in parked

    harness.reviewer_replies.append(approve_reply())
    assert harness.reviewer_once() == 1  # the verdict still applies
    assert NEEDS_HUMAN in harness.fake.labels_of(pr_number)
    reviewer_failures = [line for line in harness.logs if "evidence push failed" in line]
    assert len(reviewer_failures) > len(worker_failures)


def test_compound_parking_failure_never_blocks_escalation(harness: Harness, monkeypatch):
    """M5: the evidence push fails AND both parking attempts fail — the
    escalation still completes in full, every failure is a structured
    record, and NOTHING remains in the active jobs dir for the Node
    Daemon's queue-behind signal to misread as a live Run."""

    def down(*args, **kwargs):
        raise GitError("evidence remote down")

    monkeypatch.setattr("theozolith_worker.evidence.push_bundle", down)
    parking = pending_dir(harness.worker_config)
    parking.parent.mkdir(parents=True, exist_ok=True)
    parking.write_text("a file squatting on the parking path")  # both park attempts fail

    number = harness.file_issue("Doomed", CRITERIA_BODY)
    harness.worker_behaviors.append(_no_op)
    harness.worker_behaviors.append(_no_op)  # both Runs fail (no commits, no reasoning)
    assert harness.worker_once() == 2

    # Escalation completed in full despite the compound failure.
    assert harness.fake.assignees_of(number) == []
    labels = harness.fake.labels_of(number)
    assert {FAILED, NEEDS_HUMAN} <= labels and IN_PROGRESS not in labels
    (comment,) = harness.fake.comments[number]
    body = comment["body"]
    assert "local-retry budget is spent" in body
    # Honest forensics: no dead links, no false boot-sweep promise.
    assert "[evidence](" not in body
    assert "was lost" in body and "loss accepted" in body

    # The active jobs dir shows nothing in flight: targeted recycle and
    # node-wide update queue behind exactly this signal (daemon side).
    jobs = Path(harness.worker_config.jobs_dir)
    assert not jobs.exists() or [p for p in jobs.iterdir() if p.is_dir()] == []

    # Both Runs produced both structured failure records, in order.
    records = [
        json.loads(line.partition("evidence parking failed: ")[2])
        for line in harness.logs
        if "evidence parking failed: " in line
    ]
    assert [r["event"] for r in records] == [
        "theozolith.evidence-park-failed",
        "theozolith.evidence-lost",
    ] * 2
    for record in records:
        assert record["issue"] == number
        assert record["run_id"] and record["source"] and record["destination"]
        assert record["error"]  # the OSError, verbatim


# -- 7. authority -------------------------------------------------------------

GRADE_LABELS = {
    f"{kind}:{grade}" for kind in ("risk", "deviation") for grade in ("low", "medium", "high")
}
REVIEWER_PR_LABELS = {NEEDS_HUMAN, BLOCKED, "attempt-1", "attempt-2", "attempt-3"} | GRADE_LABELS
WORKER_LOGINS = {WORKER_LOGIN, "ozolith-worker-b"}


def _audit_write(actor: str, method: str, path: str, payload, issues: set[int], prs: set[int]):
    """One GitHub API write against the M2 authority matrix."""
    tail = path.split("/repos/acme/sandbox")[1]
    number = next((int(part) for part in tail.split("/") if part.isdigit()), None)

    if actor in WORKER_LOGINS:
        if tail == "/pulls" and method == "POST":
            return  # open the best-effort PR
        if tail.startswith("/pulls/") and method == "PATCH":
            return  # update the PR body (Decisions Section)
        if tail.endswith("/assignees") and method == "DELETE":
            # Workers never CREATE claim state (ADR-0017: the Control Node
            # writes claims); the escalation release is the only unassign.
            assert payload["assignees"] == [actor], "workers only unassign themselves"
            assert number in issues
            return
        if tail.endswith("/comments") and method == "POST":
            # The only worker comment is the escalation record (ADR-0016).
            assert number in issues, "worker comments only on issues"
            assert "local-retry budget is spent" in payload["body"]
            return
        if tail.endswith("/labels") and method == "POST":
            wanted = set(payload["labels"])
            if number in prs:
                assert wanted <= {PR_READY}, f"worker set {wanted} on a PR"
            else:
                # The spent-budget escalation — never a claim, never a
                # re-queue (plan_ready is the Control Node's and the human's).
                assert wanted <= {FAILED, NEEDS_HUMAN}, f"worker set {wanted} on an issue"
            return
        if method == "DELETE" and "/labels/" in tail:
            label = tail.rsplit("/", 1)[1]
            assert label == IN_PROGRESS and number in issues
            return
        raise AssertionError(f"unexpected worker write: {method} {tail}")

    if actor == REVIEWER_LOGIN:
        if tail.endswith("/comments") and method == "POST":
            assert number in prs, "reviewer comments only on PRs"
            return
        if tail.endswith("/labels") and method == "POST":
            wanted = set(payload["labels"])
            if number in prs:
                assert wanted <= REVIEWER_PR_LABELS, f"reviewer set {wanted} on a PR"
            else:
                assert wanted <= {PLAN_READY}, f"reviewer set {wanted} on an issue"
            return
        if method == "DELETE" and "/labels/" in tail:
            label = tail.rsplit("/", 1)[1]
            if number in prs:
                assert label == PR_READY
            else:
                assert label == IN_PROGRESS
            return
        if tail.endswith("/assignees") and method == "DELETE":
            assert number in issues, "claim strip is an issue-side write"
            return
        raise AssertionError(f"unexpected reviewer write: {method} {tail}")

    raise AssertionError(f"unexpected writer {actor}: {method} {tail}")


def test_authority_matrix_via_write_transcript(harness: Harness):
    issue_number = harness.file_issue("Audited", CRITERIA_BODY)

    # A full revise round then an approve on the first issue.
    harness.worker_once()
    harness.reviewer_replies.append(revise_reply("do it properly"))
    harness.reviewer_once()
    harness.worker_once()
    harness.reviewer_replies.append(approve_reply())
    harness.reviewer_once()

    # A doubly-failed claim (covers the escalation writes: unassign,
    # in_progress off, failed + needs_human on, the escalation comment).
    doomed = harness.file_issue("Doomed", CRITERIA_BODY)
    harness.worker_behaviors.extend([_session_death, _no_op])
    harness.worker_once()

    issues = {issue_number, doomed}
    prs = set(harness.fake.pulls)
    assert harness.fake.write_log, "the transcript must not be empty"
    for actor, method, path, payload in harness.fake.write_log:
        _audit_write(actor, method, path, payload, issues, prs)


# -- 8. credential isolation --------------------------------------------------

HOSTILE_GATE = """\
[steps.test]
run = "env; git config --list; cat .git/config; exit 1"
"""


def test_run_container_holds_no_github_credential(tmp_path):
    """While a Run executes, the run container is token-free: process env,
    filesystem, and git config/remotes. A gate step containing simulated
    hostile code finds nothing to exfiltrate; the PAT exists only in the
    driver's process env."""
    harness = make_harness(tmp_path, gate_toml=HOSTILE_GATE)
    number = harness.file_issue("Hostile probe", CRITERIA_BODY)
    captured: dict[str, str] = {}

    def probing_agent(prompt: str, cwd: Path) -> None:
        captured["git_config"] = (cwd / ".git" / "config").read_text()
        (cwd / "change.txt").write_text("x\n")
        write_decisions(cwd)

    harness.worker_behaviors.append(probing_agent)
    assert harness.worker_once() == 1

    # The container spec: only the model API key crosses the boundary.
    spec = harness.record.specs[0]
    assert set(spec.env) == {"ANTHROPIC_API_KEY"}
    assert "tok-worker-a" not in json.dumps([spec.env, spec.labels, list(spec.mounts)])

    # The mounted checkout mid-Run: tokenless remote, hooks neutralized.
    assert "tok-worker-a" not in captured["git_config"]
    assert "x-access-token" not in captured["git_config"]
    assert "hooksPath" in captured["git_config"]

    # The hostile gate step dumped everything visible from inside; the PR
    # still shipped best-effort with the red step recorded as a finding.
    (pr_number,) = harness.fake.open_pr_numbers()
    assert PR_READY in harness.fake.labels_of(pr_number)
    section = decisions.parse(harness.fake.issues[pr_number]["body"])
    (finding,) = [f for f in section.gate_findings if f.step == "test"]
    assert "ANTHROPIC_API_KEY" in finding.detail  # positive control: env WAS dumped
    assert "tok-worker-a" not in finding.detail
    assert "THEOZOLITH_GIT_TOKEN" not in finding.detail
    assert f"runs/issue-{number}" in " ".join(harness.evidence_paths())


def test_hostile_git_metadata_cannot_reach_the_driver(harness: Harness):
    """Agent-written hooks and config are disarmed before any driver-side
    git command touches the tree (ADR-0014 sanitization)."""
    number = harness.file_issue("Boobytrap", CRITERIA_BODY)
    marker = Path(harness.worker_config.jobs_dir).parent / "hook-escaped.txt"

    def boobytrap(prompt: str, cwd: Path) -> None:
        hooks = cwd / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for name in ("pre-commit", "pre-push"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\necho pwned > {marker}\n")
            hook.chmod(0o755)
        config = cwd / ".git" / "config"
        config.write_text(
            config.read_text()
            + f'[core]\n\tfsmonitor = "touch {marker}"\n'
            + '[credential]\n\thelper = "!echo password=stolen"\n'
        )
        (cwd / "change.txt").write_text("x\n")
        write_decisions(cwd)

    harness.worker_behaviors.append(boobytrap)
    assert harness.worker_once() == 1

    assert not marker.exists(), "agent-written git hook executed in the driver"
    assert PR_READY in harness.fake.labels_of(harness.fake.open_pr_numbers()[0])
    assert harness.remote_file(branch_for(number), "change.txt") == "x"


def test_container_start_failure_emits_an_error_event(harness: Harness):
    """2026-07-21 grilling: an internal failure path (container start) emits
    one theozolith.error per Run with component and error class, alongside
    the normal failed-Run machinery."""
    number = harness.file_issue("Doomed", CRITERIA_BODY)
    real_factory = harness.session_factory

    def broken_factory(spec, job, manifest):
        session = real_factory(spec, job, manifest)
        if manifest.mode == jobdir_module.MODE_DRYRUN:
            return session  # setup dry-run works; only RUN containers break

        def broken_launch():
            raise EngineError("docker run failed: no such image")

        session.launch = broken_launch  # type: ignore[method-assign]
        return session

    harness.session_factory = broken_factory  # type: ignore[method-assign]
    harness.worker_once()

    errors = [e for e in harness.sink.events if e["type"] == "theozolith.error"]
    assert len(errors) == 2  # the Run and its one local retry
    for event in errors:
        assert event["component"] == "implementer-driver"
        assert event["error_class"] == "EngineError"
        assert "docker run failed" in event["message"]
    # The normal ADR-0016 escalation still happened.
    assert FAILED in harness.fake.labels_of(number)
    # Driver-side breakage is the infra class — on both failed events and
    # the escalated event, matching the evidence bundles (ADR-0040).
    run_events = harness.sink.run_events(number)
    assert [e["failure_class"] for e in run_events if e["phase"] == "failed"] == ["infra", "infra"]
    (escalated,) = (e for e in run_events if e["phase"] == "escalated")
    assert escalated["failure_class"] == "infra"


# -- 9. headless sessions (ADR-0019) -------------------------------------------
# The process half (one-shot invocation, exit-is-completion, timeout kill) is
# exercised against real subprocesses in test_harness.py; here we pin the
# driver-side plumbing: whatever the structured output stream contains ends
# up in the evidence bundle (test_happy_path asserts the stream reaches the
# git ref and that run.json carries its token usage).


# -- 10. verdict robustness ---------------------------------------------------


def test_malformed_verdict_escalates_immediately_with_the_raw_error(harness: Harness):
    number = harness.file_issue("Judged", CRITERIA_BODY)
    harness.worker_once()
    (pr_number,) = harness.fake.open_pr_numbers()

    harness.reviewer_replies.append("this is not json {")  # malformed file
    assert harness.reviewer_once() == 1  # one strike: the escalation applies

    assert len(review_containers(harness)) == 1  # exactly one model call
    labels = harness.fake.labels_of(pr_number)
    assert {BLOCKED, NEEDS_HUMAN} <= labels and PR_READY not in labels
    comment = harness.fake.comments[pr_number][-1]["body"]
    assert "not valid JSON" in comment  # the raw validation error
    assert f"tree/theozolith/evidence/runs/issue-{number}" in comment  # bundle link
    assert "-invalid-verdict.json" in comment  # evidence path of the bad file
    # The offending file really is preserved at the cited path.
    invalid_path = next(p for p in harness.evidence_paths() if p.endswith("-invalid-verdict.json"))
    assert harness.evidence_file(invalid_path).startswith("this is not json")
    assert harness.record.alive == set()

    # No re-poll: the PR is out of the Reviewer's queue for good.
    assert harness.reviewer_once() == 0
    assert len(review_containers(harness)) == 1


def test_missing_and_schema_invalid_verdicts_also_escalate(harness: Harness):
    first = harness.file_issue("No file", CRITERIA_BODY)
    harness.worker_once()
    harness.reviewer_replies.append(None)  # the session emitted no file at all
    assert harness.reviewer_once() == 1
    (pr_one,) = [
        n
        for n in harness.fake.open_pr_numbers()
        if branch_for(first) == harness.fake.pulls[n]["head"]
    ]
    comment = harness.fake.comments[pr_one][-1]["body"]
    assert {BLOCKED, NEEDS_HUMAN} <= harness.fake.labels_of(pr_one)
    assert "no verdict file emitted" in comment
    assert "emitted no verdict file" in comment  # and no evidence path is cited

    second = harness.file_issue("Schema fail", CRITERIA_BODY)
    harness.worker_once()
    harness.reviewer_replies.append({"verdict": "approve", "evidence": "ok"})  # grades missing
    assert harness.reviewer_once() == 1
    (pr_two,) = [
        n
        for n in harness.fake.open_pr_numbers()
        if branch_for(second) == harness.fake.pulls[n]["head"]
    ]
    comment = harness.fake.comments[pr_two][-1]["body"]
    assert {BLOCKED, NEEDS_HUMAN} <= harness.fake.labels_of(pr_two)
    assert "deviation" in comment  # the raw schema error names the field
    assert len(review_containers(harness)) == 2  # one call per PR, never more


# -- 11. Control Node availability (ADR-0017) ---------------------------------


class _FirstEmitOnlySink:
    """The Control Node dies right after grant activation: the claimed
    event lands, everything after it does not."""

    def __init__(self):
        self.calls = 0

    def emit(self, event: dict) -> bool:
        self.calls += 1
        return self.calls == 1


class _DeadSink:
    def emit(self, event: dict) -> bool:
        return False


def test_control_node_down_pauses_new_claims_and_reviews(harness: Harness):
    """Acceptance 9, availability half: with the Control Node down no new
    claims occur and review rounds pause — there is no second claim path."""
    number = harness.file_issue("Waiting", CRITERIA_BODY)
    harness.dispatch.paused = True

    assert harness.worker_once() == 0
    assert harness.record.work_launched == []
    assert PLAN_READY in harness.fake.labels_of(number)  # untouched
    assert harness.reviewer_once() == 0
    assert any("paused" in line for line in harness.logs)

    harness.dispatch.paused = False  # the Control Node returns
    assert harness.worker_once() == 1
    assert PR_READY in harness.fake.labels_of(harness.fake.open_pr_numbers()[0])


def test_in_flight_run_finishes_and_publishes_with_control_down(harness: Harness):
    """The grant activated, then the Control Node died mid-Run: the Run
    finishes and publishes (drivers hold their own PATs for all non-claim
    GitHub writes); only telemetry is lost."""
    number = harness.file_issue("Survivor", CRITERIA_BODY)
    assert harness.worker_once(sink=_FirstEmitOnlySink()) == 1

    (pr_number,) = harness.fake.open_pr_numbers()
    assert PR_READY in harness.fake.labels_of(pr_number)
    assert harness.remote_file(branch_for(number), "change.txt")


def test_unacknowledged_claimed_event_abandons_the_grant(harness: Harness):
    """The activation handshake: a grant whose claimed event never lands is
    walked away from — the Control Node releases it after the activation
    window, and running anyway would fork ownership (ADR-0017)."""
    harness.file_issue("Lost handshake", CRITERIA_BODY)
    assert harness.worker_once(sink=_DeadSink()) == 0
    assert harness.record.work_launched == []  # no Run was started
    assert harness.fake.open_pr_numbers() == []
    assert any("abandoning the grant" in line for line in harness.logs)


# -- 12. rate limits ----------------------------------------------------------


def test_secondary_rate_limit_mid_run_pauses_and_resumes(harness: Harness):
    number = harness.file_issue("Throttled", CRITERIA_BODY)
    harness.fake.fail_next(
        lambda method, path: method == "POST" and path.endswith("/pulls"),
        [rate_limited_response(retry_after=2), rate_limited_response(retry_after=3)],
    )

    assert harness.worker_once() == 1

    # The Run paused for the advertised windows, then resumed and completed.
    assert harness.worker_sleeps == [2.0, 3.0]
    (pr_number,) = harness.fake.open_pr_numbers()  # no duplicate PR
    assert PR_READY in harness.fake.labels_of(pr_number)
    assert harness.fake.labels_of(number) == {IN_PROGRESS, "risk:medium"}  # labels intact
    pull_creates = [
        entry
        for entry in harness.fake.write_log
        if entry[1] == "POST" and entry[2].endswith("/pulls")
    ]
    assert len(pull_creates) == 1


def test_stale_branch_from_crashed_run_is_overwritten(harness: Harness):
    """A Run that pushed its branch but died before the PR leaves no
    PR-side state; the next Run overwrites the never-designated branch."""
    number = harness.file_issue("Crash after push", CRITERIA_BODY)
    branch = branch_for(number)

    # Simulate the crashed Run: the branch exists on the remote, no PR does.
    subprocess.run(
        ["git", "clone", "--quiet", f"file://{harness.remote}", "stale"],
        cwd=harness.remote.parent,
        check=True,
    )
    stale = harness.remote.parent / "stale"
    (stale / "junk.txt").write_text("half-finished\n")
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=stale, check=True)
    subprocess.run(["git", "add", "-A"], cwd=stale, check=True)
    subprocess.run(
        ["git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "wip"],
        cwd=stale,
        check=True,
    )
    subprocess.run(["git", "push", "-q", "origin", branch], cwd=stale, check=True)

    assert harness.worker_once() == 1

    (pr_number,) = harness.fake.open_pr_numbers()
    assert PR_READY in harness.fake.labels_of(pr_number)
    paths = harness.remote_paths(branch)
    assert "change.txt" in paths and "junk.txt" not in paths


def test_identity_gate_failures_are_a_distinct_class_with_identity_evidence(harness: Harness):
    """ADR-0045 fail-closed amendment: a harness identity failure (preflight,
    gate, or mid-run drift) classifies as failure_class "identity" — distinct
    from plain harness breakage — and the evidence bundle embeds the
    harness's identity verdict (expected vs effective, category, and the
    confirmation the task prompt was withheld) without any new wire key."""
    from theozolith_worker import jobdir as jd

    number = harness.file_issue("Pinned", CRITERIA_BODY)

    def gate_refuses(prompt: str, cwd: Path) -> None:
        # What the real harness leaves behind when the gate fails: the
        # identity verdict in the job dir, and a PHASE_FAILED status whose
        # error carries the identity marker (surfaced as a SessionError).
        jd.write_identity(
            cwd.parent,
            {
                "expected_model": "claude-sonnet-5",
                "expected_effort": "low",
                "preflight": "failed:policy-widened",
                "detail": "the canary asked for 'claude-haiku-4-5' and executed claude-haiku-4-5",
                "cli_version": "2.1.231 (Claude Code)",
                "canary_model": "claude-haiku-4-5",
                "released": False,
                "violation": "",
                "observed_model": "",
                "observed_effort": "",
            },
        )
        raise SessionError(
            "harness failed: identity: preflight failed [policy-widened] for"
            " model 'claude-sonnet-5', effort low: the effective policy does"
            " not coerce every selection to the baked model (the real task"
            " prompt was not sent)"
        )

    harness.worker_behaviors.extend([gate_refuses, gate_refuses])
    assert harness.worker_once() == 2  # the original and the one local retry
    _assert_escalated(harness, number, "identity", "policy-widened")

    # The evidence bundle carries the identity verdict verbatim — category
    # strings and model/effort names only, never settings or credentials.
    (comment,) = harness.fake.comments[number]
    run_ids = re.findall(r"Run `([^`]+)`", comment["body"])
    for run_id in run_ids:
        record = json.loads(harness.evidence_file(f"runs/issue-{number}/{run_id}/run.json"))
        identity = record["identity"]
        assert identity["expected_model"] == "claude-sonnet-5"
        assert identity["expected_effort"] == "low"
        assert identity["preflight"] == "failed:policy-widened"
        assert identity["released"] is False
        assert "model-key" not in json.dumps(record)  # the credential never leaks
