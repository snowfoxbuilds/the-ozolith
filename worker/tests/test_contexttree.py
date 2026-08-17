"""The Context Tree (#52, ADR-0017 as amended).

Serializer determinism, full pagination, the two prompt shapes, and the
re-read doctrine: the dispatch grant is claim authority only — every Run,
including local retries, sees the complete issue/PR context fetched fresh
at checkout as a navigable ``input/`` tree.
"""

from __future__ import annotations

from pathlib import Path

from conftest import Harness, behavior_write
from fakegithub import FakeGitHub
from test_acceptance import CRITERIA_BODY, revise_reply
from theozolith_worker import contexttree
from theozolith_worker.githubapi import (
    CheckRun,
    Comment,
    GitHubClient,
    Issue,
    PrCommit,
    PullRequest,
    Review,
    ReviewComment,
)
from theozolith_worker.jobdir import AgentOutcome
from theozolith_worker.runner import branch_for


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _full_snapshot() -> contexttree.ContextSnapshot:
    issue = Issue(
        number=7,
        title="Add feature",
        body="Do the thing.\n",
        labels={"in_progress", "risk:low"},
        assignees=["worker"],
        is_pr=False,
    )
    pr = PullRequest(
        number=9,
        title="#7: Add feature",
        body="Closes #7.",
        head_ref="ozolith/issue-7",
        head_sha="headsha1",
        base_ref="main",
        labels={"pr_ready"},
        state="open",
    )
    return contexttree.ContextSnapshot(
        issue=issue,
        # Deliberately out of order: serialization must sort, not trust.
        issue_comments=[
            Comment(2, "github-actions[bot]", "bot says\nmore", "2026-08-16T01:00:00Z", "u2"),
            Comment(1, "sean", "first note", "2026-08-16T00:00:00Z", "u1"),
        ],
        timeline=[
            {
                "event": "labeled",
                "actor": {"login": "sean"},
                "label": {"name": "plan_ready"},
                "created_at": "2026-08-16T00:00:01Z",
            },
            {
                "event": "assigned",
                "actor": {"login": "control"},
                "assignee": {"login": "worker"},
                "created_at": "2026-08-16T00:00:02Z",
            },
        ],
        pr=pr,
        pr_conversation=[Comment(3, "reviewer", "### Reviewer verdict", "t3", "u3")],
        pr_review_comments=[
            ReviewComment(4, "reviewer", "rename this", "t4", "src/app.py", 42, "u4")
        ],
        pr_reviews=[Review(5, "reviewer", "CHANGES_REQUESTED", "see inline", "t5", "u5")],
        pr_commits=[PrCommit("abc123", "worker", "t6", "Run 1 for #7\n\nfull message body")],
        pr_checks=[CheckRun("ci/test", "completed", "success", "u7")],
    )


def test_write_tree_is_deterministic_and_complete(tmp_path):
    """Same snapshot → byte-identical tree; every surface present in full."""
    snapshot = _full_snapshot()
    first, second = tmp_path / "one", tmp_path / "two"
    contexttree.write_tree(first, snapshot)
    contexttree.write_tree(second, snapshot)
    assert _tree_bytes(first) == _tree_bytes(second)

    files = _tree_bytes(first)
    assert set(files) == {
        "issue/body.md",
        "issue/comments/INDEX.md",
        "issue/comments/0001-sean.md",
        "issue/comments/0002-github-actions-bot-.md",  # slug: stable, portable
        "issue/timeline.md",
        "pr/body.md",
        "pr/conversation/INDEX.md",
        "pr/conversation/0001-reviewer.md",
        "pr/review-comments/INDEX.md",
        "pr/review-comments/0001-reviewer.md",
        "pr/reviews/INDEX.md",
        "pr/reviews/0001-reviewer.md",
        "pr/commits.md",
        "pr/checks.md",
    }

    body = files["issue/body.md"].decode()
    assert "# Issue #7: Add feature" in body and "Do the thing." in body
    assert "labels: in_progress, risk:low" in body

    # Ordering by timestamp, not list position; the index carries seq,
    # author, timestamp, and the first line, and links the item file.
    index = files["issue/comments/INDEX.md"].decode()
    assert "Issue comments (2)" in index
    assert index.index("0001-sean.md") < index.index("0002-github-actions-bot-.md")
    assert "- [0001](0001-sean.md) sean 2026-08-16T00:00:00Z — first note" in index
    assert "bot says" in index  # first line only, full text in the item file
    item = files["issue/comments/0002-github-actions-bot-.md"].decode()
    assert "- id: 2" in item and "- url: u2" in item and "bot says\nmore" in item

    timeline = files["issue/timeline.md"].decode()
    assert "labeled by sean plan_ready" in timeline
    assert "assigned by control worker" in timeline

    # Review comments carry their file/line anchor in the header.
    inline = files["pr/review-comments/0001-reviewer.md"].decode()
    assert "- path: src/app.py" in inline and "- line: 42" in inline

    review = files["pr/reviews/0001-reviewer.md"].decode()
    assert "CHANGES_REQUESTED" in review and "see inline" in review

    commits = files["pr/commits.md"].decode()
    assert "## abc123" in commits and "full message body" in commits  # never truncated

    checks = files["pr/checks.md"].decode()
    assert "# Checks on headsha1 (1)" in checks
    assert "- ci/test: completed (success)" in checks


