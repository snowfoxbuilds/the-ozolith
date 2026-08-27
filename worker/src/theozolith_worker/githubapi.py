"""Rate-limited GitHub REST client for the Worker and Reviewer actors.

Stdlib only (ADR-0010). The HTTP layer is an injectable ``Transport`` so tests
run the real client against an in-memory GitHub. Every non-GET request lands
in ``GitHubClient.writes`` — the transcript the authority test audits.

Rate limiting (AGENTIC-CODING-PIPELINE.md, M2 acceptance 9): every request
retries with exponential backoff on 5xx, honors ``Retry-After`` and
``X-RateLimit-Reset`` on 403/429 (primary and secondary limits), and gives up
only on genuine client errors. A rate-limited Run therefore pauses inside the
call and resumes when the limit lifts.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Protocol

MAX_RETRIES = 8
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 120.0


class GitHubError(RuntimeError):
    """A GitHub API call failed for good (after retries, if retryable)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lower-cased keys
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


class Transport(Protocol):
    """One HTTP exchange. Returns a Response for every outcome, including
    HTTP errors; raises only on transport failure (network down)."""

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> Response: ...


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return Response(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=resp.read(),
            )
    except urllib.error.HTTPError as exc:
        return Response(
            status=exc.code,
            headers={k.lower(): v for k, v in exc.headers.items()},
            body=exc.read(),
        )
    except urllib.error.URLError as exc:
        raise GitHubError(f"{method} {url} failed: {exc.reason}") from exc


def _retry_delay(response: Response, attempt: int, now: float) -> float | None:
    """Seconds to wait before retrying, or None if the failure is permanent."""
    if response.status >= 500:
        return min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_CAP_SECONDS)
    if response.status in (403, 429):
        retry_after = response.header("retry-after")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        if response.header("x-ratelimit-remaining") == "0":
            reset = response.header("x-ratelimit-reset")
            if reset and reset.isdigit():
                return max(float(reset) - now, 0.0) + 1.0
        if b"secondary rate limit" in response.body.lower():
            return min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_CAP_SECONDS)
    return None


@dataclass(frozen=True)
class Issue:
    """An issue — or a PR seen through the issues API (labels, assignees).

    ``state``/``state_reason`` distinguish a blocker closed as completed
    (satisfies its Dependency Edge) from one closed as not_planned (does
    not; ADR-0053). ``repo`` is empty for the client's own repo and is
    populated only by ``list_blocked_by`` — a cross-repo edge on a claimable
    issue is a malformed state the caller must detect."""

    number: int
    title: str
    body: str
    labels: set[str]
    assignees: list[str]
    is_pr: bool
    state: str = "open"
    state_reason: str = ""
    repo: str = ""


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    head_ref: str
    head_sha: str
    base_ref: str
    labels: set[str]
    state: str
    # List endpoints omit the ``merged`` boolean, so it is derived from
    # ``merged_at`` there; ``merge_commit_sha`` is the durable merge record
    # a chained dependent's history builds on (ADR-0053).
    merged: bool = False
    merge_commit_sha: str = ""


@dataclass(frozen=True)
class RepoMergeSettings:
    """The repo merge-method settings the Chained Base preconditions read
    (ADR-0053). ``complete`` is False when GitHub omitted any field — the
    token cannot see them — and callers must treat that as preconditions
    failed (chaining off with a visible reason), never a silent pass."""

    merge_commit_allowed: bool
    squash_allowed: bool
    rebase_allowed: bool
    delete_branch_on_merge: bool
    complete: bool


@dataclass(frozen=True)
class Comment:
    """A conversation comment (issue or PR). ``author_association`` is
    GitHub's word on who wrote it (OWNER, MEMBER, COLLABORATOR, ...); the
    Context Tree's authority filter keys on it (#52 amendment). ``author``
    is empty when GitHub reports no user (deleted accounts)."""

    id: int
    author: str
    body: str
    created_at: str
    url: str = ""
    author_association: str = ""


