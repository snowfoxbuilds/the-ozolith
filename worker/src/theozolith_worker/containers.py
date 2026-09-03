"""Run-container conventions and the docker engine the drivers drive.

Every Run and review round executes in one ephemeral container created by a
driver (ADR-0013): deterministic names (``ozolith-run-<run-id>``,
``ozolith-review-<pr>-round-<n>``), identifying labels (``theozolith.run-id``,
``theozolith.owner=<stack>``), container lifetime = Run lifetime, and warm
dependency caches as named volumes. Run containers are headless and never
attach targets (ADR-0019) — diagnostics are progress telemetry and the
evidence bundle, never a shell inside the container.

The ``Engine`` protocol is the seam tests fake; ``DockerEngine`` shells out
to the docker CLI. Secret env values are passed to ``docker run`` as bare
``-e NAME`` (value read from the CLI's process environment), so no secret
ever appears in argv.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

LABEL_RUN_ID = "theozolith.run-id"
LABEL_OWNER = "theozolith.owner"

RUN_NAME_PREFIX = "ozolith-run-"
REVIEW_NAME_PREFIX = "ozolith-review-"

# docker's definitive "the object does not exist" answers (case-insensitive):
# an exited --rm container is gone, which is evidence of absence. ANY OTHER
# inspect failure is a failed observation and proves nothing (the substrate
# observation doctrine, NODE-SUBSTRATE.md; the dockerctl remove() precedent).
_NO_SUCH_OBJECT = ("no such object", "no such container")

# The three outcomes of one aliveness inspect, before the bounded-retry policy.
_ALIVE = "alive"
_ABSENT = "absent"  # definitively not running (exited, or --rm removed)
_UNOBSERVED = "unobserved"  # the inspect itself failed — proves nothing


def _is_no_such_object(stderr: str) -> bool:
    low = (stderr or "").lower()
    return any(marker in low for marker in _NO_SUCH_OBJECT)


class EngineError(RuntimeError):
    """The container engine could not perform an operation."""


def run_container_name(run_id: str) -> str:
    return f"{RUN_NAME_PREFIX}{run_id}"


def review_container_name(pr_number: int, round_number: int) -> str:
    return f"{REVIEW_NAME_PREFIX}{pr_number}-round-{round_number}"


@dataclass(frozen=True)
class ContainerSpec:
    """Everything a driver declares about one ephemeral run container."""

    name: str
    image: str
    labels: dict[str, str] = field(default_factory=dict)
    mounts: tuple[tuple[str, str], ...] = ()  # (host path, container path)
    volumes: tuple[tuple[str, str], ...] = ()  # (named volume, container path)
    env: dict[str, str] = field(default_factory=dict)
    user: str | None = None  # uid:gid; None = the image's default user


def container_labels(run_id: str, stack: str) -> dict[str, str]:
    return {LABEL_RUN_ID: run_id, LABEL_OWNER: stack}


class Engine(Protocol):
    """The container operations a driver needs. Tests provide fakes."""

    def launch(self, spec: ContainerSpec) -> None:
        """Create and start the container, detached, auto-removed on exit."""
        ...

    def alive(self, name: str) -> bool:
        """Whether the container is running. Docker's definitive no-such-object
        answer means it is gone (an exited --rm container). A NON-DEFINITIVE
        observation failure is never reported as 'not alive': it is retried
        bounded, then RAISES EngineError (the substrate observation doctrine,
        NODE-SUBSTRATE.md)."""
        ...

    def wait(self, name: str, timeout: float) -> int | None:
        """Block until the container exits; exit code, or None on timeout. A
        non-definitive observation failure RAISES EngineError rather than
        fabricate an exit — session completion is never inferred from a failed
        read."""
        ...

    def remove(self, name: str) -> None:
        """Force-remove the container; a no-op when it is already gone."""
        ...


class DockerEngine:
    """Drives the docker CLI. Requires docker on the driver's PATH."""

    def __init__(
        self,
        binary: str = "docker",
        *,
        alive_attempts: int = 3,
        alive_retry_seconds: float = 2.5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._binary = binary
        # Bounded aliveness retry for a NON-DEFINITIVE inspect failure (grilling
        # 2026-09-02): a Run is expensive and its finished Output Proposal may
        # already sit in the job dir, so a transient blip is retried a few times
        # over ~5s before failing loud — never read as an exit. The sleep seam
        # is injectable so tests exercise the retry with no real delay.
        self._alive_attempts = alive_attempts
        self._alive_retry_seconds = alive_retry_seconds
        self._sleep = sleep

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [self._binary, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={**os.environ, **env} if env else None,
        )
        if check and proc.returncode != 0:
            raise EngineError(f"docker {args[0]} failed: {proc.stderr.strip()}")
        return proc

    def launch(self, spec: ContainerSpec) -> None:
        args = ["run", "--detach", "--rm", "--init", "--name", spec.name]
        for key, value in sorted(spec.labels.items()):
            args += ["--label", f"{key}={value}"]
        for host, container in spec.mounts:
            args += ["--volume", f"{Path(host).resolve()}:{container}"]
        for volume, container in spec.volumes:
            args += ["--volume", f"{volume}:{container}"]
        # Bare -e NAME: docker reads the value from its own environment, so
        # secrets (the model credential) never appear in a process listing.
        for key in sorted(spec.env):
            args += ["--env", key]
        if spec.user:
            args += ["--user", spec.user]
        args.append(spec.image)
        self._run(args, env=dict(spec.env))

    def _observe_running(self, name: str) -> str:
        """One docker inspect, classified per the observation doctrine.

        ``{{.State.Running}}`` prints exactly ``true`` or ``false`` when the
        container exists, and ONLY those two values are trusted: ``true`` is
        alive, ``false`` is a definitive exited answer (absence). A zero exit
        carrying anything else — blank output, a partial line, an error string
        the CLI wrote to stdout, any unexpected token — is a failed read that
        proves nothing, classified UNOBSERVED so the bounded-retry policy
        governs it (never silently read as absence). Among non-zero exits,
        docker's definitive no-such-object error is evidence of absence (an
        exited --rm container); any other proves nothing."""
        proc = self._run(
            ["inspect", "--format", "{{.State.Running}}", name], check=False, timeout=30
        )
        if proc.returncode == 0:
            value = proc.stdout.strip()
            if value == "true":
                return _ALIVE
            if value == "false":
                return _ABSENT
            return _UNOBSERVED
        if _is_no_such_object(proc.stderr or ""):
            return _ABSENT
        return _UNOBSERVED

    def _alive_or_raise(self, name: str) -> bool:
        """The shared aliveness path for alive() and wait()'s fallback: a
        definitive answer (running, or absent) returns at once; a
        non-definitive observation failure retries bounded, then raises
        EngineError. Never infers absence or completion from a failed read."""
        for attempt in range(self._alive_attempts):
            outcome = self._observe_running(name)
            if outcome == _ALIVE:
                return True
            if outcome == _ABSENT:
                return False
            if attempt + 1 < self._alive_attempts:
                self._sleep(self._alive_retry_seconds)
        raise EngineError(
            f"docker inspect for {name} failed on every one of {self._alive_attempts}"
            " attempts — container aliveness unobservable (observation doctrine)"
        )

    def alive(self, name: str) -> bool:
        return self._alive_or_raise(name)

    def wait(self, name: str, timeout: float) -> int | None:
        try:
            proc = self._run(["wait", name], check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            # `docker wait` failed — resolve through the shared aliveness path so
            # its doctrine governs here too: a definitively absent container
            # exited (--rm removed it) -> 0; a verifiably alive one is still
            # running -> None (the caller re-waits); an unobservable inspect
            # RAISES rather than fabricate an exit 0.
            return 0 if not self._alive_or_raise(name) else None
        return int(proc.stdout.strip())

    def remove(self, name: str) -> None:
        self._run(["rm", "--force", name], check=False, timeout=60)
