"""Minimal GitHub REST client for the bootstrap tool (stdlib only).

The bootstrap core talks to the small RepoClient interface below; tests
substitute an in-memory fake.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Protocol


class BootstrapError(RuntimeError):
    """A bootstrap operation against the target repo failed."""


class RepoClient(Protocol):
    """What the bootstrap core needs from a target repo."""

    def list_labels(self) -> list[dict[str, Any]]: ...

    def create_label(self, name: str, color: str, description: str) -> None: ...

    def update_label(self, name: str, color: str, description: str) -> None: ...

    def get_file(self, path: str) -> bytes | None: ...

    def put_file(self, path: str, content: bytes, message: str) -> None: ...


class GitHubRepoClient:
    """RepoClient backed by the GitHub REST API."""

    def __init__(self, repo: str, token: str, api_url: str = "https://api.github.com"):
        if "/" not in repo:
            raise BootstrapError(f"expected --repo owner/name, got {repo!r}")
        self._base = f"{api_url.rstrip('/')}/repos/{repo}"
        self._token = token
        # Contents-API blob SHAs seen by get_file, needed to update files.
        self._file_shas: dict[str, str] = {}

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "theozolith-bootstrap",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read().decode(errors="replace")[:500]
            raise BootstrapError(f"{method} {url} failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise BootstrapError(f"{method} {url} failed: {exc.reason}") from exc
        return json.loads(payload) if payload else None

    def list_labels(self) -> list[dict[str, Any]]:
        labels: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request("GET", f"{self._base}/labels?per_page=100&page={page}")
            if not batch:
                break
            labels.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return labels

    def create_label(self, name: str, color: str, description: str) -> None:
        self._request(
            "POST",
            f"{self._base}/labels",
            {"name": name, "color": color, "description": description},
        )

    def update_label(self, name: str, color: str, description: str) -> None:
        self._request(
            "PATCH",
            f"{self._base}/labels/{urllib.request.quote(name)}",
            {"new_name": name, "color": color, "description": description},
        )

    def get_file(self, path: str) -> bytes | None:
        data = self._request("GET", f"{self._base}/contents/{urllib.request.quote(path)}")
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("type") != "file":
            raise BootstrapError(f"{path} exists in the target repo but is not a file")
        self._file_shas[path] = data["sha"]
        return base64.b64decode(data["content"])

    def put_file(self, path: str, content: bytes, message: str) -> None:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode(),
        }
        if path in self._file_shas:
            body["sha"] = self._file_shas[path]
        self._request("PUT", f"{self._base}/contents/{urllib.request.quote(path)}", body)
