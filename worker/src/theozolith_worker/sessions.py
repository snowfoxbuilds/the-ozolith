"""The driver's view of one live run container: the job-dir file protocol.

A ``RunSession`` is how a driver commissions work from the credential-free
harness: launch the container, wait for the agent phase to end, submit
sequenced jobs (gate steps), shut down. Everything crosses the boundary as
files (ADR-0013); the only docker operations are launch, liveness, and
removal. Tests fake this protocol at the same seam.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from theozolith_worker.containers import ContainerSpec, Engine
from theozolith_worker.jobdir import (
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_SERVING_JOBS,
    AgentOutcome,
    JobRequest,
    Manifest,
    read_job_result,
    read_status,
    write_job_request,
)

WAIT_GRACE_SECONDS = 120.0  # startup + container overhead beyond the timeouts
JOB_GRACE_SECONDS = 60.0
SHUTDOWN_WAIT_SECONDS = 60.0


class SessionError(RuntimeError):
    """The run container failed before producing its outputs."""


class RunSession(Protocol):
    """What a driver needs from one Run's container. Tests provide fakes."""

    def launch(self) -> None: ...

    def wait_for_agent(self) -> AgentOutcome:
        """Block until the agent session has ended (however it ended)."""
        ...

    def run_job(self, name: str, command: str, timeout: float) -> tuple[bool, str]:
        """One harness job in the session workdir; (ok, output)."""
        ...

    def finish(self) -> None:
        """Best-effort graceful shutdown, then force removal. Idempotent."""
        ...


# How drivers build the production session for a prepared job directory.
SessionFactory = Callable[[ContainerSpec, Path, Manifest], RunSession]


def container_session_factory(engine: Engine) -> SessionFactory:
    """The production session factory: one live run container per job dir.
    Lives here (not in a worker type) so no driver imports another — the
    Reviewer and Implementer share this seam (ADR-0020)."""
    return lambda spec, job, manifest: ContainerSession(engine, spec, job, manifest)


class ContainerSession:
    def __init__(
        self,
        engine: Engine,
        spec: ContainerSpec,
        job: Path,
        manifest: Manifest,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 1.0,
    ):
        self._engine = engine
        self._spec = spec
        self._job = job
        self._manifest = manifest
        self._clock = clock
        self._sleep = sleep
        self._poll = poll_seconds
        self._sequence = 0

    def launch(self) -> None:
        # A retried round reuses its deterministic name: clear any leftover.
        self._engine.remove(self._spec.name)
        self._engine.launch(self._spec)

    def _agent_phase_over(self) -> AgentOutcome | None:
        status = read_status(self._job)
        if status is None:
            return None
        if status.phase == PHASE_FAILED:
            raise SessionError(f"harness failed: {status.error or 'no error recorded'}")
        if status.phase in (PHASE_SERVING_JOBS, PHASE_DONE):
            return status.agent or AgentOutcome()
        return None

    def wait_for_agent(self) -> AgentOutcome:
        manifest = self._manifest
        budget = manifest.agent_timeout_seconds + WAIT_GRACE_SECONDS
        deadline = self._clock() + budget
        while True:
            outcome = self._agent_phase_over()
            if outcome is not None:
                return outcome
            if not self._engine.alive(self._spec.name):
                # One last read: the container may have exited cleanly
                # between the status check and the liveness check.
                outcome = self._agent_phase_over()
                if outcome is not None:
                    return outcome
                raise SessionError("run container exited before the agent phase completed")
            if self._clock() >= deadline:
                raise SessionError(f"agent phase did not complete within {budget:.0f}s")
            self._sleep(self._poll)

    def run_job(self, name: str, command: str, timeout: float) -> tuple[bool, str]:
        self._sequence += 1
        job_name = f"{self._sequence:03d}-{name}"
        write_job_request(self._job, JobRequest(job_name, command, timeout))
        deadline = self._clock() + timeout + JOB_GRACE_SECONDS
        while True:
            result = read_job_result(self._job, job_name)
            if result is not None:
                return result.ok, result.output
            if not self._engine.alive(self._spec.name):
                raise SessionError(f"run container died while executing job {job_name}")
            if self._clock() >= deadline:
                raise SessionError(f"job {job_name} produced no result within {timeout:.0f}s")
            self._sleep(self._poll)

    def finish(self) -> None:
        try:
            if self._manifest.serve_jobs and self._engine.alive(self._spec.name):
                self._sequence += 1
                write_job_request(self._job, JobRequest(f"{self._sequence:03d}-shutdown", ""))
                self._engine.wait(self._spec.name, SHUTDOWN_WAIT_SECONDS)
        finally:
            # Container lifetime = Run lifetime, no matter what happened.
            self._engine.remove(self._spec.name)
