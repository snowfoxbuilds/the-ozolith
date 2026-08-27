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

from theozolith_worker import jobdir, proposal
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
        schema_version=proposal.SCHEMA_VERSION,
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


def test_review_mode_serves_no_jobs(tmp_path):
    job, _manifest = make_job(tmp_path, mode=jobdir.MODE_REVIEW)
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
    assert jobdir.read_status(job).phase == jobdir.PHASE_DONE


def test_schema_version_mismatch_fails_before_the_session_starts(tmp_path):
    """ADR-0046: a driver and run image speaking different Output Proposal
    schemas fail strictly pre-work with the anchored marker the driver
    classes as a pre-session infra failure — the agent never launches."""
    job, manifest = make_job(tmp_path)
    stale = jobdir.Manifest(
        **{
            **{f: getattr(manifest, f) for f in manifest.__dataclass_fields__},
            "schema_version": proposal.SCHEMA_VERSION + 1,
        }
    )
    jobdir.write_manifest(job, stale)
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

    assert code == 1
    assert launcher.calls == []  # pre-work: no agent process was ever spawned
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert proposal.schema_error_detail(status.error) is not None
    # An unstamped manifest (a pre-channel driver) is equally a mismatch.
    assert proposal.schema_mismatch(0) is not None


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


# -- the identity monitor (ADR-0045, best effort) -------------------------------
#
# The static checks and the monitor's state machine are unit-tested in
# test_identity.py; here the HARNESS contract is exercised: the ordinary
# launch (prompt in the argv, task file on disk, checkout sources loading —
# nothing gated, nothing withheld), the observation hooks riding --settings,
# static failures refusing to launch with the stable category and a redacted
# identity.json, drift/config kills mid-run, the post-exit applied-effort
# check (a detected clamp fails loud; a missing observation is a recorded
# gap), the setup dry-run mode, and the identity.json record the driver
# embeds into evidence.

from theozolith_worker.adapters import ClaudeAdapter  # noqa: E402
from theozolith_worker.identity import PreflightReport  # noqa: E402

PIN = "claude-sonnet-5"
TASK_TEXT = "do the thing\n"


def _bake_root(tmp_path: Path, model: str = PIN, effort: str = "") -> Path:
    """A derived-image filesystem exactly as materialize() writes it: the
    managed model DEFAULT (no allowlist — main-agent-only enforcement)."""
    root = tmp_path / "idroot"
    (root / "etc/theozolith").mkdir(parents=True, exist_ok=True)
    (root / "etc/theozolith/model").write_text(model + "\n")
    settings: dict = {"model": model}
    if effort:
        (root / "etc/theozolith/effort").write_text(effort + "\n")
        settings["effortLevel"] = effort
        settings["env"] = {"CLAUDE_CODE_EFFORT_LEVEL": effort}
    (root / "etc/claude-code").mkdir(parents=True, exist_ok=True)
    (root / "etc/claude-code/managed-settings.json").write_text(json.dumps(settings))
    return root


def _run_identity(job, launcher, clock, root, tmp_path):
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


def _turn_event_line(model: str, parent: str | None = None) -> dict:
    event: dict = {"type": "assistant", "message": {"model": model, "content": []}}
    if parent is not None:
        event["parent_tool_use_id"] = parent
    return event


def _stop_record(tmp_path: Path, effort: str = "") -> None:
    """What the materialized Stop hook helper does: append one applied-effort
    record to the journal in the harness scratch."""
    journal = tmp_path / "scratch" / "stop.jsonl"
    record: dict = {"effort": effort} if effort else {}
    with journal.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def test_monitored_harness_runs_the_task_normally(tmp_path):
    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeAgent(clock, exit_code=0, exits_at=3.0)
    launcher = FakeLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))

    clock.on_tick.append(script)

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 0
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE and status.agent.completed
    # NOTHING is gated or withheld: the pointer prompt rides the argv, the
    # task file never left the disk, and no isolation flag suppresses the
    # checkout's settings sources (CLAUDE.md and skills belong to the work).
    argv = launcher.calls[0]["argv"]
    assert any(str(job / jobdir.PROMPT_FILE) in part for part in argv)
    assert "--input-format" not in argv
    assert "--setting-sources" not in argv
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT
    # The observation hooks ride --settings: the Stop applied-effort journal
    # and the ConfigChange recorder, materialized in the scratch outside /job.
    settings_json = argv[argv.index("--settings") + 1]
    stop_script = tmp_path / "scratch" / "stop_hook.py"
    config_script = tmp_path / "scratch" / "configchange_hook.py"
    assert "Stop" in settings_json and str(stop_script) in settings_json
    assert "ConfigChange" in settings_json and str(config_script) in settings_json
    assert "PreToolUse" not in settings_json  # no tool gate: nothing is gated
    assert stop_script.is_file() and config_script.is_file()
    # The identity record the driver embeds into evidence.
    ident = jobdir.read_identity(job)
    assert ident["checks"] == "passed"
    assert ident["violation"] == "" and ident["category"] == ""
    assert ident["observed_model"] == PIN