@dataclass(frozen=True)
class ReviewComment:
    """An inline PR review comment, anchored to a file position.

    Anchors are lossless (#52 amendment): the current line/side pair plus
    the original-commit anchors, so an outdated comment (``line`` is None)
    keeps its original position; start-line fields carry multiline ranges,
    and ``in_reply_to_id``/``review_id`` carry thread and review linkage.
    ``diff_hunk`` is the diff excerpt the comment anchors to; ``position``/
    ``original_position`` are the legacy diff offsets; ``subject_type``
    distinguishes file-level from line-level comments; ``updated_at``
    records edits."""

    id: int
    author: str
    body: str
    created_at: str
    path: str
    line: int | None
    url: str = ""
    author_association: str = ""
    original_line: int | None = None
    start_line: int | None = None
    original_start_line: int | None = None
    side: str = ""
    start_side: str = ""
    commit_id: str = ""
    original_commit_id: str = ""
    in_reply_to_id: int | None = None
    review_id: int | None = None
    diff_hunk: str = ""
    position: int | None = None
    original_position: int | None = None
    subject_type: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ReviewThread:
    """One PR review thread (GraphQL): resolution and grouping state for the
    inline comments it contains. ``comment_ids`` are REST database ids, so
    threads join against :class:`ReviewComment` items; the thread's own
    ``id`` is its GraphQL node id — an identifier of the thread resource,
    never of any comment."""

    id: str
    is_resolved: bool
    is_outdated: bool = False
    resolved_by: str = ""
    comment_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Review:
    """A PR review: a state (APPROVED, CHANGES_REQUESTED, ...) plus a body."""

    id: int
    author: str
    state: str
    body: str
    submitted_at: str
    url: str = ""
    author_association: str = ""
    commit_id: str = ""


@dataclass(frozen=True)
class CheckRun:
    """One check run on a commit. ``id`` plus the timestamps distinguish
    reruns and same-named runs from different suites (#52 amendment)."""

    name: str
    status: str
    conclusion: str
    url: str = ""
    id: int = 0
    started_at: str = ""
    completed_at: str = ""


@dataclass(frozen=True)
class CommitStatus:
    """One legacy commit-status event (the pre-Checks CI API). The statuses
    list endpoint returns every event, superseded ones included; ``id`` and
    ``created_at`` distinguish reruns of the same context."""

    id: int
    context: str
    state: str
    description: str = ""
    target_url: str = ""
    created_at: str = ""
    creator: str = ""


def _login_of(data: dict[str, Any]) -> str:
    """The payload's author login, defensively: deleted accounts arrive as
    ``"user": null`` and must parse (the authority filter decides what
    happens to the item; parsing never crashes a Run)."""
    user = data.get("user")
    return (user.get("login") or "") if isinstance(user, dict) else ""


def _association_of(data: dict[str, Any]) -> str:
    return str(data.get("author_association") or "")


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _issue_from(data: dict[str, Any]) -> Issue:
    return Issue(
        number=data["number"],
        title=data.get("title") or "",
        body=data.get("body") or "",
        labels={label["name"] for label in data.get("labels", [])},
        assignees=[a["login"] for a in data.get("assignees", [])],
        is_pr="pull_request" in data,
        state=data.get("state") or "open",
        # GitHub sends null for an open issue's state_reason.
        state_reason=data.get("state_reason") or "",
    )


def _pull_from(data: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=data["number"],
        title=data.get("title") or "",
        body=data.get("body") or "",
        head_ref=data["head"]["ref"],
        head_sha=data["head"]["sha"],
        base_ref=data["base"]["ref"],
        labels={label["name"] for label in data.get("labels", [])},
        state=data.get("state", "open"),
        # List endpoints omit ``merged``; ``merged_at`` is present on both.
        merged=bool(data.get("merged")) or bool(data.get("merged_at")),
        merge_commit_sha=data.get("merge_commit_sha") or "",
    )


# Review-thread resolution lives only in GraphQL (REST review comments carry
# no resolved state). Both queries page at 100 — threads at the top level,
# comments within a thread through the follow-up node query — so neither
# level is ever capped (#52).
_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          resolvedBy { login }
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { databaseId }
          }
        }
      }
    }
  }
}
"""

_THREAD_COMMENTS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId }
      }
    }
  }
}
"""


