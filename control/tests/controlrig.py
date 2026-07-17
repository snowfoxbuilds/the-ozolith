"""Control Node test rig: the real app over TestClient, a fake clock, an
in-memory GitHub for the janitor/auditor, and Config Repo scaffolding.

(Named controlrig, not conftest: a module literally named `conftest` is
order-dependent across the multi-directory pytest run — worker/tests owns
that name.)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from theozolith_control.app import create_app
from theozolith_control.crypto import SecretBox, generate_key
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store
from theozolith_worker.githubapi import Issue, PullRequest

NODE_TOKEN = "node-token"
ADMIN_TOKEN = "admin-token"


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_settings(tmp_path: Path, **overrides: Any) -> ControlSettings:
    values: dict[str, Any] = dict(
        data_dir=tmp_path / "data",
        config_repo=tmp_path / "configs",
        node_token=NODE_TOKEN,
        admin_token=ADMIN_TOKEN,
        repo="acme/sandbox",
        github_token="gh-token",
        api_url="https://api.github.invalid",
        zombie_grace_seconds=600,
        janitor_sweep_seconds=60,
        activation_window_seconds=60,
        tail_budget_bytes=10 * 1024**3,
        secrets_channel_ok=True,
    )
    values.update(overrides)
    return ControlSettings(**values)


@dataclass
class ControlRig:
    settings: ControlSettings
    store: Store
    box: SecretBox
    client: TestClient
    clock: FakeClock
    github: "FakeGitHubLite"

    def node_post(self, path: str, body: dict, token: str = NODE_TOKEN):
        return self.client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})

    def admin(self, method: str, path: str, body: dict | None = None, token: str = ADMIN_TOKEN):
        return self.client.request(
            method, path, json=body, headers={"Authorization": f"Bearer {token}"}
        )

    def heartbeat(self, node: str = "box1", **overrides):
        payload: dict[str, Any] = {
            "node": node,
            "version": "0.3.0",
            "stacks": [],
            "run_containers": [],
            "images": [],
            "config_commit": "",
            "completed_commands": [],
        }
        payload.update(overrides)
        return self.node_post("/api/v1/heartbeats", payload)

    def write_config(self, relpath: str, text: str) -> None:
        target = self.settings.config_repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def dispatch(self, role: str = "worker", worker: str = "worker-a", node: str = "box1"):
        return self.node_post(
            "/api/v1/dispatch",
            {"role": role, "worker": worker, "node": node, "login": f"ozolith-{worker}"},
        )


def make_rig(tmp_path: Path, **settings_overrides: Any) -> ControlRig:
    settings = make_settings(tmp_path, **settings_overrides)
    clock = FakeClock()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path, clock=clock)
    box = SecretBox(generate_key())
    github = FakeGitHubLite()
    app = create_app(
        settings, store, box,
        github_client=github if settings.coordination_jobs_enabled else None,
    )
    return ControlRig(settings, store, box, TestClient(app), clock, github)


@pytest.fixture
def control(tmp_path: Path) -> ControlRig:
    return make_rig(tmp_path)


# -- the GitHub the dispatcher and janitor see -----------------------------------


class FakeGitHubLite:
    """Duck-typed GitHubClient surface for dispatch and the janitor. Every
    mutation lands in ``writes`` — the janitor tests prove it contains only
    the sanctioned moves; the dispatch tests prove write-through ordering."""

    def __init__(self):
        self.repo = "acme/sandbox"
        self.issues: dict[int, dict[str, Any]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}
        # Paths that exist on the evidence branch (janitor phase 2).
        self.evidence: set[str] = set()
        self.writes: list[tuple] = []

    def add_issue(self, number: int, labels: set[str], assignees: list[str]) -> None:
        self.issues[number] = {"labels": set(labels), "assignees": list(assignees)}

    def add_pr(self, number: int, head_ref: str, labels: set[str], head_sha: str = "a" * 40):
        self.pulls[number] = {"head_ref": head_ref, "labels": set(labels), "head_sha": head_sha}

    # -- reads ----------------------------------------------------------------

    def get_issue(self, number: int) -> Issue:
        data = self.issues[number]
        return Issue(
            number=number,
            title="",
            body="",
            labels=set(data["labels"]),
            assignees=list(data["assignees"]),
            is_pr=False,
        )

    def _pull(self, number: int) -> PullRequest:
        data = self.pulls[number]
        return PullRequest(
            number=number,
            title="",
            body="",
            head_ref=data["head_ref"],
            head_sha=data["head_sha"],
            base_ref="main",
            labels=set(data["labels"]),
            state="open",
        )

    def get_pull(self, number: int) -> PullRequest:
        return self._pull(number)

    def find_open_pr_by_head(self, head_ref: str) -> PullRequest | None:
        for number, data in self.pulls.items():
            if data["head_ref"] == head_ref:
                return self._pull(number)
        return None

    def list_open_prs_by_label(self, label: str) -> list[Issue]:
        return [
            Issue(number=n, title="", body="", labels=set(d["labels"]), assignees=[], is_pr=True)
            for n, d in sorted(self.pulls.items())
            if label in d["labels"]
        ]

    def list_open_issues(self, label: str) -> list[Issue]:
        return [
            self.get_issue(n) for n, d in sorted(self.issues.items()) if label in d["labels"]
        ]

    def path_exists(self, path: str, *, ref: str) -> bool:
        return path in self.evidence

    # -- writes ---------------------------------------------------------------

    def remove_assignee(self, number: int, login: str) -> None:
        self.writes.append(("remove_assignee", number, login))
        self.issues[number]["assignees"] = [
            a for a in self.issues[number]["assignees"] if a != login
        ]

    def add_assignees(self, number: int, *logins: str) -> None:
        self.writes.append(("add_assignees", number, *logins))
        self.issues[number]["assignees"].extend(logins)

    def remove_label(self, number: int, label: str) -> None:
        self.writes.append(("remove_label", number, label))
        self.issues[number]["labels"].discard(label)

    def add_labels(self, number: int, *labels: str) -> None:
        self.writes.append(("add_labels", number, *labels))
        self.issues[number]["labels"].update(labels)

    def add_comment(self, number: int, body: str) -> None:
        self.writes.append(("add_comment", number))
        self.comments.setdefault(number, []).append(body)


@pytest.fixture
def github(control: ControlRig) -> FakeGitHubLite:
    return control.github


# -- event factories (the drivers' wire shapes, ADR-0015) --------------------------


def run_event(
    issue: int,
    phase: str,
    *,
    worker: str = "worker-a",
    node: str = "box1",
    run_id: str = "r1",
    attempt: int | None = 1,
    pr: int | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "theozolith.run",
        "worker": worker,
        "node": node,
        "stack": "worker",
        "issue": issue,
        "run_id": run_id,
        "phase": phase,
    }
    if attempt is not None:
        event["attempt"] = attempt
    if pr is not None:
        event["pr"] = pr
    return event


def review_event(pr: int, issue: int, round_number: int, verdict: str) -> dict[str, Any]:
    return {
        "type": "theozolith.review",
        "reviewer": "reviewer-1",
        "node": "box1",
        "stack": "reviewer",
        "pr": pr,
        "issue": issue,
        "round": round_number,
        "verdict": verdict,
    }
