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


def _decode_ps_rows(stdout: str, verb: str) -> list[dict]:
    """Parse a docker/compose ``ps`` JSON-lines listing, fail-closed.

    A failed observation is never evidence of absence, and neither is an
    unparseable one (NODE-SUBSTRATE observation doctrine, grilling 2026-09-02):
    empty stdout is the legitimate zero-container result, but EVERY non-empty
    row must be well-formed. A row that is not valid JSON, or is valid JSON that
    is not an object, RAISES ``DockerError``. A malformed row is never silently
    skipped, so a mixture of good and bad rows fails the whole observation
    rather than returning a partial listing that under-counts containers (the
    same class of bug as coercing a 500 to empty). Field-shape validation is the
    caller's, on the object this returns.
    """
    rows: list[dict] = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue  # a blank separator carries no record (empty stdout => [])
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DockerError(f"docker {verb} returned malformed JSON: {line[:120]!r}") from exc
        if not isinstance(data, dict):
            raise DockerError(
                f"docker {verb} returned an unexpected JSON shape "
                f"({type(data).__name__}, expected object): {line[:120]!r}"
            )
        rows.append(data)
    return rows


def _require_str_fields(row: dict, fields: tuple[str, ...], verb: str) -> None:
    """Every named field of the supported ``ps`` shape must be PRESENT and a
    string. The explicit ``{{json .Field}}`` format guarantees each field as a
    JSON string on every row, so a missing key, a JSON ``null``, or any other
    type is malformed observation data — never an optional value to default.
    Each case raises ``DockerError`` naming only the verb and field (no
    unbounded row content). Defaulting instead would let a null ``State`` read
    as stopped, or a null/absent ``Labels`` collapse to an empty label set that
    provokes a missing-spec replacement — the whole observation is rejected here,
    before any destructive consumer can act on a fabricated shape."""
    for key in fields:
        if key not in row:
            raise DockerError(f"docker {verb} row is missing required field {key!r}")
        value = row[key]
        if value is None:
            raise DockerError(f"docker {verb} row field {key!r} is null (expected string)")
        if not isinstance(value, str):
            raise DockerError(
                f"docker {verb} row field {key!r} has unexpected type "
                f"{type(value).__name__} (expected string)"
            )


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
        # A failed observation is never evidence of absence (NODE-SUBSTRATE
        # observation doctrine, grilling 2026-09-02): a non-zero exit RAISES
        # DockerError (check defaults True) rather than coercing to an empty
        # listing — every consumer that would act destructively on "no
        # containers" now surfaces the failure instead. NO in-call retry: the
        # ~60s reconcile pass cadence is the retry. The PARSE is fail-closed
        # too (_decode_ps_rows): a non-empty listing with a malformed or
        # wrong-shape row raises rather than return a partial, under-counting
        # result — an unparseable read is as blind as a non-zero one.
        #
        # The explicit per-field format is deliberate, NOT `{{json .}}`: the
        # whole-struct form makes the docker CLI request container sizes
        # (size=1), an overlay-snapshot walk that races this repo's own temp
        # churn and returns a transient dockerd 500 (the #109 root cause). This
        # format references no `.Size`, so no size walk happens; the row shape
        # (name/state/status + flattened labels) is byte-for-byte what every
        # consumer already reads.
        proc = self._run(
            [
                "ps",
                "--all",
                "--filter",
                f"label={label_filter}",
                "--format",
                '{"Names":{{json .Names}},"State":{{json .State}},'
                '"Status":{{json .Status}},"Labels":{{json .Labels}}}',
            ],
            timeout=60,
        )
        rows = []
        for data in _decode_ps_rows(proc.stdout, "ps"):
            _require_str_fields(data, ("Names", "State", "Status", "Labels"), "ps")
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
        tmpfs: list[str] | None = None,
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
        # No Docker restart policy on daemon-managed Stack containers. The
        # daemon is the sole reconciler of Stack desired state (NODE-SUBSTRATE.md)
        # and already recreates a stopped/exited/crashed container on its next
        # pass — with freshly materialized secrets. A Docker `--restart` policy
        # would instead let dockerd restart these containers on host boot BEFORE
        # the daemon has materialized their secrets onto the freshly-wiped tmpfs
        # (`/run/theozolith` is a systemd RuntimeDirectory, so empty every boot):
        # the restarted container's missing bind source is then auto-vivified by
        # dockerd as a DIRECTORY, which both fails the mount (dir->file mismatch)
        # and wedges the secret writer (#114). Leaving restart to the reconcile
        # loop keeps secret materialization strictly ahead of every start.
        args = [
            "run",
            "--detach",
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
        # tmpfs mounts (grilling 2026-09-02): each entry is docker's own
        # `--tmpfs` value syntax (`/path` or `/path:opts`), emitted in declared
        # order. RAM-backed scratch off the overlay writable layer — invisible
        # to the size walk that made `docker ps` racy at the source.
        for entry in tmpfs or []:
            args += ["--tmpfs", entry]
        args.append(image)
        if command:
            args.extend(command[1:])
        self._run(args)

    def container_restart_policy(self, name: str) -> str | None:
        """The configured restart-policy name of a container — ``no``, ``always``,
        ``unless-stopped``, or ``on-failure`` — or ``None`` when the container has
        DISAPPEARED.

        Reads ``HostConfig.RestartPolicy.Name`` via ``docker inspect``. A
        container created with no ``--restart`` flag reports ``no`` (older Docker
        can report an empty string; the caller treats both as already-converged).

        The two non-zero outcomes are kept DISTINCT (observation doctrine, #114):

        - A definitive ``no such container``/``no such object`` is not a failed
          observation — it is positive evidence the container vanished between the
          listing and this inspect (a benign migration race). It returns ``None``
          so the caller skips it without an error event — no ``theozolith.error``,
          only an informational log — since a container that simply went away is
          not an infrastructure fault.
        - Any OTHER non-zero result is a failed observation (dockerd unreachable, a
          transient 500) and RAISES ``DockerError`` with secret-free context — a
          failed read is never evidence the policy is already ``no``, so the
          migration caller surfaces it and retries on a later pass rather than
          treating an unreadable container as converged.
        """
        proc = self._run(
            ["inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", name],
            check=False,
            timeout=60,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
        stderr = (proc.stderr or "").lower()
        if "no such container" in stderr or "no such object" in stderr:
            return None  # gone between listing and inspect — a benign race
        raise DockerError(
            f"docker inspect failed for restart policy of {name}: {(proc.stderr or '').strip()}"
        )

    def set_restart_policy(self, name: str, policy: str = "no") -> None:
        """Converge a container's restart policy IN PLACE, without recreating or
        restarting it (``docker update --restart``). Used to retire the legacy
        ``unless-stopped`` policy off daemon-managed single-image Stack containers
        (#114) so dockerd never restarts them ahead of tmpfs secret
        materialization on boot; the reconcile loop becomes the sole restarter.
        Raises ``DockerError`` on failure so the caller can isolate and retry."""
        self._run(["update", f"--restart={policy}", name], timeout=60)

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
        """Containers of one compose project (status reporting).

        Fail-closed like ``_ps`` (observation doctrine): a non-zero exit RAISES
        rather than coercing to an empty listing, and so does a non-empty
        listing carrying a malformed or non-object row (_decode_ps_rows) — a
        partial parse never under-counts a running project into "gone". Unlike
        ``docker ps``, this needs no explicit no-size format — ``docker compose
        ps`` computes
        container size only behind its opt-in ``--size``/``-s`` flag (the
        list-API WithSize option is set only then), so ``--format json`` without
        it triggers no overlay-snapshot walk (#109 Decisions Section)."""
        proc = self._run(
            ["compose", "--project-name", project, "ps", "--all", "--format", "json"],
            timeout=60,
        )
        rows = []
        for data in _decode_ps_rows(proc.stdout, "compose ps"):
            _require_str_fields(data, ("Name", "State", "Status"), "compose ps")
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
