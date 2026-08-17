"""End-to-end harness: real drivers + real git remote + in-memory GitHub.

The pipeline under test is the real thing except at three edges: the GitHub
REST transport is FakeGitHub, run containers are replaced by an in-process
FakeSession speaking the same job-dir protocol at the driver's session seam,
and the agents are scripted. Branches, clones, pushes, resets, and
cherry-picks all hit a genuine bare repo; gate jobs execute for real in the
checkout under the container's (credential-free) environment.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fakegithub import FakeGitHub
from theozolith_worker import jobdir, proposal
from theozolith_worker.bootstrap.vocabulary import FAILED, IN_PROGRESS, PLAN_READY
from theozolith_worker.config import DriverConfig, load_config
from theozolith_worker.containers import ContainerSpec
from theozolith_worker.evidence import EVIDENCE_BRANCH
from theozolith_worker.formatoutput import format_output_main
from theozolith_worker.githubapi import GitHubClient
from theozolith_worker.implementer import Implementer
from theozolith_worker.jobdir import AgentOutcome, Manifest
from theozolith_worker.reviewer import Reviewer
from theozolith_worker.sessions import SessionError
from theozolith_worker.shell import run_shell

WORKER_LOGIN = "ozolith-worker-a"
REVIEWER_LOGIN = "ozolith-reviewer"

# What a run container's process environment looks like from the inside:
# whatever the driver put in the ContainerSpec, plus the image basics —
# and nothing from the driver's own environment.
CONTAINER_BASE_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/home/ozolith"}


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def format_output(job: Path, name: str, value) -> None:
    """One field written the way a well-behaved agent does: through the real
    format-output CLI code path (write-time rules included)."""
    text = value if isinstance(value, str) else json.dumps(value)
    code = format_output_main(["--job", str(job), name, text])
    assert code == 0, f"format-output refused {name}: exit {code}"


def write_proposal(cwd: Path, *, skip: set[str] | None = None, **fields) -> None:
    """Fill the Run's Output Proposal from inside the scripted agent.

    ``cwd`` is the session workdir (job/checkout, like the container's);
    the job dir is its parent. Required fields get sane defaults unless
    named in ``skip`` (how tests script an incomplete proposal); kwargs use
    Python names (pr_title, commit_message, decisions, ...)."""
    job = cwd.parent
    skip = skip or set()
    defaults = {
        "pr-title": "scripted change",
        "pr-description": "What the scripted agent did and why.",
        "commit-message": "scripted change\n\nWhat changed and why, decisions, dead ends.",
    }
    values = {**defaults, **{key.replace("_", "-"): value for key, value in fields.items()}}
    for name, value in values.items():
        if name in skip or value in (None, [], ""):
            continue
        format_output(job, name, value)


# A Run behavior: (prompt, checkout) -> AgentOutcome | None (None = completed).
RunBehavior = Callable[[str, Path], AgentOutcome | None]


@dataclass
class IdentityFailure:
    """A scripted reviewer reply that makes the session die the way a real
    identity-monitor kill does (ADR-0045): the harness wrote its redacted
    identity.json, status.json carries the anchored ``identity:`` marker, and
    the driver sees it as a SessionError from wait_for_agent."""

    detail: str = (
        "[substituted] a main-agent turn executed on 'claude-opus-5',"
        " not the baked 'claude-sonnet-5'"
    )
    record: dict = field(
        default_factory=lambda: {
            "expected_model": "claude-sonnet-5",
            "expected_effort": "",
            "checks": "passed",
            "category": "substituted",
            "detail": "",
            "observed_model": "claude-opus-5",
            "observed_effort": "",
            "violation": (
                "a main-agent turn executed on 'claude-opus-5', not the baked 'claude-sonnet-5'"
            ),
            "notes": [],
        }
    )


@dataclass
class SchemaSkew:
    """A scripted reviewer reply that makes the session refuse the way a
    real schema-version mismatch does (ADR-0046): the harness asserts the
    manifest's stamp strictly BEFORE the agent launches — no prompt is
    consumed, no transcript exists — records the anchored refusal in
    status.json, and the driver sees a SessionError from wait_for_agent."""

    stamped: int = 0  # the manifest stamp the refusal reports (0 = unstamped)


class RecordingSink:
    """Collects driver events; the default 'Control Node is fine' sink."""

    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event: dict) -> bool:
        self.events.append(event)
        return True

    def run_phases(self, issue: int | None = None) -> list[str]:
        return [e["phase"] for e in self.run_events(issue)]

    def run_events(self, issue: int | None = None) -> list[dict]:
        return [
            e
            for e in self.events
            if e["type"] == "theozolith.run" and (issue is None or e["issue"] == issue)
        ]


class FakeDispatch:
    """The Control Node's dispatch in miniature (ADR-0017): write-through
    claim creation applied directly to the fake GitHub state, so the driver
    under test receives issues exactly as production does — already
    assigned, already in_progress, plan_ready gone. `paused` simulates an
    unreachable Control Node (claims and review rounds pause)."""

    def __init__(self, fake: FakeGitHub):
        self.fake = fake
        self.paused = False

    def request_work(self, worker: str, node: str, login: str) -> dict | None:
        if self.paused:
            return None
        for number, issue in sorted(self.fake.issues.items()):
            if number in self.fake.pulls or issue["state"] != "open":
                continue
            labels = self.fake.labels_of(number)
            if PLAN_READY not in labels or FAILED in labels:
                continue
            if IN_PROGRESS in labels or issue["assignees"]:
                continue
            self.fake.force_assign(number, login)
            issue["labels"] = [la for la in issue["labels"] if la["name"] != PLAN_READY] + [
                {"name": IN_PROGRESS}
            ]
            return {
                "number": number,
                "title": issue["title"],
                "body": issue["body"],
                "labels": sorted(self.fake.labels_of(number)),
            }
        return None

    def review_targets(self, worker: str, node: str, login: str) -> list[int] | None:
        if self.paused:
            return None
        return [
            number
            for number in sorted(self.fake.pulls)
            if self.fake.pulls[number]["state"] == "open"
            and "pr_ready" in self.fake.labels_of(number)
            and not {"needs_human", "blocked"} & self.fake.labels_of(number)
        ]


def behavior_write(files: dict[str, str], **proposal_kwargs) -> RunBehavior:
    def behavior(prompt: str, cwd: Path) -> None:
        for relpath, content in files.items():
            target = cwd / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        write_proposal(cwd, **proposal_kwargs)

    return behavior


@dataclass
class SessionRecord:
    """What containers the drivers created and whether they were removed."""

    specs: list[ContainerSpec] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def alive(self) -> set[str]:
        return set(self.launched) - set(self.removed)

    @property
    def work_launched(self) -> list[str]:
        """Run/review containers only — the per-boot identity dry-run
        container (ADR-0045) is setup, not work."""
        return [name for name in self.launched if not name.startswith("ozolith-identity-")]


class FakeSession:
    """In-process stand-in for ContainerSession: same seam, same job dir.

    The scripted "agent" acts on the real checkout/workspace; job commands
    (gate steps) run in a real shell with the container's env — which is how
    the credential-isolation test can prove there is nothing to exfiltrate.
    """

    def __init__(self, spec: ContainerSpec, job: Path, manifest: Manifest, harness: Harness):
        self.spec = spec
        self.job = job
        self.manifest = manifest
        self.harness = harness
        self.workdir = job / manifest.workdir

    def _container_env(self) -> dict[str, str]:
        return {**CONTAINER_BASE_ENV, **self.spec.env}

    def launch(self) -> None:
        self.harness.record.specs.append(self.spec)
        self.harness.record.launched.append(self.spec.name)

    def _write_transcript(self, prompt: str) -> None:
        # The structured output stream of a headless session (ADR-0019):
        # line-per-event JSON, with the usage records real adapters emit.
        transcript = self.job / jobdir.TRANSCRIPT_FILE
        transcript.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": self.manifest.run_id}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "tool_use", "name": "Edit", "input": {}},
                        ],
                        "usage": {"input_tokens": 100, "output_tokens": 40},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "usage": {"input_tokens": 120, "output_tokens": 60},
                }
            ),
        ]
        transcript.write_text("\n".join(lines) + "\n")

    def wait_for_agent(self) -> AgentOutcome:
        if self.manifest.mode == jobdir.MODE_DRYRUN:
            # The setup dry-run (ADR-0045): no task file, no workdir — the
            # fake passes it the way a healthy image does.
            jobdir.write_identity(self.job, {"dry_run": "passed", "expected_model": ""})
            return AgentOutcome(completed=True)
        if (
            self.manifest.mode == jobdir.MODE_REVIEW
            and self.harness.reviewer_replies
            and isinstance(self.harness.reviewer_replies[0], SchemaSkew)
        ):
            # The pre-work schema assert (ADR-0046) refuses before the agent
            # exists: no prompt consumed, no transcript, no model call. Like
            # the real harness, the refusal lands in status.json first.
            skew = self.harness.reviewer_replies.pop(0)
            error = proposal.schema_mismatch(skew.stamped)
            jobdir.write_status(self.job, jobdir.Status(phase=jobdir.PHASE_FAILED, error=error))
            raise SessionError(f"harness failed: {error}")
        prompt = (self.job / jobdir.PROMPT_FILE).read_text()
        self._write_transcript(prompt)
        if self.manifest.mode == jobdir.MODE_RUN:
            self.harness.worker_calls.append((prompt, str(self.workdir)))
            if self.harness.worker_behaviors:
                behavior = self.harness.worker_behaviors.pop(0)
            else:
                behavior = behavior_write(
                    {"change.txt": f"run {len(self.harness.worker_calls)}\n"},
                    decisions=[{"what": "made the change", "why": "the issue asked"}],
                )
            outcome = behavior(prompt, self.workdir)
            return outcome or AgentOutcome(completed=True)

        self.harness.reviewer_prompts.append(prompt)
        assert self.harness.reviewer_replies, "review round started without a scripted reply"
        reply = self.harness.reviewer_replies.pop(0)
        if isinstance(reply, IdentityFailure):
            jobdir.write_identity(self.job, reply.record)
            raise SessionError(f"harness failed: identity: {reply.detail}")
        if callable(reply):
            reply = reply(prompt, self.workdir)
        if isinstance(reply, str):
            # A garbled hand-written proposal: the bind mount makes the file
            # agent-writable, so the driver must survive raw bytes (ADR-0046:
            # the CLI is convenience, never the trust boundary).
            target = self.job / proposal.PROPOSAL_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(reply)
        elif reply is not None:
            for key, value in reply.items():
                if value in (None, "", []):
                    continue  # an unset field is omitted, never written empty
                self._write_review_field(key.replace("_", "-"), value)
        return AgentOutcome(completed=True)

    def _write_review_field(self, name: str, value) -> None:
        """The real CLI write path when the value is legal; a direct file
        poke (what a misbehaving agent can always do) when the CLI refuses —
        so scripted invalid verdicts still reach the driver's validation."""
        manifest = jobdir.read_manifest(self.job)
        text = value if isinstance(value, str) else json.dumps(value)
        try:
            proposal.write_field(self.job, manifest, name, text)
        except proposal.ProposalError:
            raw = proposal.read_raw(self.job) or {
                "schema_version": proposal.SCHEMA_VERSION,
                "mode": manifest.mode,
                "fields": {},
            }
            raw.setdefault("fields", {})[name] = value
            jobdir.atomic_write(self.job / proposal.PROPOSAL_FILE, json.dumps(raw))

    def run_job(self, name: str, command: str, timeout: float) -> tuple[bool, str]:
        self.harness.gate_jobs.append(command)
        ok, _, output = run_shell(command, self.workdir, timeout, env=self._container_env())
        return ok, output

    def finish(self) -> None:
        self.harness.record.removed.append(self.spec.name)


@dataclass
class Harness:
    fake: FakeGitHub
    remote: Path
    worker_config: DriverConfig
    reviewer_config: DriverConfig
    worker_client: GitHubClient
    reviewer_client: GitHubClient
    dispatch: FakeDispatch
    sink: RecordingSink = field(default_factory=RecordingSink)
    record: SessionRecord = field(default_factory=SessionRecord)
    worker_behaviors: list[RunBehavior] = field(default_factory=list)
    worker_calls: list[tuple[str, str]] = field(default_factory=list)  # (prompt, workdir)
    reviewer_replies: list = field(default_factory=list)  # dict | str | None | callable
    reviewer_prompts: list[str] = field(default_factory=list)
    gate_jobs: list[str] = field(default_factory=list)
    worker_sleeps: list[float] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    # -- driving the drivers --------------------------------------------------

    def session_factory(self, spec: ContainerSpec, job: Path, manifest: Manifest) -> FakeSession:
        return FakeSession(spec, job, manifest, self)

    def worker_once(self, client: GitHubClient | None = None, sink=None) -> int:
        return Implementer(
            self.worker_config,
            client=client or self.worker_client,
            session_factory=self.session_factory,
            dispatch=self.dispatch,
            log=self.logs.append,
            sink=sink or self.sink,
        ).run(once=True)

    def reviewer_once(self, sink=None) -> int:
        return Reviewer(
            self.reviewer_config,
            client=self.reviewer_client,
            session_factory=self.session_factory,
            dispatch=self.dispatch,
            log=self.logs.append,
            sink=sink or self.sink,
        ).run(once=True)

    # -- GitHub-side helpers --------------------------------------------------

    def file_issue(self, title: str, body: str, risk: str = "medium") -> int:
        """A human files an issue and approves the plan (plan_ready + risk)."""
        return self.fake.create_issue(title, body, {PLAN_READY, f"risk:{risk}"})

    def human_comment(self, number: int, body: str) -> None:
        """The repository owner comments (an authorized author, #52)."""
        self.fake.add_issue_comment(number, "sean", body, association="OWNER")

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

    def evidence_file(self, path: str) -> str:
        return self.remote_file(EVIDENCE_BRANCH, path)


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
        "THEOZOLITH_JOBS_DIR": str(tmp_path / "jobs"),
        "THEOZOLITH_MIRRORS_DIR": str(tmp_path / "mirrors"),
        "THEOZOLITH_WORKER_ID": "worker-a",
        # Required since ADR-0017 (no second claim path); tests inject a
        # FakeDispatch and a RecordingSink, so nothing dials this URL.
        "CONTROL_NODE_URL": "https://control.invalid:8443",
        "THEOZOLITH_NODE_TOKEN": "node-token",
        # The node identity events declare; must match the per-node token's
        # node when a live Control Node is in play (ADR-0023).
        "THEOZOLITH_NODE_NAME": "box1",
        # The model API key is ALLOWED in run containers (a rotatable spend
        # credential); the GitHub PATs are not (ADR-0013).
        "ANTHROPIC_API_KEY": "model-key",
    }
    worker_config = load_config({**base_env, "GITHUB_TOKEN": "tok-worker-a"}, role=Implementer.role)
    reviewer_config = load_config({**base_env, "GITHUB_TOKEN": "tok-reviewer"}, role=Reviewer.role)

    harness = Harness(
        fake=fake,
        remote=remote,
        worker_config=worker_config,
        reviewer_config=reviewer_config,
        worker_client=GitHubClient(fake.repo, "tok-worker-a", transport=fake, sleep=lambda s: None),
        reviewer_client=GitHubClient(
            fake.repo, "tok-reviewer", transport=fake, sleep=lambda s: None
        ),
        dispatch=FakeDispatch(fake),
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
