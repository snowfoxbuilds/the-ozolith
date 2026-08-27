"""The harness main loop: headless agent process, job serving, exit.

PID 1 of every run container. Everything it knows arrives as files under the
job directory (mounted at /job); everything it produces leaves the same way.
The agent runs headless (ADR-0019): the adapter's one-shot command with the
prompt passed at invocation, stdout captured as the structured-output
transcript, completion detected by process exit with the hard agent timeout
as backstop. It makes no pipeline decision: a timed-out or crashed agent
process is recorded in ``output/status.json`` and the harness carries on —
the driver owns what that means (best-effort contract).

When the image bakes a model/effort identity (ADR-0045, best-effort
doctrine), the launch is watched, never gated: the harness runs the
zero-cost static identity checks (file reads only), then launches the task
session exactly as an unbaked image would — pointer prompt in the argv,
task file on disk, checkout CLAUDE.md/skills/settings loading normally —
with two observation hooks riding ``--settings`` (a Stop applied-effort
journal and a ConfigChange recorder). A fail-loud monitor reads the stream
as it grows and kills the session on a POSITIVE detection only: a
main-agent turn executing off the baked model (subagent turns are
deliberately free — enforcement is main-agent-only) or a recorded
identity-relevant mid-session settings change. After exit the last
applied-effort observation is checked; a detected clamp fails the Run, a
missing observation is recorded as a gap. Every identity failure carries a
distinct ``identity:`` error (and stable category) the driver classifies
separately, plus a redacted ``output/identity.json`` record for evidence.

The token-spending probe lives in the SETUP DRY-RUN (``identity-dryrun``
manifest mode, driven once per driver process per run image): identity
checks, the CLI version floor, and one neutral probe session — a broken
image/credential/policy combination fails loud at worker setup, never per
Run.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from theozolith_worker import proposal, shell
from theozolith_worker.adapters import AgentAdapterError, make_agent_adapter
from theozolith_worker.identity import (
    CATEGORY_EFFORT_CLAMPED,
    CATEGORY_INCONSISTENT,
    CATEGORY_UNVERIFIABLE,
    IDENTITY_ERROR_PREFIX,
    MonitorHooks,
    read_last_journal_effort,
)
from theozolith_worker.jobdir import (
    CONTAINER_JOB_PATH,
    MODE_DRYRUN,
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
# The identity machinery's scratch lives OUTSIDE the job mount (a
# container-local temp dir): the observation hook files (capture journals,
# hook helper scripts) live where nothing in the checkout can name or reach
# them by a job-relative path, and the setup dry-run's probe session runs
# with it as cwd — a neutral directory with no task inputs.
SCRATCH_PREFIX = "theozolith-identity-"

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
AgentLauncher = Callable[..., AgentProcess]


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
    """Spawn the headless agent: stdout+stderr append to the transcript.

    The kernel appends directly to the transcript file, so the identity
    monitor can read the stream as it grows without any pump thread."""
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


def _read_new_lines(transcript: Path, offset: int, buffer: str) -> tuple[int, str, list[str]]:
    """New complete transcript lines since ``offset``; partial tails carry
    over in ``buffer`` so the monitor only ever sees whole lines."""
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


def await_monitored(
    process: AgentProcess,
    manifest: Manifest,
    monitor,
    transcript: Path,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    kill_grace_seconds: float = KILL_GRACE_SECONDS,
) -> tuple[AgentOutcome, str, str]:
    """The watched wait (ADR-0045, best effort): the timeout contract of
    ``await_exit``, plus the fail-loud identity monitor — the growing
    transcript is fed line by line, and a POSITIVE detection (an
    off-identity main-agent turn, a recorded identity-affecting
    ConfigChange) kills the session immediately instead of letting a
    wrong-identity Run burn the rest of its budget.

    Returns ``(outcome, violation, category)`` — a nonempty violation means
    the Run is invalid, with the stable category naming the failure class.
    A session the monitor never faulted keeps the ordinary ADR-0019
    semantics: exit is completion, the hard timeout is a timeout."""
    start = clock()
    offset = 0
    buffer = ""

    def feed() -> None:
        nonlocal offset, buffer
        offset, buffer, lines = _read_new_lines(transcript, offset, buffer)
        for line in lines:
            monitor.observe(line)

    def flush() -> None:
        # After the process is gone, a final event flushed complete but
        # without a trailing newline sits in the carry-over buffer — feed it
        # too, or the monitor misses what stream_stats will later report
        # (a truncated mid-write tail just fails to parse and is ignored).
        nonlocal buffer
        feed()
        if buffer.strip():
            monitor.observe(buffer)
            buffer = ""

    while True:
        feed()
        reason, category = monitor.violation()
        if reason:
            _shutdown(
                process,
                clock=clock,
                sleep=sleep,
                poll_seconds=poll_seconds,
                kill_grace_seconds=kill_grace_seconds,
            )
            return AgentOutcome(), reason, category
        code = process.poll()
        if code is not None:
            flush()
            reason, category = monitor.violation()
            if reason:
                return AgentOutcome(), reason, category
            return (
                AgentOutcome(completed=code == 0, session_died=code != 0, exit_code=code),
                "",
                "",
            )
        if clock() - start >= manifest.agent_timeout_seconds:
            _shutdown(
                process,
                clock=clock,
                sleep=sleep,
                poll_seconds=poll_seconds,
                kill_grace_seconds=kill_grace_seconds,
            )
            # Lines flushed between the last feed and the kill can carry the
            # detection; an identity verdict outranks the timeout class (it
            # routes the deterministic-failure lanes, ADR-0045).
            flush()
            reason, category = monitor.violation()
            if reason:
                return AgentOutcome(), reason, category
            return AgentOutcome(timed_out=True), "", ""
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


def _run_identity_dryrun(job: Path, adapter, identity_root: Path, scratch_root: Path | None) -> int:
    """The setup dry-run (ADR-0045): the identity checks, the CLI version
    floor, and the one-time neutral probe session — no task file, no
    workdir, no agent process. The driver runs this once per process per
    run image; a failure is a loud ``identity:`` status the driver reports
    without burning any issue or claim."""
    record: dict = {
        "expected_model": "",
        "expected_effort": "",
        "dry_run": "",
        "category": "",
        "detail": "",
        "cli_version": "",
        "probe_model": "",
        "probe_effort": "",
    }

    def fail(category: str, detail: str) -> int:
        record.update(dry_run=f"failed:{category}", category=category, detail=detail)
        with contextlib.suppress(OSError):
            write_identity(job, record)
        write_status(
            job,
            Status(phase=PHASE_FAILED, error=f"{IDENTITY_ERROR_PREFIX}[{category}] {detail}"),
        )
        return 1

    try:
        identity = adapter.baked_identity(identity_root)
    except AgentAdapterError as exc:
        return fail(CATEGORY_INCONSISTENT, str(exc))
    if identity is None:
        # A model-less worker type: nothing declared, nothing to check.
        record.update(dry_run="passed")
        with contextlib.suppress(OSError):  # the status verdict outranks the record
            write_identity(job, record)
        write_status(job, Status(phase=PHASE_DONE))
        return 0
    record.update(expected_model=identity.model, expected_effort=identity.effort)
    try:
        scratch = (
            scratch_root
            if scratch_root is not None
            else Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
        )
        scratch.mkdir(parents=True, exist_ok=True)
        report = adapter.preflight(identity, root=identity_root, scratch=scratch / "preflight")
    except (AgentAdapterError, OSError) as exc:
        return fail(CATEGORY_UNVERIFIABLE, str(exc))
    record.update(
        dry_run="passed" if report.ok else f"failed:{report.category}",
        category="" if report.ok else report.category,
        detail=report.detail,
        cli_version=report.cli_version,
        probe_model=report.probe_model,
        probe_effort=report.probe_effort,
    )
    with contextlib.suppress(OSError):  # the status verdict outranks the record
        write_identity(job, record)
    if not report.ok:
        write_status(
            job, Status(phase=PHASE_FAILED, error=IDENTITY_ERROR_PREFIX + report.describe())
        )
        return 1
    write_status(job, Status(phase=PHASE_DONE))
    return 0


def run_harness(
    job: Path,
    launcher: AgentLauncher = launch_agent,
    *,
    runner: JobRunner = _default_runner,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    identity_root: Path = Path("/"),
    scratch_root: Path | None = None,
) -> int:
    manifest = read_manifest(job)
    write_status(job, Status(phase=PHASE_STARTING))
    adapter = make_agent_adapter(manifest.adapter)

    if manifest.mode == MODE_DRYRUN:
        return _run_identity_dryrun(job, adapter, identity_root, scratch_root)

    # The Output Proposal schema assert (ADR-0046), strictly pre-work: a
    # driver and run image speaking different proposal schemas must fail
    # BEFORE the session starts — a pre-session infra failure (ADR-0016),
    # marked so the driver never classes it as harness breakage.
    mismatch = proposal.schema_mismatch(manifest.schema_version)
    if mismatch is not None:
        write_status(job, Status(phase=PHASE_FAILED, error=mismatch))
        return 1

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
    # THEOZOLITH_JOB lets in-session tools (format-output / view-output)
    # find the manifest and outputs from inside the workdir.
    try:
        agent_env = adapter.prepare(workdir, job, identity_root=identity_root)
    except (AgentAdapterError, OSError) as exc:
        # A prepare that cannot deliver its session preconditions (the codex
        # adapter's missing credential, an unassemblable CODEX_HOME) is a
        # pre-session infra failure with a clean status, never a crash.
        write_status(job, Status(phase=PHASE_FAILED, error=f"agent prepare failed: {exc}"))
        return 1
    env = {**agent_env, "THEOZOLITH_JOB": str(job)}
    pointer = POINTER_PROMPT.format(path=task_file)

    identity_record: dict = {
        "expected_model": "",
        "expected_effort": "",
        "checks": "",
        "category": "",
        "detail": "",
        "observed_model": "",
        "observed_effort": "",
        "violation": "",
        "notes": [],
    }

    def fail_identity(category: str, detail: str) -> int:
        """One identity failure, everywhere it must land: the redacted
        identity.json (full record shape — nothing observed, no violation
        beyond the named check) and the marked status error the driver
        classifies as failure_class identity."""
        identity_record.update(checks=f"failed:{category}", category=category, detail=detail)
        with contextlib.suppress(OSError):
            write_identity(job, identity_record)
        write_status(
            job,
            Status(phase=PHASE_FAILED, error=f"{IDENTITY_ERROR_PREFIX}[{category}] {detail}"),
        )
        return 1

    # The identity checks (ADR-0045, best effort): an image that declares a
    # baked identity gets the free static checks and a monitored launch; an
    # image that declares none (a model-less worker type) launches exactly
    # as before. A corrupt or half-declared identity is
    # identity-inconsistent — it gets the same redacted evidence record as
    # any runtime detection.
    try:
        identity = adapter.baked_identity(identity_root)
    except AgentAdapterError as exc:
        return fail_identity(CATEGORY_INCONSISTENT, str(exc))
    monitor = None
    hooks: MonitorHooks | None = None
    if identity is not None:
        identity_record.update(expected_model=identity.model, expected_effort=identity.effort)
        report = adapter.static_checks(identity, root=identity_root)
        if not report.ok:
            return fail_identity(report.category, report.detail)
        identity_record.update(checks="passed")
        with contextlib.suppress(OSError):  # the status verdict outranks the record
            write_identity(job, identity_record)
        # The observation hooks live in a scratch OUTSIDE the job mount. A
        # scratch that cannot be materialized is an identity failure (the
        # observation channel would be silently absent), never a generic
        # harness crash.
        try:
            scratch = (
                scratch_root
                if scratch_root is not None
                else Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
            )
            scratch.mkdir(parents=True, exist_ok=True)
            hooks = adapter.materialize_hooks(scratch, workdir)
        except OSError as exc:
            return fail_identity(
                CATEGORY_UNVERIFIABLE,
                f"the observation hooks could not be materialized ({exc})"
                " — the task session was never launched",
            )
        monitor = adapter.session_monitor(identity, hooks)
        argv = adapter.monitored_command(manifest, pointer, hooks)
    else:
        argv = adapter.command(manifest, pointer)

    write_status(job, Status(phase=PHASE_AGENT))
    try:
        process = launcher(argv, workdir, env, transcript)
    except OSError as exc:
        write_status(job, Status(phase=PHASE_FAILED, error=f"agent launch failed: {exc}"))
        return 1
    violation = ""
    category = ""
    error = ""
    outcome = AgentOutcome()
    try:
        if monitor is None:
            outcome = await_exit(
                process, manifest, clock=clock, sleep=sleep, poll_seconds=poll_seconds
            )
        else:
            assert identity is not None and hooks is not None
            outcome, violation, category = await_monitored(
                process,
                manifest,
                monitor,
                transcript,
                clock=clock,
                sleep=sleep,
                poll_seconds=poll_seconds,
            )
            # The applied-effort observation. A monitor that carries its own
            # ``observed_effort`` owns the channel (codex: the benign
            # observer already read the rollout turn_context, which echoes
            # the CONFIGURED effort, not a proven post-clamp value) — that
            # reading is the authoritative evidence, no Stop journal is
            # consulted, and no Stop-hook gap applies (ADR-0052). Otherwise
            # the channel is Claude's Stop-hook journal: a DETECTED clamp
            # fails loud; a missing observation is a recorded gap — never a
            # silent pass, never a blocked Run (ADR-0045).
            observed_effort = getattr(monitor, "observed_effort", None)
            if observed_effort is None:
                observed_effort = read_last_journal_effort(hooks.stop_capture)
                if not violation and identity.effort:
                    if observed_effort and observed_effort != identity.effort:
                        violation = (
                            f"the session applied effort {observed_effort!r}, not"
                            f" the baked {identity.effort!r} — an effort surface or"
                            " organization cap superseded the pin"
                        )
                        category = CATEGORY_EFFORT_CLAMPED
                    elif not observed_effort:
                        identity_record["notes"].append(
                            "no applied-effort observation (the Stop hook produced"
                            " no record) — gap recorded, not failed (ADR-0045)"
                        )
            if not monitor.observed_model and not violation:
                identity_record["notes"].append(
                    "no main-agent turn signal in the stream — gap recorded, not failed (ADR-0045)"
                )
            identity_record.update(
                violation=violation,
                observed_model=monitor.observed_model,
                observed_effort=observed_effort,
            )
            if violation:
                identity_record["category"] = category
            # Never let a failed record write erase a DETECTED violation's
            # classification: the identity-marked status below must land
            # even when the evidence write cannot (ENOSPC on the job mount).
            with contextlib.suppress(OSError):
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
        # driver classifies the failure distinctly via the identity marker;
        # the stable category rides in front of the reason.
        marked = f"[{category}] {violation}" if category else violation
        write_status(
            job,
            Status(phase=PHASE_FAILED, agent=outcome, error=IDENTITY_ERROR_PREFIX + marked),
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
