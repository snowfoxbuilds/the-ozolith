"""ContainerSession ↔ harness: both ends of the job-dir protocol, live.

The harness runs in a thread (in place of a container) with a fake agent
launcher; the driver-side ContainerSession talks to it purely through
job-dir files — exactly the production seam, minus docker.
"""

from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

from theozolith_worker import jobdir, proposal
from theozolith_worker.containers import ContainerSpec, DockerEngine, EngineError
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
        schema_version=proposal.SCHEMA_VERSION,
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


# -- fail-closed observation at the session seam (#109, grilling 2026-09-02) ------


def test_finish_contains_an_observation_failure_and_still_removes(tmp_path):
    """An unobservable container at session END must never escape finish(): a
    blip there would, via `finally: session.finish()` on the Run's SUCCESS path,
    reclassify an already-completed Run as an infra failure. The force-remove
    still runs, so container lifetime = Run lifetime is preserved regardless."""
    job, manifest, spec = make_run_job(tmp_path)  # a serve_jobs (MODE_RUN) manifest

    class BlipAtFinishEngine:
        def __init__(self):
            self.removed: list[str] = []

        def launch(self, spec):
            pass

        def alive(self, name):
            raise EngineError("aliveness unobservable at session end")

        def wait(self, name, timeout):
            return None

        def remove(self, name):
            self.removed.append(name)

    engine = BlipAtFinishEngine()
    session = ContainerSession(engine, spec, job, manifest, poll_seconds=0.01)
    session.finish()  # must NOT raise, even though alive() raises EngineError
    assert engine.removed == [spec.name]  # the force-remove still ran


def test_wait_for_agent_propagates_an_engine_error_into_the_infra_lane(tmp_path):
    """An unobservable container mid-wait raises EngineError OUT of
    wait_for_agent — it is NOT reclassified as a SessionError (a fabricated
    'container exited'). The runner's generic driver-side catch then maps the
    escaping EngineError to the ADR-0016 infra lane."""
    job, manifest, spec = make_run_job(tmp_path)

    class UnobservableEngine:
        def launch(self, spec):
            pass

        def alive(self, name):
            raise EngineError("container aliveness unobservable (observation doctrine)")

        def wait(self, name, timeout):
            return 0

        def remove(self, name):
            pass

    session = ContainerSession(UnobservableEngine(), spec, job, manifest, poll_seconds=0.01)
    session.launch()
    try:
        session.wait_for_agent()
        raise AssertionError("expected EngineError")
    except SessionError as exc:  # must NOT be swallowed/reclassified as a Run outcome
        raise AssertionError(f"EngineError was reclassified as SessionError: {exc}") from exc
    except EngineError:
        pass  # escapes as-is -> the runner maps it to the infra lane


def _scripted_docker(tmp_path: Path, plan: dict) -> Path:
    """A docker stand-in returning scripted ``[rc, stdout, stderr]`` per
    subcommand (unlisted subcommands succeed silently), so a REAL DockerEngine
    runs inside a ContainerSession with no live docker."""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    count_path = tmp_path / "counts.json"
    count_path.write_text("{}", encoding="utf-8")
    binary = tmp_path / "docker"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"plan = json.load(open({str(plan_path)!r}))\n"
        f"counts = json.load(open({str(count_path)!r}))\n"
        "sub = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "responses = plan.get(sub, [[0, '', '']])\n"
        "i = counts.get(sub, 0)\n"
        "rc, out, err = responses[min(i, len(responses) - 1)]\n"
        "counts[sub] = i + 1\n"
        f"json.dump(counts, open({str(count_path)!r}, 'w'))\n"
        "sys.stdout.write(out)\n"
        "sys.stderr.write(err)\n"
        "sys.exit(rc)\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


def test_wait_for_agent_absorbs_a_transient_inspect_blip_and_completes(tmp_path):
    """The recovery half at the ContainerSession↔engine seam, with a REAL
    DockerEngine: a transient unobservable inspect during wait_for_agent() is
    absorbed by the engine's bounded retry (it clears to a real ``true``), so the
    session NEVER mistakes the blip for a container-exit SessionError and goes on
    to read a genuinely completed agent status."""
    job, manifest, spec = make_run_job(tmp_path)
    binary = _scripted_docker(
        tmp_path,
        # inspect: a 500 blip, then recovered to `true` (still running). run/rm
        # default to success, so launch() works with no live docker.
        {"inspect": [[1, "", "Error response from daemon: 500"], [0, "true\n", ""]]},
    )
    engine = DockerEngine(binary=str(binary), alive_attempts=3, sleep=lambda _s: None)

    def land_done(_seconds):
        # Between the first poll (whose alive() weathered the blip) and the next,
        # the harness finishes: status.json lands DONE with a completed agent.
        jobdir.write_status(
            job,
            jobdir.Status(
                phase=jobdir.PHASE_DONE,
                agent=jobdir.AgentOutcome(completed=True, exit_code=0),
            ),
        )

    session = ContainerSession(engine, spec, job, manifest, sleep=land_done, poll_seconds=0.01)
    session.launch()
    outcome = session.wait_for_agent()  # must NOT raise on the transient blip
    assert outcome.completed  # a real completed agent status, read after recovery
