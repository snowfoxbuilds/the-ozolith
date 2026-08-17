"""The Context Tree: the issue/PR snapshot as a navigable input/ tree.

ADR-0017 as amended (#52): the dispatch grant carries claim authority only,
never context. Every Run — including local retries — re-reads the issue and
PR context from GitHub at checkout, and the driver materializes it into the
job directory as per-item files with a per-surface ``INDEX.md``::

    input/
      issue/
        body.md
        comments/INDEX.md            one line per item: seq, author,
        comments/0001-<author>.md    timestamp, first line
        timeline.md
      pr/                            present only when a PR exists
        body.md
        conversation/INDEX.md + per-item files
        review-comments/INDEX.md + per-item files (full anchors in the header)
        reviews/INDEX.md + per-item files
        commits.md                   every PR commit, from the trusted checkout
        checks.md                    check runs AND legacy commit statuses

Authority boundary (#52 amendment — the one deliberate exception to "never
relevance-filter"): the tree contains comments, reviews, and review payloads
ONLY from authors GitHub reports as ``OWNER`` or ``MEMBER``. Every other
association — COLLABORATOR, CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, FIRST_TIMER,
NONE, or missing/unknown — is filtered before serialization and leaves no
worker-visible trace: no body, snippet, filename, index entry, or count. The
filter is applied at fetch time (so the driver's machine-verdict discovery
obeys the same boundary) AND again at serialization (so no producer of a
snapshot can leak past it).

Within that boundary the tree is lossless and uncapped: authorized content
is never summarized or truncated — verdict machine blocks included — and PR
commits are enumerated from the driver's own git checkout (the REST endpoint
caps at 250). The constraint is agent context, which progressive discovery
(read the indexes, open only what you need) solves, not disk or network.
Serialization is deterministic: stable ordering, stable filenames, no
generation timestamps — the same inputs produce a byte-identical tree, so
evidence bundles diff cleanly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from theozolith_worker import gitops, jobdir
from theozolith_worker.githubapi import (
    CheckRun,
    Comment,
    CommitStatus,
    GitHubClient,
    Issue,
    PullRequest,
    Review,
    ReviewComment,
)

ISSUE_DIR = "issue"
PR_DIR = "pr"
INDEX_FILE = "INDEX.md"

# The authority boundary (#52 amendment): the only author associations whose
# comments, reviews, and review payloads a Run may see. Deliberately narrow —
# COLLABORATOR and below are excluded, and an absent/unknown association is
# never authorized.
AUTHORIZED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})

# Deterministic stand-in when GitHub reports no user on an AUTHORIZED payload
# (deleted account): the content is kept, the byline never crashes the Run.
UNKNOWN_AUTHOR = "unknown"

# Filenames stay portable and stable: anything outside this set (GitHub
# logins are alphanumerics and dashes; bot logins carry brackets) folds to
# a dash.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
# Index lines are navigation, not content: the full text lives in the
# per-item file the line points at.
_INDEX_SNIPPET_CHARS = 120


@dataclass(frozen=True)
class PrCommit:
    """One PR commit, enumerated from the driver's trusted git checkout
    (never the 250-capped REST endpoint, #52 amendment)."""

    sha: str
    author: str
    authored_at: str
    message: str


@dataclass(frozen=True)
class ContextSnapshot:
    """Everything one Run gets to see, fetched fresh at checkout. Comment
    surfaces are authority-filtered at construction (fetch_snapshot);
    ``timeline`` stays raw and is filtered at serialization."""

    issue: Issue
    issue_comments: list[Comment] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    pr: PullRequest | None = None
    pr_conversation: list[Comment] = field(default_factory=list)
    pr_review_comments: list[ReviewComment] = field(default_factory=list)
    pr_reviews: list[Review] = field(default_factory=list)
    pr_commits: list[PrCommit] = field(default_factory=list)
    pr_checks: list[CheckRun] = field(default_factory=list)
    pr_statuses: list[CommitStatus] = field(default_factory=list)


def authorized(items: Iterable) -> list:
    """The authority filter: items (Comment/ReviewComment/Review) whose
    author GitHub reports as OWNER or MEMBER. Everything else — including a
    missing or unknown association — is dropped."""
    return [item for item in items if item.author_association in AUTHORIZED_ASSOCIATIONS]


def fetch_snapshot(
    client: GitHubClient, issue_number: int, pr: PullRequest | None
) -> ContextSnapshot:
    """The full re-read (ADR-0017 as amended): fresh issue — the granted
    payload froze at dispatch time — plus every comment surface, fully
    paginated, authority-filtered here so both the serializer and the
    driver's machine-verdict discovery see only OWNER/MEMBER content.

    ``pr_commits`` is NOT fetched here: the REST endpoint caps at 250, so
    the runner enumerates the range from its trusted checkout
    (:func:`git_pr_commits`) and attaches it before serialization."""
    issue = client.get_issue(issue_number)
    comments = authorized(client.list_comments(issue_number))
    timeline = client.list_timeline(issue_number)
    if pr is None:
        return ContextSnapshot(issue=issue, issue_comments=comments, timeline=timeline)
    return ContextSnapshot(
        issue=issue,
        issue_comments=comments,
        timeline=timeline,
        pr=pr,
        pr_conversation=authorized(client.list_comments(pr.number)),
        pr_review_comments=authorized(client.list_review_comments(pr.number)),
        pr_reviews=authorized(client.list_reviews(pr.number)),
        pr_checks=client.list_check_runs(pr.head_sha),
        pr_statuses=client.list_statuses(pr.head_sha),
    )


def git_pr_commits(workdir: Path, base_ref: str, head: str) -> list[PrCommit]:
    """Every commit in ``base_ref..head``, chronological, from the trusted
    driver-side checkout — uncapped where /pulls/{n}/commits stops at 250
    (#52 amendment). Callers pass the fetched PR head BEFORE any
    reviewer-designated reset, so the recorded snapshot is the PR as it
    stood at checkout regardless of where the branch is reset afterwards.

    Records are NUL-separated (``-z``; git forbids NUL in commit messages)
    with 0x01 field separators, so complete multi-line messages round-trip.
    """
    out = gitops.git(
        ["log", "--reverse", "-z", "--format=%H%x01%an%x01%aI%x01%B", f"{base_ref}..{head}"],
        workdir,
    )
    commits: list[PrCommit] = []
    for record in out.split("\0"):
        record = record.strip("\n")
        if not record:
            continue
        sha, author, authored_at, message = record.split("\x01", 3)
        commits.append(
            PrCommit(sha=sha, author=author, authored_at=authored_at, message=message.rstrip("\n"))
        )
    return commits


# -- serialization -------------------------------------------------------------


def _slug(author: str) -> str:
    return _UNSAFE.sub("-", author) or UNKNOWN_AUTHOR


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
            "author": c.author or UNKNOWN_AUTHOR,
            "timestamp": c.created_at,
            "body": c.body,
            "title": f"{kind} {seq}",
            "fields": [
                ("id", c.id),
                ("author", c.author or UNKNOWN_AUTHOR),
                ("association", c.author_association),
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
            "author": c.author or UNKNOWN_AUTHOR,
            "timestamp": c.created_at,
            "body": c.body,
            "title": f"Review comment {seq}",
            # The complete anchor set (#52): an outdated comment has no
            # current line but keeps its original one; multiline comments
            # carry start-line ranges; replies name their thread.
            "fields": [
                ("id", c.id),
                ("author", c.author or UNKNOWN_AUTHOR),
                ("association", c.author_association),
                ("created", c.created_at),
                ("path", c.path),
                ("line", c.line),
                ("side", c.side),
                ("start-line", c.start_line),
                ("start-side", c.start_side),
                ("original-line", c.original_line),
                ("original-start-line", c.original_start_line),
                ("commit", c.commit_id),
                ("original-commit", c.original_commit_id),
                ("in-reply-to", c.in_reply_to_id),
                ("review", c.review_id),
                ("url", c.url),
            ],
        }
        for seq, c in enumerate(ordered, start=1)
    ]


