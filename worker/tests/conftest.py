"""End-to-end harness: real client + real git remote + in-memory GitHub.

The pipeline under test is the real thing except at two edges: the GitHub
REST transport is FakeGitHub, and the agent adapters are scripted. Branches,
clones, pushes, resets, and cherry-picks all hit a genuine bare repo.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fakegithub import FakeGitHub
from theozolith_worker.adapters import AdapterResult
from theozolith_worker.bootstrap.vocabulary import PLAN_READY
from theozolith_worker.config import ActorConfig, load_config
from theozolith_worker.evidence import EVIDENCE_BRANCH
from theozolith_worker.githubapi import GitHubClient
from theozolith_worker.prefilter import NullPrefilter
from theozolith_worker.reviewer import run_reviewer
from theozolith_worker.worker import run_worker

WORKER_LOGIN = "ozolith-worker-a"
REVIEWER_LOGIN = "ozolith-reviewer"


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def write_decisions(cwd: Path, **kwargs) -> None:
    """Write the agent-side decisions file the way a well-behaved agent does."""
    payload = {
        "decisions": [],
        "open_questions": [],
        "remaining_work": [],
        "dead_ends": [],
        **kwargs,
    }
    target = cwd / ".theozolith" / "decisions.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))


def behavior_write(files: dict[str, str], **decisions_kwargs) -> Callable[[str, Path], None]:
    def behavior(prompt: str, cwd: Path) -> None:
        for relpath, content in files.items():
            target = cwd / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        write_decisions(cwd, **decisions_kwargs)

    return behavior


class ScriptedWorkerAdapter:
    """Behaviors consumed one per Run; the default writes a trivial change."""

    name = "scripted"

    def __init__(self):
        self.behaviors: list[Callable[[str, Path], None]] = []
        self.calls: list[tuple[str, str]] = []  # (prompt, cwd)

    def execute(self, prompt: str, cwd: Path) -> AdapterResult:
        self.calls.append((prompt, str(cwd)))
        behavior = (
            self.behaviors.pop(0)
            if self.behaviors
            else behavior_write(
                {"change.txt": f"run {len(self.calls)}\n"},
                decisions=[{"what": "made the change", "why": "the issue asked"}],
            )
        )
        behavior(prompt, cwd)
        return AdapterResult(ok=True, text="done", transcript=f"transcript {len(self.calls)}")

    def complete(self, prompt: str) -> AdapterResult:
        raise AssertionError("the Worker adapter never reviews")


class ScriptedReviewerAdapter:
    """Model verdicts consumed one per review; dicts or callables(prompt)."""

    name = "scripted"

    def __init__(self):
        self.replies: list[dict | Callable[[str], dict]] = []
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> AdapterResult:
        self.prompts.append(prompt)
        assert self.replies, "reviewer adapter called without a scripted reply"
        reply = self.replies.pop(0)
        data = reply(prompt) if callable(reply) else reply
        return AdapterResult(ok=True, text=json.dumps(data), transcript="")

    def execute(self, prompt: str, cwd: Path) -> AdapterResult:
        raise AssertionError("the Reviewer never implements")


@dataclass
class Harness:
    fake: FakeGitHub
    remote: Path
    worker_config: ActorConfig
    reviewer_config: ActorConfig
    worker_client: GitHubClient
    reviewer_client: GitHubClient
    worker_adapter: ScriptedWorkerAdapter = field(default_factory=ScriptedWorkerAdapter)
    reviewer_adapter: ScriptedReviewerAdapter = field(default_factory=ScriptedReviewerAdapter)
    worker_sleeps: list[float] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    # -- driving the actors -------------------------------------------------

    def worker_once(self, prefilter=None, client: GitHubClient | None = None) -> int:
        return run_worker(
            self.worker_config,
            client or self.worker_client,
            self.worker_adapter,
            prefilter or NullPrefilter(),
            once=True,
            log=self.logs.append,
        )

    def reviewer_once(self) -> int:
        return run_reviewer(
            self.reviewer_config,
            self.reviewer_client,
            self.reviewer_adapter,
            once=True,
            log=self.logs.append,
        )

    # -- GitHub-side helpers --------------------------------------------------

    def file_issue(self, title: str, body: str, risk: str = "medium") -> int:
        """A human files an issue and approves the plan (plan_ready + risk)."""
        return self.fake.create_issue(title, body, {PLAN_READY, f"risk:{risk}"})

    def human_comment(self, number: int, body: str) -> None:
        self.fake.comments[number].append(
            {
                "id": 90_000 + len(self.fake.comments[number]),
                "user": {"login": "sean"},
                "body": body,
                "created_at": self.fake._timestamp(),
            }
        )

    def human_requeue(self, issue_number: int, pr_number: int) -> None:
        """The human answers a blocked PR and re-queues the issue."""
        issue = self.fake.issues[issue_number]
        issue["assignees"] = []
        issue["labels"] = [label for label in issue["labels"] if label["name"] != "in_progress"] + [
            {"name": PLAN_READY}
        ]
        pr = self.fake.issues[pr_number]
        pr["labels"] = [
            label for label in pr["labels"] if label["name"] not in ("blocked", "needs_human")
        ]

    # -- git-side helpers -----------------------------------------------------

    def remote_sha(self, branch: str) -> str:
        args = ["--git-dir", str(self.remote), "rev-parse", f"refs/heads/{branch}"]
        return _git(args, self.remote)

    def remote_file(self, branch: str, path: str) -> str:
        args = ["--git-dir", str(self.remote), "show", f"refs/heads/{branch}:{path}"]
        return _git(args, self.remote)

    def remote_paths(self, branch: str) -> set[str]:
        listing = _git(
            ["--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", f"refs/heads/{branch}"],
            self.remote,
        )
        return set(listing.splitlines())

    def evidence_paths(self) -> set[str]:
        try:
            return self.remote_paths(EVIDENCE_BRANCH)
        except subprocess.CalledProcessError:
            return set()


def make_harness(tmp_path: Path, gate_toml: str | None = None) -> Harness:
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "--quiet", "--initial-branch", "main", str(remote)], tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "--quiet", "--initial-branch", "main"], seed)
    (seed / "README.md").write_text("# sandbox\n")
    if gate_toml is not None:
        gate_path = seed / ".theozolith" / "gate.toml"
        gate_path.parent.mkdir(parents=True)
        gate_path.write_text(gate_toml)
    _git(["add", "--all"], seed)
    identity = ["-c", "user.name=seed", "-c", "user.email=seed@example.com"]
    _git([*identity, "commit", "--quiet", "-m", "seed"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    _git(["push", "--quiet", "origin", "main"], seed)

    fake = FakeGitHub(git_dir=remote)
    fake.register("tok-worker-a", WORKER_LOGIN)
    fake.register("tok-reviewer", REVIEWER_LOGIN)

    base_env = {
        "THEOZOLITH_REPO": fake.repo,
        "THEOZOLITH_CLONE_URL": f"file://{remote}",
        "THEOZOLITH_POLL_SECONDS": "0",
        "THEOZOLITH_WORKDIR": str(tmp_path / "runs"),
        "THEOZOLITH_WORKER_ID": "worker-a",
    }
    worker_config = load_config(
        {**base_env, "GITHUB_TOKEN": "tok-worker-a", "THEOZOLITH_RECYCLE_RUNS": "10"},
        role="worker",
    )
    reviewer_config = load_config({**base_env, "GITHUB_TOKEN": "tok-reviewer"}, role="reviewer")

    harness = Harness(
        fake=fake,
        remote=remote,
        worker_config=worker_config,
        reviewer_config=reviewer_config,
        worker_client=GitHubClient(fake.repo, "tok-worker-a", transport=fake, sleep=lambda s: None),
        reviewer_client=GitHubClient(
            fake.repo, "tok-reviewer", transport=fake, sleep=lambda s: None
        ),
    )
    harness.worker_client._sleep = harness.worker_sleeps.append  # type: ignore[attr-defined]
    return harness


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """Sandbox repo with a green one-step gate (the common case)."""
    return make_harness(
        tmp_path,
        gate_toml='[steps.test]\nrun = "test -f README.md"\n',
    )
