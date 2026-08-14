"""ContainerSession ↔ harness: both ends of the job-dir protocol, live.

The harness runs in a thread (in place of a container) with a fake agent
launcher; the driver-side ContainerSession talks to it purely through
job-dir files — exactly the production seam, minus docker.
"""

from __future__ import annotations

import threading
from pathlib import Path

from theozolith_worker import jobdir
from theozolith_worker.containers import ContainerSpec
from theozolith_worker.harness.main import run_harness
from theozolith_worker.sessions import ContainerSession, SessionError
from theozolith_worker.shell import run_shell


def _runner(command: str, cwd: Path, timeout: float):
    return run_shell(command, cwd, timeout)


class _InstantAgent:
    """A headless agent that has already exited 0."""

    def poll(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _instant_launcher(argv, cwd, env, transcript):
    return _InstantAgent()


class ThreadEngine:
    """Runs the real harness in a thread when the driver launches."""

    def __init__(self, job: Path):
        self.job = job
        self.threads: dict[str, threading.Thread] = {}
        self.removed: list[str] = []

    def launch(self, spec: ContainerSpec) -> None:
        thread = threading.Thread(
            target=run_harness,
            args=(self.job, _instant_launcher),
            kwargs={"runner": _runner, "poll_seconds": 0.01},
            daemon=True,
        )
        self.threads[spec.name] = thread
        thread.start()

    def alive(self, name: str) -> bool:
        thread = self.threads.get(name)
        return thread is not None and thread.is_alive()

    def wait(self, name: str, timeout: float) -> int | None:
        thread = self.threads.get(name)
        if thread is None:
            return 0
        thread.join(timeout)
        return None if thread.is_alive() else 0

    def remove(self, name: str) -> None:
        self.removed.append(name)


def make_run_job(tmp_path: Path) -> tuple[Path, jobdir.Manifest, ContainerSpec]:
    job = jobdir.create_job_dir(tmp_path / "jobs", "r1")
    (job / jobdir.CHECKOUT_DIR).mkdir()
    (job / jobdir.PROMPT_FILE).write_text("implement it\n")
    manifest = jobdir.Manifest(
        run_id="r1",
        mode=jobdir.MODE_RUN,
        adapter="claude",
        agent_timeout_seconds=20.0,
    )
    jobdir.write_manifest(job, manifest)
    spec = ContainerSpec(name="ozolith-run-r1", image="img")
    return job, manifest, spec


def test_container_session_full_protocol_roundtrip(tmp_path):
    job, manifest, spec = make_run_job(tmp_path)
    engine = ThreadEngine(job)
    session = ContainerSession(engine, spec, job, manifest, poll_seconds=0.01)

    session.launch()
    outcome = session.wait_for_agent()
    assert outcome.completed

    ok, _ = session.run_job("gate", "printf hi > marker.txt", 10.0)
    assert ok
    assert (job / jobdir.CHECKOUT_DIR / "marker.txt").read_text() == "hi"
    ok, output = session.run_job("gate", "echo broken >&2; exit 3", 10.0)
    assert not ok and "broken" in output

    session.finish()
    assert not engine.alive(spec.name)  # shutdown request ended the harness
    # Removed twice: the stale-name clear at launch, then the finish() sweep
    # (container lifetime = Run lifetime).
    assert engine.removed == [spec.name, spec.name]
    status = jobdir.read_status(job)
    assert status is not None and status.phase == jobdir.PHASE_DONE


def test_wait_for_agent_raises_when_container_dies_silently(tmp_path):
    job, manifest, spec = make_run_job(tmp_path)

    class DeadEngine:
        def alive(self, name):
            return False

        def remove(self, name):
            pass

        def wait(self, name, timeout):
            return 0

        def launch(self, spec):
            pass

    session = ContainerSession(DeadEngine(), spec, job, manifest, poll_seconds=0.01)
    session.launch()
    try:
        session.wait_for_agent()
        raise AssertionError("expected SessionError")
    except SessionError as exc:
        assert "exited before" in str(exc)