def _review_items(reviews: list[Review]) -> list[dict]:
    ordered = sorted(reviews, key=lambda r: (r.submitted_at, r.id))
    return [
        {
            "author": r.author or UNKNOWN_AUTHOR,
            "timestamp": r.submitted_at,
            "body": r.body,
            "title": f"Review {seq}: {r.state}",
            "fields": [
                ("id", r.id),
                ("author", r.author or UNKNOWN_AUTHOR),
                ("association", r.author_association),
                ("state", r.state),
                ("submitted", r.submitted_at),
                ("commit", r.commit_id),
                ("url", r.url),
            ],
        }
        for seq, r in enumerate(ordered, start=1)
    ]


# -- the timeline --------------------------------------------------------------

# Timeline events that embed third-party comment/review content: the
# authority boundary applies to them exactly as to the primary surfaces.
# Authorized entries reference their canonical per-item files (or, for
# commit comments, which have no canonical surface, carry the body inline);
# unauthorized entries vanish entirely — no line, no count.
_COMMENT_EVENTS = {"commented", "reviewed", "line-commented", "commit-commented"}


def _payload_authorized(payload: dict[str, Any]) -> bool:
    return str(payload.get("author_association") or "") in AUTHORIZED_ASSOCIATIONS


def _payload_author(payload: dict[str, Any]) -> str:
    for key in ("user", "actor"):
        value = payload.get(key)
        if isinstance(value, dict) and value.get("login"):
            return str(value["login"])
    return UNKNOWN_AUTHOR


