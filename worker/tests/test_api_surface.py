"""The stable api surface (ADR-0042): it imports clean, its ``__all__`` is a
pinned snapshot, and a custom worker type built only from api names runs.

``theozolith_worker.api`` is the sole import contract for custom drivers, so a
change to what it exports is a deliberate, release-noted event — the literal
snapshot here makes an accidental addition or removal fail loudly. The smoke
subclass proves the surface is sufficient: a new worker type extends
``api.Worker``, fills the ADR-0020 seams, and completes a scripted ``run`` pass
without importing anything private.
"""

from __future__ import annotations

from fakegithub import FakeGitHub
from theozolith_worker import api

# Pinned surface: additions/removals are release-note events (ADR-0042). Update
# this snapshot only alongside a deliberate api change. The Run Contract names
# (renderers, PR-body composition, gate sequence, proposal validation) are
# additionally ``schema_version`` surface (ADR-0054).
EXPECTED_API = [
    "APPROVE",
    "CONTAINER_JOB_PATH",
    "ESCALATE",
    "MANIFEST_FILE",
    "MODE_REVIEW",
    "MODE_RUN",
    "PROMPT_FILE",
    "PROPOSAL_FILE",
    "REVISE",
    "SCHEMA_VERSION",
    "STATUS_FILE",
    "STEP_ORDER",
    "TRANSCRIPT_FILE",
    "WORK_DIR",
    "AgentOutcome",
    "BasedOn",
    "Comment",
    "ConfigError",
    "ContainerSession",
    "ContainerSpec",
    "ContextSnapshot",
    "Decision",
    "DecisionsSection",
    "DiffSignals",
    "DispatchClient",
    "DockerEngine",
    "DriverConfig",
    "EventSink",
    "Finding",
    "GateConfigError",
    "GateResult",
    "GitHubClient",
    "Implementer",
    "Issue",
    "JobDirError",
    "JobRequest",
    "JobResult",
    "Manifest",
    "PrCommit",
    "PullRequest",
    "RepoMergeSettings",
    "Reviewer",
    "RunProposal",
    "RunReport",
    "SessionError",
    "SessionFactory",
    "Status",
    "StepSpec",
    "Verdict",
    "WorkDispatch",
    "Worker",
    "atomic_write",
    "commit_message_with_trailer",
    "compose_pr_body",
    "container_session_factory",
    "create_job_dir",
    "emit_error",
    "execute_claim",
    "git_pr_commits",
    "load_config",
    "load_steps",
    "read_manifest",
    "render_base_md",
    "render_review_prompt",
    "render_run_prompt",
    "required_fields",
    "run_driver",
    "run_event",
    "run_gate",
    "signals_from_git",
    "upsert_zone",
    "validate_review",
    "validate_run",
    "vocabulary",
    "write_manifest",
    "write_tree",
]


def test_api_all_matches_the_pinned_snapshot():
    assert api.__all__ == EXPECTED_API
    for name in api.__all__:
        assert hasattr(api, name), f"api.{name} is exported but not importable"


class _Login:
    def viewer_login(self) -> str:
        return "ozolith-smoke"


class _Dispatch:
    last_unreachable = False

    def request_work(self, *args):
        return None

    def review_targets(self, *args):
        return None


class _Sink:
    def emit(self, event) -> bool:
        return True


def test_a_worker_subclass_built_only_from_api_names_runs(tmp_path):
    """The extension surface extends: a custom worker type imports api and
    nothing else, and its loop runs to completion against fakes."""

    class Smoke(api.Worker):
        role = "implementer"  # reuse a known env prefix for load_config

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.executed: list[int] = []

        def fetch_work(self):
            return [{"n": 1}, {"n": 2}]

        def execute(self, item) -> int:
            self.executed.append(item["n"])
            return 1

    (tmp_path / "jobs").mkdir()
    config = api.load_config(
        {
            "THEOZOLITH_REPO": "acme/sandbox",
            "GITHUB_TOKEN": "tok",
            "CONTROL_NODE_URL": "https://control.invalid:8443",
            "THEOZOLITH_JOBS_DIR": str(tmp_path / "jobs"),
        },
        role=Smoke.role,
    )

    class _DryrunSession:
        # The base loop's setup dry-run (ADR-0045) commissions one session
        # per driver process; a custom worker's fakes serve it like any
        # other session seam.
        def __init__(self, job):
            self._job = job

        def launch(self):
            pass

        def wait_for_agent(self):
            return None  # a passing dry-run: no identity error raised

        def finish(self):
            pass

    worker = Smoke(
        config,
        client=_Login(),
        dispatch=_Dispatch(),
        sink=_Sink(),
        session_factory=lambda spec, job, manifest: _DryrunSession(job),
    )

    assert worker.run(once=True) == 2
    assert worker.executed == [1, 2]


# -- GitHubClient compatibility (ADR-0042) -------------------------------------
# GitHubClient is part of the stable surface, so its public read helpers are a
# custom-driver contract even when nothing in this repo calls them. Private
# Config Repo drivers are invisible to repo grep; these tests stand in for those
# consumers and fail if a method — or the fake behavior it needs — is dropped.


def _api_client(fake: FakeGitHub, token: str = "tok-a", login: str = "worker-a"):
    """A client built from the stable ``api.GitHubClient`` export, on the fake."""
    fake.register(token, login)
    return api.GitHubClient(
        fake.repo, token, transport=fake, sleep=lambda _s: None, clock=lambda: 0.0
    )


def test_github_client_exposes_default_branch_and_assign_order():
    for name in ("default_branch", "assign_order"):
        assert callable(getattr(api.GitHubClient, name)), f"api.GitHubClient.{name} is missing"


def test_default_branch_is_fetched_once_then_cached():
    fake = FakeGitHub()
    client = _api_client(fake)
    assert client.default_branch() == "main"
    # A later change to the repo default must not be observed: the second call is
    # served from the lazy cache rather than re-fetching repo metadata.
    fake.default_branch = "trunk"
    assert client.default_branch() == "main"


def test_assign_order_orders_dedupes_and_ignores_other_events():
    fake = FakeGitHub()
    client = _api_client(fake)
    number = fake.create_issue("t", "", set())
    # Two workers self-assign, earliest first.
    fake.force_assign(number, "worker-b")
    fake.force_assign(number, "worker-a")
    # A reassignment (unassign+reassign leaves a second "assigned" event) and an
    # unrelated timeline entry the method must skip.
    fake.events[number].append(
        {"event": "assigned", "assignee": {"login": "worker-b"}, "created_at": "z"}
    )
    fake.events[number].append({"event": "labeled", "label": {"name": "x"}})
    assert client.assign_order(number) == ["worker-b", "worker-a"]


def test_assign_order_pages_through_the_event_timeline():
    fake = FakeGitHub()
    client = _api_client(fake)
    number = fake.create_issue("t", "", set())
    for i in range(150):  # crosses the 100-per-page boundary
        fake.force_assign(number, f"worker-{i:03d}")
    order = client.assign_order(number)
    assert len(order) == 150
    assert order[0] == "worker-000"
    assert order[-1] == "worker-149"