def test_monitored_harness_kills_on_model_drift(tmp_path):
    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeAgent(clock)  # never exits on its own
    launcher = FakeLauncher(agent)
    stages = []

    def script(now: float) -> None:
        if now >= 1.0 and len(stages) == 0:
            stages.append("clean")
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
        if now >= 3.0 and len(stages) == 1:
            stages.append("drift")
            _append_stream(job, _turn_event_line("claude-opus-5"))

    clock.on_tick.append(script)

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert status.error.startswith("identity: ") and "[substituted]" in status.error
    assert "claude-opus-5" in status.error
    assert agent.terminated_at is not None  # killed immediately, mid-run
    ident = jobdir.read_identity(job)
    assert ident["category"] == "substituted" and "claude-opus-5" in ident["violation"]
    # The Run is invalid: no gate jobs were served after the kill.
    assert jobdir.read_job_result(job, "001-shutdown") is None


def test_monitored_harness_leaves_subagents_free(tmp_path):
    # Main-agent-only enforcement: subagent turns on other models are a
    # legitimate capability, never a kill.
    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeAgent(clock, exit_code=0, exits_at=3.0)
    launcher = FakeLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(
                job,
                _init_event_line(PIN),
                _turn_event_line("claude-haiku-4-5", parent="toolu_01"),
                _turn_event_line(PIN),
            )

    clock.on_tick.append(script)

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 0
    ident = jobdir.read_identity(job)
    assert ident["violation"] == "" and ident["observed_model"] == PIN


def test_monitored_harness_kills_on_a_config_change(tmp_path):
    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeAgent(clock)
    launcher = FakeLauncher(agent)
    capture = tmp_path / "scratch" / "config-change.jsonl"
    stages = []

    def script(now: float) -> None:
        if now >= 1.0 and len(stages) == 0:
            stages.append("clean")
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
        if now >= 3.0 and len(stages) == 1:
            stages.append("config-change")
            capture.write_text(
                json.dumps({"source": "policy_settings", "file_path": "/etc/claude-code/x.json"})
                + "\n"
            )

    clock.on_tick.append(script)

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert "[config-changed]" in status.error and "policy_settings" in status.error
    assert agent.terminated_at is not None
    assert jobdir.read_identity(job)["category"] == "config-changed"


def test_monitored_harness_clamped_effort_fails_after_exit(tmp_path):
    root = _bake_root(tmp_path, effort="xhigh")
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    agent = FakeAgent(clock, exit_code=0, exits_at=3.0)
    launcher = FakeLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
            # An organization effort cap clamps silently; the journal record
            # is the observation, and a DETECTED clamp fails loud.
            _stop_record(tmp_path, effort="high")

    clock.on_tick.append(script)

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert "[effort-clamped]" in status.error and "'xhigh'" in status.error
    ident = jobdir.read_identity(job)
    assert ident["observed_effort"] == "high" and ident["category"] == "effort-clamped"


def test_monitored_harness_missing_effort_observation_is_a_gap(tmp_path):
    # Best-effort doctrine: only DETECTED mismatches fail. A Stop hook that
    # never produced a record is a recorded gap, not a failed Run.
    root = _bake_root(tmp_path, effort="low")
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeAgent(clock, exit_code=0, exits_at=3.0)
    launcher = FakeLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))

    clock.on_tick.append(script)

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 0
    ident = jobdir.read_identity(job)
    assert ident["observed_effort"] == "" and ident["violation"] == ""
    assert any("gap recorded" in note for note in ident["notes"])


def test_monitored_harness_no_stream_signal_is_a_gap(tmp_path):
    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock, exit_code=0, exits_at=2.0))

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 0  # nothing detected, nothing failed
    ident = jobdir.read_identity(job)
    assert ident["observed_model"] == ""
    assert any("no main-agent turn signal" in note for note in ident["notes"])