# Per-kind detail extractors for non-comment events: complete, deterministic
# renderings of what each known kind carries (both sides of renames, full
# cross-reference identity, ...). A kind mapped to an empty extractor carries
# nothing beyond actor/timestamp/commit; an UNMAPPED kind fails closed — its
# payload is withheld, because an unrecognized future event could embed
# comment content the authority filter has not classified.
def _cross_reference_details(event: dict[str, Any]) -> list[tuple[str, object]]:
    source = (event.get("source") or {}).get("issue") or {}
    return [
        ("kind", "pull request" if "pull_request" in source else "issue"),
        ("repository", (source.get("repository") or {}).get("full_name")),
        ("number", source.get("number")),
        ("url", source.get("html_url")),
    ]


_EVENT_DETAILS: dict[str, Any] = {
    "labeled": lambda e: [("label", (e.get("label") or {}).get("name"))],
    "unlabeled": lambda e: [("label", (e.get("label") or {}).get("name"))],
    "assigned": lambda e: [("assignee", (e.get("assignee") or {}).get("login"))],
    "unassigned": lambda e: [("assignee", (e.get("assignee") or {}).get("login"))],
    "milestoned": lambda e: [("milestone", (e.get("milestone") or {}).get("title"))],
    "demilestoned": lambda e: [("milestone", (e.get("milestone") or {}).get("title"))],
    "renamed": lambda e: [
        ("from", (e.get("rename") or {}).get("from")),
        ("to", (e.get("rename") or {}).get("to")),
    ],
    "review_requested": lambda e: [
        ("reviewer", (e.get("requested_reviewer") or {}).get("login")),
        ("team", (e.get("requested_team") or {}).get("name")),
    ],
    "review_request_removed": lambda e: [
        ("reviewer", (e.get("requested_reviewer") or {}).get("login")),
        ("team", (e.get("requested_team") or {}).get("name")),
    ],
    "cross-referenced": _cross_reference_details,
    "locked": lambda e: [("reason", e.get("lock_reason"))],
    "closed": lambda e: [("state_reason", e.get("state_reason"))],
    "committed": lambda e: [("sha", e.get("sha"))],
    **dict.fromkeys(
        (
            "reopened",
            "merged",
            "referenced",
            "head_ref_deleted",
            "head_ref_restored",
            "head_ref_force_pushed",
            "base_ref_changed",
            "ready_for_review",
            "convert_to_draft",
            "unlocked",
            "pinned",
            "unpinned",
            "connected",
            "disconnected",
            "mentioned",
            "subscribed",
            "unsubscribed",
            "marked_as_duplicate",
            "unmarked_as_duplicate",
            "transferred",
            "converted_to_discussion",
            "converted_note_to_issue",
            "added_to_project",
            "removed_from_project",
            "moved_columns_in_project",
            "comment_deleted",
            "automatic_base_change_succeeded",
            "automatic_base_change_failed",
            "deployed",
            "user_blocked",
        ),
        lambda e: [],
    ),
}


def _timeline_comment_entry(kind: str, event: dict[str, Any]) -> list[str] | None:
    """A comment-bearing timeline event: authorized ones point at their
    canonical files (bodies live there, never duplicated here); commit
    comments, which have no canonical surface, carry authorized bodies
    inline. Unauthorized content yields None — the event never renders."""
    when = event.get("created_at") or event.get("submitted_at") or ""
    if kind == "commented":
        if not _payload_authorized(event):
            return None
        head = " ".join(p for p in (when, "commented by", _payload_author(event)) if p)
        return [f"- {head} — issue comment id {event.get('id')}: full text in issue/comments/"]
    if kind == "reviewed":
        if not _payload_authorized(event):
            return None
        state = str(event.get("state") or "").upper()
        head = " ".join(p for p in (when, "reviewed by", _payload_author(event)) if p)
        detail = f"review id {event.get('id')}"
        if state:
            detail = f"{state}, {detail}"
        return [f"- {head} — {detail}: full text in pr/reviews/"]
    if kind == "line-commented":
        comments = [c for c in event.get("comments") or [] if _payload_authorized(c)]
        if not comments:
            return None
        ids = ", ".join(str(c.get("id")) for c in comments)
        return [f"- line-commented — review comment ids {ids}: full text in pr/review-comments/"]
    # commit-commented: no canonical surface exists, so authorized bodies
    # ride inline (indented under the entry).
    comments = [c for c in event.get("comments") or [] if _payload_authorized(c)]
    if not comments:
        return None
    lines: list[str] = []
    for c in comments:
        head = " ".join(
            p
            for p in (
                c.get("created_at") or "",
                "commit comment by",
                _payload_author(c),
                f"on {c.get('commit_id')}" if c.get("commit_id") else "",
            )
            if p
        )
        lines.append(f"- {head} (id {c.get('id')}):")
        lines += ["  " + line for line in (c.get("body") or "(no body)").splitlines()]
    return lines


