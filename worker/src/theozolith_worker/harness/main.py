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
amendment), the launch runs behind a real pre-verification boundary. The
harness first WITHHOLDS the task: ``input/prompt.md`` is read into memory
and removed from disk, so no pre-verification process can read it. The
adapter's preflight then proves the effective policy with the Run's own
credential in a neutral scratch outside the job mount — canaries and an
identity probe with no tools, no project sources, no MCP. Only after that
proof is the task session launched at all — itself loading NO user/project/
local settings source, so nothing a checkout or home directory declares can
steer it — stdin-driven, with every tool denied by a release-marker hook,
and its first turn a no-op probe; the task file is restored and the pointer
prompt sent only once the session's own announced and executed identity
match AND the probe turn has completely stopped (its record in the
Stop-hook turn journal, the probe/task boundary). One deadline, started
before the preflight, budgets verification and task together: exhausting it
pre-release withholds the task with the timeout category, and the driver's
own wait (agent timeout + grace) can never expire first. The release is
atomic and observable: ``released`` is recorded only after the task input
write and delivery succeed, delivery failures are classified (task-delivery
vs stdin-close), and an exit whose journal shows no completed post-release
turn is ``task-unprocessed``, never a completed Run. A violation at any
point — a corrupt identity declaration, a boundary that cannot be
established, preflight, gate, delivery, mid-session drift, or an
identity-affecting ConfigChange — kills the agent and fails the Run with a
distinct ``identity:`` error, a stable category, and a redacted
``output/identity.json`` record the driver embeds into evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from theozolith_worker import shell
from theozolith_worker.adapters import AgentAdapterError, make_agent_adapter
from theozolith_worker.identity import (
    CATEGORY_INCONSISTENT,
    CATEGORY_STDIN_CLOSE,
    CATEGORY_TASK_DELIVERY,
    CATEGORY_TASK_UNPROCESSED,
    CATEGORY_TIMEOUT,
    CATEGORY_UNVERIFIABLE,
    CONFIG_CHANGE_HOOK_SOURCE,
    GUARD_KILL,
    GUARD_RELEASE,
    IDENTITY_ERROR_PREFIX,
    STOP_HOOK_SOURCE,
    TaskGate,
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
    atomic_write,
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
# The identity gate's scratch lives OUTSIDE the job mount (a container-local
# temp dir): the canary/probe sessions run with it as cwd — a neutral
# directory with no task inputs, no project settings, no CLAUDE.md — and the
# task session's gate files (release marker, capture files, hook helper)
# live under it too, where nothing in the checkout can name or reach them
# by a job-relative path.
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
        # No suppression: a failing close after task delivery is classified
        # explicitly (stdin-close) instead of silently becoming a Run whose
        # session never learned its input ended.
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


# deliver() -> (released, violation, category): performs the release protocol
# (open the tool gate, write the task input, send the pointer, close stdin)
# and reports exactly how far it got. ``released`` is True only once the
# task prompt has actually entered the process.
TaskDeliverer = Callable[[], tuple[bool, str, str]]


def await_guarded(
    process: AgentProcess,
    manifest: Manifest,
    guard,
    transcript: Path,
    deliver: TaskDeliverer,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    kill_grace_seconds: float = KILL_GRACE_SECONDS,
    release_timeout_seconds: float = GATE_RELEASE_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> tuple[AgentOutcome, str, str, bool]:
    """The gated wait (ADR-0045): feed the growing transcript to the session
    guard; run the release protocol only when the guard verifies the
    session's identity; keep monitoring after release and kill on drift.

    ``deadline`` is the Run's single end-to-end budget (started before the
    preflight): the release wait ends at the earlier of it and the release
    timeout, and the task itself runs only until it — verification time is
    never silently added on top of ``agent_timeout_seconds``. Without one,
    the budget is the manifest timeout from this call.

    Returns ``(outcome, violation, category, released)`` — a nonempty
    violation means the Run is invalid (category names the stable failure
    class), and ``released`` says whether the task prompt ever entered the
    process. A zero exit that never processed the released task (no
    completed post-release turn in the boundary journal) is a violation,
    not a completed Run."""
    start = clock()
    if deadline is None:
        deadline = start + manifest.agent_timeout_seconds
    release_deadline = min(start + release_timeout_seconds, deadline)
    offset = 0
    buffer = ""
    released = False

    def feed() -> None:
        nonlocal offset, buffer
        offset, buffer, lines = _read_new_lines(transcript, offset, buffer)
        for line in lines:
            guard.observe(line)

    def killed(reason: str, category: str) -> tuple[AgentOutcome, str, str, bool]:
        _shutdown(
            process,
            clock=clock,
            sleep=sleep,
            poll_seconds=poll_seconds,
            kill_grace_seconds=kill_grace_seconds,
        )
        return AgentOutcome(), reason, category, released

    while True:
        feed()
        decision = guard.decision()
        if decision.action == GUARD_KILL:
            return killed(decision.reason, decision.category)
        if decision.action == GUARD_RELEASE and not released:
            released, violation, category = deliver()
            if violation:
                return killed(violation, category)
        code = process.poll()
        if code is not None:
            drain = getattr(process, "drain", None)
            if drain is not None:
                drain()
            feed()
            decision = guard.decision()
            if decision.action == GUARD_KILL:
                return AgentOutcome(), decision.reason, decision.category, released
            if not released:
                return (
                    AgentOutcome(),
                    f"agent session exited (exit {code}) before the identity"
                    " gate verified the session — the task prompt was never"
                    " sent",
                    CATEGORY_UNVERIFIABLE,
                    released,
                )
            if code == 0 and guard.task_turns == 0:
                # Exit zero after only the probe: the released task was never
                # observably processed — a stdin EOF race, a session that
                # quit early, or residual probe output must not become a
                # completed Run. The proof is the boundary journal (a
                # completed post-release turn), never a late-read assistant
                # event.
                return (
                    AgentOutcome(),
                    "the agent exited without processing the released task"
                    " (no completed post-release turn in the boundary journal)",
                    CATEGORY_TASK_UNPROCESSED,
                    released,
                )
            return (
                AgentOutcome(completed=code == 0, session_died=code != 0, exit_code=code),
                "",
                "",
                released,
            )
        now = clock()
        if not released and now >= release_deadline:
            return killed(
                "the session identity could not be verified within"
                f" {max(release_deadline - start, 0):.0f}s (the remaining"
                " verification budget) — the task prompt was never sent",
                CATEGORY_TIMEOUT,
            )
        if now >= deadline:
            _shutdown(
                process,
                clock=clock,
                sleep=sleep,
                poll_seconds=poll_seconds,
                kill_grace_seconds=kill_grace_seconds,
            )
            return AgentOutcome(timed_out=True), "", "", released
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
    scratch_root: Path | None = None,
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

    identity_record: dict = {
        "expected_model": "",
        "expected_effort": "",
        "preflight": "",
        "category": "",
        "detail": "",
        "cli_version": "",
        "canary_model": "",
        "probe_model": "",
        "probe_effort": "",
        "released": False,
        "violation": "",
        "observed_model": "",
        "observed_effort": "",
        "probe_turns": 0,
        "task_turns": 0,
    }

    def fail_identity(category: str, detail: str) -> int:
        """One identity failure, everywhere it must land: the redacted
        identity.json (full record shape, internally consistent — nothing
        released, nothing observed, no turns) and the marked status error
        the driver classifies as failure_class identity."""
        identity_record.update(preflight=f"failed:{category}", category=category, detail=detail)
        with contextlib.suppress(OSError):
            write_identity(job, identity_record)
        write_status(
            job,
            Status(phase=PHASE_FAILED, error=f"{IDENTITY_ERROR_PREFIX}[{category}] {detail}"),
        )
        return 1

    # The identity gate (ADR-0045): an image that declares a baked identity
    # must prove it effective before the task prompt is sent; an image that
    # declares none (a model-less worker type) launches exactly as before.
    # A corrupt or half-declared identity is identity-inconsistent — it gets
    # the same redacted evidence record as any later gate failure.
    try:
        identity = adapter.baked_identity(identity_root)
    except AgentAdapterError as exc:
        return fail_identity(CATEGORY_INCONSISTENT, str(exc))
    guard = None
    gate: TaskGate | None = None
    withheld_task = ""

    def restore_task() -> None:
        # Best-effort restore of the withheld task input so the evidence
        # bundle of a failed gated Run still carries what the Run was asked
        # to do. Never before the agent process is dead or the gate released.
        if withheld_task and not task_file.exists():
            with contextlib.suppress(OSError):
                atomic_write(task_file, withheld_task)

    if identity is not None:
        identity_record.update(expected_model=identity.model, expected_effort=identity.effort)
        # ONE deadline budgets everything from here to task exit (ADR-0045):
        # started before the preflight, so verification spends the Run's own
        # agent_timeout_seconds — never a silent extension of it, and the
        # driver's wait (agent timeout + its grace) can never expire first.
        deadline = clock() + manifest.agent_timeout_seconds
        # The pre-verification boundary starts here: the task input leaves
        # the disk before ANY subprocess runs, and every verification
        # session works in a neutral scratch outside the job mount. A
        # boundary that cannot be established (scratch, withhold, or hook
        # I/O failure) is an identity failure with the task withheld — never
        # a generic harness crash.
        try:
            scratch = (
                scratch_root
                if scratch_root is not None
                else Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
            )
            preflight_dir = scratch / "preflight"
            preflight_dir.mkdir(parents=True, exist_ok=True)
            withheld_task = task_file.read_text(encoding="utf-8")
            task_file.unlink()
        except OSError as exc:
            restore_task()
            return fail_identity(
                CATEGORY_UNVERIFIABLE,
                f"the pre-verification boundary could not be established ({exc})"
                " — the task session was never launched",
            )
        try:
            report = adapter.preflight(
                identity,
                root=identity_root,
                scratch=preflight_dir,
                deadline=deadline,
                clock=clock,
            )
        except AgentAdapterError as exc:
            restore_task()
            return fail_identity(CATEGORY_UNVERIFIABLE, str(exc))
        except OSError as exc:
            restore_task()
            return fail_identity(
                CATEGORY_UNVERIFIABLE,
                f"the pre-verification boundary could not be established ({exc})"
                " — the task session was never launched",
            )
        identity_record.update(
            preflight="passed" if report.ok else f"failed:{report.category}",
            category="" if report.ok else report.category,
            detail=report.detail,
            cli_version=report.cli_version,
            canary_model=report.canary_model,
            probe_model=report.probe_model,
            probe_effort=report.probe_effort,
        )
        write_identity(job, identity_record)
        if not report.ok:
            restore_task()
            write_status(
                job,
                Status(phase=PHASE_FAILED, error=IDENTITY_ERROR_PREFIX + report.describe()),
            )
            return 1
        try:
            gate_dir = scratch / "gate"
            gate_dir.mkdir(parents=True, exist_ok=True)
            gate = TaskGate(
                release_marker=gate_dir / "released",
                stop_capture=gate_dir / "stop.jsonl",
                config_capture=gate_dir / "config-change.jsonl",
                config_hook_script=gate_dir / "configchange_hook.py",
                stop_hook_script=gate_dir / "stop_hook.py",
            )
            gate.config_hook_script.write_text(CONFIG_CHANGE_HOOK_SOURCE, encoding="utf-8")
            gate.stop_hook_script.write_text(STOP_HOOK_SOURCE, encoding="utf-8")
        except OSError as exc:
            restore_task()
            return fail_identity(
                CATEGORY_UNVERIFIABLE,
                f"the gate hooks could not be materialized ({exc}) — the task"
                " session was never launched",
            )
        guard = adapter.session_guard(identity, gate)
        argv = adapter.guarded_command(manifest, gate)
    else:
        argv = adapter.command(manifest, pointer)

    write_status(job, Status(phase=PHASE_AGENT))
    try:
        if guard is None:
            process = launcher(argv, workdir, env, transcript)
        else:
            process = launcher(argv, workdir, env, transcript, True)
    except OSError as exc:
        restore_task()
        write_status(job, Status(phase=PHASE_FAILED, error=f"agent launch failed: {exc}"))
        return 1
    violation = ""
    category = ""
    error = ""
    outcome = AgentOutcome()
    try:
        if guard is None:
            outcome = await_exit(
                process, manifest, clock=clock, sleep=sleep, poll_seconds=poll_seconds
            )
        else:
            assert gate is not None

            def deliver() -> tuple[bool, str, str]:
                """The atomic release protocol: open the tool gate, put the
                task input back on disk, deliver the pointer, close stdin —
                released only counts once the prompt has entered the
                process, and every failure names its stage."""
                try:
                    gate.release_marker.write_text("released\n", encoding="utf-8")
                    atomic_write(task_file, withheld_task)
                except OSError as exc:
                    return (
                        False,
                        f"the task input could not be materialized for release ({exc})",
                        CATEGORY_TASK_DELIVERY,
                    )
                try:
                    process.send(guard.render_input(pointer))
                except OSError as exc:
                    return (
                        False,
                        f"the task prompt could not be delivered to the session ({exc})",
                        CATEGORY_TASK_DELIVERY,
                    )
                guard.mark_released()
                try:
                    process.end_input()
                except OSError as exc:
                    return (
                        True,
                        f"the input channel could not be closed after task delivery ({exc})",
                        CATEGORY_STDIN_CLOSE,
                    )
                return True, "", ""

            # A dead process here is reported by the guarded wait below.
            with contextlib.suppress(OSError):
                process.send(guard.probe_input())
            outcome, violation, category, released = await_guarded(
                process,
                manifest,
                guard,
                transcript,
                deliver,
                clock=clock,
                sleep=sleep,
                poll_seconds=poll_seconds,
                deadline=deadline,
            )
            identity_record.update(
                released=released,
                violation=violation,
                category=category,
                observed_model=guard.observed_model,
                observed_effort=guard.observed_effort,
                probe_turns=guard.probe_turns,
                task_turns=guard.task_turns,
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
        # Whatever happened, no agent process outlives the harness — and the
        # withheld task returns to disk for the evidence bundle only after
        # nothing unverified is left running to read it.
        if process.poll() is None:
            process.kill()
        restore_task()

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
