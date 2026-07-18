"""The Node Daemon: register, heartbeat, reconcile, supervise, build, reap.

One pass every heartbeat interval (60s):

1. Gather status — supervised Stacks, labeled run containers (via docker),
   derived-image build metadata — and POST it as the heartbeat.
2. The response carries infrastructure commands and this node's desired
   state (ADR-0006); the desired state is cached to disk so an unreachable
   Control Node degrades to reconciling the last-applied config, forever.
3. Execute commands (drain / recycle / update / rebuild), then converge
   Stacks: build missing derived images, start/stop process children and
   container workloads, and reap orphaned run containers (owner gone).

Queue-behind (NODE-SUBSTRATE, grilling 2026-07-17): recycle and update
received mid-Run defer behind the current Run — job-dir presence under a
live driver child is the in-flight signal; a dead child proceeds
immediately (orphaned dirs never block). A deferred command is simply not
acknowledged (the Control Node re-delivers it every heartbeat) and the
heartbeat reports the deferral; --force keeps ADR-0013 kill-the-tree
semantics. Drain stays immediate by design.

Command acknowledgements persist to disk before they ride the next
heartbeat, so a crash (or the update command's re-exec) never replays a
completed command. Drain marks persist too: a drained Stack stays down
across daemon restarts until a recycle clears it (ADR-0015).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from theozolith_nodedaemon.builds import ensure_image, image_status
from theozolith_nodedaemon.config import DaemonConfig, DaemonConfigError, load_daemon_config
from theozolith_nodedaemon.controlclient import (
    ControlClient,
    ControlError,
    ControlUnreachable,
)
from theozolith_nodedaemon.dockerctl import DockerCtl
from theozolith_nodedaemon.stacks import (
    ProcessSupervisor,
    WireStack,
    materialize_secrets,
    secret_env_files,
)

UPDATE_PACKAGES = ("theozolith-nodedaemon", "theozolith-worker", "theozolith-knowledge")

# Every process Stack gets its own jobs directory — <base>/<stack-name>,
# injected as THEOZOLITH_JOBS_DIR unless the Stack's env overrides it — so
# the queue-behind in-flight signal observes exactly one driver's Runs
# (ADR-0019). Must match control's configrepo.DEFAULT_JOBS_BASE, where
# duplicate resolved paths are rejected at parse time.
DEFAULT_JOBS_BASE = "/var/tmp/theozolith/jobs"


def stack_jobs_dir(stack: WireStack) -> Path:
    explicit = stack.env.get("THEOZOLITH_JOBS_DIR", "")
    return Path(explicit) if explicit else Path(DEFAULT_JOBS_BASE) / stack.name


def _log(message: str) -> None:
    print(message, flush=True)


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


class NodeDaemon:
    def __init__(
        self,
        config: DaemonConfig,
        *,
        docker: DockerCtl | None = None,
        client: ControlClient | None = None,
        supervisor: ProcessSupervisor | None = None,
        log=_log,
        execv=os.execv,
        update_runner=subprocess.run,
    ):
        self._config = config
        self._docker = docker or DockerCtl()
        self._log = log
        self._execv = execv
        self._update_runner = update_runner
        self._supervisor = supervisor or ProcessSupervisor(log=log)
        if client is not None:
            self._client = client
        elif config.control_url:
            self._client = ControlClient(
                config.control_url,
                config.node_token,
                ca=config.tls_ca,
                insecure_dev=config.insecure_dev,
            )
        else:
            self._client = None  # permanent degraded mode: cache only
        self._registered = False
        self._desired: dict[str, Any] = _read_json(config.cache_path, {})
        self._drained: set[str] = set(_read_json(config.drained_path, []))
        self._completed: list[int] = _read_json(self._acks_path, [])
        self._applied_compose: dict[str, str] = {}
        self._rebuild_targets: set[str] = set()
        # Queue-behind: command id -> deferral reason, reported in heartbeats.
        self._deferrals: dict[int, str] = {}

    @property
    def _acks_path(self) -> Path:
        return self._config.state_dir / "pending-acks.json"

    # -- desired-state accessors ------------------------------------------------

    def _stacks(self) -> list[WireStack]:
        return [WireStack.from_wire(s) for s in self._desired.get("stacks", [])]

    def _images(self) -> dict[str, dict[str, Any]]:
        return {i["name"]: i for i in self._desired.get("images", []) if i.get("name")}

    # -- one pass -----------------------------------------------------------------

    def once(self) -> None:
        commands = self._exchange_heartbeat()
        for command in commands:
            if not self._execute(command):
                # Queue-behind blocks the QUEUE: commands after a deferred
                # one wait too, or a later drain could land before the
                # recycle it was issued after and be undone by it.
                break
        self._reconcile()

    def run(self, *, sleep=time.sleep) -> None:
        self._log(
            f"node daemon {self._config.node} "
            + (f"heartbeating to {self._config.control_url}" if self._client else "(cache only)")
        )
        while True:
            try:
                self.once()
            except Exception as exc:  # a bad pass must never kill the daemon
                self._log(f"pass failed: {exc}")
            sleep(self._config.heartbeat_seconds)

    # -- heartbeat ------------------------------------------------------------------

    def _status_payload(self) -> dict[str, Any]:
        stacks = []
        for stack in self._stacks():
            if stack.kind == "process":
                state, detail = self._supervisor.status(stack.name)
            else:
                rows = (
                    self._docker.compose_ps(f"ozolith-{stack.name}")
                    if stack.compose_files
                    else self._docker.stack_containers(stack.name)
                )
                running = [r for r in rows if r.get("state") == "running"]
                state = "running" if running else "stopped"
                detail = "; ".join(r.get("status", "") for r in rows[:3])
            if stack.name in self._drained:
                detail = (detail + " (drained)").strip()
            stacks.append(
                {"name": stack.name, "kind": stack.kind, "state": state, "detail": detail}
            )
        return {
            "node": self._config.node,
            "version": self._config.version,
            "stacks": stacks,
            "run_containers": self._docker.run_containers(),
            "images": [image_status(self._docker, img) for img in self._images().values()],
            "config_commit": self._desired.get("commit", ""),
            "completed_commands": list(self._completed),
            # Queue-behind visibility: what is waiting behind an in-flight Run.
            "deferred_commands": [
                {"id": command_id, "reason": reason}
                for command_id, reason in sorted(self._deferrals.items())
            ],
        }

    def _exchange_heartbeat(self) -> list[dict[str, Any]]:
        if self._client is None:
            return []
        if not self._registered:
            try:
                self._client.register(self._config.node, self._config.version)
                self._registered = True
                self._log(f"registered node {self._config.node}")
            except (ControlUnreachable, ControlError) as exc:
                self._log(f"registration deferred: {exc}")
        try:
            answer = self._client.heartbeat(self._status_payload())
        except (ControlUnreachable, ControlError) as exc:
            self._log(f"control node unreachable ({exc}); reconciling from cached config")
            return []
        # Acks landed on the Control Node: stop re-sending them.
        self._completed = []
        _atomic_json(self._acks_path, self._completed)
        config = answer.get("config")
        if isinstance(config, dict):
            self._desired = config
            _atomic_json(self._config.cache_path, config)
        commands = answer.get("commands")
        return [c for c in commands if isinstance(c, dict)] if isinstance(commands, list) else []

    # -- commands -----------------------------------------------------------------

    def _execute(self, command: dict[str, Any]) -> bool:
        """Run one command; False = deferred (queue-behind), so the caller
        must not execute anything queued after it this pass."""
        verb = command.get("verb", "")
        target = command.get("target") or None
        command_id = command.get("id")
        if verb in ("recycle", "update") and not command.get("force"):
            blocker = self._inflight_blocker([target] if verb == "recycle" and target else None)
            if blocker is not None and isinstance(command_id, int):
                # Queue-behind: no ack, so the Control Node re-delivers the
                # command every heartbeat and this check re-runs until the
                # Run ends (or a --force arrives). Grants to this node pause
                # meanwhile (dispatch gate), bounding the deferral.
                if self._deferrals.get(command_id) != blocker:
                    self._log(f"command {command_id}: {verb} deferred ({blocker})")
                self._deferrals[command_id] = blocker
                return False
        if isinstance(command_id, int):
            self._deferrals.pop(command_id, None)
        self._log(f"command {command_id}: {verb}" + (f" {target}" if target else ""))
        try:
            if verb == "drain":
                self._drain(target)
            elif verb == "recycle":
                self._recycle(target)
            elif verb == "rebuild":
                self._rebuild_targets.update([target] if target else self._images())
            elif verb == "update":
                self._update(command_id)
                return True  # unreachable when the update re-execs
            else:
                self._log(f"command {command_id}: unknown verb {verb!r}; acking as a no-op")
        except Exception as exc:
            # No ack: the Control Node re-delivers next heartbeat (logged loop
            # beats a silently dropped command).
            self._log(f"command {command_id} failed: {exc}")
            return True
        self._ack(command_id)
        return True

    def _ack(self, command_id: Any) -> None:
        if isinstance(command_id, int):
            self._completed.append(command_id)
            _atomic_json(self._acks_path, self._completed)

    def _stack_by_name(self, name: str) -> WireStack | None:
        return next((s for s in self._stacks() if s.name == name), None)

    def _inflight_blocker(self, names: list[str] | None) -> str | None:
        """The in-flight signal for queue-behind: a live driver child whose
        jobs dir holds a job directory. A dead child never blocks — its
        orphaned dirs are the boot sweep's business, not a Run."""
        for stack in self._stacks():
            if names is not None and stack.name not in names:
                continue
            if stack.kind != "process" or not self._supervisor.alive(stack.name):
                continue
            jobs_dir = stack_jobs_dir(stack)
            try:
                running = sorted(p.name for p in jobs_dir.iterdir() if p.is_dir())
            except OSError:
                running = []
            if running:
                return f"behind run {running[0]} (stack {stack.name})"
        return None

    def _stop_stack(self, stack: WireStack) -> None:
        """Stop a Stack AND its labeled run containers (kill-the-tree)."""
        if stack.kind == "process":
            self._supervisor.stop(stack.name, grace_seconds=self._config.stop_grace_seconds)
        elif stack.compose_files:
            self._docker.compose(f"ozolith-{stack.name}", self._compose_paths(stack), "down")
            self._applied_compose.pop(stack.name, None)
        else:
            self._docker.remove(f"ozolith-stack-{stack.name}")
        for container in self._docker.run_containers():
            if container.get("owner") == stack.name:
                self._log(f"removing run container {container['name']} (owner {stack.name})")
                self._docker.remove(container["name"])

    def _drain(self, target: str | None) -> None:
        names = [target] if target else [s.name for s in self._stacks()]
        for name in names:
            stack = self._stack_by_name(name)
            if stack is not None:
                self._stop_stack(stack)
            self._drained.add(name)
        _atomic_json(self._config.drained_path, sorted(self._drained))

    def _recycle(self, target: str | None) -> None:
        names = [target] if target else [s.name for s in self._stacks()]
        for name in names:
            stack = self._stack_by_name(name)
            if stack is not None:
                self._stop_stack(stack)
            self._drained.discard(name)
        _atomic_json(self._config.drained_path, sorted(self._drained))
        # The reconcile step of this same pass starts them again.

    def _update(self, command_id: Any) -> None:
        """Product update (ADR-0013 §8): stop the tree, install the pinned
        version, re-exec this daemon in place. The ack is persisted first so
        the re-exec'd daemon does not replay the command."""
        self._supervisor.stop_all(grace_seconds=self._config.stop_grace_seconds)
        version = str(self._desired.get("product_version", "") or "")
        packages = [f"{name}=={version}" if version else name for name in UPDATE_PACKAGES]
        proc = self._update_runner(
            [sys.executable, "-m", "pip", "install", "--upgrade", *packages],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pip install failed: {(proc.stderr or '').strip()[-300:]}")
        self._ack(command_id)
        self._log(f"updated to {version or 'latest'}; re-exec")
        self._execv(sys.executable, [sys.executable, "-m", "theozolith_nodedaemon", *sys.argv[1:]])

    # -- reconciliation ---------------------------------------------------------------

    def _reconcile(self) -> None:
        images = self._images()
        for name, image in images.items():
            try:
                ensure_image(
                    self._docker, image, force=name in self._rebuild_targets, log=self._log
                )
                self._rebuild_targets.discard(name)
            except Exception as exc:
                self._log(f"image {name}: build failed: {exc}")
        for stack in self._stacks():
            want_running = stack.state == "running" and stack.name not in self._drained
            try:
                if stack.kind == "process":
                    self._converge_process(stack, want_running, images)
                else:
                    self._converge_container(stack, want_running)
            except Exception as exc:
                self._log(f"stack {stack.name}: reconcile failed: {exc}")
        self._reap_orphans()

    def _pull_stack_secrets(self, stack: WireStack) -> bool:
        """Materialize the Stack's secrets in tmpfs; False = cannot deploy.

        A previously materialized set keeps working when the Control Node is
        away (the values live in tmpfs for exactly this degraded window)."""
        if not stack.secrets:
            return True
        names = sorted(set(stack.secrets.values()))
        try:
            if self._client is None:
                raise ControlUnreachable("no CONTROL_NODE_URL configured")
            values = self._client.pull_secrets(self._config.node, names)
            materialize_secrets(self._config.secrets_dir, values)
            return True
        except (ControlUnreachable, ControlError) as exc:
            if all((self._config.secrets_dir / name).is_file() for name in names):
                self._log(f"stack {stack.name}: secrets pull failed ({exc}); using tmpfs copies")
                return True
            self._log(f"stack {stack.name}: cannot deploy, secrets unavailable ({exc})")
            return False

    def _converge_process(
        self, stack: WireStack, want_running: bool, images: dict[str, dict[str, Any]]
    ) -> None:
        if not want_running:
            if self._supervisor.alive(stack.name):
                self._stop_stack(stack)
            return
        if not self._supervisor.alive(stack.name) and not self._pull_stack_secrets(stack):
            return
        env = {
            "THEOZOLITH_NODE_NAME": self._config.node,
            # The dedicated per-Stack jobs directory (ADR-0019); an explicit
            # env value in the Stack definition wins.
            "THEOZOLITH_JOBS_DIR": str(stack_jobs_dir(stack)),
            **stack.env,
        }
        if stack.run_image and stack.run_image in images:
            # The derived image this driver launches per Run (ADR-0013).
            env.setdefault("THEOZOLITH_RUN_IMAGE", images[stack.run_image]["tag"])
        env_files = secret_env_files(stack, self._config.secrets_dir)
        env.update({f"{name}_FILE": path for name, path in env_files.items()})
        self._supervisor.ensure_running(stack.name, stack.command, env)

    def _compose_paths(self, stack: WireStack) -> list[Path]:
        """Materialize the inlined compose + overlay documents on disk."""
        base = self._config.state_dir / "stacks" / stack.name
        paths = []
        for entry in stack.compose_files:
            target = (base / entry["name"]).resolve()
            if base.resolve() not in target.parents:
                raise RuntimeError(f"compose path {entry['name']!r} escapes the stack dir")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(entry["content"], encoding="utf-8")
            paths.append(target)
        return paths

    def _converge_container(self, stack: WireStack, want_running: bool) -> None:
        if not want_running:
            if stack.compose_files:
                if self._applied_compose.get(stack.name) or self._docker.compose_ps(
                    f"ozolith-{stack.name}"
                ):
                    self._stop_stack(stack)
            elif self._docker.stack_containers(stack.name):
                self._stop_stack(stack)
            return
        if stack.compose_files:
            fingerprint = json.dumps(stack.compose_files, sort_keys=True)
            if self._applied_compose.get(stack.name) == fingerprint:
                return
            if not self._pull_stack_secrets(stack):
                return
            self._docker.compose(f"ozolith-{stack.name}", self._compose_paths(stack), "up")
            self._applied_compose[stack.name] = fingerprint
            return
        rows = self._docker.stack_containers(stack.name)
        if any(r.get("state") == "running" for r in rows):
            return
        if not self._pull_stack_secrets(stack):
            return
        self._docker.run_stack_container(
            stack.name,
            stack.image,
            env_files=secret_env_files(stack, self._config.secrets_dir),
            env=stack.env,
            ports=list(stack.ports),
            volumes=list(stack.volumes),
        )

    def _reap_orphans(self) -> None:
        """Run containers whose owning driver is gone are corpses (ADR-0013):
        remove them here; the zombie-claim janitor restores GitHub state."""
        for container in self._docker.run_containers():
            owner = container.get("owner", "")
            if owner and self._supervisor.alive(owner):
                continue
            self._log(f"reaping orphaned run container {container['name']} (owner {owner or '?'})")
            self._docker.remove(container["name"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-nodedaemon",
        description=(
            "TheOzolith Node Daemon: register this box as a Container-Host, heartbeat to the"
            " Control Node, reconcile declarative Stacks, build derived images, reap orphans."
        ),
    )
    parser.add_argument(
        "--once", action="store_true", help="One heartbeat + reconcile pass, then exit."
    )
    args = parser.parse_args(argv)
    try:
        config = load_daemon_config()
    except DaemonConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    daemon = NodeDaemon(config)
    try:
        if args.once:
            daemon.once()
        else:
            daemon.run()
    except KeyboardInterrupt:
        daemon._supervisor.stop_all(grace_seconds=config.stop_grace_seconds)
        print("node daemon stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
