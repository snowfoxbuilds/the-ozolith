"""The harness main loop: headless agent process, job serving, exit.

PID 1 of every run container. Everything it knows arrives as files under the
job directory (mounted at /job); everything it produces leaves the same way.
The agent runs headless (ADR-0019): the adapter's one-shot command with the
prompt passed at invocation, stdout captured as the structured-output
transcript, completion detected by process exit with the hard agent timeout
as backstop. It makes no pipeline decision: a timed-out or crashed agent
process is recorded in ``output/status.json`` and the harness carries on —
the driver owns what that means (best-effort contract).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from theozolith_worker import shell
from theozolith_worker.harness.adapters import make_agent_adapter
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
    write_job_result,
    write_status,
)

DEFAULT_POLL_SECONDS = 0.5
KILL_GRACE_SECONDS = 10.0  # SIGTERM at the deadline, SIGKILL after this

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


# (argv, cwd, env, transcript) -> the running agent. Tests provide fakes.
AgentLauncher = Callable[[list[str], Path, dict[str, str], Path], AgentProcess]


class _SubprocessAgent:
    """The real agent process, signalled as a whole process group."""

    def __init__(self, popen: subprocess.Popen):
        self._popen = popen

    def poll(self) -> int | None:
        return self._popen.poll()

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._popen.pid, sig)


def launch_agent(argv: list[str], cwd: Path, env: dict[str, str], transcript: Path) -> AgentProcess:
    """Spawn the headless agent: stdout+stderr append to the transcript."""
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
    argv = adapter.command(manifest, POINTER_PROMPT.format(path=task_file))
    write_status(job, Status(phase=PHASE_AGENT))
    try:
        process = launcher(argv, workdir, env, transcript)
    except OSError as exc:
        write_status(job, Status(phase=PHASE_FAILED, error=f"agent launch failed: {exc}"))
        return 1
    try:
        outcome = await_exit(process, manifest, clock=clock, sleep=sleep, poll_seconds=poll_seconds)
        adapter.collect(workdir, job, manifest.mode)

        error = ""
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