def test_monitored_harness_static_failure_never_launches(tmp_path):
    # The well-known file declares an identity the managed settings do not
    # carry: the free static checks fail the Run loud before any launch.
    root = tmp_path / "idroot"
    (root / "etc/theozolith").mkdir(parents=True)
    (root / "etc/theozolith/model").write_text(PIN + "\n")  # no managed file
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock))

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 1
    assert launcher.calls == []  # the task session never existed
    status = jobdir.read_status(job)
    assert "[identity-inconsistent]" in status.error
    ident = jobdir.read_identity(job)
    assert ident["checks"] == "failed:identity-inconsistent"
    assert ident["category"] == "identity-inconsistent"
    assert ident["expected_model"] == PIN
    assert (job / jobdir.PROMPT_FILE).read_text() == TASK_TEXT  # untouched


def test_monitored_harness_corrupt_declaration_fails_before_launch(tmp_path):
    root = tmp_path / "idroot"
    (root / "etc/theozolith").mkdir(parents=True)
    (root / "etc/theozolith/model").write_text("")  # corrupt: empty declaration
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock))

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 1
    assert launcher.calls == []
    status = jobdir.read_status(job)
    assert "[identity-inconsistent]" in status.error
    ident = jobdir.read_identity(job)
    assert ident["checks"] == "failed:identity-inconsistent"


def test_monitored_harness_hook_scratch_failure_is_an_identity_category(tmp_path):
    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock))
    fence = tmp_path / "fence"
    fence.mkdir()
    fence.chmod(0o500)  # the scratch cannot be created under it
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
    assert launcher.calls == []
    status = jobdir.read_status(job)
    assert "[unverifiable]" in status.error and "observation hooks" in status.error
    assert jobdir.read_identity(job)["category"] == "unverifiable"


def test_monitored_harness_timeout_keeps_ordinary_semantics(tmp_path):
    # A session the monitor never faulted keeps ADR-0019 semantics: the hard
    # timeout is a timeout, not an identity failure.
    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path, timeout=30.0)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeAgent(clock)  # never exits
    launcher = FakeLauncher(agent)
    fed = []

    def script(now: float) -> None:
        if now >= 1.0 and not fed:
            fed.append(True)
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))

    clock.on_tick.append(script)

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 0
    status = jobdir.read_status(job)
    assert status.agent.timed_out is True
    assert jobdir.read_identity(job)["violation"] == ""


def test_monitored_harness_timeout_flush_still_detects_drift(tmp_path):
    # A detection flushed to the transcript only during the timeout kill is
    # still an identity verdict: the identity class outranks the timeout
    # class (it routes the deterministic-failure lanes, ADR-0045).
    root = _bake_root(tmp_path)
    job, _manifest = make_job(tmp_path, timeout=30.0)
    clock = ScriptedClock()
    agent = FakeAgent(clock, term_reaps=False)  # survives SIGTERM, dies to SIGKILL
    launcher = FakeLauncher(agent)
    stages = []

    def script(now: float) -> None:
        if now >= 1.0 and len(stages) == 0:
            stages.append("clean")
            _append_stream(job, _init_event_line(PIN), _turn_event_line(PIN))
        if now >= 30.5 and len(stages) == 1:  # lands during the kill grace
            stages.append("drift")
            _append_stream(job, _turn_event_line("claude-opus-5"))

    clock.on_tick.append(script)

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert status.error.startswith("identity: ") and "[substituted]" in status.error
    assert agent.killed_at is not None
    assert jobdir.read_identity(job)["category"] == "substituted"


def test_model_less_image_launches_exactly_as_before(tmp_path):
    job, _manifest = make_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock, exit_code=0, exits_at=2.0))

    code = run_harness(
        job,
        launcher,
        runner=_real_runner,
        clock=clock,
        sleep=clock.sleep,
        identity_root=tmp_path / "no-identity",
    )

    assert code == 0
    argv = launcher.calls[0]["argv"]
    assert "--settings" not in argv  # no hooks, no monitor, no extra cost
    assert not (job / jobdir.IDENTITY_FILE).exists()


# -- the setup dry-run mode -----------------------------------------------------


def make_dryrun_job(tmp_path: Path) -> Path:
    job = jobdir.create_job_dir(tmp_path, "d1")
    manifest = jobdir.Manifest(run_id="d1", mode=jobdir.MODE_DRYRUN, adapter="claude")
    jobdir.write_manifest(job, manifest)
    return job


