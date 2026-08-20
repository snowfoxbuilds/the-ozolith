"""The daemon's docker operations, over the docker CLI.

The Node Daemon observes labeled ephemeral run containers, runs container
Stacks (single image or compose + overlays), builds derived images locally,
and force-removes what drain/recycle/reaping condemns (ADR-0013). The
``runner`` seam is a plain subprocess.run-shaped callable so tests drive the
whole daemon against a fake docker.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

# Labels on ephemeral run containers (created by drivers, ADR-0013).
LABEL_RUN_ID = "theozolith.run-id"
LABEL_OWNER = "theozolith.owner"
# Label on containers the daemon itself runs for container Stacks.
LABEL_STACK = "theozolith.stack"
# Applied effective-spec fingerprint stamped on a single-image stack container
# so convergence survives a daemon restart: the running container carries the
# spec it was launched with, and a mismatch (or a missing label on a
# pre-existing container) triggers exactly one controlled replacement (ADR-0044).
LABEL_STACK_SPEC = "theozolith.spec"

STACK_CONTAINER_PREFIX = "ozolith-stack-"

Runner = Callable[..., subprocess.CompletedProcess]


class DockerError(RuntimeError):
    """A docker operation failed."""


class DockerCtl:
    def __init__(self, runner: Runner | None = None, binary: str = "docker"):
        self._binary = binary
        self._runner = runner or (
            lambda args, timeout=None, env=None: subprocess.run(
                args, capture_output=True, text=True, check=False, timeout=timeout, env=env
            )
        )

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float | None = 300,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        # env=None inherits the daemon's environment (the default for every
        # docker call); a build carrying a registry pull credential passes a
        # copy with DOCKER_CONFIG pointed at its tmpfs config dir (ADR-0049).
        proc = self._runner([self._binary, *args], timeout=timeout, env=env)
        if check and proc.returncode != 0:
            raise DockerError(f"docker {args[0]} failed: {(proc.stderr or '').strip()}")
        return proc

    # -- observation ---------------------------------------------------------

    def _ps(self, label_filter: str) -> list[dict[str, str]]:
        proc = self._run(
            ["ps", "--all", "--filter", f"label={label_filter}", "--format", "{{json .}}"],
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            return []
        rows = []
        for line in (proc.stdout or "").splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            labels = dict(
                item.partition("=")[::2]
                for item in (data.get("Labels") or "").split(",")
                if "=" in item
            )
            rows.append(
                {
                    "name": data.get("Names", ""),
                    "state": data.get("State", ""),
                    "status": data.get("Status", ""),
                    **{k: v for k, v in labels.items()},
                }
            )
        return rows

    def run_containers(self) -> list[dict[str, str]]:
        """Labeled ephemeral run containers on this host (any owner)."""
        return [
            {
                "name": row["name"],
                "run_id": row.get(LABEL_RUN_ID, ""),
                "owner": row.get(LABEL_OWNER, ""),
                "status": row.get("status", ""),
            }
            for row in self._ps(LABEL_OWNER)
        ]

    def stack_containers(self, stack: str | None = None) -> list[dict[str, str]]:
        label = LABEL_STACK if stack is None else f"{LABEL_STACK}={stack}"
        return self._ps(label)

    def _container_exists(self, name: str) -> bool:
        """Postcondition backstop for ``remove``: is a container by this exact
        name still present? A ``ps`` failure cannot prove absence, so it reports
        present (fail closed — a genuine removal failure must never masquerade as
        idempotent success)."""
        proc = self._run(
            ["ps", "--all", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            return True
        return any(line.strip() == name for line in (proc.stdout or "").splitlines())

    def remove(self, name: str) -> None:
        """Force-remove a container, raising DockerError on a genuine failure.

        A previous version suppressed the CLI return code, so a failed
        ``docker rm --force`` silently looked like success — letting a caller
        start a successor while the predecessor still ran (two forms under one
        name). Removal stays IDEMPOTENT: an already-absent container is success,
        classified from the CLI's ``no such container`` and, as a backstop for
        any other non-zero result, confirmed by a postcondition existence check.
        Only a container that genuinely survives the remove raises (ADR-0044
        amendment)."""
        proc = self._run(["rm", "--force", name], check=False, timeout=60)
        if proc.returncode == 0:
            return
        stderr = proc.stderr or ""
        if "no such container" in stderr.lower():
            return  # already absent — idempotent success
        if not self._container_exists(name):
            return  # gone despite the non-zero result — idempotent success
        raise DockerError(f"docker rm failed for {name}: {stderr.strip()}")

    # -- container Stacks ------------------------------------------------------

    def run_stack_container(
        self,
        stack: str,
        image: str,
        *,
        env_files: dict[str, str],  # ENV_NAME -> host secret path
        env: dict[str, str],
        ports: list[str],
        volumes: list[str],
        command: list[str] | None = None,
        spec: str = "",
    ) -> None:
        """Single-image container Stack: one long-running container.

        A configured ``command`` is the FULL container start command, not an
        argument list for the image's own entrypoint: its first token is
        passed via ``--entrypoint`` (replacing any ENTRYPOINT the image
        inherited — e.g. a derived run image's harness) and the remaining
        tokens ride after the image as that entrypoint's arguments. Appending
        the tokens after the image alone would hand them to the inherited
        ENTRYPOINT as argv instead of executing them. With no ``command``,
        nothing is passed and the image's own ENTRYPOINT/CMD run unchanged."""
        name = f"{STACK_CONTAINER_PREFIX}{stack}"
        self.remove(name)
        args = [
            "run",
            "--detach",
            "--restart",
            "unless-stopped",
            "--name",
            name,
            "--label",
            f"{LABEL_STACK}={stack}",
        ]
        if spec:
            args += ["--label", f"{LABEL_STACK_SPEC}={spec}"]
        if command:
            args += ["--entrypoint", command[0]]
        for key, value in sorted(env.items()):
            args += ["--env", f"{key}={value}"]
        for key, host_path in sorted(env_files.items()):
            # The value stays in a file: mounted read-only at the /run/secrets
            # convention path, referenced via VAR_FILE (NODE-SUBSTRATE.md).
            target = f"/run/secrets/{Path(host_path).name}"
            args += ["--volume", f"{host_path}:{target}:ro", "--env", f"{key}_FILE={target}"]
        for port in ports:
            args += ["--publish", port]
        for volume in volumes:
            args += ["--volume", volume]
        args.append(image)
        if command:
            args.extend(command[1:])
        self._run(args)

    def compose(self, project: str, files: list[Path], verb: str) -> None:
        """Compose-form container Stack: base file + overlays, in order."""
        args = ["compose", "--project-name", project]
        for path in files:
            args += ["--file", str(path)]
        if verb == "up":
            args += ["up", "--detach", "--remove-orphans"]
        elif verb == "down":
            args += ["down", "--remove-orphans"]
        else:
            raise DockerError(f"unsupported compose verb {verb!r}")
        self._run(args, timeout=600)

    def compose_ps(self, project: str) -> list[dict[str, str]]:
        """Containers of one compose project (status reporting)."""
        proc = self._run(
            ["compose", "--project-name", project, "ps", "--all", "--format", "json"],
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            return []
        rows = []
        for line in (proc.stdout or "").splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                rows.append(
                    {
                        "name": data.get("Name", ""),
                        "state": (data.get("State") or "").lower(),
                        "status": data.get("Status", ""),
                    }
                )
        return rows

    # -- images ------------------------------------------------------------------

    def image_exists(self, tag: str) -> bool:
        proc = self._run(["image", "inspect", tag], check=False, timeout=60)
        return proc.returncode == 0

    def image_labels(self, tag: str) -> dict[str, str]:
        proc = self._run(
            ["image", "inspect", "--format", "{{json .Config.Labels}}", tag],
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            return {}
        try:
            labels = json.loads(proc.stdout.strip() or "null")
        except json.JSONDecodeError:
            return {}
        return labels if isinstance(labels, dict) else {}

    def build(
        self,
        context_dir: Path,
        tag: str,
        *,
        no_cache: bool = False,
        docker_config: Path | None = None,
    ) -> None:
        args = ["build", "--tag", tag]
        if no_cache:
            args.append("--no-cache")
        args.append(str(context_dir))
        # A private base needs a pull credential at build time (ADR-0049):
        # DOCKER_CONFIG names a tmpfs dir holding a config.json with the
        # registry auth, scoped to this one build. None inherits the daemon's
        # environment (public bases resolve anonymously).
        env = None
        if docker_config is not None:
            env = {**os.environ, "DOCKER_CONFIG": str(docker_config)}
        self._run(args, timeout=3600, env=env)