def test_round_one_tree_has_no_pr_directory(tmp_path):
    snapshot = contexttree.ContextSnapshot(issue=_full_snapshot().issue)
    contexttree.write_tree(tmp_path, snapshot)
    assert not (tmp_path / "pr").exists()
    assert (
        (tmp_path / "issue" / "comments" / "INDEX.md")
        .read_text()
        .startswith("# Issue comments (0)")
    )


def test_fetch_paginates_every_surface_without_caps():
    """>100 items on a surface spans pages; nothing is dropped (#52: no
    caps at any size)."""
    fake = FakeGitHub()
    fake.register("tok", "worker")
    client = GitHubClient(fake.repo, "tok", transport=fake, sleep=lambda s: None)
    number = fake.create_issue("Big thread", "body", {"plan_ready"})
    for i in range(150):
        fake.comments[number].append(
            {
                "id": 1000 + i,
                "user": {"login": "sean"},
                "body": f"comment {i}",
                "created_at": fake._timestamp(),
                "html_url": "",
            }
        )
    snapshot = contexttree.fetch_snapshot(client, number, None)
    assert len(snapshot.issue_comments) == 150
    assert [c.body for c in snapshot.issue_comments][:2] == ["comment 0", "comment 1"]

    # The check-runs endpoint wraps its items in an object; pagination must
    # still walk every page under the wrapper key.
    for i in range(120):
        fake.add_check_run("sha1", f"check-{i:03d}", "completed", "success")
    runs = client.list_check_runs("sha1")
    assert len(runs) == 120


def test_round_one_run_sees_preexisting_issue_comments(harness: Harness):
    """The round-1 comment-blindness regression (#41 Problem 2): comments
    that exist before the first Run are in the tree — and only there."""
    number = harness.file_issue("Feature", CRITERIA_BODY)
    harness.human_comment(number, "Constraint: keep it backwards compatible.")

    seen: dict = {}

    def capture(prompt: str, cwd: Path) -> None:
        seen["prompt"] = prompt
        comments_dir = cwd.parent / "input" / "issue" / "comments"
        seen["comments"] = [p.read_text() for p in sorted(comments_dir.glob("0*.md"))]
        seen["issue_json"] = (cwd.parent / "input" / "issue.json").is_file()
        behavior_write({"change.txt": "x\n"})(prompt, cwd)

    harness.worker_behaviors.append(capture)
    assert harness.worker_once() == 1
    assert any("keep it backwards compatible" in c for c in seen["comments"])
    # The prompt teaches navigation instead of injecting content.
    assert "keep it backwards compatible" not in seen["prompt"]
    assert "/job/input/issue/comments/INDEX.md" in seen["prompt"]
    assert seen["issue_json"]  # boot-sweep metadata survives (#52 ruling)


