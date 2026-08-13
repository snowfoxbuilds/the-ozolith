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
# so the tests exercise the HARNESS contract: no launch on a failed
# preflight, the probe-then-release stdin protocol, the task prompt withheld
# from unverified sessions, mid-run drift killing the Run, and the
# identity.json verdict the driver embeds into evidence.

from theozolith_worker.adapters import ClaudeAdapter  # noqa: E402
from theozolith_worker.identity import PreflightReport  # noqa: E402

PIN = "claude-sonnet-5"


def _bake_root(tmp_path: Path, model: str = PIN, effort: str = "") -> Path:
    root = tmp_path / "idroot"
    (root / "etc/theozolith").mkdir(parents=True)
    (root / "etc/theozolith/model").write_text(model + "\n")
    settings: dict = {"model": model, "availableModels": [model], "enforceAvailableModels": True}
    if effort:
        (root / "etc/theozolith/effort").write_text(effort + "\n")
        settings["effortLevel"] = effort
        settings["env"] = {"CLAUDE_CODE_EFFORT_LEVEL": effort}
    (root / "etc/claude-code").mkdir(parents=True)
    (root / "etc/claude-code/managed-settings.json").write_text(json.dumps(settings))
    return root


def _pass_preflight(monkeypatch, model: str = PIN, effort: str = ""):
    def fake_preflight(self, identity, *, root, scratch, run=None):
        return PreflightReport(
            ok=True, expected_model=model, expected_effort=effort, cli_version="2.1.231"
        )

    monkeypatch.setattr(ClaudeAdapter, "preflight", fake_preflight)


class FakeGatedAgent(FakeAgent):
    """A gated fake: records stdin deliveries and the input-close."""

    def __init__(self, clock, **kwargs):
        super().__init__(clock, **kwargs)
        self.sent: list[str] = []
        self.input_closed = False

    def send(self, line: str) -> None:
        self.sent.append(line)

    def end_input(self) -> None:
        self.input_closed = True

    def exit_at(self, when: float, code: int = 0) -> None:
        self._exits_at = when
        self._exit_code = code


class FakeGatedLauncher:
    def __init__(self, agent: FakeGatedAgent):
        self._agent = agent
        self.calls: list[dict] = []

    def __call__(self, argv, cwd, env, transcript, gated=False) -> FakeGatedAgent:
        self.calls.append(
            {"argv": argv, "cwd": cwd, "env": env, "transcript": transcript, "gated": gated}
        )
        return self._agent


def _append_stream(job: Path, *events: dict) -> None:
    with (job / jobdir.TRANSCRIPT_FILE).open("a") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _init_event_line(model: str) -> dict:
    return {"type": "system", "subtype": "init", "model": model}


def _turn_event_line(model: str) -> dict:
    return {"type": "assistant", "message": {"model": model, "content": []}}


def test_gated_harness_releases_the_prompt_only_after_identity_verifies(tmp_path, monkeypatch):
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
        if agent.input_closed and agent._exits_at is None:
            agent.exit_at(now + 1.0)

    clock.on_tick.append(script)

    code = run_harness(
        job, launcher, runner=_real_runner, clock=clock, sleep=clock.sleep, identity_root=root
    )

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
    # The stdin protocol: the no-op probe first, the pointer prompt second
    # (only after init + an executed turn matched), then end of input.
    assert len(agent.sent) == 2
    assert "Preflight check" in agent.sent[0]
    assert str(job / jobdir.PROMPT_FILE) in agent.sent[1]
    assert agent.input_closed
    # The identity verdict the driver embeds into evidence.
    ident = jobdir.read_identity(job)
    assert ident["preflight"] == "passed"
    assert ident["released"] is True and ident["violation"] == ""
    assert ident["observed_model"] == PIN


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

    code = run_harness(
        job, launcher, runner=_real_runner, clock=clock, sleep=clock.sleep, identity_root=root
    )

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert status.error.startswith("identity: ") and "substituted" in status.error
    # THE assertion of the whole amendment: the real task prompt never
    # entered the process — only the probe did.
    assert len(agent.sent) == 1 and "Preflight check" in agent.sent[0]
    assert agent.terminated_at is not None  # the session was killed
    ident = jobdir.read_identity(job)
    assert ident["released"] is False and "claude-opus-5" in ident["violation"]


