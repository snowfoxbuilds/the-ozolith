"""The nine M2 acceptance criteria, end to end.

Each test drives the real Worker/Reviewer code paths (real GitHubClient, real
git remote); only the GitHub transport and the agent models are substituted.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from conftest import (
    REVIEWER_LOGIN,
    WORKER_LOGIN,
    Harness,
    behavior_write,
)
from fakegithub import rate_limited_response
from theozolith_worker import decisions, verdict
from theozolith_worker.bootstrap.vocabulary import (
    ATTEMPT_PREFIX,
    BLOCKED,
    IN_PROGRESS,
    NEEDS_HUMAN,
    PLAN_READY,
    PR_READY,
)
from theozolith_worker.claim import attempt_claim
from theozolith_worker.githubapi import GitHubClient
from theozolith_worker.prefilter import ControlNodePrefilter, NullPrefilter, make_prefilter
from theozolith_worker.runner import branch_for
from theozolith_worker.worker import run_worker

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

    harness.reviewer_adapter.replies.append(approve_reply())
    assert harness.reviewer_once() == 1

    labels = harness.fake.labels_of(pr_number)
    assert {PR_READY, NEEDS_HUMAN, "deviation:low", "risk:low"} <= labels
    comment = harness.fake.comments[pr_number][-1]["body"]
    assert comment.startswith("### Reviewer verdict: approve")
    assert "acceptance criteria" in comment  # evidence-citing
    assert f"tree/theozolith/evidence/runs/issue-{number}" in comment

    # The bundle link resolves to a real git ref holding this Run's evidence.
    paths = harness.evidence_paths()
    assert any(p.startswith(f"runs/issue-{number}/") and p.endswith("/run.json") for p in paths)
    assert any(f"runs/issue-{number}/reviews/round-1" in p for p in paths)


# -- 2. race ------------------------------------------------------------------


def test_claim_race_exactly_one_survives(harness: Harness):
    number = harness.file_issue("Contested", CRITERIA_BODY)
    harness.fake.register("tok-worker-b", "ozolith-worker-b")
    client_b = GitHubClient(
        harness.fake.repo, "tok-worker-b", transport=harness.fake, sleep=lambda s: None
    )

    # Both Workers polled while the issue was unclaimed (stale snapshots).
    snapshot_a = harness.worker_client.get_issue(number)
    snapshot_b = client_b.get_issue(number)

    assert attempt_claim(harness.worker_client, snapshot_a) is True
    assert attempt_claim(client_b, snapshot_b) is False

    # Exactly one claim survives; the loser left no net side effects.
    assert harness.fake.assignees_of(number) == [WORKER_LOGIN]
    assert harness.fake.labels_of(number) == {IN_PROGRESS, "risk:medium"}


def test_claim_race_simultaneous_assign_earliest_wins(harness: Harness):
    number = harness.file_issue("Contested", CRITERIA_BODY)
    fired: list[bool] = []

    def concurrent_assign(actor: str, method: str, path: str) -> None:
        if method == "POST" and path.endswith("/assignees") and not fired:
            fired.append(True)
            harness.fake.force_assign(number, "ozolith-worker-b")

    harness.fake.after_request = concurrent_assign
    snapshot = harness.worker_client.get_issue(number)

    # Both assigns land before the verify read; the earliest-assigned wins.
    assert attempt_claim(harness.worker_client, snapshot) is True
    assert WORKER_LOGIN in harness.fake.assignees_of(number)
    assert PLAN_READY not in harness.fake.labels_of(number)


def test_claim_race_later_assignee_backs_off(harness: Harness):
    number = harness.file_issue("Contested", CRITERIA_BODY)
    snapshot = harness.worker_client.get_issue(number)
    harness.fake.force_assign(number, "ozolith-worker-b")  # b landed first

    assert attempt_claim(harness.worker_client, snapshot) is False
    assert harness.fake.assignees_of(number) == ["ozolith-worker-b"]
    assert PLAN_READY in harness.fake.labels_of(number)  # loser never dequeues


# -- 3. review loop -----------------------------------------------------------


def test_review_loop_revise_resumes_same_pr(harness: Harness):
    number = harness.file_issue("Feature", CRITERIA_BODY)
    branch = branch_for(number)
    harness.worker_adapter.behaviors.append(
        behavior_write(
            {"feature.txt": "flawed\n"},
            decisions=[{"what": "took a shortcut", "why": "speed"}],
        )
    )
    harness.worker_once()
    (pr_number,) = harness.fake.open_pr_numbers()
    c1 = harness.remote_sha(branch)

    harness.reviewer_adapter.replies.append(
        revise_reply("1. Replace 'flawed' with 'fixed' in feature.txt")
    )
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

    harness.worker_adapter.behaviors.append(behavior_write({"feature.txt": "fixed\n"}))
    harness.worker_once()

    assert harness.fake.open_pr_numbers() == [pr_number]  # PR count == 1
    assert harness.remote_file(branch, "feature.txt") == "fixed"
    assert PR_READY in harness.fake.labels_of(pr_number)
    resumed_prompt = harness.worker_adapter.calls[-1][0]
    assert "Replace 'flawed' with 'fixed'" in resumed_prompt  # revised plan injected


def test_reviewer_designated_reset_and_cherry_pick(harness: Harness):
    number = harness.file_issue("Layered", CRITERIA_BODY)
    branch = branch_for(number)
    harness.worker_adapter.behaviors.append(behavior_write({"a.txt": "a\n"}))
    harness.worker_once()
    c1 = harness.remote_sha(branch)

    harness.reviewer_adapter.replies.append(revise_reply("1. add b.txt"))
    harness.reviewer_once()
    harness.worker_adapter.behaviors.append(behavior_write({"b.txt": "b\n"}))
    harness.worker_once()
    c2 = harness.remote_sha(branch)

    # Round 3: reset the branch to c1, keep c2's change by cherry-pick.
    harness.reviewer_adapter.replies.append(
        revise_reply("1. add fix.txt", resume=c1, cherry_pick=[c2])
    )
    harness.reviewer_once()
    harness.worker_adapter.behaviors.append(behavior_write({"fix.txt": "fix\n"}))
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
    assert harness.fake.open_pr_numbers() == [harness.fake.open_pr_numbers()[0]]


# -- 4. escalation on a human-only decision -----------------------------------


def test_escalation_and_human_decision_round(harness: Harness):
    number = harness.file_issue(
        "Contradictory", "## Acceptance criteria\n- flag A is on\n- flag A is off\n"
    )
    harness.worker_adapter.behaviors.append(
        behavior_write(
            {"flag.txt": "on\n"},
            decisions=[{"what": "implemented the 'on' reading", "why": "had to pick one"}],
            open_questions=["The acceptance criteria contradict: flag A on vs off."],
        )
    )
    harness.worker_once()
    (pr_number,) = harness.fake.open_pr_numbers()
    assert "criteria contradict" in harness.fake.issues[pr_number]["body"]

    harness.reviewer_adapter.replies.append(
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

    harness.worker_adapter.behaviors.append(compliant)
    harness.worker_once()
    assert harness.fake.open_pr_numbers() == [pr_number]  # same PR completes

    harness.reviewer_adapter.replies.append(approve_reply())
    harness.reviewer_once()
    assert {PR_READY, NEEDS_HUMAN} <= harness.fake.labels_of(pr_number)


# -- 5. round budget ----------------------------------------------------------


def test_round_budget_exhaustion_escalates(harness: Harness):
    number = harness.file_issue("Forever failing", CRITERIA_BODY)
    for round_number in (1, 2, 3):
        harness.worker_once()
        harness.reviewer_adapter.replies.append(revise_reply(f"try again ({round_number})"))
        harness.reviewer_once()
        (pr_number,) = harness.fake.open_pr_numbers()
        assert f"{ATTEMPT_PREFIX}{round_number}" in harness.fake.labels_of(pr_number)

    harness.worker_once()  # round 4 ships; the budget is already spent
    assert PR_READY in harness.fake.labels_of(pr_number)
    harness.reviewer_once()  # deterministic escalate: no model reply scripted

    labels = harness.fake.labels_of(pr_number)
    assert {"attempt-1", "attempt-2", "attempt-3", BLOCKED, NEEDS_HUMAN} <= labels
    assert PR_READY not in labels
    last = harness.fake.comments[pr_number][-1]["body"]
    parsed = verdict.parse_comment(last)
    assert parsed is not None and parsed.verdict == verdict.ESCALATE
    assert "budget" in parsed.evidence.lower()
    assert f"tree/theozolith/evidence/runs/issue-{number}" in last
    assert harness.fake.open_pr_numbers() == [pr_number]  # one PR through it all


# -- 6. statelessness ---------------------------------------------------------


def test_statelessness_between_runs(harness: Harness):
    first = harness.file_issue("First", CRITERIA_BODY)
    second = harness.file_issue("Second", CRITERIA_BODY)
    config = dataclasses.replace(harness.worker_config, recycle_runs=2)

    runs = run_worker(
        config,
        harness.worker_client,
        harness.worker_adapter,
        NullPrefilter(),
        log=harness.logs.append,
    )
    assert runs == 2  # recycled on schedule: the loop ended itself

    # No filesystem state survives a Run.
    workdir = Path(config.workdir)
    assert not workdir.exists() or list(workdir.iterdir()) == []
    # Each Run got a fresh checkout in a fresh location.
    (_, cwd_one), (_, cwd_two) = harness.worker_adapter.calls
    assert cwd_one != cwd_two
    # The agent-side decisions file never lands on the branch.
    for number in (first, second):
        paths = harness.remote_paths(branch_for(number))
        assert "change.txt" in paths
        assert ".theozolith/decisions.json" not in paths
    # Fresh context: the second prompt carries nothing from the first issue.
    assert "First" not in harness.worker_adapter.calls[1][0]


def test_crashed_run_leaves_no_pr_side_state(harness: Harness):
    number = harness.file_issue("Doomed", CRITERIA_BODY)

    def boom(prompt: str, cwd: Path) -> None:
        raise RuntimeError("agent infrastructure died")

    harness.worker_adapter.behaviors.append(boom)
    harness.worker_once()

    assert harness.fake.open_pr_numbers() == []  # no PR, no PR labels, no rounds
    # The stale claim remains for the M3 zombie janitor (manual cleanup in M2).
    assert harness.fake.assignees_of(number) == [WORKER_LOGIN]
    assert IN_PROGRESS in harness.fake.labels_of(number)
    assert any("crashed" in line for line in harness.logs)


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
        if tail.endswith("/assignees") and method in ("POST", "DELETE"):
            assert payload["assignees"] == [actor], "workers only (un)assign themselves"
            assert number in issues
            return
        if tail.endswith("/labels") and method == "POST":
            wanted = set(payload["labels"])
            if number in prs:
                assert wanted <= {PR_READY}, f"worker set {wanted} on a PR"
            else:
                assert wanted <= {IN_PROGRESS}, f"worker set {wanted} on an issue"
            return
        if method == "DELETE" and "/labels/" in tail:
            assert tail.endswith(f"/labels/{PLAN_READY}") and number in issues
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
    contested = harness.file_issue("Contested", CRITERIA_BODY)

    # A second worker loses a claim race (covers the back-off writes).
    harness.fake.register("tok-worker-b", "ozolith-worker-b")
    client_b = GitHubClient(
        harness.fake.repo, "tok-worker-b", transport=harness.fake, sleep=lambda s: None
    )
    stale_a = harness.worker_client.get_issue(contested)
    stale_b = client_b.get_issue(contested)
    assert attempt_claim(harness.worker_client, stale_a)
    assert not attempt_claim(client_b, stale_b)

    # A full revise round then an approve on the first issue.
    harness.worker_once()
    harness.reviewer_adapter.replies.append(revise_reply("do it properly"))
    harness.reviewer_once()
    harness.worker_once()
    harness.reviewer_adapter.replies.append(approve_reply())
    harness.reviewer_once()

    issues = {issue_number, contested}
    prs = set(harness.fake.pulls)
    assert harness.fake.write_log, "the transcript must not be empty"
    for actor, method, path, payload in harness.fake.write_log:
        _audit_write(actor, method, path, payload, issues, prs)


# -- 8. degraded mode ---------------------------------------------------------


def test_degraded_mode_without_control_node(harness: Harness):
    """No Control Node configured or reachable: everything still works
    (GitHub-only operation is the permanent degraded mode, ADR-0002)."""
    assert isinstance(make_prefilter(None), NullPrefilter)

    number = harness.file_issue("Degraded", CRITERIA_BODY)
    unreachable = ControlNodePrefilter("http://127.0.0.1:1", timeout=0.2)
    assert harness.worker_once(prefilter=unreachable) == 1

    (pr_number,) = harness.fake.open_pr_numbers()
    assert PR_READY in harness.fake.labels_of(pr_number)
    harness.reviewer_adapter.replies.append(approve_reply())
    harness.reviewer_once()
    assert NEEDS_HUMAN in harness.fake.labels_of(pr_number)
    assert harness.remote_file(branch_for(number), "change.txt")


# -- 9. rate limits -----------------------------------------------------------


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
