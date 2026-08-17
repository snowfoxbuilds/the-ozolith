"""In-memory GitHub REST server used as a Transport by the real client.

One FakeGitHub instance backs all actors: each GitHubClient authenticates
with its own token, the fake maps tokens to logins, and every write is logged
with its actor — the transcript the authority test audits (M2 acceptance 7).

When ``git_dir`` points at a local bare repo (the test remote), PR head SHAs
and the /pulls/{n}/files listing are derived live from real git state, so the
pipeline under test runs against genuine branches and diffs.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from theozolith_worker.githubapi import Response


def _json_response(status: int, payload: Any, headers: dict[str, str] | None = None) -> Response:
    return Response(
        status=status,
        headers={"content-type": "application/json", **(headers or {})},
        body=json.dumps(payload).encode(),
    )


def rate_limited_response(retry_after: int | None = 1) -> Response:
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return Response(
        status=403,
        headers=headers,
        body=b'{"message": "You have exceeded a secondary rate limit."}',
    )


class FakeGitHub:
    """Transport-level fake of the GitHub REST endpoints the actors use."""

    def __init__(self, repo: str = "acme/sandbox", git_dir: Path | None = None):
        self.repo = repo
        self.default_branch = "main"
        self.git_dir = git_dir
        self.tokens: dict[str, str] = {}
        # login -> author_association stamped on comments/reviews (#52).
        self.associations: dict[str, str] = {}
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.events: dict[int, list[dict[str, Any]]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.review_comments: dict[int, list[dict[str, Any]]] = {}
        self.review_threads: dict[int, list[dict[str, Any]]] = {}
        self.reviews: dict[int, list[dict[str, Any]]] = {}
        self.check_runs: dict[str, list[dict[str, Any]]] = {}  # keyed by sha
        self.statuses: dict[str, list[dict[str, Any]]] = {}  # keyed by sha
        self._next_number = 1
        self._next_id = 1
        self._tick = 0
        # (actor login, method, repo-relative path, payload) for every write.
        self.write_log: list[tuple[str, str, str, Any]] = []
        # Scripted failures: (predicate(method, path), [Response, ...]).
        self.failures: list[tuple[Callable[[str, str], bool], list[Response]]] = []
        # Fired after each successful request; lets tests interleave actors.
        self.after_request: Callable[[str, str, str], None] | None = None

    # -- test setup helpers -------------------------------------------------

    def register(self, token: str, login: str, association: str = "MEMBER") -> None:
        """Register an API actor. Pipeline machine accounts are org MEMBERs
        in production (the #52 authority boundary requires it), so that is
        the default association their comments carry."""
        self.tokens[token] = login
        self.associations[login] = association

    def create_issue(self, title: str, body: str, labels: set[str] | None = None) -> int:
        number = self._next_number
        self._next_number += 1
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": name} for name in sorted(labels or set())],
            "assignees": [],
        }
        self.comments[number] = []
        self.events[number] = []
        return number

    def labels_of(self, number: int) -> set[str]:
        return {label["name"] for label in self.issues[number]["labels"]}

    def assignees_of(self, number: int) -> list[str]:
        return [a["login"] for a in self.issues[number]["assignees"]]

    def open_pr_numbers(self) -> list[int]:
        return [n for n, pr in self.pulls.items() if pr["state"] == "open"]

    def fail_next(self, predicate: Callable[[str, str], bool], responses: list[Response]) -> None:
        self.failures.append((predicate, list(responses)))

    def _association_of(self, login: str | None) -> str:
        return self.associations.get(login or "", "NONE")

    def add_issue_comment(
        self,
        number: int,
        login: str | None,
        body: str,
        association: str | None = None,
    ) -> int:
        """A conversation comment (issue or PR) plus its mirrored timeline
        ``commented`` event — the fake keeps both surfaces in sync the way
        real GitHub does, so timeline leak-prevention is exercised on every
        comment. ``login=None`` models a deleted account (``user: null``);
        ``association=None`` looks the login up in the registry."""
        comment = {
            "id": self._next_id,
            "user": {"login": login} if login is not None else None,
            "body": body,
            "created_at": self._timestamp(),
            "author_association": (
                association if association is not None else self._association_of(login)
            ),
            "html_url": f"fake://comment/{self._next_id}",
        }
        self._next_id += 1
        self.comments.setdefault(number, []).append(comment)
        self.events.setdefault(number, []).append(
            {**comment, "event": "commented", "actor": comment["user"]}
        )
        return comment["id"]

    def add_review_comment(
        self,
        number: int,
        login: str | None,
        body: str,
        path: str,
        line: int | None = None,
        association: str | None = None,
        **fields: Any,
    ) -> int:
        """An inline review comment; ``fields`` passes anchor extras through
        verbatim (original_line, start_line, side, commit_id, in_reply_to_id,
        pull_request_review_id, ...)."""
        comment = {
            "id": self._next_id,
            "user": {"login": login} if login is not None else None,
            "body": body,
            "created_at": self._timestamp(),
            "author_association": (
                association if association is not None else self._association_of(login)
            ),
            "path": path,
            "line": line,
            "html_url": f"fake://review-comment/{self._next_id}",
            **fields,
        }
        self._next_id += 1
        self.review_comments.setdefault(number, []).append(comment)
        return comment["id"]

    def add_review(
        self,
        number: int,
        login: str | None,
        state: str,
        body: str = "",
        association: str | None = None,
    ) -> int:
        review = {
            "id": self._next_id,
            "user": {"login": login} if login is not None else None,
            "state": state,
            "body": body,
            "submitted_at": self._timestamp(),
            "author_association": (
                association if association is not None else self._association_of(login)
            ),
            "html_url": f"fake://review/{self._next_id}",
        }
        self._next_id += 1
        self.reviews.setdefault(number, []).append(review)
        return review["id"]

    def add_review_thread(
        self,
        number: int,
        comment_ids: list[int],
        resolved: bool = False,
        resolved_by: str = "",
        outdated: bool = False,
    ) -> str:
        """A review thread over existing review-comment ids (resolution and
        grouping live only in GraphQL on real GitHub, and here too)."""
        thread_id = f"RT_{self._next_id}"
        self._next_id += 1
        self.review_threads.setdefault(number, []).append(
            {
                "id": thread_id,
                "isResolved": resolved,
                "isOutdated": outdated,
                "resolvedBy": {"login": resolved_by} if resolved_by else None,
                "comment_ids": list(comment_ids),
            }
        )
        return thread_id

    def add_check_run(
        self,
        sha: str,
        name: str,
        status: str,
        conclusion: str = "",
        started_at: str = "",
        completed_at: str = "",
    ) -> int:
        run_id = self._next_id
        self._next_id += 1
        self.check_runs.setdefault(sha, []).append(
            {
                "id": run_id,
                "name": name,
                "status": status,
                "conclusion": conclusion,
                "started_at": started_at,
                "completed_at": completed_at,
                "html_url": "",
            }
        )
        return run_id

    def add_status(
        self,
        sha: str,
        context: str,
        state: str,
        description: str = "",
        target_url: str = "",
        creator: str = "",
    ) -> int:
        status_id = self._next_id
        self._next_id += 1
        self.statuses.setdefault(sha, []).append(
            {
                "id": status_id,
                "context": context,
                "state": state,
                "description": description,
                "target_url": target_url,
                "created_at": self._timestamp(),
                "creator": {"login": creator} if creator else None,
            }
        )
        return status_id

    def force_assign(self, number: int, login: str) -> None:
        """Land another actor's concurrent self-assign (race simulation)."""
        self._assign(number, login)

    # -- internal state transitions -----------------------------------------

    def _timestamp(self) -> str:
        self._tick += 1
        return f"2026-07-15T00:00:00.{self._tick:06d}Z"

    def _assign(self, number: int, login: str) -> None:
        issue = self.issues[number]
        if login not in [a["login"] for a in issue["assignees"]]:
            issue["assignees"].append({"login": login})
            self.events[number].append(
                {
                    "event": "assigned",
                    "assignee": {"login": login},
                    "created_at": self._timestamp(),
                }
            )

    def _git(self, args: list[str]) -> str:
        assert self.git_dir is not None
        proc = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _head_sha(self, branch: str) -> str:
        if self.git_dir is None:
            return f"fake-sha-{branch}"
        return self._git(["rev-parse", f"refs/heads/{branch}"])

    def _pr_payload(self, number: int) -> dict[str, Any]:
        pr = self.pulls[number]
        issue = self.issues[number]
        return {
            "number": number,
            "title": issue["title"],
            "body": issue["body"],
            "state": pr["state"],
            "labels": issue["labels"],
            "head": {"ref": pr["head"], "sha": self._head_sha(pr["head"])},
            "base": {"ref": pr["base"], "sha": self._head_sha(pr["base"])},
        }

    def _pr_files(self, number: int) -> list[dict[str, Any]]:
        pr = self.pulls[number]
        if self.git_dir is None:
            return []
        numstat = self._git(
            ["diff", "--numstat", f"refs/heads/{pr['base']}...refs/heads/{pr['head']}"]
        )
        files = []
        for line in numstat.splitlines():
            added, deleted, path = line.split("\t", 2)
            patch = self._git(
                ["diff", f"refs/heads/{pr['base']}...refs/heads/{pr['head']}", "--", path]
            )
            files.append(
                {
                    "filename": path,
                    "status": "modified",
                    "additions": 0 if added == "-" else int(added),
                    "deletions": 0 if deleted == "-" else int(deleted),
                    "patch": patch,
                }
            )
        return files

    # -- the Transport interface --------------------------------------------

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> Response:
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.tokens:
            return _json_response(401, {"message": "Bad credentials"})
        actor = self.tokens[token]

        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))
        payload = json.loads(body) if body else None

        for predicate, responses in self.failures:
            if responses and predicate(method, path):
                return responses.pop(0)

        response = self._route(actor, method, path, params, payload)
        # POST /graphql carries only read-only queries (mirroring the real
        # client's writes-transcript exemption): never a logged write.
        if method != "GET" and path != "/graphql" and response.status < 400:
            self.write_log.append((actor, method, path, payload))
        if self.after_request is not None:
            self.after_request(actor, method, path)
        return response

    def _route(
        self,
        actor: str,
        method: str,
        path: str,
        params: dict[str, str],
        payload: Any,
    ) -> Response:
        prefix = f"/repos/{self.repo}"
        if path == "/user":
            return _json_response(200, {"login": actor})
        if path == "/graphql":
            return self._h_graphql(payload)
        if path == prefix:
            return _json_response(200, {"default_branch": self.default_branch})
        if not path.startswith(prefix):
            return _json_response(404, {"message": "Not Found"})
        tail = path.removeprefix(prefix)

        for pattern, handler in self._handlers():
            match = re.fullmatch(pattern, tail)
            if match:
                return handler(actor, method, match, params, payload)
        return _json_response(404, {"message": f"no fake route for {tail}"})

    def _handlers(self):
        return [
            (r"/issues", self._h_issues),
            (r"/issues/(\d+)", self._h_issue),
            (r"/issues/(\d+)/labels", self._h_labels),
            (r"/issues/(\d+)/labels/([^/]+)", self._h_label_one),
            (r"/issues/(\d+)/assignees", self._h_assignees),
            (r"/issues/(\d+)/events", self._h_events),
            (r"/issues/(\d+)/timeline", self._h_timeline),
            (r"/issues/(\d+)/comments", self._h_comments),
            (r"/pulls", self._h_pulls),
            (r"/pulls/(\d+)", self._h_pull),
            (r"/pulls/(\d+)/files", self._h_pull_files),
            (r"/pulls/(\d+)/comments", self._h_review_comments),
            (r"/pulls/(\d+)/reviews", self._h_reviews),
            (r"/commits/([^/]+)/check-runs", self._h_check_runs),
            (r"/commits/([^/]+)/statuses", self._h_statuses),
        ]

    # GraphQL page size the real queries request; the fake honors it so the
    # client's two-level pagination is exercised for real.
    _GRAPHQL_PAGE = 100

    def _graphql_comments(self, thread: dict[str, Any], cursor: str | None) -> dict[str, Any]:
        start = int(cursor) if cursor else 0
        ids = thread["comment_ids"]
        page = ids[start : start + self._GRAPHQL_PAGE]
        return {
            "pageInfo": {
                "hasNextPage": start + self._GRAPHQL_PAGE < len(ids),
                "endCursor": str(start + self._GRAPHQL_PAGE),
            },
            "nodes": [{"databaseId": cid} for cid in page],
        }

    def _graphql_thread_node(self, thread: dict[str, Any], cursor: str | None) -> dict[str, Any]:
        return {
            "id": thread["id"],
            "isResolved": thread["isResolved"],
            "isOutdated": thread["isOutdated"],
            "resolvedBy": thread["resolvedBy"],
            "comments": self._graphql_comments(thread, cursor),
        }

    def _h_graphql(self, payload: Any) -> Response:
        """The two read-only queries the client sends: the reviewThreads
        page walk and the per-thread comment page walk."""
        query = (payload or {}).get("query") or ""
        variables = (payload or {}).get("variables") or {}
        if "reviewThreads" in query:
            threads = self.review_threads.get(variables.get("number"), [])
            start = int(variables["cursor"]) if variables.get("cursor") else 0
            page = threads[start : start + self._GRAPHQL_PAGE]
            connection = {
                "pageInfo": {
                    "hasNextPage": start + self._GRAPHQL_PAGE < len(threads),
                    "endCursor": str(start + self._GRAPHQL_PAGE),
                },
                "nodes": [self._graphql_thread_node(t, None) for t in page],
            }
            return _json_response(
                200,
                {"data": {"repository": {"pullRequest": {"reviewThreads": connection}}}},
            )
        if "PullRequestReviewThread" in query:
            wanted = variables.get("id")
            for threads in self.review_threads.values():
                for thread in threads:
                    if thread["id"] == wanted:
                        return _json_response(
                            200,
                            {
                                "data": {
                                    "node": {
                                        "comments": self._graphql_comments(
                                            thread, variables.get("cursor")
                                        )
                                    }
                                }
                            },
                        )
            return _json_response(200, {"data": {"node": None}})
        return _json_response(200, {"errors": [{"message": f"unsupported query: {query[:80]}"}]})

    @staticmethod
    def _page(items: list[Any], params: dict[str, str]) -> list[Any]:
        per_page = int(params.get("per_page", "30"))
        page = int(params.get("page", "1"))
        return items[(page - 1) * per_page : page * per_page]

    def _issue_payload(self, number: int) -> dict[str, Any]:
        data = dict(self.issues[number])
        if number in self.pulls:
            data["pull_request"] = {"url": f"fake://pulls/{number}"}
        return data

    def _h_issues(self, actor, method, match, params, payload) -> Response:
        wanted = {name for name in params.get("labels", "").split(",") if name}
        state = params.get("state", "open")
        items = [
            self._issue_payload(n)
            for n, issue in sorted(self.issues.items())
            if issue["state"] == state and wanted <= {la["name"] for la in issue["labels"]}
        ]
        return _json_response(200, self._page(items, params))

    def _h_issue(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        if number not in self.issues:
            return _json_response(404, {"message": "Not Found"})
        return _json_response(200, self._issue_payload(number))

    def _h_labels(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        issue = self.issues[number]
        if method == "POST":
            present = {la["name"] for la in issue["labels"]}
            for name in payload["labels"]:
                if name not in present:
                    issue["labels"].append({"name": name})
            return _json_response(200, issue["labels"])
        return _json_response(200, issue["labels"])

    def _h_label_one(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        name = urllib.parse.unquote(match.group(2))
        issue = self.issues[number]
        if method == "DELETE":
            before = len(issue["labels"])
            issue["labels"] = [la for la in issue["labels"] if la["name"] != name]
            if len(issue["labels"]) == before:
                return _json_response(404, {"message": "Label does not exist"})
            return _json_response(200, issue["labels"])
        return _json_response(404, {"message": "Not Found"})

    def _h_assignees(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        issue = self.issues[number]
        if method == "POST":
            for login in payload["assignees"]:
                self._assign(number, login)
            return _json_response(201, self._issue_payload(number))
        if method == "DELETE":
            gone = set(payload["assignees"])
            issue["assignees"] = [a for a in issue["assignees"] if a["login"] not in gone]
            return _json_response(200, self._issue_payload(number))
        return _json_response(404, {"message": "Not Found"})

    def _h_events(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        return _json_response(200, self._page(self.events[number], params))

    def _h_timeline(self, actor, method, match, params, payload) -> Response:
        # The fake's event log doubles as the timeline: same chronological
        # per-issue records, served through the timeline endpoint's shape.
        number = int(match.group(1))
        return _json_response(200, self._page(self.events[number], params))

    def _h_comments(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        if method == "POST":
            comment_id = self.add_issue_comment(number, actor, payload["body"])
            (comment,) = [c for c in self.comments[number] if c["id"] == comment_id]
            return _json_response(201, comment)
        return _json_response(200, self._page(self.comments[number], params))

    def _h_review_comments(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        return _json_response(200, self._page(self.review_comments.get(number, []), params))

    def _h_reviews(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        return _json_response(200, self._page(self.reviews.get(number, []), params))

    def _h_check_runs(self, actor, method, match, params, payload) -> Response:
        ref = urllib.parse.unquote(match.group(1))
        items = self.check_runs.get(ref, [])
        page = self._page(items, params)
        return _json_response(200, {"total_count": len(items), "check_runs": page})

    def _h_statuses(self, actor, method, match, params, payload) -> Response:
        ref = urllib.parse.unquote(match.group(1))
        return _json_response(200, self._page(self.statuses.get(ref, []), params))

    def _h_pulls(self, actor, method, match, params, payload) -> Response:
        if method == "POST":
            head = payload["head"]
            for pr in self.pulls.values():
                if pr["state"] == "open" and pr["head"] == head:
                    return _json_response(
                        422, {"message": f"A pull request already exists for {head}."}
                    )
            number = self._next_number
            self._next_number += 1
            self.issues[number] = {
                "number": number,
                "title": payload["title"],
                "body": payload.get("body") or "",
                "state": "open",
                "labels": [],
                "assignees": [],
            }
            self.comments[number] = []
            self.events[number] = []
            self.pulls[number] = {"state": "open", "head": head, "base": payload["base"]}
            return _json_response(201, self._pr_payload(number))
        # GET /pulls?state=open&head=owner:branch
        state = params.get("state", "open")
        head_filter = params.get("head", "")
        branch = head_filter.split(":", 1)[1] if ":" in head_filter else head_filter
        items = [
            self._pr_payload(n)
            for n, pr in sorted(self.pulls.items())
            if pr["state"] == state and (not branch or pr["head"] == branch)
        ]
        return _json_response(200, items)

    def _h_pull(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        if number not in self.pulls:
            return _json_response(404, {"message": "Not Found"})
        if method == "PATCH":
            if "title" in payload:
                self.issues[number]["title"] = payload["title"]
            if "body" in payload:
                self.issues[number]["body"] = payload["body"]
            return _json_response(200, self._pr_payload(number))
        return _json_response(200, self._pr_payload(number))

    def _h_pull_files(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        return _json_response(200, self._page(self._pr_files(number), params))