def test_gated_harness_gate_timeout_fails_closed_without_sending_the_task(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)
    _pass_preflight(monkeypatch)
    job, _manifest = make_job(tmp_path, timeout=30.0)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)  # never emits a stream, never exits
    launcher = FakeGatedLauncher(agent)

    code = run_harness(
        job, launcher, runner=_real_runner, clock=clock, sleep=clock.sleep, identity_root=root
    )

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
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
        # After the release, a server-side policy change moves the session
        # onto another model mid-run.
        if agent.input_closed and now >= 3.0 and len(stages) == 1:
            stages.append("drift")
            _append_stream(job, _turn_event_line("claude-opus-5"))

    clock.on_tick.append(script)

    code = run_harness(
        job, launcher, runner=_real_runner, clock=clock, sleep=clock.sleep, identity_root=root
    )

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert status.error.startswith("identity: ") and "claude-opus-5" in status.error
    assert agent.terminated_at is not None  # terminated immediately, mid-run
    ident = jobdir.read_identity(job)
    assert ident["released"] is True  # the task HAD been released...
    assert "claude-opus-5" in ident["violation"]  # ...and the drift invalidated the Run
    # The Run is invalid: no gate jobs were served after the kill.
    assert jobdir.read_job_result(job, "001-shutdown") is None


def test_gated_harness_failed_preflight_never_launches_the_agent(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)

    def failing_preflight(self, identity, *, root, scratch, run=None):
        return PreflightReport(
            ok=False,
            expected_model=PIN,
            expected_effort="",
            category="policy-widened",
            detail="the canary asked for 'claude-haiku-4-5' and executed claude-haiku-4-5",
            cli_version="2.1.231",
        )

    monkeypatch.setattr(ClaudeAdapter, "preflight", failing_preflight)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeGatedLauncher(FakeGatedAgent(clock))

    code = run_harness(
        job, launcher, runner=_real_runner, clock=clock, sleep=clock.sleep, identity_root=root
    )

    assert code == 1
    assert launcher.calls == []  # the agent process never existed
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert status.error.startswith("identity: ")
    assert "policy-widened" in status.error
    assert "the real task prompt was not sent" in status.error
    ident = jobdir.read_identity(job)
    assert ident["preflight"] == "failed:policy-widened"
    assert ident["expected_model"] == PIN


def test_gated_harness_corrupt_identity_declaration_fails_before_launch(tmp_path):
    root = tmp_path / "idroot"
    (root / "etc/theozolith").mkdir(parents=True)
    (root / "etc/theozolith/model").write_text("")  # corrupt: empty declaration
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeGatedLauncher(FakeGatedAgent(clock))

    code = run_harness(
        job, launcher, runner=_real_runner, clock=clock, sleep=clock.sleep, identity_root=root
    )

    assert code == 1
    assert launcher.calls == []
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED and status.error.startswith("identity: ")


def test_gated_harness_effort_pin_uses_the_hook_capture(tmp_path, monkeypatch):
    root = _bake_root(tmp_path, effort="low")
    _pass_preflight(monkeypatch, effort="low")
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    capture = job / "preflight" / "effort.json"
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            capture.write_text(json.dumps({"effort": {"level": "low"}}))
        if agent.input_closed and agent._exits_at is None:
            agent.exit_at(now + 1.0)

    clock.on_tick.append(script)

    code = run_harness(
        job, launcher, runner=_real_runner, clock=clock, sleep=clock.sleep, identity_root=root
    )

    assert code == 0
    # The gated argv carries the PostToolUse capture hook via --settings —
    # a source managed settings outrank, so it cannot weaken enforcement.
    argv = launcher.calls[0]["argv"]
    settings_json = argv[argv.index("--settings") + 1]
    assert "PostToolUse" in settings_json and str(capture) in settings_json
    # The probe asks for the tool call that makes the hook fire.
    assert "Bash tool" in agent.sent[0]
    ident = jobdir.read_identity(job)
    assert ident["observed_effort"] == "low" and ident["released"] is True


def test_gated_harness_clamped_effort_withholds_the_task(tmp_path, monkeypatch):
    root = _bake_root(tmp_path, effort="xhigh")
    _pass_preflight(monkeypatch, effort="xhigh")
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeGatedAgent(clock)
    launcher = FakeGatedLauncher(agent)
    capture = job / "preflight" / "effort.json"
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            # An organization effort cap clamps silently; the hook capture is
            # the only observation, and a clamp is a failure, not a downgrade.
            capture.write_text(json.dumps({"effort": {"level": "high"}}))

    clock.on_tick.append(script)

    code = run_harness(
        job, launcher, runner=_real_runner, clock=clock, sleep=clock.sleep, identity_root=root
    )

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED and "'xhigh'" in status.error
    assert len(agent.sent) == 1  # probe only; the task never entered the process
    ident = jobdir.read_identity(job)
    assert ident["observed_effort"] == "high" and ident["released"] is False
