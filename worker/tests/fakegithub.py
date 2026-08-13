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
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.events: dict[int, list[dict[str, Any]]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
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

    def register(self, token: str, login: str) -> None:
        self.tokens[token] = login

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
        if method != "GET" and response.status < 400:
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
            (r"/issues/(\d+)/comments", self._h_comments),
            (r"/pulls", self._h_pulls),
            (r"/pulls/(\d+)", self._h_pull),
            (r"/pulls/(\d+)/files", self._h_pull_files),
        ]

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

    def _h_comments(self, actor, method, match, params, payload) -> Response:
        number = int(match.group(1))
        if method == "POST":
            comment = {
                "id": self._next_id,
                "user": {"login": actor},
                "body": payload["body"],
                "created_at": self._timestamp(),
            }
            self._next_id += 1
            self.comments[number].append(comment)
            return _json_response(201, comment)
        return _json_response(200, self._page(self.comments[number], params))

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
