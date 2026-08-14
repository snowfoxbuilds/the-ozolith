"""The agent harness: headless one-shot invocation, exit-is-completion, jobs.

Unit tests drive the harness single-threaded with a fake agent process and
scripted clocks; the integration tests at the bottom run the real thing — a
real subprocess standing in for the agent CLI — and exercise the headless
contract (ADR-0019): the prompt rides the invocation argv, stdout is the
structured-output transcript, process exit completes the Run, and the hard
timeout kills an overrunning session.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

from theozolith_worker import jobdir
from theozolith_worker.harness.main import await_exit, launch_agent, run_harness, serve_jobs


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


class FakeAgent:
    """A scripted headless agent process driven by the test clock."""

    def __init__(
        self,
        clock,
        *,
        exit_code: int = 0,
        exits_at: float | None = None,
        term_reaps: bool = True,
    ):
        self._clock = clock
        self._exit_code = exit_code
        self._exits_at = exits_at
        self._term_reaps = term_reaps
        self.terminated_at: float | None = None
        self.killed_at: float | None = None

    def poll(self) -> int | None:
        if self.killed_at is not None:
            return -9
        if self.terminated_at is not None and self._term_reaps:
            return -15
        if self._exits_at is not None and self._clock() >= self._exits_at:
            return self._exit_code
        return None

    def terminate(self) -> None:
        if self.terminated_at is None:
            self.terminated_at = self._clock()

    def kill(self) -> None:
        if self.killed_at is None:
            self.killed_at = self._clock()


class FakeLauncher:
    """Records the invocation and returns the prepared FakeAgent, writing a
    scripted structured-output stream into the transcript first."""

    def __init__(self, agent: FakeAgent, stream: str = ""):
        self._agent = agent
        self._stream = stream
        self.calls: list[dict] = []

    def __call__(self, argv, cwd, env, transcript) -> FakeAgent:
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "transcript": transcript})
        if self._stream:
            with Path(transcript).open("a") as handle:
                handle.write(self._stream)
        return self._agent


def make_job(
    tmp_path: Path,
    *,
    mode: str = jobdir.MODE_RUN,
    timeout: float = 100.0,
) -> tuple[Path, jobdir.Manifest]:
    job = jobdir.create_job_dir(tmp_path, "r1")
    workdir = jobdir.CHECKOUT_DIR if mode == jobdir.MODE_RUN else jobdir.WORK_DIR
    (job / workdir).mkdir(parents=True, exist_ok=True)
    manifest = jobdir.Manifest(
        run_id="r1",
        mode=mode,
        adapter="claude",
        workdir=workdir,
        agent_timeout_seconds=timeout,
        jobs_idle_timeout_seconds=30.0,
    )
    jobdir.write_manifest(job, manifest)
    (job / jobdir.PROMPT_FILE).parent.mkdir(parents=True, exist_ok=True)
    (job / jobdir.PROMPT_FILE).write_text("do the thing\n")
    return job, manifest


# -- completion detection (process exit, ADR-0019) ---------------------------


def test_process_exit_zero_is_completion(tmp_path):
    _job, manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeAgent(clock, exit_code=0, exits_at=3.0)

    outcome = await_exit(agent, manifest, clock=clock, sleep=clock.sleep)

    assert outcome.completed and not outcome.timed_out and not outcome.session_died
    assert outcome.exit_code == 0


def test_nonzero_exit_is_a_died_session(tmp_path):
    _job, manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeAgent(clock, exit_code=2, exits_at=3.0)

    outcome = await_exit(agent, manifest, clock=clock, sleep=clock.sleep)

    assert outcome.session_died and not outcome.completed
    assert outcome.exit_code == 2
    assert "exit 2" in outcome.describe()


def test_hard_timeout_terminates_then_kills(tmp_path):
    _job, manifest = make_job(tmp_path, timeout=30.0)
    clock = ScriptedClock()
    agent = FakeAgent(clock, term_reaps=False)  # ignores SIGTERM

    outcome = await_exit(agent, manifest, clock=clock, sleep=clock.sleep)

    assert outcome.timed_out and not outcome.completed
    assert agent.terminated_at is not None and agent.terminated_at >= 30.0
    assert agent.killed_at is not None and agent.killed_at >= agent.terminated_at + 10.0


def test_hard_timeout_needs_no_sigkill_when_sigterm_reaps(tmp_path):
    _job, manifest = make_job(tmp_path, timeout=30.0)
    clock = ScriptedClock()
    agent = FakeAgent(clock, term_reaps=True)

    outcome = await_exit(agent, manifest, clock=clock, sleep=clock.sleep)

    assert outcome.timed_out
    assert agent.terminated_at is not None
    assert agent.killed_at is None


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


# -- the full harness cycle (fake agent process) ------------------------------


def test_run_mode_full_cycle(tmp_path):
    job, manifest = make_job(tmp_path)
    workdir = job / manifest.workdir
    jobdir.write_job_request(job, jobdir.JobRequest("001-gate", "printf ok > gate.txt", 10.0))
    jobdir.write_job_request(job, jobdir.JobRequest("002-shutdown", ""))
    clock = ScriptedClock()
    stream = '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}\n'
    launcher = FakeLauncher(FakeAgent(clock, exit_code=0, exits_at=1.0), stream=stream)

    code = run_harness(
        job,
        launcher,
        runner=_real_runner,
        clock=clock,
        sleep=clock.sleep,
        identity_root=tmp_path / "no-identity",
    )

    assert code == 0
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE and status.agent.completed
    # Headless one-shot with the POINTER prompt (ADR-0019 as amended): the
    # argv names the mounted task file and never carries the task content,
    # so the invocation is constant-size regardless of task size.
    call = launcher.calls[0]
    argv = call["argv"]
    pointer = argv[argv.index("-p") + 1]
    assert str(job / jobdir.PROMPT_FILE) in pointer
    assert "do the thing" not in " ".join(argv)  # the task stays in the file
    assert "--model" not in argv  # the model is baked into the image (ADR-0045)
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert call["cwd"] == workdir
    assert call["env"]["THEOZOLITH_JOB"] == str(job)  # in-session tools find the job dir
    # The structured output stream is the transcript.
    assert call["transcript"] == job / jobdir.TRANSCRIPT_FILE
    assert "done" in (job / jobdir.TRANSCRIPT_FILE).read_text()
    # No hook machinery: nothing writes agent settings into the workdir.
    assert not (workdir / ".claude").exists()
    # The gate job ran inside the workdir, after the agent process exited.
    assert (workdir / "gate.txt").read_text() == "ok"


def test_review_mode_copies_verdict_and_serves_no_jobs(tmp_path):
    job, manifest = make_job(tmp_path, mode=jobdir.MODE_REVIEW)
    verdict = {"verdict": "approve", "deviation": "low", "risk": "low", "evidence": "fine"}
    (job / manifest.workdir / "verdict.json").write_text(json.dumps(verdict))
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock, exit_code=0, exits_at=1.0))

    code = run_harness(
        job,
        launcher,
        runner=_real_runner,
        clock=clock,
        sleep=clock.sleep,
        identity_root=tmp_path / "no-identity",
    )

    assert code == 0
    assert json.loads((job / jobdir.VERDICT_FILE).read_text()) == verdict
    assert jobdir.read_status(job).phase == jobdir.PHASE_DONE


def test_harness_survives_agent_timeout_and_reports_it(tmp_path):
    job, _manifest = make_job(tmp_path, timeout=5.0)  # the agent never exits
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock))

    code = run_harness(
        job,
        launcher,
        runner=_real_runner,
        clock=clock,
        sleep=clock.sleep,
        identity_root=tmp_path / "no-identity",
    )

    assert code == 0  # a timed-out agent is an outcome, not a harness failure
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE
    assert status.agent.timed_out and not status.agent.completed


def test_missing_task_file_is_a_harness_failure(tmp_path):
    job, _manifest = make_job(tmp_path)
    (job / jobdir.PROMPT_FILE).unlink()
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock, exits_at=1.0))

    code = run_harness(
        job,
        launcher,
        runner=_real_runner,
        clock=clock,
        sleep=clock.sleep,
        identity_root=tmp_path / "no-identity",
    )

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED and "task file" in status.error
    assert launcher.calls == []  # nothing was ever spawned


def test_agent_launch_failure_is_a_harness_failure(tmp_path):
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()

    def broken_launcher(argv, cwd, env, transcript):
        raise OSError("no such binary: claude")

    code = run_harness(
        job,
        broken_launcher,
        runner=_real_runner,
        clock=clock,
        sleep=clock.sleep,
        identity_root=tmp_path / "no-identity",
    )

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED and "agent launch failed" in status.error


# -- the real thing: a real headless subprocess (ADR-0019) --------------------


def _fake_claude(tmp_path: Path, script: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "claude"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def test_real_headless_invocation_streams_and_completes(tmp_path, monkeypatch):
    # A fake `claude` CLI: streams its prompt argument and its environment
    # to stdout, then exits 0 — completion is that exit.
    _fake_claude(
        tmp_path,
        "#!/bin/sh\n"
        'printf \'{"type":"system","subtype":"init"}\\n\'\n'
        "printf 'PROMPT:%s\\n' \"$2\"\n"
        "printf 'JOB:%s\\n' \"$THEOZOLITH_JOB\"\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

    job, _manifest = make_job(tmp_path, timeout=30.0)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))

    code = run_harness(
        job,
        launch_agent,
        runner=_real_runner,
        poll_seconds=0.05,
        identity_root=tmp_path / "no-identity",
    )

    assert code == 0
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE and status.agent.completed
    transcript = (job / jobdir.TRANSCRIPT_FILE).read_text()
    # The argv carried the pointer at the task file, never the task content.
    assert f"PROMPT:Work on the task specified in {job / jobdir.PROMPT_FILE}" in transcript
    assert "do the thing" not in transcript
    assert f"JOB:{job}" in transcript  # in-session tools can find the job dir


def test_real_hard_timeout_kills_the_overrunning_agent(tmp_path, monkeypatch):
    _fake_claude(tmp_path, "#!/bin/sh\nsleep 300\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

    job, _manifest = make_job(tmp_path, timeout=0.5)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))

    started = time.monotonic()
    code = run_harness(
        job,
        launch_agent,
        runner=_real_runner,
        poll_seconds=0.05,
        identity_root=tmp_path / "no-identity",
    )

    assert code == 0
    assert time.monotonic() - started < 30  # killed, not waited out
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE
    assert status.agent.timed_out and not status.agent.completed


# -- the identity gate (ADR-0045, fail closed) --------------------------------
#
# The preflight itself is unit-tested in test_identity.py; here it is faked
# so the tests exercise the HARNESS contract: the task file withheld from
# disk before any verification subprocess, no launch on a failed preflight,
# the probe-then-release stdin protocol with the atomic delivery ladder
# (marker → task write → send → close), the task prompt withheld from
# unverified sessions, delivery failures classified, mid-run drift and
# config changes killing the Run, exit-zero-after-probe rejected, and the
# identity.json verdict the driver embeds into evidence.

from theozolith_worker.adapters import ClaudeAdapter  # noqa: E402
from theozolith_worker.identity import PreflightReport  # noqa: E402

PIN = "claude-sonnet-5"
TASK_TEXT = "do the thing\n"


def _bake_root(tmp_path: Path, model: str = PIN, effort: str = "") -> Path:
    root = tmp_path / "idroot"
    (root / "etc/theozolith").mkdir(parents=True)
    (root / "etc/theozolith/model").write_text(model + "\n")
    settings: dict = {
        "model": model,
        "availableModels": [model],
        "enforceAvailableModels": True,
        "forceRemoteSettingsRefresh": True,
    }
    if effort:
        (root / "etc/theozolith/effort").write_text(effort + "\n")
        settings["effortLevel"] = effort
        settings["env"] = {"CLAUDE_CODE_EFFORT_LEVEL": effort}
    (root / "etc/claude-code").mkdir(parents=True)
    (root / "etc/claude-code/managed-settings.json").write_text(json.dumps(settings))
    return root


def _pass_preflight(monkeypatch, model: str = PIN, effort: str = ""):
    def fake_preflight(self, identity, *, root, scratch, run=None, deadline=None, clock=None):
        return PreflightReport(
            ok=True,
            expected_model=model,
            expected_effort=effort,
            cli_version="2.1.232",
            canary_model=model,
            probe_model=model,
            probe_effort=effort,
        )

    monkeypatch.setattr(ClaudeAdapter, "preflight", fake_preflight)


class FakeGatedAgent(FakeAgent):
    """A gated fake: records stdin deliveries and the input-close, and can
    fail either to simulate a dying session (broken pipe, stuck stdin)."""

    def __init__(self, clock, **kwargs):
        super().__init__(clock, **kwargs)
        self.sent: list[str] = []
        self.input_closed = False
        self.send_error_at: int | None = None  # 0-based index of the failing send
        self.end_input_error: Exception | None = None

    def send(self, line: str) -> None:
        if self.send_error_at is not None and len(self.sent) == self.send_error_at:
            raise BrokenPipeError("agent stdin is gone")
        self.sent.append(line)

    def end_input(self) -> None:
        if self.end_input_error is not None:
            raise self.end_input_error
        self.input_closed = True

    def exit_at(self, when: float, code: int = 0) -> None:
        self._exits_at = when
        self._exit_code = code


class FakeGatedLauncher:
    def __init__(self, agent: FakeGatedAgent):
        self._agent = agent
        self.calls: list[dict] = []
        self.task_file_present_at_launch: bool | None = None

    def __call__(self, argv, cwd, env, transcript, gated=False) -> FakeGatedAgent:
        self.calls.append(
            {"argv": argv, "cwd": cwd, "env": env, "transcript": transcript, "gated": gated}
        )
        return self._agent


def _run_gated(job, launcher, clock, root, tmp_path):
    return run_harness(
        job,
        launcher,
        runner=_real_runner,
        clock=clock,
        sleep=clock.sleep,
        identity_root=root,
        scratch_root=tmp_path / "scratch",
    )


def _append_stream(job: Path, *events: dict) -> None:
    with (job / jobdir.TRANSCRIPT_FILE).open("a") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _init_event_line(model: str) -> dict:
    return {"type": "system", "subtype": "init", "model": model}


def _turn_event_line(model: str) -> dict:
    return {"type": "assistant", "message": {"model": model, "content": []}}


def _stop_record(tmp_path: Path, effort: str = "") -> None:
    """What the materialized Stop hook helper does: append one completed-turn
    record to the boundary journal in the gate scratch."""
    journal = tmp_path / "scratch" / "gate" / "stop.jsonl"
    record: dict = {"effort": effort} if effort else {}
    with journal.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def test_gated_harness_releases_the_prompt_only_after_identity_verifies(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    stages = []

    def script(now: float) -> None:
        if now >= 1.0 and len(stages) == 0:
            stages.append("probe")
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            _stop_record(tmp_path)  # the probe turn completely stopped
        if agent.input_closed and len(stages) == 1:
            # The released task observably processed: one post-release
            # COMPLETED turn (its boundary record), not merely an event.
            stages.append("task")
            _append_stream(job, _turn_event_line(PIN))
            _stop_record(tmp_path)
            agent.exit_at(now + 1.0)

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 0
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE and status.agent.completed
    # The launch was gated: stdin-driven argv, and the task prompt is in NO
    # argv element — it can only ever arrive over stdin, after verification.
    call = launcher.calls[0]
    assert call["gated"] is True
    argv = call["argv"]
    assert "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
    assert all(str(job / jobdir.PROMPT_FILE) not in part for part in argv)
    # No user/project/local settings source loads into the task session —
    # nothing a checkout or home directory declares can steer it (ADR-0045).
    assert argv[argv.index("--setting-sources") + 1] == ""
    # The task session's --settings carry the gate hooks: the PreToolUse
    # release-marker denial, the Stop turn-boundary recorder (registered
    # even with no effort baked), and the ConfigChange recorder.
    settings_json = argv[argv.index("--settings") + 1]
    marker = tmp_path / "scratch" / "gate" / "released"
    hook_script = tmp_path / "scratch" / "gate" / "configchange_hook.py"
    stop_script = tmp_path / "scratch" / "gate" / "stop_hook.py"
    assert "PreToolUse" in settings_json and str(marker) in settings_json
    assert "ConfigChange" in settings_json and str(hook_script) in settings_json
    assert "Stop" in settings_json and str(stop_script) in settings_json
    assert hook_script.is_file() and stop_script.is_file()
    # The stdin protocol: the no-op probe first, the pointer prompt second
    # (only after init + an executed turn matched AND the probe stopped),
    # then end of input.
    assert len(agent.sent) == 2
    assert "Preflight check" in agent.sent[0]
    assert str(job / jobdir.PROMPT_FILE) in agent.sent[1]
    assert agent.input_closed
    # The release protocol ran: the tool gate opened and the task input is
    # back on disk with its original content.
    assert marker.is_file()
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT
    # The identity verdict the driver embeds into evidence: exact completed-
    # turn counts from the boundary journal.
    ident = jobdir.read_identity(job)
    assert ident["preflight"] == "passed"
    assert ident["released"] is True and ident["violation"] == "" and ident["category"] == ""
    assert ident["observed_model"] == PIN
    assert ident["probe_turns"] == 1 and ident["task_turns"] == 1


def test_gated_harness_withholds_the_task_file_until_release(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    task_file = job / jobdir.PROMPT_FILE
    seen = {}

    def fake_preflight(self, identity, *, root, scratch, run=None, deadline=None, clock=None):
        # THE boundary assertion: while verification subprocesses run, the
        # task input does not exist anywhere a process could read it, and
        # the scratch is outside the job directory.
        seen["during_preflight"] = task_file.exists()
        seen["scratch"] = scratch
        return PreflightReport(
            ok=True, expected_model=PIN, expected_effort="", cli_version="2.1.232"
        )

    monkeypatch.setattr(ClaudeAdapter, "preflight", fake_preflight)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    stages = []

    def script(now: float) -> None:
        if len(stages) == 0:
            # Before verification: the file is still gone.
            stages.append(("pre-release", task_file.exists()))
        if now >= 1.0 and len(stages) == 1:
            stages.append("probe")
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            _stop_record(tmp_path)
        if agent.input_closed and len(stages) == 2:
            stages.append(("post-release", task_file.exists()))
            _append_stream(job, _turn_event_line(PIN))
            _stop_record(tmp_path)
            agent.exit_at(now + 1.0)

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 0
    assert seen["during_preflight"] is False
    assert not str(seen["scratch"]).startswith(str(job))
    assert ("pre-release", False) in stages
    assert ("post-release", True) in stages
    assert task_file.read_text() == TASK_TEXT


def test_gated_harness_withholds_the_prompt_from_a_substituted_session(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line("claude-opus-5"))

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert status.error.startswith("identity: ")
    assert "[substituted]" in status.error and "substituted" in status.error
    # THE assertion of the whole amendment: the real task prompt never
    # entered the process — only the probe did — and the task file was
    # withheld the whole time, restored only for the evidence bundle.
    assert len(agent.sent) == 1 and "Preflight check" in agent.sent[0]
    assert agent.terminated_at is not None  # the session was killed
    ident = jobdir.read_identity(job)
    assert ident["released"] is False and "claude-opus-5" in ident["violation"]
    assert ident["category"] == "substituted"
    assert not (tmp_path / "scratch" / "gate" / "released").exists()  # gate never opened
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT  # evidence restore


def test_gated_harness_gate_timeout_fails_closed_without_sending_the_task(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path, timeout=30.0)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)  # never emits a stream, never exits
    launcher = FakeGatedLauncher(agent)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert "[preflight-timeout]" in status.error
    assert "could not be verified" in status.error and "never sent" in status.error
    assert len(agent.sent) == 1  # the probe only
    assert jobdir.read_identity(job)["released"] is False


def test_gated_harness_kills_on_mid_run_identity_drift(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    stages = []

    def script(now: float) -> None:
        if now >= 1.0 and len(stages) == 0:
            stages.append("probe")
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            _stop_record(tmp_path)
        # After the release, a server-side policy change moves the session
        # onto another model mid-run.
        if agent.input_closed and now >= 3.0 and len(stages) == 1:
            stages.append("drift")
            _append_stream(job, _turn_event_line("claude-opus-5"))

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert status.error.startswith("identity: ") and "claude-opus-5" in status.error
    assert "[substituted]" in status.error
    assert agent.terminated_at is not None  # terminated immediately, mid-run
    ident = jobdir.read_identity(job)
    assert ident["released"] is True  # the task HAD been released...
    assert "claude-opus-5" in ident["violation"]  # ...and the drift invalidated the Run
    # The drifted turn was killed mid-flight: it never completed, so the
    # journal shows no post-release turn — counters stay truthful.
    assert ident["task_turns"] == 0
    # The Run is invalid: no gate jobs were served after the kill.
    assert jobdir.read_job_result(job, "001-shutdown") is None


def test_gated_harness_kills_on_a_mid_run_config_change(tmp_path, monkeypatch):
    # The ConfigChange hook helper recorded an identity-affecting settings
    # change after release: the Run dies before further task work instead of
    # continuing under unproven configuration.
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    capture = tmp_path / "scratch" / "gate" / "config-change.jsonl"
    stages = []

    def script(now: float) -> None:
        if now >= 1.0 and len(stages) == 0:
            stages.append("probe")
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            _stop_record(tmp_path)
        if agent.input_closed and len(stages) == 1:
            stages.append("config-change")
            capture.write_text(
                json.dumps({"source": "policy_settings", "file_path": "/etc/claude-code/x.json"})
                + "\n"
            )

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert "[config-changed]" in status.error and "policy_settings" in status.error
    assert agent.terminated_at is not None
    ident = jobdir.read_identity(job)
    assert ident["released"] is True and ident["category"] == "config-changed"


def test_gated_harness_broken_pipe_during_task_send_fails_the_run(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    agent.send_error_at = 1  # the probe (send 0) lands; the task send breaks
    launcher = FakeGatedLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            _stop_record(tmp_path)

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert "[task-delivery]" in status.error and "could not be delivered" in status.error
    ident = jobdir.read_identity(job)
    # The prompt never entered the process: released stays false — a swallowed
    # BrokenPipe here must never become a completed (or even released) Run.
    assert ident["released"] is False
    assert ident["category"] == "task-delivery"
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT  # evidence restore


def test_gated_harness_stdin_close_failure_is_classified_explicitly(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    agent.end_input_error = OSError("close failed")
    launcher = FakeGatedLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            _stop_record(tmp_path)

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert "[stdin-close]" in status.error  # distinct from task-delivery
    ident = jobdir.read_identity(job)
    # The task DID enter the process before the close failed — recorded
    # truthfully, and the Run still fails rather than hanging on a session
    # that will never see EOF.
    assert ident["released"] is True
    assert ident["category"] == "stdin-close"


def test_gated_harness_exit_zero_after_only_the_probe_fails(tmp_path, monkeypatch):
    # A stdin EOF race (or a session that quits early) can exit 0 without
    # ever processing the queued task message — that must be a failed Run,
    # not a completed one.
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            _stop_record(tmp_path)
        if agent.input_closed and agent._exits_at is None:
            agent.exit_at(now + 1.0)  # exits clean — but no task turn ever

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert "[task-unprocessed]" in status.error
    ident = jobdir.read_identity(job)
    assert ident["released"] is True and ident["task_turns"] == 0
    # No gate jobs were served for the invalid Run.
    assert jobdir.read_job_result(job, "001-shutdown") is None


def test_gated_harness_residual_probe_events_never_count_as_the_task(tmp_path, monkeypatch):
    # A probe that emitted multiple assistant events (denied-tool
    # continuations) whose tail the transcript pump delivers only AFTER the
    # release: without the boundary journal those residuals would read as
    # "the task was processed". They must not — exit 0 with no post-release
    # COMPLETED turn is task-unprocessed.
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    stages = []

    def script(now: float) -> None:
        if now >= 1.0 and len(stages) == 0:
            stages.append("probe")
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            _stop_record(tmp_path)  # the probe stopped; release follows
        if agent.input_closed and len(stages) == 1:
            stages.append("residuals")
            # Late-read probe continuations: assistant events and a per-turn
            # result, but NO second boundary record — nothing completed
            # post-release.
            _append_stream(
                job,
                _turn_event_line(PIN),
                _turn_event_line(PIN),
                {"type": "result", "subtype": "success", "is_error": False},
            )
            agent.exit_at(now + 1.0)

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert "[task-unprocessed]" in status.error
    ident = jobdir.read_identity(job)
    assert ident["released"] is True
    assert ident["probe_turns"] == 1 and ident["task_turns"] == 0


def test_gated_harness_failed_preflight_never_launches_the_agent(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)

    def failing_preflight(self, identity, *, root, scratch, run=None, deadline=None, clock=None):
        return PreflightReport(
            ok=False,
            expected_model=PIN,
            expected_effort="",
            category="policy-widened",
            detail="the widen canary asked for 'claude-haiku-4-5' and executed claude-haiku-4-5",
            cli_version="2.1.232",
        )

    monkeypatch.setattr(ClaudeAdapter, "preflight", failing_preflight)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeGatedLauncher(FakeGatedAgent(clock))

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    assert launcher.calls == []  # the agent process never existed
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert status.error.startswith("identity: ")
    assert "policy-widened" in status.error
    assert "the real task prompt was not sent" in status.error
    ident = jobdir.read_identity(job)
    assert ident["preflight"] == "failed:policy-widened"
    assert ident["category"] == "policy-widened"
    assert ident["expected_model"] == PIN
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT  # evidence restore


def test_gated_harness_corrupt_identity_declaration_fails_before_launch(tmp_path):
    root = tmp_path / "idroot"
    (root / "etc/theozolith").mkdir(parents=True)
    (root / "etc/theozolith/model").write_text("")  # corrupt: empty declaration
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeGatedLauncher(FakeGatedAgent(clock))

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    assert launcher.calls == []
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED and status.error.startswith("identity: ")
    # A corrupt declaration is a first-class identity failure: the stable
    # category rides the status error AND a redacted identity.json exists
    # for evidence — not only failures that reached a PreflightReport.
    assert "[identity-inconsistent]" in status.error
    ident = jobdir.read_identity(job)
    assert ident["category"] == "identity-inconsistent"
    assert ident["preflight"] == "failed:identity-inconsistent"
    assert ident["released"] is False and ident["violation"] == ""
    assert ident["probe_turns"] == 0 and ident["task_turns"] == 0


def test_gated_harness_missing_managed_pin_is_identity_inconsistent(tmp_path):
    # The well-known file declares an identity the managed settings do not
    # enforce: the REAL preflight fails statically (no subprocess, no agent)
    # and the redacted identity.json carries the stable category.
    root = tmp_path / "idroot"
    (root / "etc/theozolith").mkdir(parents=True)
    (root / "etc/theozolith/model").write_text(PIN + "\n")  # no managed file
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeGatedLauncher(FakeGatedAgent(clock))

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    assert launcher.calls == []
    status = jobdir.read_status(job)
    assert "[identity-inconsistent]" in status.error
    ident = jobdir.read_identity(job)
    assert ident["category"] == "identity-inconsistent"
    assert ident["preflight"] == "failed:identity-inconsistent"
    assert ident["released"] is False
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT  # evidence restore


def test_gated_harness_boundary_setup_failure_is_an_identity_category(tmp_path, monkeypatch):
    # The pre-verification boundary could not even be established (the
    # scratch is unwritable): a stable identity category with the task
    # withheld and identity evidence written — never a generic harness crash.
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeGatedLauncher(FakeGatedAgent(clock))
    fence = tmp_path / "fence"
    fence.mkdir()
    fence.chmod(0o500)  # scratch_root cannot be created under it
    try:
        code = run_harness(
            job,
            launcher,
            runner=_real_runner,
            clock=clock,
            sleep=clock.sleep,
            identity_root=root,
            scratch_root=fence / "scratch",
        )
    finally:
        fence.chmod(0o700)

    assert code == 1
    assert launcher.calls == []  # the task session never existed
    status = jobdir.read_status(job)
    assert "[unverifiable]" in status.error
    assert "boundary could not be established" in status.error
    ident = jobdir.read_identity(job)
    assert ident["category"] == "unverifiable"
    assert ident["expected_model"] == PIN and ident["released"] is False
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT  # never left disk


def test_gated_harness_task_withhold_failure_is_an_identity_category(tmp_path, monkeypatch):
    # The task file can be read but not removed (unwritable input dir): the
    # boundary cannot be established, so the Run fails with the stable
    # category and the task still on disk — it was never sent anywhere.
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeGatedLauncher(FakeGatedAgent(clock))
    input_dir = (job / jobdir.PROMPT_FILE).parent
    input_dir.chmod(0o500)  # unlink of the task file will fail
    try:
        code = _run_gated(job, launcher, clock, root, tmp_path)
    finally:
        input_dir.chmod(0o700)

    assert code == 1
    assert launcher.calls == []
    status = jobdir.read_status(job)
    assert "[unverifiable]" in status.error
    assert "boundary could not be established" in status.error
    ident = jobdir.read_identity(job)
    assert ident["category"] == "unverifiable"
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT


def test_gated_harness_hook_materialization_failure_is_an_identity_category(tmp_path, monkeypatch):
    # Preflight passed but the gate hooks cannot be materialized: the task
    # session must never launch on an ungated argv — the failure is an
    # identity category with the preflight verdict preserved in evidence.
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeGatedLauncher(FakeGatedAgent(clock))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "gate").write_text("not a directory")  # gate dir creation fails
    code = run_harness(
        job,
        launcher,
        runner=_real_runner,
        clock=clock,
        sleep=clock.sleep,
        identity_root=root,
        scratch_root=scratch,
    )

    assert code == 1
    assert launcher.calls == []
    status = jobdir.read_status(job)
    assert "[unverifiable]" in status.error and "gate hooks" in status.error
    ident = jobdir.read_identity(job)
    assert ident["preflight"] == "failed:unverifiable"
    assert ident["category"] == "unverifiable"
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT  # evidence restore


def test_gated_harness_one_deadline_covers_preflight_and_task(tmp_path, monkeypatch):
    # The end-to-end budget starts BEFORE the preflight: a preflight that
    # eats the whole agent timeout leaves nothing for the gate, and the
    # harness emits its own timeout category well inside the driver's wait
    # budget (agent timeout + WAIT_GRACE) instead of silently extending the
    # task's time by the preflight's duration.
    from theozolith_worker.sessions import WAIT_GRACE_SECONDS

    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path, timeout=30.0)
    clock = ScriptedClock()

    def slow_preflight(self, identity, *, root, scratch, run=None, deadline=None, clock=None):
        while clock() < deadline - 1.0:  # canaries ate all but 1s of the budget
            sleep(0.5)
        return PreflightReport(
            ok=True, expected_model=PIN, expected_effort="", cli_version="2.1.232"
        )

    sleep = clock.sleep
    monkeypatch.setattr(ClaudeAdapter, "preflight", slow_preflight)
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    # The ~1s remaining budget expired before the session verified: the
    # harness's own timeout category, task withheld.
    assert "[preflight-timeout]" in status.error and "never sent" in status.error
    assert jobdir.read_identity(job)["released"] is False
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT
    # And it all happened comfortably before the driver-side wait would
    # expire — the container-session budget can never fire first.
    assert clock.now < 30.0 + WAIT_GRACE_SECONDS


def test_gated_harness_effort_pin_uses_the_stop_journal(tmp_path, monkeypatch):
    root = _bake_root(tmp_path, effort="low")
    _pass_preflight(monkeypatch, effort="low")
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    stages = []

    def script(now: float) -> None:
        if now >= 1.0 and len(stages) == 0:
            stages.append("probe")
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            _stop_record(tmp_path, effort="low")
        if agent.input_closed and len(stages) == 1:
            stages.append("task")
            _append_stream(job, _turn_event_line(PIN))
            _stop_record(tmp_path, effort="low")
            agent.exit_at(now + 1.0)

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 0
    # The gated argv carries the Stop journal hook via --settings — a source
    # managed settings outrank, so it cannot weaken enforcement — and the
    # probe turn needs NO tool: the Stop payload reports the applied effort
    # after a plain turn (verified live), so tools stay denied pre-release.
    argv = launcher.calls[0]["argv"]
    settings_json = argv[argv.index("--settings") + 1]
    journal = tmp_path / "scratch" / "gate" / "stop.jsonl"
    assert "Stop" in settings_json and str(journal) in settings_json
    assert "PostToolUse" not in settings_json
    assert "Do not use any tools" in agent.sent[0]
    ident = jobdir.read_identity(job)
    assert ident["observed_effort"] == "low" and ident["released"] is True


def test_gated_harness_clamped_effort_withholds_the_task(tmp_path, monkeypatch):
    root = _bake_root(tmp_path, effort="xhigh")
    _pass_preflight(monkeypatch, effort="xhigh")
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            # An organization effort cap clamps silently; the probe turn's
            # boundary record is the only observation, and a clamp is a
            # failure, not a downgrade.
            _stop_record(tmp_path, effort="high")

    clock.on_tick.append(script)

    code = _run_gated(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED and "'xhigh'" in status.error
    assert "[effort-clamped]" in status.error
    assert len(agent.sent) == 1  # probe only; the task never entered the process
    ident = jobdir.read_identity(job)
    assert ident["observed_effort"] == "high" and ident["released"] is False
    assert ident["category"] == "effort-clamped"
