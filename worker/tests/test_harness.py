"""The agent harness: completion detection, job serving, and the tmux session.

Unit tests drive the harness single-threaded with a fake tmux and scripted
clocks; the integration test at the bottom runs the real thing — real tmux,
real pipe-pane transcript, a fake agent CLI — and exercises the interactivity
contract (M2 acceptance 9): input injected into the live session mid-Run
lands in the transcript and the Run completes normally.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest
from theozolith_worker import jobdir
from theozolith_worker.harness.main import await_completion, run_harness, serve_jobs
from theozolith_worker.harness.tmux import RealTmux


class FakeTmux:
    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.transcripts: dict[str, Path] = {}
        self.killed: list[str] = []

    def new_session(self, session, command, cwd, env):
        self.sessions[session] = {"command": command, "cwd": cwd, "env": env, "alive": True}

    def pipe_pane(self, session, capture_file):
        self.transcripts[session] = Path(capture_file)

    def paste(self, session, text):
        target = self.transcripts.get(session)
        if target is not None:
            with target.open("a") as handle:
                handle.write(text)

    def has_session(self, session):
        return self.sessions.get(session, {}).get("alive", False)

    def kill(self, session):
        if session in self.sessions:
            self.sessions[session]["alive"] = False
        self.killed.append(session)


class ScriptedClock:
    """A clock advanced by sleep(); on_tick callbacks simulate the world."""

    def __init__(self):
        self.now = 0.0
        self.on_tick: list = []  # callables(now) run after every sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)
        for hook in list(self.on_tick):
            hook(self.now)


def make_job(
    tmp_path: Path,
    *,
    mode: str = jobdir.MODE_RUN,
    settle: float = 5.0,
    timeout: float = 100.0,
    events: str | None = None,
) -> tuple[Path, jobdir.Manifest]:
    job = jobdir.create_job_dir(tmp_path, "r1")
    workdir = jobdir.CHECKOUT_DIR if mode == jobdir.MODE_RUN else jobdir.WORK_DIR
    (job / workdir).mkdir(parents=True, exist_ok=True)
    manifest = jobdir.Manifest(
        run_id="r1",
        mode=mode,
        session=f"{'run' if mode == jobdir.MODE_RUN else 'review'}-r1",
        adapter="claude",
        model="claude-sonnet-5",
        workdir=workdir,
        agent_timeout_seconds=timeout,
        settle_seconds=settle,
        startup_seconds=0.0,
        jobs_idle_timeout_seconds=30.0,
    )
    jobdir.write_manifest(job, manifest)
    (job / jobdir.PROMPT_FILE).parent.mkdir(parents=True, exist_ok=True)
    (job / jobdir.PROMPT_FILE).write_text("do the thing\n")
    if events is not None:
        log = job / jobdir.HOOK_EVENTS_FILE
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(events)
    return job, manifest


def append_event(job: Path, event: str) -> None:
    log = job / jobdir.HOOK_EVENTS_FILE
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as handle:
        handle.write(f"{event}\n")


# -- completion detection -------------------------------------------------


def test_completion_settles_after_stop(tmp_path):
    job, manifest = make_job(tmp_path, settle=5.0, events="stop\n")
    tmux = FakeTmux()
    tmux.new_session(manifest.session, "agent", tmp_path, {})
    clock = ScriptedClock()

    outcome = await_completion(job, tmux, manifest, clock=clock, sleep=clock.sleep)

    assert outcome.completed and not outcome.timed_out
    assert clock.now >= 5.0  # the settle window was honored


def test_queued_human_input_rearms_the_wait(tmp_path):
    """A prompt event after stop (an attached human) must re-arm completion."""
    job, manifest = make_job(tmp_path, settle=5.0, events="stop\n")
    tmux = FakeTmux()
    tmux.new_session(manifest.session, "agent", tmp_path, {})
    clock = ScriptedClock()
    fired: list[float] = []

    def interject(now: float) -> None:
        if now >= 2.0 and not fired:
            fired.append(now)
            append_event(job, "prompt")  # human submits mid-settle

        if now >= 10.0 and len(fired) == 1:
            fired.append(now)
            append_event(job, "stop")  # the agent answers and stops again

    clock.on_tick.append(interject)
    outcome = await_completion(job, tmux, manifest, clock=clock, sleep=clock.sleep)

    assert outcome.completed
    assert clock.now >= 15.0  # second stop + a fresh settle window


def test_hard_timeout_backstop(tmp_path):
    job, manifest = make_job(tmp_path, settle=5.0, timeout=30.0)  # no events ever
    tmux = FakeTmux()
    tmux.new_session(manifest.session, "agent", tmp_path, {})
    clock = ScriptedClock()

    outcome = await_completion(job, tmux, manifest, clock=clock, sleep=clock.sleep)

    assert outcome.timed_out and not outcome.completed
    assert clock.now >= 30.0


def test_session_death_ends_the_wait(tmp_path):
    job, manifest = make_job(tmp_path, settle=5.0)
    tmux = FakeTmux()
    tmux.new_session(manifest.session, "agent", tmp_path, {})
    clock = ScriptedClock()
    clock.on_tick.append(lambda now: tmux.kill(manifest.session) if now >= 3.0 else None)

    outcome = await_completion(job, tmux, manifest, clock=clock, sleep=clock.sleep)

    assert outcome.session_died and not outcome.completed


# -- job serving ------------------------------------------------------------


def test_serve_jobs_runs_requests_then_shuts_down(tmp_path):
    job, manifest = make_job(tmp_path)
    workdir = job / manifest.workdir
    jobdir.write_job_request(job, jobdir.JobRequest("001-gate", "printf hi > marker.txt", 10.0))
    jobdir.write_job_request(job, jobdir.JobRequest("002-shutdown", ""))
    clock = ScriptedClock()

    error = serve_jobs(job, workdir, manifest, runner=_real_runner, clock=clock, sleep=clock.sleep)

    assert error == ""
    assert (workdir / "marker.txt").read_text() == "hi"
    result = jobdir.read_job_result(job, "001-gate")
    assert result is not None and result.ok


def _real_runner(command: str, cwd: Path, timeout: float):
    from theozolith_worker.shell import run_shell

    return run_shell(command, cwd, timeout)


def test_serve_jobs_idle_timeout_protects_against_dead_driver(tmp_path):
    job, manifest = make_job(tmp_path)
    clock = ScriptedClock()

    error = serve_jobs(
        job, job / manifest.workdir, manifest, runner=_real_runner, clock=clock, sleep=clock.sleep
    )

    assert "idle timeout" in error


# -- the full harness cycle (fake tmux) --------------------------------------


def test_run_mode_full_cycle(tmp_path):
    job, manifest = make_job(tmp_path, settle=0.0, events="stop\n")
    workdir = job / manifest.workdir
    jobdir.write_job_request(job, jobdir.JobRequest("001-gate", "printf ok > gate.txt", 10.0))
    jobdir.write_job_request(job, jobdir.JobRequest("002-shutdown", ""))
    tmux = FakeTmux()
    clock = ScriptedClock()

    code = run_harness(job, tmux, runner=_real_runner, clock=clock, sleep=clock.sleep)

    assert code == 0
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE and status.agent.completed
    # The interactive session: correct command, prompt pasted, then killed.
    session = tmux.sessions[manifest.session]
    assert "--model claude-sonnet-5" in session["command"]
    assert "--dangerously-skip-permissions" in session["command"]
    assert " -p " not in f" {session['command']} "  # headless one-shot is banned
    assert "do the thing" in (job / jobdir.TRANSCRIPT_FILE).read_text()
    assert manifest.session in tmux.killed
    # The completion hook was installed in the workdir.
    settings = json.loads((workdir / ".claude" / "settings.local.json").read_text())
    assert "Stop" in settings["hooks"] and "UserPromptSubmit" in settings["hooks"]
    assert session["env"]["THEOZOLITH_HOOK_LOG"] == str(job / jobdir.HOOK_EVENTS_FILE)
    assert session["env"]["THEOZOLITH_JOB"] == str(job)  # in-session tools find the job dir
    # The gate job ran inside the workdir.
    assert (workdir / "gate.txt").read_text() == "ok"


def test_review_mode_copies_verdict_and_serves_no_jobs(tmp_path):
    job, manifest = make_job(tmp_path, mode=jobdir.MODE_REVIEW, settle=0.0, events="stop\n")
    verdict = {"verdict": "approve", "deviation": "low", "risk": "low", "evidence": "fine"}
    (job / manifest.workdir / "verdict.json").write_text(json.dumps(verdict))
    tmux = FakeTmux()
    clock = ScriptedClock()

    code = run_harness(job, tmux, runner=_real_runner, clock=clock, sleep=clock.sleep)

    assert code == 0
    assert json.loads((job / jobdir.VERDICT_FILE).read_text()) == verdict
    assert jobdir.read_status(job).phase == jobdir.PHASE_DONE


def test_harness_survives_agent_timeout_and_reports_it(tmp_path):
    job, _manifest = make_job(tmp_path, settle=1.0, timeout=5.0)  # hook never fires
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    tmux = FakeTmux()
    clock = ScriptedClock()

    code = run_harness(job, tmux, runner=_real_runner, clock=clock, sleep=clock.sleep)

    assert code == 0  # a timed-out agent is an outcome, not a harness failure
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE
    assert status.agent.timed_out and not status.agent.completed


# -- the real thing: tmux + pipe-pane + mid-run injection (acceptance 9) -----


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


@pytest.mark.skipif(not _tmux_available(), reason="tmux unavailable")
def test_real_tmux_session_with_midrun_injection(tmp_path, monkeypatch):
    # Isolate from any developer tmux server on the box.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    # A fake `claude` CLI: echoes whatever it receives (so pasted prompts and
    # injected instructions land in the pane, which pipe-pane captures).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_claude = bindir / "claude"
    fake_claude.write_text("#!/bin/sh\nexec cat\n")
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")

    job, manifest = make_job(tmp_path, settle=0.5, timeout=30.0)
    manifest = jobdir.Manifest(
        **{**manifest.__dict__, "startup_seconds": 0.3, "session": "run-test-inject"}
    )
    jobdir.write_manifest(job, manifest)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))

    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(run_harness(job, RealTmux(), poll_seconds=0.05))
    )
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = jobdir.read_status(job)
            if status is not None and status.phase == jobdir.PHASE_AGENT:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("harness never reached the agent phase")

        # A human attaches and injects an instruction mid-Run.
        subprocess.run(
            ["tmux", "send-keys", "-t", manifest.session, "human: also update the docs", "Enter"],
            check=True,
            capture_output=True,
        )
        time.sleep(0.5)
        append_event(job, "stop")  # the CLI's Stop hook fires
        thread.join(timeout=25)
        assert not thread.is_alive(), "harness did not finish"
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True, check=False)  # isolated server
        thread.join(timeout=5)

    assert result == [0]
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE and status.agent.completed
    transcript = (job / jobdir.TRANSCRIPT_FILE).read_text()
    assert "do the thing" in transcript  # the pasted prompt
    assert "human: also update the docs" in transcript  # the injected exchange
    # The Run completed normally after the injection; the session is gone.
    assert (
        subprocess.run(
            ["tmux", "has-session", "-t", manifest.session], capture_output=True, check=False
        ).returncode
        != 0
    )
