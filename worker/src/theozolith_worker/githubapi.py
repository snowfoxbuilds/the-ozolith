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
from dataclasses import dataclass
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
    """An issue — or a PR seen through the issues API (labels, assignees)."""

    number: int
    title: str
    body: str
    labels: set[str]
    assignees: list[str]
    is_pr: bool


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
    and ``in_reply_to_id``/``review_id`` carry thread and review linkage."""

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


@dataclass(frozen=True)
class PrFile:
    path: str
    status: str
    additions: int
    deletions: int
    patch: str


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
    )


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
                if method != "GET":
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
        if self._default_branch is None:
            self._default_branch = self._json("GET", self._repo_path(""))["default_branch"]
        return self._default_branch

    # -- issues (and PRs through the issues API) --------------------------

    def list_open_issues(self, label: str) -> list[Issue]:
        """Open true issues (not PRs) carrying ``label``."""
        query = urllib.parse.quote(label)
        items = self._paged(self._repo_path(f"/issues?state=open&labels={query}"))
        return [_issue_from(item) for item in items if "pull_request" not in item]

    def list_open_prs_by_label(self, label: str) -> list[Issue]:
        """Open PRs carrying ``label``, issue-shaped (labels + assignees)."""
        query = urllib.parse.quote(label)
        items = self._paged(self._repo_path(f"/issues?state=open&labels={query}"))
        return [_issue_from(item) for item in items if "pull_request" in item]

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

    def update_pr(self, number: int, *, title: str | None = None, body: str | None = None) -> None:
        patch: dict[str, str] = {}
        if title is not None:
            patch["title"] = title
        if body is not None:
            patch["body"] = body
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

    def pr_files(self, number: int) -> list[PrFile]:
        items = self._paged(self._repo_path(f"/pulls/{number}/files"))
        return [
            PrFile(
                path=item["filename"],
                status=item.get("status", "modified"),
                additions=item.get("additions", 0),
                deletions=item.get("deletions", 0),
                patch=item.get("patch") or "",
            )
            for item in items
        ]
