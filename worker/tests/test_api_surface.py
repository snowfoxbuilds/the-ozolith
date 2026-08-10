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

from theozolith_worker import api

# Pinned surface: additions/removals are release-note events (ADR-0042). Update
# this snapshot only alongside a deliberate api change.
EXPECTED_API = [
    "CONTAINER_JOB_PATH",
    "MANIFEST_FILE",
    "MODE_REVIEW",
    "MODE_RUN",
    "PROMPT_FILE",
    "STATUS_FILE",
    "TRANSCRIPT_FILE",
    "VERDICT_FILE",
    "WORK_DIR",
    "AgentOutcome",
    "Comment",
    "ConfigError",
    "ContainerSession",
    "ContainerSpec",
    "DispatchClient",
    "DockerEngine",
    "DriverConfig",
    "EventSink",
    "GitHubClient",
    "Implementer",
    "Issue",
    "JobDirError",
    "JobRequest",
    "JobResult",
    "Manifest",
    "PullRequest",
    "Reviewer",
    "RunReport",
    "SessionError",
    "SessionFactory",
    "Status",
    "WorkDispatch",
    "Worker",
    "atomic_write",
    "container_session_factory",
    "create_job_dir",
    "emit_error",
    "execute_claim",
    "load_config",
    "run_driver",
    "run_event",
    "vocabulary",
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
        default_model = "claude-sonnet-5"

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
        default_model=Smoke.default_model,
    )
    worker = Smoke(
        config,
        client=_Login(),
        dispatch=_Dispatch(),
        sink=_Sink(),
        session_factory=lambda spec, job, manifest: None,
    )

    assert worker.run(once=True) == 2
    assert worker.executed == [1, 2]
