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
      deps/                          present only when the issue carries
        INDEX.md                     Dependency Edges (ADR-0053): the full
        issue-<N>/...                transitive blocked-by closure, one tree
                                     per dependency, serialized exactly like
                                     the primary (issue/ + pr/ layout above)

Authority boundary (#52 amendment — the one deliberate exception to "never
relevance-filter"): the tree contains comments, reviews, and review payloads
ONLY from authors GitHub reports as ``OWNER`` or ``MEMBER``. Every other
association — COLLABORATOR, CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, FIRST_TIMER,
NONE, or missing/unknown — is filtered before serialization and leaves no
worker-visible trace: no body, snippet, filename, index entry, or count. The
filter is applied at fetch time (so the driver's machine-verdict discovery
obeys the same boundary) AND again at serialization (so no producer of a
snapshot can leak past it). No trace includes linkage: an authorized reply
never names an unauthorized parent (``in-reply-to`` is emitted only when the
referenced comment is itself authorized), and review-thread state attaches
only to authorized comments (an all-unauthorized thread vanishes entirely).
The boundary covers comments, reviews, and embedded comment/review payloads
— and nothing else: issue/PR metadata such as a cross-referenced item's
title is not comment content and renders whenever GitHub provides it.

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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from theozolith_worker import gitops, jobdir
from theozolith_worker.deps import DependencyClosure, branch_for
from theozolith_worker.githubapi import (
    CheckRun,
    Comment,
    CommitStatus,
    GitHubClient,
    Issue,
    PullRequest,
    Review,
    ReviewComment,
    ReviewThread,
)

ISSUE_DIR = "issue"
PR_DIR = "pr"
DEPS_DIR = "deps"
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
    # Thread resolution/grouping metadata keyed by comment id; consulted
    # only for authorized comments at serialization, so an all-unauthorized
    # thread leaves no trace.
    pr_review_threads: list[ReviewThread] = field(default_factory=list)
    # A stated commit-enumeration gap, rendered in commits.md when the list
    # is empty (ADR-0053): a dependency PR whose branch is gone cannot be
    # enumerated from the checkout — the tree says so instead of failing
    # the Run or silently showing "(none)".
    pr_commits_note: str = ""


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
        # An empty head SHA (defensive parsing of a degenerate payload)
        # cannot carry checks; never query GitHub with it.
        pr_checks=client.list_check_runs(pr.head_sha) if pr.head_sha else [],
        pr_statuses=client.list_statuses(pr.head_sha) if pr.head_sha else [],
        pr_review_threads=client.list_review_threads(pr.number),
    )


def fetch_deps(
    client: GitHubClient,
    closure: DependencyClosure,
    primary: int,
    workdir: Path | None = None,
) -> list[tuple[int, ContextSnapshot]]:
    """One full :class:`ContextSnapshot` per closure member except the
    primary, in topological order (ADR-0053): the dependency context is
    fetched exactly like the primary's — same surfaces, same OWNER/MEMBER
    authority boundary — with closed and merged dependencies included. The
    dependency's PR (any state, merged included) resolves deterministically
    by branch naming; its commits enumerate best-effort from the caller's
    trusted checkout, and anything unenumerable becomes a stated gap in
    commits.md — a dependency's commits never fail the Run."""
    snapshots: list[tuple[int, ContextSnapshot]] = []
    for number in closure.order:
        if number == primary:
            continue
        pr = client.find_pr_by_head(branch_for(number), state="all")
        snapshot = fetch_snapshot(client, number, pr)
        if pr is not None:
            commits, note = _dep_pr_commits(workdir, pr)
            snapshot = replace(snapshot, pr_commits=commits, pr_commits_note=note)
        snapshots.append((number, snapshot))
    return snapshots