def _timeline_entry(event: dict[str, Any]) -> list[str] | None:
    kind = str(event.get("event") or "unknown")
    if kind in _COMMENT_EVENTS:
        return _timeline_comment_entry(kind, event)
    actor = (event.get("actor") or {}).get("login") or ""
    when = event.get("created_at") or ""
    head = "- " + " ".join(p for p in (when, kind, f"by {actor}" if actor else "") if p)
    maker = _EVENT_DETAILS.get(kind)
    if maker is None:
        details = [("note", "unrecognized event type; payload withheld (authority unknown)")]
    else:
        details = list(maker(event))
    details.append(("commit", event.get("commit_id")))
    rendered = "; ".join(f"{name}: {value}" for name, value in details if value not in ("", None))
    return [head + (f" — {rendered}" if rendered else "")]


def _timeline_md(timeline: list[dict[str, Any]]) -> str:
    entries = [entry for event in timeline if (entry := _timeline_entry(event)) is not None]
    lines = [f"# Issue timeline ({len(entries)})", ""]
    lines += [line for entry in entries for line in entry] or ["(none)"]
    return "\n".join(lines) + "\n"


# -- the remaining PR surfaces -------------------------------------------------


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


def _checks_md(pr: PullRequest, checks: list[CheckRun], statuses: list[CommitStatus]) -> str:
    """Both CI surfaces on the PR head (#52): GitHub Check Runs and legacy
    commit statuses, each with enough identity (id, timestamps) to tell
    duplicate names/contexts and reruns apart."""
    lines = [f"# Checks on {pr.head_sha}", ""]
    lines += [f"## Check runs ({len(checks)})", ""]
    if not checks:
        lines.append("(none)")
    for check in sorted(checks, key=lambda c: (c.name, c.id)):
        line = f"- {check.name} [id {check.id}]: {check.status}"
        if check.conclusion:
            line += f" ({check.conclusion})"
        window = "..".join(t for t in (check.started_at, check.completed_at) if t)
        if window:
            line += f" {window}"
        if check.url:
            line += f" — {check.url}"
        lines.append(line)
    lines += ["", f"## Commit statuses ({len(statuses)})", ""]
    if not statuses:
        lines.append("(none)")
    for status in sorted(statuses, key=lambda s: (s.created_at, s.id)):
        line = f"- {status.context} [id {status.id}]: {status.state}"
        if status.created_at:
            line += f" at {status.created_at}"
        if status.creator:
            line += f" by {status.creator}"
        if status.description:
            line += f" — {status.description}"
        if status.target_url:
            line += f" — {status.target_url}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def write_tree(input_dir: Path, snapshot: ContextSnapshot) -> None:
    """Materialize the snapshot under ``input_dir`` (the job dir's input/).

    The authority filter is enforced here as well as at fetch time: however
    a snapshot was produced, nothing unauthorized is ever serialized."""
    issue_root = input_dir / ISSUE_DIR
    _write(issue_root, "body.md", _issue_body_md(snapshot.issue))
    _write_items(
        issue_root,
        "comments",
        "Issue comments",
        _comment_items(authorized(snapshot.issue_comments), "Issue comment"),
    )
    _write(issue_root, "timeline.md", _timeline_md(snapshot.timeline))

    if snapshot.pr is None:
        return
    pr_root = input_dir / PR_DIR
    _write(pr_root, "body.md", _pr_body_md(snapshot.pr))
    _write_items(
        pr_root,
        "conversation",
        "PR conversation",
        _comment_items(authorized(snapshot.pr_conversation), "PR comment"),
    )
    _write_items(
        pr_root,
        "review-comments",
        "PR review comments",
        _review_comment_items(authorized(snapshot.pr_review_comments)),
    )
    _write_items(pr_root, "reviews", "PR reviews", _review_items(authorized(snapshot.pr_reviews)))
    _write(pr_root, "commits.md", _commits_md(snapshot.pr_commits))
    _write(pr_root, "checks.md", _checks_md(snapshot.pr, snapshot.pr_checks, snapshot.pr_statuses))