def test_grant_payload_is_claim_authority_not_context(harness: Harness):
    """ADR-0017 as amended: a stale grant snapshot is invisible to the Run —
    the issue body and title come from the fresh checkout re-read."""
    number = harness.file_issue("Fresh title", CRITERIA_BODY)
    real = harness.dispatch.request_work

    def stale_grant(worker, node, login):
        granted = real(worker, node, login)
        if granted is not None:
            granted = {**granted, "title": "stale title", "body": "STALE GRANT BODY"}
        return granted

    harness.dispatch.request_work = stale_grant
    seen: dict = {}

    def capture(prompt: str, cwd: Path) -> None:
        seen["prompt"] = prompt
        behavior_write({"change.txt": "x\n"})(prompt, cwd)

    harness.worker_behaviors.append(capture)
    assert harness.worker_once() == 1
    assert "STALE GRANT BODY" not in seen["prompt"]
    assert "change.txt exists on the branch" in seen["prompt"]  # the fresh body
    (pr_number,) = harness.fake.open_pr_numbers()
    assert harness.fake.issues[pr_number]["title"] == f"#{number}: Fresh title"


def test_prompt_shapes_and_resume_tree(harness: Harness):
    """Round 1 vs resume: rules + navigation guide in both shapes; the
    revised plan and resume commit only on resume; discussion injection is
    gone; the resume tree carries the PR surfaces, verdicts unfiltered."""
    number = harness.file_issue("Feature", CRITERIA_BODY)
    branch = branch_for(number)
    harness.worker_behaviors.append(behavior_write({"feature.txt": "flawed\n"}))
    harness.worker_once()
    round1 = harness.worker_calls[-1][0]
    assert "## Rules" in round1 and "## Context tree" in round1
    assert "## Revised plan" not in round1
    (pr_number,) = harness.fake.open_pr_numbers()
    c1 = harness.remote_sha(branch)

    harness.reviewer_replies.append(revise_reply("1. Replace 'flawed' with 'fixed'"))
    harness.reviewer_once()
    harness.human_comment(pr_number, "Also: please keep the filename.")

    seen: dict = {}

    def capture(prompt: str, cwd: Path) -> None:
        pr_dir = cwd.parent / "input" / "pr"
        seen["conversation"] = [
            p.read_text() for p in sorted((pr_dir / "conversation").glob("0*.md"))
        ]
        seen["files"] = {str(p.relative_to(pr_dir)) for p in pr_dir.rglob("*") if p.is_file()}
        seen["commits"] = (pr_dir / "commits.md").read_text()
        behavior_write({"feature.txt": "fixed\n"})(prompt, cwd)

    harness.worker_behaviors.append(capture)
    harness.worker_once()

    resumed = harness.worker_calls[-1][0]
    assert "## Rules" in resumed and "## Context tree" in resumed
    assert "## Revised plan (round 2)" in resumed
    assert "Replace 'flawed' with 'fixed'" in resumed  # the plan, verbatim
    assert f"resumed from commit `{c1}`" in resumed
    # DISCUSSION_CONTEXT is dead: the human comment lives in the tree only.
    assert "Review discussion since the last verdict" not in resumed
    assert "please keep the filename" not in resumed
    assert any("please keep the filename" in c for c in seen["conversation"])
    # Verdict comments appear unfiltered — machine block included.
    assert any("<!-- theozolith:verdict" in c for c in seen["conversation"])
    assert {"body.md", "commits.md", "checks.md"} <= seen["files"]
    assert {"conversation/INDEX.md", "review-comments/INDEX.md", "reviews/INDEX.md"} <= seen[
        "files"
    ]
    assert c1 in seen["commits"]  # live PR commits, from the real remote


def test_local_retry_refetches_context(harness: Harness):
    """A local retry is a full fresh Run (ADR-0016 + #52): its tree is
    re-fetched, never the failed Run's snapshot."""
    number = harness.file_issue("Feature", CRITERIA_BODY)
    trees: list[list[str]] = []

    def read_comments(cwd: Path) -> list[str]:
        comments_dir = cwd.parent / "input" / "issue" / "comments"
        return [p.read_text() for p in sorted(comments_dir.glob("0*.md"))]

    def dying(prompt: str, cwd: Path) -> AgentOutcome:
        trees.append(read_comments(cwd))
        # Arrives after Run 1's checkout fetch: only the retry can see it.
        harness.human_comment(number, "landed between the runs")
        return AgentOutcome(session_died=True, exit_code=1)

    def succeeding(prompt: str, cwd: Path) -> None:
        trees.append(read_comments(cwd))
        behavior_write({"change.txt": "x\n"})(prompt, cwd)

    harness.worker_behaviors += [dying, succeeding]
    harness.worker_once()
    first, second = trees
    assert not any("landed between the runs" in c for c in first)
    assert any("landed between the runs" in c for c in second)