def _dep_pr_commits(workdir: Path | None, pr: PullRequest) -> tuple[list[PrCommit], str]:
    """Best-effort commit enumeration for a dependency PR (ADR-0053): a
    merged PR reads its merge commit's second-parent range; a live PR needs
    both of its endpoints present in the checkout. Anything unresolvable
    yields ``([], reason)`` for commits.md to state."""
    deleted_note = (
        f"(commit enumeration unavailable — branch deleted; merged as {pr.merge_commit_sha})"
    )
    try:
        if workdir is not None:
            if pr.merged and pr.merge_commit_sha:
                if gitops.commit_exists(workdir, pr.merge_commit_sha):
                    range_commits = git_pr_commits(
                        workdir, f"{pr.merge_commit_sha}^1", pr.merge_commit_sha
                    )
                    return range_commits, ""
                return [], deleted_note
            head = ""
            if pr.head_sha and gitops.commit_exists(workdir, pr.head_sha):
                head = pr.head_sha
            elif gitops.ref_exists(workdir, f"origin/{pr.head_ref}"):
                head = f"origin/{pr.head_ref}"
            if head and gitops.ref_exists(workdir, f"origin/{pr.base_ref}"):
                return git_pr_commits(workdir, f"origin/{pr.base_ref}", head), ""
    except gitops.GitError:
        pass
    if pr.merged and pr.merge_commit_sha:
        return [], deleted_note
    return [], "(commit enumeration unavailable from this checkout)"


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


def _item_file(title: str, fields: list[tuple[str, object]], body: str, context: str = "") -> str:
    """``context`` renders between header and body (e.g. the diff hunk a
    review comment anchors to); the body stays last so index snippets keep
    quoting the comment text, not the attachment."""
    parts = [*_header(title, fields)]
    if context:
        parts += ["", context]
    parts += ["", body or "(no body)", ""]
    return "\n".join(parts)


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
        _write(
            root,
            f"{subdir}/{name}",
            _item_file(item["title"], item["fields"], item["body"], item.get("context", "")),
        )
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


def _review_comment_items(comments: list[ReviewComment], threads: list[ReviewThread]) -> list[dict]:
    """``comments`` must already be authority-filtered. Reply linkage obeys
    the no-trace rule: ``in-reply-to`` is emitted only when the referenced
    comment is itself authorized — an unauthorized thread root must leave no
    id, placeholder, or count anywhere. Thread state (resolution, grouping)
    is attached per authorized comment, keyed by the thread's own GraphQL
    node id (never a comment id), so an all-unauthorized thread also leaves
    no trace."""
    visible_ids = {c.id for c in comments}
    thread_of = {cid: thread for thread in threads for cid in thread.comment_ids}
    ordered = sorted(comments, key=lambda c: (c.created_at, c.id))
    items = []
    for seq, c in enumerate(ordered, start=1):
        thread = thread_of.get(c.id)
        # The complete anchor set (#52): an outdated comment has no
        # current line but keeps its original one; multiline comments
        # carry start-line ranges; replies name their thread.
        fields: list[tuple[str, object]] = [
            ("id", c.id),
            ("author", c.author or UNKNOWN_AUTHOR),
            ("association", c.author_association),
            ("created", c.created_at),
            ("updated", c.updated_at if c.updated_at != c.created_at else None),
            ("path", c.path),
            ("subject", c.subject_type),
            ("line", c.line),
            ("side", c.side),
            ("start-line", c.start_line),
            ("start-side", c.start_side),
            ("original-line", c.original_line),
            ("original-start-line", c.original_start_line),
            ("position", c.position),
            ("original-position", c.original_position),
            ("commit", c.commit_id),
            ("original-commit", c.original_commit_id),
            (
                "in-reply-to",
                c.in_reply_to_id if c.in_reply_to_id in visible_ids else None,
            ),
            ("review", c.review_id),
            ("url", c.url),
        ]
        if thread is not None:
            fields += [
                ("thread", thread.id),
                ("thread-resolved", "yes" if thread.is_resolved else "no"),
                ("thread-resolved-by", thread.resolved_by),
                ("thread-outdated", "yes" if thread.is_outdated else None),
            ]
        items.append(
            {
                "author": c.author or UNKNOWN_AUTHOR,
                "timestamp": c.created_at,
                "body": c.body,
                "title": f"Review comment {seq}",
                "fields": fields,
                "context": f"```diff\n{c.diff_hunk}\n```" if c.diff_hunk else "",
            }
        )
    return items


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
# cross-reference identity, label colors, who assigned/requested, ...). A
# kind mapped to an empty extractor carries nothing beyond the common
# identity fields (id/actor/timestamp/commit); an UNMAPPED kind fails
# closed — its payload is withheld, because an unrecognized future event
# could embed comment content the authority filter has not classified.
def _cross_reference_details(event: dict[str, Any]) -> list[tuple[str, object]]:
    """Full cross-reference identity — kind, repository, number, title,
    state, URL. The title is issue/PR metadata, not comment content, so the
    comment-authority boundary does not apply: it renders whenever GitHub
    provides it, same-repo or not."""
    source = (event.get("source") or {}).get("issue") or {}
    return [
        ("kind", "pull request" if "pull_request" in source else "issue"),
        ("repository", (source.get("repository") or {}).get("full_name")),
        ("number", source.get("number")),
        ("title", source.get("title")),
        ("state", source.get("state")),
        ("url", source.get("html_url")),
    ]


