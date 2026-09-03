"""Declarative Stacks on one node: the wire model, process supervision, and
secret materialization.

Two workload kinds share one format (NODE-SUBSTRATE.md): container Stacks
(image or compose + overlays) and process Stacks — a native command run as a
supervised Node Daemon child in its own process group, which is how the
Worker and Reviewer drivers deploy (ADR-0013). The daemon has no hardcoded
workload knowledge: built-in and user-defined Stacks are the same thing.

Kill-the-tree: process children start in a new session (their own process
group); stopping one signals the whole group — SIGTERM, a grace window,
SIGKILL — so a driver's subprocesses die with it, and the caller removes the
Stack's labeled run containers in the same operation. Under systemd,
KillMode=control-group guarantees the same at daemon death.

Secrets are materialized to the runtime dir (tmpfs under /run) behind a
0700-directory/0444-leaf boundary and wired via the VAR_FILE convention;
they never touch node disk. The non-traversable directory is what isolates
the leaves from other host users; the world-readable leaf mode is what lets
a container running as an arbitrary non-root uid read the files bind-mounted
into it (mounts preserve numeric ownership, so an owner-only leaf written by
the daemon's service uid would be unreadable across the uid boundary).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The generic driver launcher every worker type resolves to control-side
# (`theozolith-driver <ref>`, ADR-0020) — one executable covers builtin:* and
# future drivers/* refs, so the daemon recognizes drivers by argv[0] alone and
# never duplicates control's ref registry or parses a worker type. It is a
# console script that lives only in the daemon's own venv bin/ (ADR-0041:
# "console scripts are machine-run by absolute path"); the daemon resolves it
# there rather than trusting its PATH (see resolve_launcher). daemon.py imports
# this name for its argv[0] driver checks.
DRIVER_LAUNCHER = "theozolith-driver"


@dataclass(frozen=True)
class WireStack:
    """One Stack as it arrives in desired state (control/configrepo wire form)."""

    name: str
    kind: str
    state: str
    env: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)  # ENV_NAME -> secret name
    command: str = ""
    image: str = ""
    ports: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    # tmpfs mounts for the single-image container form (grilling 2026-09-02):
    # docker `--tmpfs` value syntax, beside volumes. A missing wire key degrades
    # to today's behavior (old control / new daemon), so skew stays advisory.
    tmpfs: tuple[str, ...] = ()
    compose_files: tuple[dict[str, str], ...] = ()  # [{name, content}]

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> WireStack:
        return cls(
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "")),
            state=str(data.get("state", "running")),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            secrets={str(k): str(v) for k, v in (data.get("secrets") or {}).items()},
            command=str(data.get("command", "")),
            image=str(data.get("image", "")),
            ports=tuple(str(p) for p in (data.get("ports") or [])),
            volumes=tuple(str(v) for v in (data.get("volumes") or [])),
            tmpfs=tuple(str(t) for t in (data.get("tmpfs") or [])),
            compose_files=tuple(
                {"name": str(f.get("name", "")), "content": str(f.get("content", ""))}
                for f in (data.get("compose_files") or [])
                if isinstance(f, dict)
            ),
        )


# -- secrets: tmpfs materialization + VAR_FILE wiring ---------------------------


def materialize_secrets(secrets_dir: Path, values: dict[str, str]) -> dict[str, Path]:
    """Write values under the runtime dir (tmpfs), atomically, behind the
    0700-directory/0444-leaf boundary.

    The DIRECTORY is the host-side barrier: mode 0700, owned by the daemon's
    service user, so no other host user can traverse to a leaf. Each LEAF is
    exactly 0444 — ``fchmod``-pinned, because the ``os.open`` mode argument
    is umask-masked and a restrictive service umask must not quietly produce
    an owner-only file. Bind mounts hand a container the leaf INODE with its
    numeric ownership preserved, so inside the container only the leaf's own
    mode decides access: 0444 is what lets an arbitrary non-root container
    uid (the Flight Deck's uid-1000 ozolith) read exactly the files mounted
    into it, while every unmounted sibling stays sealed behind the directory.
    Updates stay atomic (temp file + replace): a reader never observes a
    partial value."""
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secrets_dir.chmod(0o700)
    paths: dict[str, Path] = {}
    for name, value in values.items():
        target = secrets_dir / name
        tmp = secrets_dir / f".{name}.tmp"
        # A crash between fchmod and replace can leave a read-only temp file
        # behind; O_TRUNC alone would then fail EACCES, so clear it first.
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
        os.replace(tmp, target)
        paths[name] = target
    return paths


def secret_env_files(stack: WireStack, secrets_dir: Path) -> dict[str, str]:
    """ENV_NAME -> materialized host path for one Stack's secret refs."""
    return {env: str(secrets_dir / name) for env, name in sorted(stack.secrets.items())}


# -- process Stacks: supervised children in their own process groups ------------


class LauncherMissing(RuntimeError):
    """The generic Driver launcher is not installed beside the daemon's interpreter."""


def resolve_launcher(argv: list[str], launcher_dir: Path) -> list[str]:
    """argv[0] == DRIVER_LAUNCHER -> the absolute path under launcher_dir
    (the daemon's own venv bin/), validated executable. Any other argv[0] is
    returned untouched: generic process Stacks keep PATH semantics.

    Under systemd the daemon's PATH is the default one, and the launcher is a
    console script linked only into the venv bin/ (ADR-0041) — a bare
    `theozolith-driver` would never resolve. Only the launcher argv[0] is
    rewritten; its ref and flags (argv[1:]) are preserved verbatim, and the
    logical command a caller fingerprints stays PATH-relative so a venv move
    never churns a running Driver."""
    if not argv or argv[0] != DRIVER_LAUNCHER:
        return argv
    target = launcher_dir / DRIVER_LAUNCHER
    if not target.is_file() or not os.access(target, os.X_OK):
        raise LauncherMissing(
            f"{DRIVER_LAUNCHER} is not installed at {target} — theozolith-worker"
            f" is missing from the daemon's environment {launcher_dir.parent};"
            " re-run install-nodedaemon.sh or let the product update converge"
        )
    return [str(target), *argv[1:]]


# Env keys redacted before an effective-spec fingerprint is computed: the
# node's control-channel token is a credential injected into every driver's
# env, not a spec input — it must never enter a fingerprint (ADR-0044
# amendment: secret values stay out of fingerprints, state, and logs). Stack
# secret VALUES never reach env in the first place (only their <ENV>_FILE
# paths do, via secret_env_files), so the mapping is captured while the
# values are not.
_FINGERPRINT_REDACT = ("THEOZOLITH_NODE_TOKEN",)


def spec_fingerprint(command: str, env: dict[str, str]) -> str:
    """A deterministic digest of a process Stack's effective launch spec —
    the argv command plus its full environment (which already carries the
    resolved worker-type env: repository, adapter, run-image tag, and the
    secret <ENV>_FILE mappings). The model is not in env (ADR-0045: it is
    baked into the run image), but a model change still restarts the driver —
    it re-tags the image, and THEOZOLITH_RUN_IMAGE is fingerprinted here.
    Dictionary ordering is irrelevant (sort_keys), so a mere reordering never
    forces a restart; a change to any effective input does."""
    safe_env = {
        key: ("<redacted>" if key in _FINGERPRINT_REDACT else value) for key, value in env.items()
    }
    canonical = json.dumps({"command": command, "env": safe_env}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Child:
    process: subprocess.Popen
    command: str
    fingerprint: str


class ProcessSupervisor:
    """The daemon's children: one per running process Stack."""

    def __init__(self, *, popen=subprocess.Popen, log=print, launcher_dir: Path | None = None):
        self._popen = popen
        self._log = log
        # Where the generic Driver launcher is resolved from (ADR-0020/0041):
        # the daemon's own venv bin/. Derived from sys.prefix (the venv root,
        # read from pyvenv.cfg), NOT sys.executable: python3 -m venv creates
        # bin/python as a symlink to the system interpreter, so
        # Path(sys.executable).resolve() follows it out of the venv and lands
        # in /usr/bin, where no launcher is installed (#94). sys.prefix also
        # backs daemon.py's _venv_writable, so preflight and launcher agree on
        # which environment they mean. Never a spec input — see spec_fingerprint.
        self._launcher_dir = Path(sys.prefix) / "bin" if launcher_dir is None else launcher_dir
        self._children: dict[str, Child] = {}
        self._last_exit: dict[str, int] = {}

    def names(self) -> list[str]:
        """The Stack names of the children this supervisor currently tracks
        (each either live or exited-but-not-yet-reaped). The minimal inventory
        surface the daemon needs to converge runtime objects absent from
        desired state — a removed or renamed process Stack, or the losing side
        of a kind transition — WITHOUT reaching into the private child map
        (ADR-0044 amendment)."""
        return list(self._children)

    def alive(self, name: str) -> bool:
        child = self._children.get(name)
        return child is not None and child.process.poll() is None

    def needs_restart(self, name: str, command: str, env: dict[str, str]) -> bool:
        """True when a running child's effective spec no longer matches the
        given (command, env) — the caller then tears the tree down and relaunches.
        A missing or dead child also needs (re)starting."""
        child = self._children.get(name)
        if child is None or child.process.poll() is not None:
            return True
        return child.fingerprint != spec_fingerprint(command, env)

    def resolve_command(self, command: str) -> list[str]:
        """The argv the supervisor WOULD launch for this command: the generic
        Driver launcher resolved to its venv-absolute path (ADR-0020/0041),
        raising LauncherMissing when it is absent. Lets the daemon fail a
        driver Stack closed BEFORE tearing a working child down for a restart
        (the resolution never has to reach into the private launcher dir)."""
        return resolve_launcher(shlex.split(command), self._launcher_dir)

    def ensure_running(self, name: str, command: str, env: dict[str, str]) -> None:
        """Start (or restart) the Stack's child unless its effective spec is
        already live. The effective spec is the argv command AND its
        environment: a worker-type change resolves primarily into env now, so
        comparing the command alone would miss model/adapter/workspace/run-image
        and secret-mapping changes (ADR-0044 amendment)."""
        fingerprint = spec_fingerprint(command, env)
        child = self._children.get(name)
        if child is not None and child.process.poll() is None and child.fingerprint == fingerprint:
            return  # already live at this exact effective spec
        # Resolve the launcher BEFORE any teardown of a changed-spec live
        # child: a missing launcher (LauncherMissing) must surface without
        # ever stopping the running instance (the fingerprint uses the logical
        # PATH-relative command, so the resolved absolute path never enters it).
        argv = resolve_launcher(shlex.split(command), self._launcher_dir)
        if child is not None:
            code = child.process.poll()
            if code is None:
                # The effective spec changed: recycle to the new one.
                self.stop(name, grace_seconds=30.0)
            else:
                self._last_exit[name] = code
                self._log(f"stack {name}: child exited with {code}; restarting")
                self._children.pop(name, None)
        process = self._popen(
            argv,
            env={**os.environ, **env},
            start_new_session=True,  # its own process group: the kill-tree unit
        )
        self._children[name] = Child(process=process, command=command, fingerprint=fingerprint)
        self._log(f"stack {name}: started pid {process.pid} ({shlex.join(argv)})")

    def stop(self, name: str, *, grace_seconds: float, sleep=time.sleep) -> None:
        """SIGTERM the process group, wait, SIGKILL what remains."""
        child = self._children.pop(name, None)
        if child is None:
            return
        process = child.process
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + grace_seconds
            while process.poll() is None and time.monotonic() < deadline:
                sleep(0.1)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
        self._last_exit[name] = process.returncode if process.returncode is not None else 0
        self._log(f"stack {name}: stopped")

    def stop_all(self, *, grace_seconds: float) -> None:
        for name in list(self._children):
            self.stop(name, grace_seconds=grace_seconds)

    def status(self, name: str) -> tuple[str, str]:
        """(state, detail) for heartbeat reporting."""
        if self.alive(name):
            return "running", f"pid {self._children[name].process.pid}"
        if name in self._last_exit:
            return "stopped", f"last exit {self._last_exit[name]}"
        return "stopped", ""