def test_dryrun_mode_needs_no_task_or_workdir(tmp_path, monkeypatch):
    root = _bake_root(tmp_path, effort="low")

    def fake_preflight(self, identity, *, root, scratch, run=None):
        return PreflightReport(
            ok=True,
            expected_model=PIN,
            expected_effort="low",
            cli_version="2.1.232",
            probe_model=PIN,
            probe_effort="low",
        )

    monkeypatch.setattr(ClaudeAdapter, "preflight", fake_preflight)
    job = make_dryrun_job(tmp_path)

    code = run_harness(job, identity_root=root, scratch_root=tmp_path / "scratch")

    assert code == 0
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE
    ident = jobdir.read_identity(job)
    assert ident["dry_run"] == "passed"
    assert ident["expected_model"] == PIN and ident["probe_effort"] == "low"


def test_dryrun_mode_fails_loud_with_the_category(tmp_path, monkeypatch):
    root = _bake_root(tmp_path)

    def failing_preflight(self, identity, *, root, scratch, run=None):
        return PreflightReport(
            ok=False,
            expected_model=PIN,
            expected_effort="",
            category="substituted",
            detail="the identity probe ran on 'claude-opus-5'",
            cli_version="2.1.232",
        )

    monkeypatch.setattr(ClaudeAdapter, "preflight", failing_preflight)
    job = make_dryrun_job(tmp_path)

    code = run_harness(job, identity_root=root, scratch_root=tmp_path / "scratch")

    assert code == 1
    status = jobdir.read_status(job)
    assert status.error.startswith("identity: ") and "substituted" in status.error
    ident = jobdir.read_identity(job)
    assert ident["dry_run"] == "failed:substituted"


def test_dryrun_mode_passes_trivially_without_an_identity(tmp_path):
    job = make_dryrun_job(tmp_path)

    code = run_harness(job, identity_root=tmp_path / "no-identity")

    assert code == 0
    assert jobdir.read_status(job).phase == jobdir.PHASE_DONE
    assert jobdir.read_identity(job)["dry_run"] == "passed"


def test_dryrun_mode_corrupt_declaration_fails(tmp_path):
    root = tmp_path / "idroot"
    (root / "etc/theozolith").mkdir(parents=True)
    (root / "etc/theozolith/model").write_text("")
    job = make_dryrun_job(tmp_path)

    code = run_harness(job, identity_root=root)

    assert code == 1
    status = jobdir.read_status(job)
    assert "[identity-inconsistent]" in status.error
    assert jobdir.read_identity(job)["dry_run"] == "failed:identity-inconsistent"


# -- the codex adapter through the harness (ADR-0052, PROBE + STATIC) -----------

CODEX_PIN = "gpt-5.2-codex"


def _bake_codex_root(tmp_path: Path, model: str = CODEX_PIN) -> Path:
    """A codex derived-image filesystem exactly as CodexAdapter.materialize
    writes it: the well-known files plus the theozolith-owned config.toml."""
    root = tmp_path / "idroot"
    (root / "etc/theozolith/codex").mkdir(parents=True, exist_ok=True)
    (root / "etc/theozolith/model").write_text(model + "\n")
    (root / "etc/theozolith/codex/config.toml").write_text(f'model = "{model}"\n')
    return root


def _codex_job(tmp_path: Path) -> Path:
    job, manifest = make_job(tmp_path)
    manifest = jobdir.Manifest(
        run_id=manifest.run_id,
        mode=manifest.mode,
        adapter="codex",
        workdir=manifest.workdir,
        agent_timeout_seconds=manifest.agent_timeout_seconds,
        jobs_idle_timeout_seconds=manifest.jobs_idle_timeout_seconds,
        schema_version=manifest.schema_version,
    )
    jobdir.write_manifest(job, manifest)
    return job


def _codex_home(tmp_path: Path, monkeypatch) -> Path:
    """Pin the adapter's throwaway CODEX_HOME to a known path and deliver
    the credential — the two runtime preconditions prepare() consumes."""
    import tempfile as tempfile_mod

    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setattr(tempfile_mod, "mkdtemp", lambda prefix="": str(home))
    monkeypatch.setenv("CODEX_AUTH_JSON", '{"tokens": {"access_token": "a"}}')
    return home