def _label_details(e: dict[str, Any]) -> list[tuple[str, object]]:
    label = e.get("label") or {}
    return [("label", label.get("name")), ("color", label.get("color"))]


_EVENT_DETAILS: dict[str, Any] = {
    "labeled": _label_details,
    "unlabeled": _label_details,
    "assigned": lambda e: [
        ("assignee", (e.get("assignee") or {}).get("login")),
        ("assigner", (e.get("assigner") or {}).get("login")),
    ],
    "unassigned": lambda e: [
        ("assignee", (e.get("assignee") or {}).get("login")),
        ("assigner", (e.get("assigner") or {}).get("login")),
    ],
    "milestoned": lambda e: [("milestone", (e.get("milestone") or {}).get("title"))],
    "demilestoned": lambda e: [("milestone", (e.get("milestone") or {}).get("title"))],
    "renamed": lambda e: [
        ("from", (e.get("rename") or {}).get("from")),
        ("to", (e.get("rename") or {}).get("to")),
    ],
    "review_requested": lambda e: [
        ("reviewer", (e.get("requested_reviewer") or {}).get("login")),
        ("team", (e.get("requested_team") or {}).get("name")),
        ("requested-by", (e.get("review_requester") or {}).get("login")),
    ],
    "review_request_removed": lambda e: [
        ("reviewer", (e.get("requested_reviewer") or {}).get("login")),
        ("team", (e.get("requested_team") or {}).get("name")),
        ("requested-by", (e.get("review_requester") or {}).get("login")),
    ],
    "cross-referenced": _cross_reference_details,
    "locked": lambda e: [("reason", e.get("lock_reason"))],
    "closed": lambda e: [("state_reason", e.get("state_reason"))],
    # Commit messages/authors are NOT taken from the timeline: commits.md,
    # enumerated from the trusted checkout, is the canonical lossless
    # commit surface — the sha here joins the two.
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
    # The safe common identity fields every rendered event keeps: its id and
    # the commit it points at (kind/actor/timestamp are on the head line).
    details.append(("commit", event.get("commit_id")))
    details.append(("event-id", event.get("id")))
    rendered = "; ".join(f"{name}: {value}" for name, value in details if value not in ("", None))
    return [head + (f" — {rendered}" if rendered else "")]


def _timeline_md(timeline: list[dict[str, Any]]) -> str:
    entries = [entry for event in timeline if (entry := _timeline_entry(event)) is not None]
    lines = [f"# Issue timeline ({len(entries)})", ""]
    lines += [line for entry in entries for line in entry] or ["(none)"]
    return "\n".join(lines) + "\n"


# -- the remaining PR surfaces -------------------------------------------------


def _issue_body_md(issue: Issue) -> str:
    # state/state-reason render only when closed (ADR-0053: closed
    # dependencies carry their disposition); an open issue's file is
    # byte-identical to the pre-deps shape.
    closed = issue.state == "closed"
    fields: list[tuple[str, object]] = [
        ("state", issue.state if closed else None),
        ("state-reason", issue.state_reason if closed else None),
        ("labels", ", ".join(sorted(issue.labels))),
        ("assignees", ", ".join(issue.assignees)),
    ]
    return _item_file(f"Issue #{issue.number}: {issue.title}", fields, issue.body)


def _pr_body_md(pr: PullRequest) -> str:
    fields: list[tuple[str, object]] = [
        ("state", pr.state),
        # Merged dependencies carry the fact and the merge SHA (ADR-0053);
        # both fields vanish on the unmerged shape.
        ("merged", "yes" if pr.merged else None),
        ("merge-sha", pr.merge_commit_sha or None),
        ("head", f"{pr.head_ref} @ {pr.head_sha}"),
        ("base", pr.base_ref),
        ("labels", ", ".join(sorted(pr.labels))),
    ]
    return _item_file(f"PR #{pr.number}: {pr.title}", fields, pr.body)


def _commits_md(commits: list[PrCommit], note: str = "") -> str:
    lines = [f"# PR commits ({len(commits)})", ""]
    if not commits:
        lines.append(note or "(none)")
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
        _review_comment_items(authorized(snapshot.pr_review_comments), snapshot.pr_review_threads),
    )
    _write_items(pr_root, "reviews", "PR reviews", _review_items(authorized(snapshot.pr_reviews)))
    _write(pr_root, "commits.md", _commits_md(snapshot.pr_commits, snapshot.pr_commits_note))
    _write(pr_root, "checks.md", _checks_md(snapshot.pr, snapshot.pr_checks, snapshot.pr_statuses))


