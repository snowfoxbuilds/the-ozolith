"""The daemon's docker operations, over the docker CLI.

The Node Daemon observes labeled ephemeral run containers, runs container
Stacks (single image or compose + overlays), builds derived images locally,
and force-removes what drain/recycle/reaping condemns (ADR-0013). The
``runner`` seam is a plain subprocess.run-shaped callable so tests drive the
whole daemon against a fake docker.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

# Labels on ephemeral run containers (created by drivers, ADR-0013).
LABEL_RUN_ID = "theozolith.run-id"
LABEL_OWNER = "theozolith.owner"
# Label on containers the daemon itself runs for container Stacks.
LABEL_STACK = "theozolith.stack"

STACK_CONTAINER_PREFIX = "ozolith-stack-"

Runner = Callable[..., subprocess.CompletedProcess]


class DockerError(RuntimeError):
    """A docker operation failed."""


class DockerCtl:
    def __init__(self, runner: Runner | None = None, binary: str = "docker"):
        self._binary = binary
        self._runner = runner or (
            lambda args, timeout=None: subprocess.run(
                args, capture_output=True, text=True, check=False, timeout=timeout
            )
        )

    def _run(
        self, args: list[str], *, check: bool = True, timeout: float | None = 300
    ) -> subprocess.CompletedProcess:
        proc = self._runner([self._binary, *args], timeout=timeout)
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

    def remove(self, name: str) -> None:
        self._run(["rm", "--force", name], check=False, timeout=60)

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
    ) -> None:
        """Single-image container Stack: one long-running container."""
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
        args.extend(command or [])
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

    def build(self, context_dir: Path, tag: str, *, no_cache: bool = False) -> None:
        args = ["build", "--tag", tag]
        if no_cache:
            args.append("--no-cache")
        args.append(str(context_dir))
        self._run(args, timeout=3600)