def _plant_rollout(home: Path, model: str, effort: str = "") -> None:
    sessions = home / "sessions" / "2026" / "08" / "26"
    sessions.mkdir(parents=True, exist_ok=True)
    payload: dict = {"turn_id": "t1", "model": model}
    if effort:
        payload["effort"] = effort
    (sessions / "rollout-x.jsonl").write_text(
        json.dumps({"type": "turn_context", "payload": payload}) + "\n"
    )


def test_codex_harness_runs_and_records_the_rollout_model(tmp_path, monkeypatch):
    root = _bake_codex_root(tmp_path)
    home = _codex_home(tmp_path, monkeypatch)
    _plant_rollout(home, CODEX_PIN)
    job = _codex_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    agent = FakeAgent(clock, exit_code=0, exits_at=3.0)
    launcher = FakeLauncher(agent)

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 0
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_DONE and status.agent.completed
    # The codex launch: exec argv, prepared CODEX_HOME in the agent env,
    # no --settings hook blob (no hook surface), 0600 credential leaf.
    call = launcher.calls[0]
    assert call["argv"][:2] == ["codex", "exec"]
    assert "--settings" not in call["argv"]
    assert call["env"]["CODEX_HOME"] == str(home)
    assert (home / "auth.json").stat().st_mode & 0o777 == 0o600
    assert (home / "config.toml").read_text() == f'model = "{CODEX_PIN}"\n'
    # The identity record: checks passed, the rollout model observed.
    ident = jobdir.read_identity(job)
    assert ident["checks"] == "passed"
    assert ident["violation"] == ""
    assert ident["observed_model"] == CODEX_PIN


def test_codex_harness_never_kills_on_an_off_model_rollout(tmp_path, monkeypatch):
    """The doctrine test at harness level (ADR-0052): an off-identity codex
    session is NOT killed — the mismatch lands in evidence (observed_model)
    while the Run completes normally. Contrast the Claude monitor's
    fail-loud kill."""
    root = _bake_codex_root(tmp_path)
    home = _codex_home(tmp_path, monkeypatch)
    _plant_rollout(home, "o3")
    job = _codex_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock, exit_code=0, exits_at=3.0))

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 0
    assert jobdir.read_status(job).phase == jobdir.PHASE_DONE
    ident = jobdir.read_identity(job)
    assert ident["violation"] == "" and ident["category"] == ""
    assert ident["observed_model"] == "o3"  # the mismatch IS in evidence


def test_codex_harness_records_the_rollout_effort_without_a_stop_hook_gap(tmp_path, monkeypatch):
    """The codex effort evidence channel (ADR-0052): the benign observer's
    rollout reading is the authoritative ``observed_effort`` in the final
    identity record — the Claude Stop-hook journal is never consulted for a
    codex session, so its missing-observation gap note must not appear."""
    root = _bake_codex_root(tmp_path)
    home = _codex_home(tmp_path, monkeypatch)
    _plant_rollout(home, CODEX_PIN, effort="high")
    job = _codex_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock, exit_code=0, exits_at=3.0))

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 0
    ident = jobdir.read_identity(job)
    assert ident["observed_model"] == CODEX_PIN
    assert ident["observed_effort"] == "high"
    assert ident["violation"] == "" and ident["category"] == ""
    assert not any("Stop hook" in note for note in ident["notes"])


def test_codex_harness_missing_rollout_is_a_gap(tmp_path, monkeypatch):
    root = _bake_codex_root(tmp_path)
    _codex_home(tmp_path, monkeypatch)  # no rollout planted
    job = _codex_job(tmp_path)
    jobdir.write_job_request(job, jobdir.JobRequest("001-shutdown", ""))
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock, exit_code=0, exits_at=3.0))

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 0
    ident = jobdir.read_identity(job)
    assert ident["observed_model"] == ""
    assert any("gap recorded" in note for note in ident["notes"])


def test_codex_harness_missing_credential_fails_before_launch(tmp_path, monkeypatch):
    root = _bake_codex_root(tmp_path)
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    job = _codex_job(tmp_path)
    clock = ScriptedClock()
    launcher = FakeLauncher(FakeAgent(clock, exit_code=0, exits_at=3.0))

    code = _run_identity(job, launcher, clock, root, tmp_path)

    assert code == 1
    status = jobdir.read_status(job)
    assert status.phase == jobdir.PHASE_FAILED
    assert "agent prepare failed" in status.error
    assert "CODEX_AUTH_JSON" in status.error
    assert launcher.calls == []  # the session was never launched
