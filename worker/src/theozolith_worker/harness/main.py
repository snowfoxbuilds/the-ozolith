"""The harness main loop: headless agent process, job serving, exit.

PID 1 of every run container. Everything it knows arrives as files under the
job directory (mounted at /job); everything it produces leaves the same way.
The agent runs headless (ADR-0019): the adapter's one-shot command with the
prompt passed at invocation, stdout captured as the structured-output
transcript, completion detected by process exit with the hard agent timeout
as backstop. It makes no pipeline decision: a timed-out or crashed agent
process is recorded in ``output/status.json`` and the harness carries on —
the driver owns what that means (best-effort contract).

When the image bakes a model/effort identity (ADR-0045, fail-closed
amendment), the launch is GATED: the adapter's preflight must prove the
effective policy still binds the baked identity, the agent is started in
stdin-driven mode whose first turn is a no-op probe, and the real task
prompt is released into the process only after the session's announced and
executed identity match the baked one. A violation at any point — preflight,
gate, or mid-session drift — kills the agent and fails the Run with a
distinct ``identity:`` error the driver classifies separately; the task
prompt is never sent to an unverified session.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from theozolith_worker import shell
from theozolith_worker.adapters import AgentAdapterError, make_agent_adapter
from theozolith_worker.identity import (
    GUARD_KILL,
    GUARD_RELEASE,
    IDENTITY_ERROR_PREFIX,
)
from theozolith_worker.jobdir import (
    CONTAINER_JOB_PATH,
    PHASE_AGENT,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_SERVING_JOBS,
    PHASE_STARTING,
    PROMPT_FILE,
    TRANSCRIPT_FILE,
    AgentOutcome,
    JobResult,
    Manifest,
    Status,
    pending_job_requests,
    read_job_request,
    read_manifest,
    write_identity,
    write_job_result,
    write_status,
)

DEFAULT_POLL_SECONDS = 0.5
KILL_GRACE_SECONDS = 10.0  # SIGTERM at the deadline, SIGKILL after this
# How long a gated session gets to prove its identity before the harness
# gives up without ever sending the task prompt (well inside any sane agent
# timeout; one no-op probe turn normally completes in seconds).
GATE_RELEASE_TIMEOUT_SECONDS = 300.0
# Job-dir scratch for the identity gate: the preflight canary's cwd and the
# effort-capture hook file live here, outside input/ and output/.
PREFLIGHT_DIR = "preflight"

# The pointer prompt (ADR-0019 as amended): the argv carries a constant-size
# pointer at the driver-materialized task file, never the task content — the
# invocation cannot outgrow ARG_MAX however large the task is.
POINTER_PROMPT = (
    "Work on the task specified in {path}. Read that file first — it is your"
    " complete assignment — then execute it exactly."
)

# (command, cwd, timeout) -> (ok, exit code, output). Runs agent-authored
# code, so the default is a plain shell in this (credential-free) container.
JobRunner = Callable[[str, Path, float], tuple[bool, int, str]]


def _default_runner(command: str, cwd: Path, timeout: float) -> tuple[bool, int, str]:
    return shell.run_shell(command, cwd, timeout)


class AgentProcess(Protocol):
    """The running headless agent, as the completion wait sees it."""

    def poll(self) -> int | None:
        """The exit code once the process has exited, else None."""
        ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def send(self, line: str) -> None:
        """Deliver one stdin input line (gated sessions only)."""
        ...

    def end_input(self) -> None:
        """Close stdin: no further input will follow (gated sessions only)."""
        ...


# (argv, cwd, env, transcript[, gated]) -> the running agent. Tests provide
# fakes. The gated form (5th argument True) must pipe stdin for send()/
# end_input() and still deliver stdout to the transcript file.
AgentLauncher = Callable[..., AgentProcess]


class _SubprocessAgent:
    """The real agent process, signalled as a whole process group."""

    def __init__(self, popen: subprocess.Popen, pump: threading.Thread | None = None):
        self._popen = popen
        self._pump = pump

    def poll(self) -> int | None:
        return self._popen.poll()

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def send(self, line: str) -> None:
        stdin = self._popen.stdin
        if stdin is None:
            raise OSError("agent process has no stdin channel (not a gated launch)")
        stdin.write(line.encode("utf-8") + b"\n")
        stdin.flush()

    def end_input(self) -> None:
        with contextlib.suppress(OSError):
            if self._popen.stdin is not None:
                self._popen.stdin.close()

    def drain(self) -> None:
        """Wait briefly for the stdout pump to flush the transcript tail."""
        if self._pump is not None:
            self._pump.join(timeout=5.0)

    def _signal(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._popen.pid, sig)


def launch_agent(
    argv: list[str], cwd: Path, env: dict[str, str], transcript: Path, gated: bool = False
) -> AgentProcess:
    """Spawn the headless agent: stdout+stderr append to the transcript.

    Gated launches pipe stdin (the harness delivers the probe and — once the
    identity gate passes — the task prompt) and pump stdout to the transcript
    through a thread, so the guard can read the stream as it grows."""
    if not gated:
        with transcript.open("ab") as handle:
            popen = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env={**os.environ, **env},
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # its own process group, killable whole
            )
        return _SubprocessAgent(popen)
    popen = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env={**os.environ, **env},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def _pump() -> None:
        assert popen.stdout is not None
        with transcript.open("ab") as handle:
            for line in popen.stdout:
                handle.write(line)
                handle.flush()

    pump = threading.Thread(target=_pump, name="agent-stdout-pump", daemon=True)
    pump.start()
    return _SubprocessAgent(popen, pump)


def await_exit(
    process: AgentProcess,
    manifest: Manifest,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    kill_grace_seconds: float = KILL_GRACE_SECONDS,
) -> AgentOutcome:
    """Completion is process exit (ADR-0019); the hard timeout kills an
    overrunning session: SIGTERM at the deadline, SIGKILL after the grace."""
    start = clock()
    while True:
        code = process.poll()
        if code is not None:
            return AgentOutcome(completed=code == 0, session_died=code != 0, exit_code=code)
        if clock() - start >= manifest.agent_timeout_seconds:
            break
        sleep(poll_seconds)
    process.terminate()
    term_deadline = clock() + kill_grace_seconds
    while process.poll() is None and clock() < term_deadline:
        sleep(poll_seconds)
    if process.poll() is None:
        process.kill()
        kill_deadline = clock() + kill_grace_seconds
        while process.poll() is None and clock() < kill_deadline:
            sleep(poll_seconds)
    return AgentOutcome(timed_out=True)


def _read_new_lines(transcript: Path, offset: int, buffer: str) -> tuple[int, str, list[str]]:
    """New complete transcript lines since ``offset``; partial tails carry
    over in ``buffer`` so the guard only ever sees whole lines."""
    try:
        with transcript.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
            offset = handle.tell()
    except OSError:
        return offset, buffer, []
    if not chunk:
        return offset, buffer, []
    buffer += chunk.decode("utf-8", errors="replace")
    *complete, buffer = buffer.split("\n")
    return offset, buffer, complete


def _shutdown(
    process: AgentProcess,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float,
    kill_grace_seconds: float,
) -> None:
    """SIGTERM, a grace, then SIGKILL — the shared teardown ladder."""
    process.terminate()
    deadline = clock() + kill_grace_seconds
    while process.poll() is None and clock() < deadline:
        sleep(poll_seconds)
    if process.poll() is None:
        process.kill()
        deadline = clock() + kill_grace_seconds
        while process.poll() is None and clock() < deadline:
            sleep(poll_seconds)


def await_guarded(
    process: AgentProcess,
    manifest: Manifest,
    guard,
    transcript: Path,
    prompt: str,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    kill_grace_seconds: float = KILL_GRACE_SECONDS,
    release_timeout_seconds: float = GATE_RELEASE_TIMEOUT_SECONDS,
) -> tuple[AgentOutcome, str, bool]:
    """The gated wait (ADR-0045): feed the growing transcript to the session
    guard; release the real task prompt only when the guard verifies the
    session's identity; keep monitoring after release and kill on drift.

    Returns ``(outcome, violation, released)`` — a nonempty violation means
    the Run is invalid, and ``released`` says whether the task prompt ever
    entered the process."""
    start = clock()
    offset = 0
    buffer = ""
    released = False

    def feed() -> None:
        nonlocal offset, buffer
        offset, buffer, lines = _read_new_lines(transcript, offset, buffer)
        for line in lines:
            guard.observe(line)

    while True:
        feed()
        decision = guard.decision()
        if decision.action == GUARD_KILL:
            _shutdown(
                process,
                clock=clock,
                sleep=sleep,
                poll_seconds=poll_seconds,
                kill_grace_seconds=kill_grace_seconds,
            )
            return AgentOutcome(), decision.reason, released
        if decision.action == GUARD_RELEASE and not released:
            try:
                process.send(guard.render_input(prompt))
                process.end_input()
            except OSError:
                pass  # the process died mid-release; poll() below reports it
            released = True
        code = process.poll()
        if code is not None:
            drain = getattr(process, "drain", None)
            if drain is not None:
                drain()
            feed()
            decision = guard.decision()
            if decision.action == GUARD_KILL:
                return AgentOutcome(), decision.reason, released
            if not released:
                return (
                    AgentOutcome(),
                    f"agent session exited (exit {code}) before the identity"
                    " gate verified the session — the task prompt was never"
                    " sent",
                    released,
                )
            return (
                AgentOutcome(completed=code == 0, session_died=code != 0, exit_code=code),
                "",
                released,
            )
        now = clock()
        if not released and now - start >= min(
            release_timeout_seconds, manifest.agent_timeout_seconds
        ):
            _shutdown(
                process,
                clock=clock,
                sleep=sleep,
                poll_seconds=poll_seconds,
                kill_grace_seconds=kill_grace_seconds,
            )
            return (
                AgentOutcome(),
                "the session identity could not be verified within"
                f" {min(release_timeout_seconds, manifest.agent_timeout_seconds):.0f}s"
                " — the task prompt was never sent",
                released,
            )
        if now - start >= manifest.agent_timeout_seconds:
            _shutdown(
                process,
                clock=clock,
                sleep=sleep,
                poll_seconds=poll_seconds,
                kill_grace_seconds=kill_grace_seconds,
            )
            return AgentOutcome(timed_out=True), "", released
        sleep(poll_seconds)


def serve_jobs(
    job: Path,
    workdir: Path,
    manifest: Manifest,
    *,
    runner: JobRunner,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> str:
    """Run driver-sequenced jobs until the shutdown request; '' or an error."""
    last_activity = clock()
    while True:
        pending = pending_job_requests(job)
        if not pending:
            if clock() - last_activity >= manifest.jobs_idle_timeout_seconds:
                return "idle timeout: no job or shutdown request from the driver"
            sleep(poll_seconds)
            continue
        for path in pending:
            request = read_job_request(path)
            if request is None:
                write_job_result(
                    job,
                    JobResult(name=path.stem, ok=False, exit_code=-1, output="unreadable request"),
                )
                continue
            if not request.command:  # the shutdown request
                return ""
            ok, code, output = runner(request.command, workdir, request.timeout_seconds)
            write_job_result(job, JobResult(request.name, ok, code, output))
        last_activity = clock()


def run_harness(
    job: Path,
    launcher: AgentLauncher = launch_agent,
    *,
    runner: JobRunner = _default_runner,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    identity_root: Path = Path("/"),
) -> int:
    manifest = read_manifest(job)
    write_status(job, Status(phase=PHASE_STARTING))
    adapter = make_agent_adapter(manifest.adapter)
    workdir = job / manifest.workdir
    if not workdir.is_dir():
        write_status(job, Status(phase=PHASE_FAILED, error=f"missing workdir {manifest.workdir}"))
        return 1

    task_file = job / PROMPT_FILE
    if not task_file.is_file():
        write_status(job, Status(phase=PHASE_FAILED, error=f"missing task file {PROMPT_FILE}"))
        return 1

    transcript = job / TRANSCRIPT_FILE
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    # THEOZOLITH_JOB lets in-session tools (theozolith-validate-verdict)
    # find the manifest and outputs from inside the workdir.
    env = {**adapter.prepare(workdir, job), "THEOZOLITH_JOB": str(job)}
    pointer = POINTER_PROMPT.format(path=task_file)

    # The identity gate (ADR-0045): an image that declares a baked identity
    # must prove it effective before the task prompt is sent; an image that
    # declares none (a model-less worker type) launches exactly as before.
    try:
        identity = adapter.baked_identity(identity_root)
    except AgentAdapterError as exc:
        write_status(job, Status(phase=PHASE_FAILED, error=f"{IDENTITY_ERROR_PREFIX}{exc}"))
        return 1
    guard = None
    identity_record: dict = {}
    if identity is not None:
        scratch = job / PREFLIGHT_DIR
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            report = adapter.preflight(identity, root=identity_root, scratch=scratch)
        except AgentAdapterError as exc:
            write_status(job, Status(phase=PHASE_FAILED, error=f"{IDENTITY_ERROR_PREFIX}{exc}"))
            return 1
        identity_record = {
            "expected_model": report.expected_model,
            "expected_effort": report.expected_effort,
            "preflight": "passed" if report.ok else f"failed:{report.category}",
            "detail": report.detail,
            "cli_version": report.cli_version,
            "canary_model": report.canary_model,
            "released": False,
            "violation": "",
            "observed_model": "",
            "observed_effort": "",
        }
        write_identity(job, identity_record)
        if not report.ok:
            write_status(
                job,
                Status(phase=PHASE_FAILED, error=IDENTITY_ERROR_PREFIX + report.describe()),
            )
            return 1
        capture = (scratch / "effort.json") if identity.effort else None
        guard = adapter.session_guard(identity, capture)
        argv = adapter.guarded_command(manifest, capture)
    else:
        argv = adapter.command(manifest, pointer)

    write_status(job, Status(phase=PHASE_AGENT))
    try:
        if guard is None:
            process = launcher(argv, workdir, env, transcript)
        else:
            process = launcher(argv, workdir, env, transcript, True)
    except OSError as exc:
        write_status(job, Status(phase=PHASE_FAILED, error=f"agent launch failed: {exc}"))
        return 1
    violation = ""
    error = ""
    outcome = AgentOutcome()
    try:
        if guard is None:
            outcome = await_exit(
                process, manifest, clock=clock, sleep=sleep, poll_seconds=poll_seconds
            )
        else:
            # A dead process here is reported by the guarded wait below.
            with contextlib.suppress(OSError):
                process.send(guard.probe_input())
            outcome, violation, released = await_guarded(
                process,
                manifest,
                guard,
                transcript,
                pointer,
                clock=clock,
                sleep=sleep,
                poll_seconds=poll_seconds,
            )
            identity_record.update(
                released=released,
                violation=violation,
                observed_model=guard.observed_model,
                observed_effort=guard.observed_effort,
            )
            write_identity(job, identity_record)
        if not violation:
            adapter.collect(workdir, job, manifest.mode)
            if manifest.serve_jobs:
                write_status(job, Status(phase=PHASE_SERVING_JOBS, agent=outcome))
                error = serve_jobs(
                    job,
                    workdir,
                    manifest,
                    runner=runner,
                    clock=clock,
                    sleep=sleep,
                    poll_seconds=poll_seconds,
                )
    finally:
        # Whatever happened, no agent process outlives the harness.
        if process.poll() is None:
            process.kill()

    if violation:
        # The Run is invalid (ADR-0045): no gate jobs were served, and the
        # driver classifies the failure distinctly via the identity marker.
        write_status(
            job,
            Status(phase=PHASE_FAILED, agent=outcome, error=IDENTITY_ERROR_PREFIX + violation),
        )
        return 1
    write_status(
        job,
        Status(phase=PHASE_FAILED if error else PHASE_DONE, agent=outcome, error=error),
    )
    return 1 if error else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-harness",
        description="TheOzolith agent harness: PID 1 of a run container.",
    )
    parser.add_argument(
        "--job",
        default=CONTAINER_JOB_PATH,
        help=f"job directory (default: {CONTAINER_JOB_PATH})",
    )
    args = parser.parse_args(argv)
    job = Path(args.job)
    try:
        return run_harness(job)
    except Exception as exc:
        with contextlib.suppress(OSError):
            write_status(job, Status(phase=PHASE_FAILED, error=f"harness crashed: {exc}"))
        print(f"harness error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
