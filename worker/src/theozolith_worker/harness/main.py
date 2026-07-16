"""The harness main loop: session, completion wait, job serving, exit.

PID 1 of every run container. Everything it knows arrives as files under the
job directory (mounted at /job); everything it produces leaves the same way.
It makes no pipeline decision: a timed-out or crashed agent session is
recorded in ``output/status.json`` and the harness carries on — the driver
owns what that means (best-effort contract).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from collections.abc import Callable
from pathlib import Path

from theozolith_worker import shell
from theozolith_worker.harness.adapters import EVENT_STOP, make_harness_adapter
from theozolith_worker.harness.tmux import RealTmux, Tmux
from theozolith_worker.jobdir import (
    CONTAINER_JOB_PATH,
    HOOK_EVENTS_FILE,
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

# (command, cwd, timeout) -> (ok, exit code, output). Runs agent-authored
# code, so the default is a plain shell in this (credential-free) container.
JobRunner = Callable[[str, Path, float], tuple[bool, int, str]]


def _default_runner(command: str, cwd: Path, timeout: float) -> tuple[bool, int, str]:
    return shell.run_shell(command, cwd, timeout)


def _events(job: Path) -> list[str]:
    try:
        raw = (job / HOOK_EVENTS_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def await_completion(
    job: Path,
    tmux: Tmux,
    manifest: Manifest,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> AgentOutcome:
    """Wait for the adapter's completion hook to fire and settle.

    Mechanical by contract: the last hook event being ``stop`` for a full
    settle window means the session is done. Any later event (a human
    attaching and submitting a prompt) re-arms the wait. The hard timeout is
    the backstop; a session that exits on its own ends the wait immediately.
    """
    start = clock()
    seen: list[str] = []
    last_change = start
    while True:
        now = clock()
        if now - start >= manifest.agent_timeout_seconds:
            return AgentOutcome(timed_out=True)
        events = _events(job)
        if events != seen:
            seen = events
            last_change = now
        settled = bool(seen) and seen[-1] == EVENT_STOP
        if settled and now - last_change >= manifest.settle_seconds:
            return AgentOutcome(completed=True)
        if not tmux.has_session(manifest.session):
            return AgentOutcome(completed=settled, session_died=True)
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
    tmux: Tmux | None = None,
    *,
    runner: JobRunner = _default_runner,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> int:
    tmux = tmux or RealTmux()
    manifest = read_manifest(job)
    write_status(job, Status(phase=PHASE_STARTING))
    adapter = make_harness_adapter(manifest.adapter)
    workdir = job / manifest.workdir
    if not workdir.is_dir():
        write_status(job, Status(phase=PHASE_FAILED, error=f"missing workdir {manifest.workdir}"))
        return 1

    transcript = job / TRANSCRIPT_FILE
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    # THEOZOLITH_JOB lets in-session tools (theozolith-validate-verdict)
    # find the manifest and outputs from inside the workdir.
    env = {**adapter.prepare(workdir, job), "THEOZOLITH_JOB": str(job)}
    tmux.new_session(manifest.session, adapter.command(manifest), workdir, env)
    try:
        tmux.pipe_pane(manifest.session, transcript)
        sleep(manifest.startup_seconds)  # let the CLI draw its input box
        tmux.paste(manifest.session, (job / PROMPT_FILE).read_text(encoding="utf-8"))
        write_status(job, Status(phase=PHASE_AGENT))

        outcome = await_completion(
            job, tmux, manifest, clock=clock, sleep=sleep, poll_seconds=poll_seconds
        )
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
        tmux.kill(manifest.session)

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
