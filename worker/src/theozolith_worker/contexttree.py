"""The Context Tree: the full issue/PR snapshot as a navigable input/ tree.

ADR-0017 as amended (#52): the dispatch grant carries claim authority only,
never context. Every Run — including local retries — re-reads the complete
issue and PR context from GitHub at checkout, and the driver materializes it
into the job directory as per-item files with a per-surface ``INDEX.md``::

    input/
      issue/
        body.md
        comments/INDEX.md            one line per item: seq, author,
        comments/0001-<author>.md    timestamp, first line
        timeline.md
      pr/                            present only when a PR exists
        body.md
        conversation/INDEX.md + per-item files
        review-comments/INDEX.md + per-item files (path/line in the header)
        reviews/INDEX.md + per-item files
        commits.md
        checks.md

The serializer never relevance-filters, summarizes, or truncates: every
comment surface is present in full — verdict machine blocks included — and
the constraint is agent context, which progressive discovery (read the
indexes, open only what you need) solves, not disk or network. Serialization
is deterministic: stable ordering, stable filenames, no generation
timestamps — the same inputs produce a byte-identical tree, so evidence
bundles diff cleanly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from theozolith_worker import jobdir
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

ISSUE_DIR = "issue"
PR_DIR = "pr"
INDEX_FILE = "INDEX.md"

# Filenames stay portable and stable: anything outside this set (GitHub
# logins are alphanumerics and dashes; bot logins carry brackets) folds to
# a dash.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
# Index lines are navigation, not content: the full text lives in the
# per-item file the line points at.
_INDEX_SNIPPET_CHARS = 120


@dataclass(frozen=True)
class ContextSnapshot:
    """Everything one Run gets to see, fetched fresh at checkout."""

    issue: Issue
    issue_comments: list[Comment] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    pr: PullRequest | None = None
    pr_conversation: list[Comment] = field(default_factory=list)
    pr_review_comments: list[ReviewComment] = field(default_factory=list)
    pr_reviews: list[Review] = field(default_factory=list)
    pr_commits: list[PrCommit] = field(default_factory=list)
    pr_checks: list[CheckRun] = field(default_factory=list)


def fetch_snapshot(
    client: GitHubClient, issue_number: int, pr: PullRequest | None
) -> ContextSnapshot:
    """The full re-read (ADR-0017 as amended): fresh issue — the granted
    payload froze at dispatch time — plus every comment surface, fully
    paginated, unfiltered."""
    issue = client.get_issue(issue_number)
    comments = client.list_comments(issue_number)
    timeline = client.list_timeline(issue_number)
    if pr is None:
        return ContextSnapshot(issue=issue, issue_comments=comments, timeline=timeline)
    return ContextSnapshot(
        issue=issue,
        issue_comments=comments,
        timeline=timeline,
        pr=pr,
        pr_conversation=client.list_comments(pr.number),
        pr_review_comments=client.list_review_comments(pr.number),
        pr_reviews=client.list_reviews(pr.number),
        pr_commits=client.pr_commits(pr.number),
        pr_checks=client.list_check_runs(pr.head_sha),
    )


# -- serialization -------------------------------------------------------------


def _slug(author: str) -> str:
    return _UNSAFE.sub("-", author) or "unknown"


def _first_line(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            return line.strip()[:_INDEX_SNIPPET_CHARS]
    return "(empty)"


def _header(title: str, fields: list[tuple[str, object]]) -> list[str]:
    lines = [f"# {title}", ""]
    lines += [f"- {name}: {value}" for name, value in fields if value not in ("", None)]
    return lines


def _item_file(title: str, fields: list[tuple[str, object]], body: str) -> str:
    return "\n".join([*_header(title, fields), "", body or "(no body)", ""])


def _write(root: Path, relpath: str, text: str) -> None:
    jobdir.atomic_write(root / relpath, text)


def _write_items(root: Path, subdir: str, label: str, items: list[dict]) -> None:
    """One surface: numbered per-item files plus its INDEX.md. ``items`` are
    pre-sorted; each carries title fields, a body, and index-line parts."""
    index = [f"# {label} ({len(items)})", ""]
    if not items:
        index.append("(none)")
    for seq, item in enumerate(items, start=1):
        name = f"{seq:04d}-{_slug(item['author'])}.md"
        _write(root, f"{subdir}/{name}", _item_file(item["title"], item["fields"], item["body"]))
        index.append(
            f"- [{seq:04d}]({name}) {item['author']} {item['timestamp']}"
            f" — {_first_line(item['body'])}"
        )
    _write(root, f"{subdir}/{INDEX_FILE}", "\n".join(index) + "\n")


def _comment_items(comments: list[Comment], kind: str) -> list[dict]:
    ordered = sorted(comments, key=lambda c: (c.created_at, c.id))
    return [
        {
            "author": c.author,
            "timestamp": c.created_at,
            "body": c.body,
            "title": f"{kind} {seq}",
            "fields": [
                ("id", c.id),
                ("author", c.author),
                ("created", c.created_at),
                ("url", c.url),
            ],
        }
        for seq, c in enumerate(ordered, start=1)
    ]


def _review_comment_items(comments: list[ReviewComment]) -> list[dict]:
    ordered = sorted(comments, key=lambda c: (c.created_at, c.id))
    return [
        {
            "author": c.author,
            "timestamp": c.created_at,
            "body": c.body,
            "title": f"Review comment {seq}",
            "fields": [
                ("id", c.id),
                ("author", c.author),
                ("created", c.created_at),
                ("path", c.path),
                ("line", c.line),
                ("url", c.url),
            ],
        }
        for seq, c in enumerate(ordered, start=1)
    ]


def _review_items(reviews: list[Review]) -> list[dict]:
    ordered = sorted(reviews, key=lambda r: (r.submitted_at, r.id))
    return [
        {
            "author": r.author,
            "timestamp": r.submitted_at,
            "body": r.body,
            "title": f"Review {seq}: {r.state}",
            "fields": [
                ("id", r.id),
                ("author", r.author),
                ("state", r.state),
                ("submitted", r.submitted_at),
                ("url", r.url),
            ],
        }
        for seq, r in enumerate(ordered, start=1)
    ]


def _timeline_line(event: dict[str, Any]) -> str:
    kind = event.get("event") or "unknown"
    actor = (event.get("actor") or {}).get("login") or ""
    when = event.get("created_at") or ""
    detail = ""
    if kind in ("labeled", "unlabeled"):
        detail = (event.get("label") or {}).get("name") or ""
    elif kind in ("assigned", "unassigned"):
        detail = (event.get("assignee") or {}).get("login") or ""
    elif kind == "renamed":
        detail = (event.get("rename") or {}).get("to") or ""
    elif kind == "cross-referenced":
        number = ((event.get("source") or {}).get("issue") or {}).get("number")
        detail = f"#{number}" if number else ""
    elif event.get("commit_id"):
        detail = str(event["commit_id"])
    parts = [p for p in (when, kind, f"by {actor}" if actor else "", detail) if p]
    return "- " + " ".join(parts)


def _issue_body_md(issue: Issue) -> str:
    fields: list[tuple[str, object]] = [
        ("labels", ", ".join(sorted(issue.labels))),
        ("assignees", ", ".join(issue.assignees)),
    ]
    return _item_file(f"Issue #{issue.number}: {issue.title}", fields, issue.body)


def _pr_body_md(pr: PullRequest) -> str:
    fields: list[tuple[str, object]] = [
        ("state", pr.state),
        ("head", f"{pr.head_ref} @ {pr.head_sha}"),
        ("base", pr.base_ref),
        ("labels", ", ".join(sorted(pr.labels))),
    ]
    return _item_file(f"PR #{pr.number}: {pr.title}", fields, pr.body)


def _commits_md(commits: list[PrCommit]) -> str:
    lines = [f"# PR commits ({len(commits)})", ""]
    if not commits:
        lines.append("(none)")
    for commit in commits:
        meta = [("author", commit.author), ("authored", commit.authored_at)]
        lines += [f"## {commit.sha}", ""]
        lines += [f"- {name}: {value}" for name, value in meta if value]
        lines += ["", commit.message or "(no message)", ""]
    return "\n".join(lines) + "\n"


def _checks_md(pr: PullRequest, checks: list[CheckRun]) -> str:
    lines = [f"# Checks on {pr.head_sha} ({len(checks)})", ""]
    if not checks:
        lines.append("(none)")
    for check in sorted(checks, key=lambda c: c.name):
        line = f"- {check.name}: {check.status}"
        if check.conclusion:
            line += f" ({check.conclusion})"
        if check.url:
            line += f" — {check.url}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def write_tree(input_dir: Path, snapshot: ContextSnapshot) -> None:
    """Materialize the snapshot under ``input_dir`` (the job dir's input/)."""
    issue_root = input_dir / ISSUE_DIR
    _write(issue_root, "body.md", _issue_body_md(snapshot.issue))
    _write_items(
        issue_root,
        "comments",
        "Issue comments",
        _comment_items(snapshot.issue_comments, "Issue comment"),
    )
    timeline = [f"# Issue timeline ({len(snapshot.timeline)})", ""]
    timeline += [_timeline_line(event) for event in snapshot.timeline] or ["(none)"]
    _write(issue_root, "timeline.md", "\n".join(timeline) + "\n")

    if snapshot.pr is None:
        return
    pr_root = input_dir / PR_DIR
    _write(pr_root, "body.md", _pr_body_md(snapshot.pr))
    _write_items(
        pr_root,
        "conversation",
        "PR conversation",
        _comment_items(snapshot.pr_conversation, "PR comment"),
    )
    _write_items(
        pr_root,
        "review-comments",
        "PR review comments",
        _review_comment_items(snapshot.pr_review_comments),
    )
    _write_items(pr_root, "reviews", "PR reviews", _review_items(snapshot.pr_reviews))
    _write(pr_root, "commits.md", _commits_md(snapshot.pr_commits))
    _write(pr_root, "checks.md", _checks_md(snapshot.pr, snapshot.pr_checks))