class GitHubClient:
    """Typed operations the actors need, on one target repo."""

    def __init__(
        self,
        repo: str,
        token: str,
        api_url: str = "https://api.github.com",
        transport: Transport = urllib_transport,
        sleep=time.sleep,
        clock=time.time,
    ):
        if "/" not in repo:
            raise GitHubError(f"expected repo as owner/name, got {repo!r}")
        self.repo = repo
        self._api = api_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._login: str | None = None
        self._default_branch: str | None = None
        # Transcript of successful writes: (method, repo-relative path).
        self.writes: list[tuple[str, str]] = []

    # -- plumbing ---------------------------------------------------------

    def _request(self, method: str, path: str, body: Any = None) -> Response:
        url = path if path.startswith("http") else f"{self._api}{path}"
        payload = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "theozolith-worker",
            **({"Content-Type": "application/json"} if payload else {}),
        }
        for attempt in range(MAX_RETRIES):
            response = self._transport(method, url, headers, payload)
            if response.status < 400:
                # /graphql is POST-only but this client sends it exclusively
                # read-only queries (list_review_threads); a future GraphQL
                # MUTATION must not hide behind this exemption.
                if method != "GET" and path != "/graphql":
                    self.writes.append((method, path))
                return response
            delay = _retry_delay(response, attempt, self._clock())
            if delay is None or attempt == MAX_RETRIES - 1:
                break
            self._sleep(delay)
        detail = response.body.decode(errors="replace")[:300]
        raise GitHubError(
            f"{method} {path} failed: HTTP {response.status} {detail}",
            status=response.status,
        )

    def _json(self, method: str, path: str, body: Any = None) -> Any:
        return self._request(method, path, body).json()

    def _paged(self, path: str, key: str | None = None) -> list[dict[str, Any]]:
        """Every page of a list endpoint. ``key`` handles the endpoints that
        wrap their items in an object (e.g. check-runs)."""
        sep = "&" if "?" in path else "?"
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._json("GET", f"{path}{sep}per_page=100&page={page}")
            batch = (payload or {}).get(key, []) if key else (payload or [])
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def _repo_path(self, tail: str) -> str:
        return f"/repos/{self.repo}{tail}"

    # -- identity and repo metadata ---------------------------------------

    def viewer_login(self) -> str:
        if self._login is None:
            self._login = self._json("GET", "/user")["login"]
        return self._login

    def default_branch(self) -> str:
        """Cached for the client's lifetime. A default-branch rename while
        a driver is up therefore fails fresh checkouts loudly (the cached
        name no longer clones) until the driver restarts — accepted: the
        rename is a rare deliberate operator act, and the failure is
        infra-classed and visible, never a silently wrong base."""
        if self._default_branch is None:
            self._default_branch = self._json("GET", self._repo_path(""))["default_branch"]
        return self._default_branch

    def repo_merge_settings(self) -> RepoMergeSettings:
        """The merge-method settings the Chained Base preconditions consume
        (ADR-0053). GitHub omits the allow_* fields for tokens that cannot
        see them; ``complete`` records whether every field was present."""
        data = self._json("GET", self._repo_path("")) or {}
        fields = (
            "allow_merge_commit",
            "allow_squash_merge",
            "allow_rebase_merge",
            "delete_branch_on_merge",
        )
        return RepoMergeSettings(
            merge_commit_allowed=bool(data.get("allow_merge_commit")),
            squash_allowed=bool(data.get("allow_squash_merge")),
            rebase_allowed=bool(data.get("allow_rebase_merge")),
            delete_branch_on_merge=bool(data.get("delete_branch_on_merge")),
            complete=all(name in data for name in fields),
        )

    def branch_head(self, branch: str) -> str | None:
        """The branch's tip SHA, or None when the branch does not exist —
        for a chained dependent's blocker branch, deletion is the healthy
        post-merge retarget signal, not an error (ADR-0053)."""
        query = urllib.parse.quote(branch, safe="")
        try:
            data = self._json("GET", self._repo_path(f"/branches/{query}"))
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return ((data or {}).get("commit") or {}).get("sha") or ""

    # -- issues (and PRs through the issues API) --------------------------

    # Both listings drain in creation (= plan) order (ADR-0053): GitHub's
    # default sort is newest-first, which would grant the LAST issue of a
    # plan first; oldest-first Reviewer discovery is load-bearing for
    # chains (a blocker must be reviewed before its dependents' go-ahead).
    _CREATION_ORDER = "&sort=created&direction=asc"

    def list_open_issues(self, label: str) -> list[Issue]:
        """Open true issues (not PRs) carrying ``label``, oldest first."""
        query = urllib.parse.quote(label)
        items = self._paged(
            self._repo_path(f"/issues?state=open&labels={query}{self._CREATION_ORDER}")
        )
        return [_issue_from(item) for item in items if "pull_request" not in item]

    def list_open_prs_by_label(self, label: str) -> list[Issue]:
        """Open PRs carrying ``label``, issue-shaped, oldest first."""
        query = urllib.parse.quote(label)
        items = self._paged(
            self._repo_path(f"/issues?state=open&labels={query}{self._CREATION_ORDER}")
        )
        return [_issue_from(item) for item in items if "pull_request" in item]

    def list_blocked_by(self, number: int) -> list[Issue]:
        """The issues blocking ``number`` — its Dependency Edges (ADR-0053).
        Each item is a full issue object; ``repo`` is parsed from its
        ``repository_url`` so a cross-repo edge is detectable. A 404 (the
        dependencies feature unavailable on this GitHub) raises — fail
        loud, never silently edge-less."""
        items = self._paged(self._repo_path(f"/issues/{number}/dependencies/blocked_by"))
        blockers: list[Issue] = []
        for item in items:
            url = str(item.get("repository_url") or "")
            repo = url.split("/repos/", 1)[1] if "/repos/" in url else ""
            blockers.append(replace(_issue_from(item), repo=repo))
        return blockers

    def get_issue(self, number: int) -> Issue:
        return _issue_from(self._json("GET", self._repo_path(f"/issues/{number}")))

    def add_labels(self, number: int, *labels: str) -> None:
        self._json("POST", self._repo_path(f"/issues/{number}/labels"), {"labels": list(labels)})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self._request(
                "DELETE",
                self._repo_path(f"/issues/{number}/labels/{urllib.parse.quote(label)}"),
            )
        except GitHubError as exc:
            if exc.status != 404:  # already absent: fine
                raise

    def add_assignees(self, number: int, *logins: str) -> None:
        self._json(
            "POST", self._repo_path(f"/issues/{number}/assignees"), {"assignees": list(logins)}
        )

    def remove_assignee(self, number: int, login: str) -> None:
        self._json("DELETE", self._repo_path(f"/issues/{number}/assignees"), {"assignees": [login]})

    def assign_order(self, number: int) -> list[str]:
        """Logins in earliest-assigned order, from the issue event timeline."""
        events = self._paged(self._repo_path(f"/issues/{number}/events"))
        order: list[str] = []
        for event in events:
            if event.get("event") == "assigned" and event.get("assignee"):
                login = event["assignee"]["login"]
                if login not in order:
                    order.append(login)
        return order

    def add_comment(self, number: int, body: str) -> None:
        self._json("POST", self._repo_path(f"/issues/{number}/comments"), {"body": body})

    def list_comments(self, number: int) -> list[Comment]:
        items = self._paged(self._repo_path(f"/issues/{number}/comments"))
        return [
            Comment(
                id=item["id"],
                author=_login_of(item),
                body=item.get("body") or "",
                created_at=item.get("created_at") or "",
                url=item.get("html_url") or "",
                author_association=_association_of(item),
            )
            for item in items
        ]

    def list_timeline(self, number: int) -> list[dict[str, Any]]:
        """The issue's full event timeline, raw: the events are heterogeneous
        (labeled, assigned, cross-referenced, committed, ...) and the Context
        Tree renders them without interpreting most of them."""
        return self._paged(self._repo_path(f"/issues/{number}/timeline"))

    # -- repository contents ------------------------------------------------

    def path_exists(self, path: str, *, ref: str) -> bool:
        """Does ``path`` exist at ``ref``? (contents API, read-only)."""
        query = urllib.parse.quote(path, safe="/")
        try:
            self._request(
                "GET", self._repo_path(f"/contents/{query}?ref={urllib.parse.quote(ref)}")
            )
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    # -- pull requests -----------------------------------------------------

    def get_pull(self, number: int) -> PullRequest:
        return _pull_from(self._json("GET", self._repo_path(f"/pulls/{number}")))

    def find_open_pr_by_head(self, head_ref: str) -> PullRequest | None:
        owner = self.repo.split("/")[0]
        query = urllib.parse.quote(f"{owner}:{head_ref}")
        items = self._json("GET", self._repo_path(f"/pulls?state=open&head={query}")) or []
        return _pull_from(items[0]) if items else None

    def find_pr_by_head(self, head_ref: str, state: str = "all") -> PullRequest | None:
        """Like :meth:`find_open_pr_by_head` across PR states: an open match
        wins, else the newest match — how a chained base is traced after
        its blocker PR closed (ADR-0053)."""
        owner = self.repo.split("/")[0]
        query = urllib.parse.quote(f"{owner}:{head_ref}")
        items = self._json("GET", self._repo_path(f"/pulls?state={state}&head={query}")) or []
        pulls = [_pull_from(item) for item in items]
        for pull in pulls:
            if pull.state == "open":
                return pull
        return max(pulls, key=lambda pull: pull.number, default=None)

    def list_open_prs(self) -> list[PullRequest]:
        """Every open PR, oldest first (creation order, ADR-0053)."""
        items = self._paged(self._repo_path(f"/pulls?state=open{self._CREATION_ORDER}"))
        return [_pull_from(item) for item in items]

    def create_pr(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        try:
            data = self._json(
                "POST",
                self._repo_path("/pulls"),
                {"title": title, "head": head, "base": base, "body": body},
            )
            return _pull_from(data)
        except GitHubError as exc:
            if exc.status == 422:  # PR already exists for this head: reuse it
                existing = self.find_open_pr_by_head(head)
                if existing is not None:
                    return existing
            raise

    def update_pr(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        base: str | None = None,
    ) -> None:
        patch: dict[str, str] = {}
        if title is not None:
            patch["title"] = title
        if body is not None:
            patch["body"] = body
        if base is not None:
            patch["base"] = base
        if patch:
            self._json("PATCH", self._repo_path(f"/pulls/{number}"), patch)

    def list_review_comments(self, number: int) -> list[ReviewComment]:
        """Every inline (file/line) review comment on the PR, anchors intact."""
        items = self._paged(self._repo_path(f"/pulls/{number}/comments"))
        return [
            ReviewComment(
                id=item["id"],
                author=_login_of(item),
                body=item.get("body") or "",
                created_at=item.get("created_at") or "",
                path=item.get("path") or "",
                line=_int_or_none(item.get("line")),
                url=item.get("html_url") or "",
                author_association=_association_of(item),
                original_line=_int_or_none(item.get("original_line")),
                start_line=_int_or_none(item.get("start_line")),
                original_start_line=_int_or_none(item.get("original_start_line")),
                side=item.get("side") or "",
                start_side=item.get("start_side") or "",
                commit_id=item.get("commit_id") or "",
                original_commit_id=item.get("original_commit_id") or "",
                in_reply_to_id=_int_or_none(item.get("in_reply_to_id")),
                review_id=_int_or_none(item.get("pull_request_review_id")),
                diff_hunk=item.get("diff_hunk") or "",
                position=_int_or_none(item.get("position")),
                original_position=_int_or_none(item.get("original_position")),
                subject_type=item.get("subject_type") or "",
                updated_at=item.get("updated_at") or "",
            )
            for item in items
        ]

    def list_reviews(self, number: int) -> list[Review]:
        items = self._paged(self._repo_path(f"/pulls/{number}/reviews"))
        return [
            Review(
                id=item["id"],
                author=_login_of(item),
                state=item.get("state") or "",
                body=item.get("body") or "",
                submitted_at=item.get("submitted_at") or "",
                url=item.get("html_url") or "",
                author_association=_association_of(item),
                commit_id=item.get("commit_id") or "",
            )
            for item in items
        ]

    # /pulls/{number}/commits is deliberately NOT a client method: GitHub
    # caps it at 250 commits. The PR commit snapshot comes from the trusted
    # driver-side checkout instead (contexttree.git_pr_commits, #52).

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """One GraphQL query. Errors arrive as HTTP 200 with an ``errors``
        array, so they are surfaced here rather than by the retry layer."""
        payload = self._json("POST", "/graphql", {"query": query, "variables": variables})
        if not isinstance(payload, dict) or payload.get("errors"):
            errors = (payload or {}).get("errors") if isinstance(payload, dict) else payload
            raise GitHubError(f"GraphQL query failed: {errors!r}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubError(f"GraphQL query returned no data: {payload!r}")
        return data

    def list_review_threads(self, number: int) -> list[ReviewThread]:
        """Every review thread on the PR with its resolution state — GraphQL
        only; REST has no resolution surface. Fully paginated on both levels
        (threads, and comments within a thread)."""
        owner, name = self.repo.split("/", 1)
        threads: list[ReviewThread] = []
        cursor: str | None = None
        while True:
            data = self._graphql(
                _REVIEW_THREADS_QUERY,
                {"owner": owner, "name": name, "number": number, "cursor": cursor},
            )
            connection = ((data.get("repository") or {}).get("pullRequest") or {}).get(
                "reviewThreads"
            ) or {}
            for node in connection.get("nodes") or []:
                threads.append(self._thread_from(node))
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return threads
            cursor = page.get("endCursor")

    def _thread_from(self, node: dict[str, Any]) -> ReviewThread:
        comments = node.get("comments") or {}
        ids = [
            c["databaseId"]
            for c in comments.get("nodes") or []
            if isinstance(c.get("databaseId"), int)
        ]
        page = comments.get("pageInfo") or {}
        while page.get("hasNextPage"):
            data = self._graphql(
                _THREAD_COMMENTS_QUERY, {"id": node.get("id"), "cursor": page.get("endCursor")}
            )
            comments = (data.get("node") or {}).get("comments") or {}
            ids += [
                c["databaseId"]
                for c in comments.get("nodes") or []
                if isinstance(c.get("databaseId"), int)
            ]
            page = comments.get("pageInfo") or {}
        return ReviewThread(
            id=str(node.get("id") or ""),
            is_resolved=bool(node.get("isResolved")),
            is_outdated=bool(node.get("isOutdated")),
            resolved_by=_login_of({"user": node.get("resolvedBy")}),
            comment_ids=tuple(ids),
        )

    def list_check_runs(self, ref: str) -> list[CheckRun]:
        """Check runs for a commit (the PR head), paginated under the
        endpoint's ``check_runs`` wrapper key."""
        query = urllib.parse.quote(ref, safe="")
        items = self._paged(self._repo_path(f"/commits/{query}/check-runs"), key="check_runs")
        return [
            CheckRun(
                name=item.get("name") or "",
                status=item.get("status") or "",
                conclusion=item.get("conclusion") or "",
                url=item.get("html_url") or "",
                id=item.get("id") or 0,
                started_at=item.get("started_at") or "",
                completed_at=item.get("completed_at") or "",
            )
            for item in items
        ]

    def list_statuses(self, ref: str) -> list[CommitStatus]:
        """Every legacy commit-status event on a commit, paginated. The list
        endpoint keeps superseded events; the Context Tree keeps them all,
        distinguishable by id and timestamp (#52)."""
        query = urllib.parse.quote(ref, safe="")
        items = self._paged(self._repo_path(f"/commits/{query}/statuses"))
        return [
            CommitStatus(
                id=item.get("id") or 0,
                context=item.get("context") or "",
                state=item.get("state") or "",
                description=item.get("description") or "",
                target_url=item.get("target_url") or "",
                created_at=item.get("created_at") or "",
                creator=_login_of({"user": item.get("creator")}),
            )
            for item in items
        ]
