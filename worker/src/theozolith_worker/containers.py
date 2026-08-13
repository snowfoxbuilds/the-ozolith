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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

LABEL_RUN_ID = "theozolith.run-id"
LABEL_OWNER = "theozolith.owner"

RUN_NAME_PREFIX = "ozolith-run-"
REVIEW_NAME_PREFIX = "ozolith-review-"


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

    def alive(self, name: str) -> bool: ...

    def wait(self, name: str, timeout: float) -> int | None:
        """Block until the container exits; exit code, or None on timeout."""
        ...

    def remove(self, name: str) -> None:
        """Force-remove the container; a no-op when it is already gone."""
        ...


class DockerEngine:
    """Drives the docker CLI. Requires docker on the driver's PATH."""

    def __init__(self, binary: str = "docker"):
        self._binary = binary

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

    def alive(self, name: str) -> bool:
        proc = self._run(
            ["inspect", "--format", "{{.State.Running}}", name], check=False, timeout=30
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def wait(self, name: str, timeout: float) -> int | None:
        try:
            proc = self._run(["wait", name], check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            # The container is already gone (--rm removed it): treat as exited.
            return 0 if not self.alive(name) else None
        return int(proc.stdout.strip())

    def remove(self, name: str) -> None:
        self._run(["rm", "--force", name], check=False, timeout=60)