def _dep_index_line(number: int, snapshot: ContextSnapshot, blockers: tuple[int, ...]) -> str:
    issue = snapshot.issue
    state = f"closed ({issue.state_reason or 'no reason'})" if issue.state == "closed" else "open"
    if snapshot.pr is None:
        pr_part = "no PR"
    elif snapshot.pr.merged:
        sha = f" as {snapshot.pr.merge_commit_sha}" if snapshot.pr.merge_commit_sha else ""
        pr_part = f"PR #{snapshot.pr.number} merged{sha}"
    else:
        pr_part = f"PR #{snapshot.pr.number} {snapshot.pr.state}"
    blocked_by = ", ".join(f"#{n}" for n in blockers) or "none"
    return (
        f"- [issue-{number}](issue-{number}/) #{number} {issue.title} — {state};"
        f" {pr_part}; blocked by: {blocked_by}"
    )


def write_deps(
    input_dir: Path,
    dep_snapshots: list[tuple[int, ContextSnapshot]],
    closure: DependencyClosure,
) -> None:
    """Materialize ``input/deps/`` (ADR-0053): one ``issue-N/`` tree per
    dependency, written through :func:`write_tree` — structural parity with
    the primary and the authority boundary hold by construction — plus a
    top-level INDEX.md, one line per dependency in topological order
    (number, title, state or PR disposition, direct blockers)."""
    root = input_dir / DEPS_DIR
    lines = [f"# Dependency closure ({len(dep_snapshots)})", ""]
    if not dep_snapshots:
        lines.append("(none)")
    for number, snapshot in dep_snapshots:
        write_tree(root / f"issue-{number}", snapshot)
        lines.append(_dep_index_line(number, snapshot, closure.edges.get(number, ())))
    _write(root, INDEX_FILE, "\n".join(lines) + "\n")
