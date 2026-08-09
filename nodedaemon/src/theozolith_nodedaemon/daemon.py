"""The Node Daemon: heartbeat, reconcile, supervise, build, reap.

One pass every heartbeat interval (60s):

1. Gather status — supervised Stacks, labeled run containers (via docker),
   derived-image build metadata — and POST it as the heartbeat.
2. The response carries infrastructure commands and this node's desired
   state (ADR-0006); the desired state is cached to disk so an unreachable
   Control Node degrades to reconciling the last-applied config, forever.
3. Execute commands (drain / recycle / update / rebuild / restart), converge
   the product version to the pin (the pin is desired state — ADR-0015 as
   revised: mismatch self-updates, failed installs retry next pass, the
   update command is only a nudge), then converge Stacks: build missing
   derived images, start/stop process children and container workloads, and
   reap orphaned run containers (owner gone).

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
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from theozolith_nodedaemon.builds import ensure_image, image_status
from theozolith_nodedaemon.config import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_STOP_GRACE_SECONDS,
    DaemonConfig,
    DaemonConfigError,
    load_daemon_config,
)
from theozolith_nodedaemon.controlclient import (
    ControlClient,
    ControlError,
    ControlUnreachable,
)
from theozolith_nodedaemon.dockerctl import (
    LABEL_STACK_SPEC,
    STACK_CONTAINER_PREFIX,
    DockerCtl,
)
from theozolith_nodedaemon.stacks import (
    ProcessSupervisor,
    WireStack,
    materialize_secrets,
    secret_env_files,
)

UPDATE_PACKAGES = ("theozolith-nodedaemon", "theozolith-worker", "theozolith-knowledge")

# The built-in driver commands a worker type resolves to control-side
# (control's configrepo.BUILTIN_DRIVERS values). This is driver knowledge, not
# worker-type schema — the daemon still never parses a worker type. A built-in
# driver only functions with a control-authored THEOZOLITH_RUN_IMAGE; seeing
# one of these commands WITHOUT that env means old or incomplete desired state,
# and the daemon fails that Stack closed rather than launch it against the
# worker package's default run image (ADR-0044 amendment).
BUILTIN_DRIVER_COMMANDS = ("theozolith-worker", "theozolith-reviewer")

# theozolith.error events (2026-07-21 grilling): size-capped summaries
# pointing at the failing node/component; diagnostic depth stays in the
# systemd journal. Caps mirror the worker's events module (stdlib-only
# component: no shared import).
ERROR_EVENT = "theozolith.error"
ERROR_MESSAGE_CHARS = 2_000
ERROR_CONTEXT_CHARS = 8_000

# Unreachable-Control-Node backoff cap (revision ruling amending ADR-0015).
BACKOFF_CAP_SECONDS = 300.0

# Every process Stack gets its own jobs directory — <base>/<stack-name>,
# injected as THEOZOLITH_JOBS_DIR unless the Stack's env overrides it — so
# the queue-behind in-flight signal observes exactly one driver's Runs
# (ADR-0019). Must match control's configrepo.DEFAULT_JOBS_BASE, where
# duplicate resolved paths are rejected at parse time.
DEFAULT_JOBS_BASE = "/var/tmp/theozolith/jobs"


def stack_jobs_dir(stack: WireStack) -> Path:
    explicit = stack.env.get("THEOZOLITH_JOBS_DIR", "")
    return Path(explicit) if explicit else Path(DEFAULT_JOBS_BASE) / stack.name


@dataclass
class AppliedCompose:
    """The non-secret applied record for a compose project this daemon brought
    up: its effective fingerprint (change detection) and the materialized
    compose/overlay file PATHS. Retaining the paths lets a later ``down`` run
    with real teardown context — a valid ``docker compose --file … down`` —
    rather than an empty file list (ADR-0044 amendment). Secret VALUES never
    enter this record: it holds file paths, and the compose documents carry
    secret references (the VAR_FILE convention), not values."""

    fingerprint: str
    files: list[str]


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
        self._desired: dict[str, Any] = _read_json(config.cache_path, {})
        self._drained: set[str] = set(_read_json(config.drained_path, []))
        self._completed: list[int] = _read_json(self._acks_path, [])
        # Compose projects this daemon brought up, name -> applied record. The
        # record retains the materialized compose/overlay paths so a later down
        # (change, transition, stopped/drained, absent-from-desired) is a valid
        # `docker compose --file … down`, never an empty file list (ADR-0044).
        self._applied_compose: dict[str, AppliedCompose] = {}
        # The jobs directory each live process child was LAUNCHED with (its
        # applied THEOZOLITH_JOBS_DIR). The minimum non-secret applied-spec
        # fact needed to queue a desired-state restart behind an in-flight Run
        # even when the desired jobs dir has since changed (ADR-0044 amendment).
        # In-memory by design: supervised children never outlive the daemon, so
        # a restart clears both the children and this map together.
        self._applied_jobs_dir: dict[str, str] = {}
        self._rebuild_targets: set[str] = set()
        # Queue-behind: command id -> deferral reason, reported in heartbeats.
        self._deferrals: dict[int, str] = {}
        # Product convergence (revision ruling amending ADR-0015): the pin is
        # desired state, checked every pass. True once an install succeeded
        # and the re-exec is imminent — no second attempt is in flight.
        self._update_done = False
        self._update_blocker: str | None = None
        self._product_attempted = False  # per-pass latch (nudge vs. pass check)
        # Capped exponential backoff while the Control Node is unreachable.
        self._unreachable_streak = 0

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
        self._product_attempted = False  # at most one install attempt per pass
        commands = self._exchange_heartbeat()
        for command in commands:
            if not self._execute(command):
                # Queue-behind blocks the QUEUE: commands after a deferred
                # one wait too, or a later drain could land before the
                # recycle it was issued after and be undone by it.
                break
        self._converge_product()
        self._reconcile()

    def _wire_setting(self, key: str, config_value: float, shipped_default: float) -> float:
        """A tier-2 cadence riding desired state (ADR-0023: control.toml
        replaces per-node env). An explicit local environment override —
        i.e. a config value off the shipped default — wins; otherwise the
        Control Node's value applies."""
        if config_value != shipped_default:
            return config_value
        wire = self._desired.get(key)
        if isinstance(wire, (int, float)) and not isinstance(wire, bool) and wire > 0:
            return float(wire)
        return config_value

    def _heartbeat_seconds(self) -> float:
        return self._wire_setting(
            "heartbeat_seconds", self._config.heartbeat_seconds, DEFAULT_HEARTBEAT_SECONDS
        )

    def _stop_grace_seconds(self) -> float:
        return self._wire_setting(
            "stop_grace_seconds", self._config.stop_grace_seconds, DEFAULT_STOP_GRACE_SECONDS
        )

    def _next_delay(self) -> float:
        """The inter-pass delay: the heartbeat interval normally; capped
        exponential backoff while the Control Node is unreachable, resetting
        the moment it answers again (revision ruling amending ADR-0015)."""
        base = self._heartbeat_seconds()
        if self._unreachable_streak <= 1:
            return base
        return min(BACKOFF_CAP_SECONDS, base * 2 ** (self._unreachable_streak - 1))

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
                self._emit_error(type(exc).__name__, f"pass failed: {exc}")
            sleep(self._next_delay())

    # -- heartbeat ------------------------------------------------------------------

    def _status_payload(self) -> dict[str, Any]:
        stacks = []
        stack_containers = []
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
                # Live container evidence for the web terminal (ADR-0019):
                # the Flight Deck and other container Stacks attach through
                # these records; run containers are never attach targets.
                stack_containers.extend(
                    {
                        "name": str(row.get("name", "")),
                        "stack": stack.name,
                        "state": str(row.get("state", "")),
                        "status": str(row.get("status", "")),
                    }
                    for row in rows
                    if row.get("name")
                )
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
            "stack_containers": stack_containers,
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
        # Provisioning is registration (ADR-0023): the daemon never
        # registers — it heartbeats with its per-node token, and a 401
        # (revoked/unknown) surfaces as an unregistered sighting control-side.
        if self._client is None:
            return []
        try:
            answer = self._client.heartbeat(self._status_payload())
        except ControlUnreachable as exc:
            self._unreachable_streak += 1
            self._log(f"control node unreachable ({exc}); reconciling from cached config")
            return []
        except ControlError as exc:
            self._log(f"control node refused the heartbeat ({exc}); using cached config")
            return []
        self._unreachable_streak = 0
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
        if verb in ("recycle", "update", "restart") and not command.get("force"):
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
                # The nudge, never the mechanism of record (revision ruling
                # amending ADR-0015): ack FIRST — convergence owns the retry,
                # so a failed install must not leave the command consuming
                # the queue — then attempt convergence immediately.
                self._ack(command_id)
                self._converge_product(force=bool(command.get("force")))
                return True
            elif verb == "restart":
                # Escalation step for a node that will not converge: stop
                # the tree and re-exec in place, no install.
                self._supervisor.stop_all(grace_seconds=self._stop_grace_seconds())
                self._ack(command_id)
                self._log("restart command: re-exec")
                self._execv(
                    sys.executable, [sys.executable, "-m", "theozolith_nodedaemon", *sys.argv[1:]]
                )
                return True
            else:
                self._log(f"command {command_id}: unknown verb {verb!r}; acking as a no-op")
        except Exception as exc:
            # No ack: the Control Node re-delivers next heartbeat (logged loop
            # beats a silently dropped command).
            self._log(f"command {command_id} failed: {exc}")
            self._emit_error(
                type(exc).__name__, f"command {command_id} ({verb} {target or ''}) failed: {exc}"
            )
            return True
        self._ack(command_id)
        return True

    def _ack(self, command_id: Any) -> None:
        if isinstance(command_id, int):
            self._completed.append(command_id)
            _atomic_json(self._acks_path, self._completed)

    def _emit_error(self, error_class: str, message: str, context: str = "") -> None:
        """Best-effort theozolith.error summary; never raises, never loops
        (an emission failure is itself only logged)."""
        if self._client is None:
            return
        try:
            self._client.emit_event(
                {
                    "type": ERROR_EVENT,
                    "node": self._config.node,
                    "component": "node-daemon",
                    "error_class": error_class,
                    "message": message[:ERROR_MESSAGE_CHARS],
                    "context": context[-ERROR_CONTEXT_CHARS:],
                }
            )
        except Exception as exc:
            self._log(f"error event not delivered ({error_class}): {exc}")

    def _stack_by_name(self, name: str) -> WireStack | None:
        return next((s for s in self._stacks() if s.name == name), None)

    def _live_jobs_dir(self, stack: WireStack) -> Path:
        """The jobs directory the currently running child was LAUNCHED with —
        the applied path, which can differ from the desired one when
        THEOZOLITH_JOBS_DIR changed but the child has not yet been restarted
        onto it (that restart is itself queued behind the in-flight Run). Falls
        back to the desired path when no applied path is on record (a child this
        daemon has not launched, e.g. before the first start)."""
        applied = self._applied_jobs_dir.get(stack.name)
        return Path(applied) if applied else stack_jobs_dir(stack)

    def _active_runs(self, jobs_dir: Path) -> list[str]:
        """The live Run directories under a jobs dir, sorted. Dot-prefixed
        names are never live Runs (run ids start with a timestamp digit): the
        driver's evidence-loss tombstone (worker sweep.TOMBSTONE_PREFIX,
        ADR-0019 parking ladder) renames undeletable remnants to a hidden name
        exactly so this signal skips them."""
        try:
            return sorted(
                p.name for p in jobs_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
            )
        except OSError:
            return []

    def _inflight_blocker(self, names: list[str] | None) -> str | None:
        """The in-flight signal for queue-behind: a live driver child whose
        jobs dir holds a job directory. The jobs dir inspected is the one the
        RUNNING child was launched with (``_live_jobs_dir``), not necessarily
        the desired one, so a THEOZOLITH_JOBS_DIR change still observes a Run
        under the old path. A dead child never blocks — its orphaned dirs are
        the boot sweep's business, not a Run. Scans DESIRED process Stacks, so
        it does not see a child whose desired kind has flipped away from
        process (that case uses ``_child_inflight_blocker``)."""
        for stack in self._stacks():
            if names is not None and stack.name not in names:
                continue
            if stack.kind != "process" or not self._supervisor.alive(stack.name):
                continue
            running = self._active_runs(self._live_jobs_dir(stack))
            if running:
                return f"behind run {running[0]} (stack {stack.name})"
        return None

    def _child_inflight_blocker(self, name: str) -> str | None:
        """The in-flight signal keyed off a supervised child directly, by its
        APPLIED jobs dir — for a process->container transition, where the
        desired Stack's kind is no longer ``process`` so ``_inflight_blocker``
        (which scans desired process Stacks) would miss the still-running
        child. A dead child never blocks."""
        if not self._supervisor.alive(name):
            return None
        applied = self._applied_jobs_dir.get(name)
        if not applied:
            return None
        running = self._active_runs(Path(applied))
        return f"behind run {running[0]} (stack {name})" if running else None

    def _isolated(self, description: str, action) -> None:
        """Run one teardown step so its failure is CONTAINED: log it, emit the
        normal capped error event, and return. Nothing the failed step did not
        clear (a compose project's applied record, a still-present container) is
        dropped, so the object is retried on the next pass. The caller's other
        teardown steps, the convergence of every desired Stack, and orphan
        reaping all continue past it (ADR-0044 amendment: a failure stopping one
        absent runtime never aborts the reconcile pass)."""
        try:
            action()
        except Exception as exc:
            self._log(f"{description}: cleanup failed: {exc}")
            self._emit_error(type(exc).__name__, f"{description}: cleanup failed: {exc}")

    def _remove_owned_run_containers(self, name: str) -> None:
        """Remove every labeled run container this Stack's driver owns (its
        share of kill-the-tree). Named after the Stack, not a WireStack, so it
        serves both a normal stop and a kind transition where the desired
        Stack's kind no longer matches the runtime form being torn down."""
        for container in self._docker.run_containers():
            if container.get("owner") == name:
                self._log(f"removing run container {container['name']} (owner {name})")
                self._docker.remove(container["name"])

    def _stop_process_child(self, name: str) -> None:
        """Tear a supervised process child down completely: stop it
        (kill-the-tree), remove its labeled run containers, and clear ALL of
        its applied-spec bookkeeping (``_applied_jobs_dir``). The single
        teardown for stopped/drained desire, a Stack removed from desired
        state, and a process->container transition — no path may leave a
        hidden child or its run containers behind (ADR-0044 amendment)."""
        self._supervisor.stop(name, grace_seconds=self._stop_grace_seconds())
        self._applied_jobs_dir.pop(name, None)
        self._remove_owned_run_containers(name)

    def _remove_single_image(self, name: str) -> None:
        """Remove this name's labeled single-image Stack container, if one is
        present. A no-op when no single-image form exists (a process or compose
        Stack, or an ordinary start)."""
        if self._docker.stack_containers(name):
            self._docker.remove(f"{STACK_CONTAINER_PREFIX}{name}")

    def _compose_down(self, name: str) -> None:
        """Compose a tracked project down using the compose/overlay files it was
        brought up with (retained in ``_applied_compose``) — a valid teardown,
        never an empty file list. The applied record is dropped only AFTER the
        down succeeds, so a failed down is retried on the next pass rather than
        forgotten (ADR-0044 amendment). A no-op for a name this daemon does not
        track — the accepted prior-daemon compose-discovery limitation."""
        applied = self._applied_compose.get(name)
        if applied is None:
            return
        self._docker.compose(f"ozolith-{name}", [Path(p) for p in applied.files], "down")
        self._applied_compose.pop(name, None)

    def _teardown_container_forms(self, name: str) -> None:
        """Remove any container-kind runtime under this name — a single-image
        Stack container and/or a compose project this daemon started — so a
        container->process transition never leaves both forms active. Raises on
        the first failure (the caller retries the whole transition next pass); a
        no-op on an ordinary process start (no container form to remove)."""
        self._remove_single_image(name)
        self._compose_down(name)

    def _teardown_all_forms(self, name: str) -> None:
        """Tear down EVERY runtime form under one Stack name — supervised
        process child (with its owned Run containers and applied bookkeeping),
        labeled single-image container, and tracked compose project — regardless
        of which kind/form is currently live. The teardown for a stopped or
        drained desire whose desired kind/form may differ from what is actually
        running (ADR-0044 amendment): a container->stopped-process,
        process->stopped-container, compose->stopped-single-image, or
        single-image->stopped-compose desire each leave nothing behind.

        Raises on the first form that fails — the caller (a drain/recycle
        command handler, or the reconcile per-stack handler) logs it, emits the
        capped error event exactly once, leaves the command un-acked (drain) or
        the Stack for the next pass, and whatever a failed step did not clear is
        retried then. In the ordinary error-free case all three forms are torn
        down in the one call. Failures of STALE (absent-from-desired) objects
        are isolated separately in ``_reconcile_removed`` so one never aborts the
        whole sweep."""
        self._stop_process_child(name)
        self._remove_single_image(name)
        self._compose_down(name)

    def _drain(self, target: str | None) -> None:
        # Every runtime form under the name goes, whatever kind is live (a drain
        # must not leave a form of a different kind behind); each pass re-tears
        # a drained Stack down via the want_running=False converge path.
        names = [target] if target else [s.name for s in self._stacks()]
        for name in names:
            self._teardown_all_forms(name)
            self._drained.add(name)
        _atomic_json(self._config.drained_path, sorted(self._drained))

    def _recycle(self, target: str | None) -> None:
        names = [target] if target else [s.name for s in self._stacks()]
        for name in names:
            self._teardown_all_forms(name)
            self._drained.discard(name)
        _atomic_json(self._config.drained_path, sorted(self._drained))
        # The reconcile step of this same pass starts them again.

    def _converge_product(self, *, force: bool = False) -> None:
        """The pin is desired state (revision ruling amending ADR-0015):
        every pass compares the running product version against the pinned
        one and self-updates on mismatch — startup is just the first pass,
        and a failed install retries on a later pass with no re-queued
        command. Drain-aware queue-behind semantics are unchanged: an
        in-flight Run defers the attempt (``force`` keeps kill-the-tree)."""
        if self._update_done or self._product_attempted:
            return  # already succeeded (re-exec imminent) or tried this pass
        pin = str(self._desired.get("product_version", "") or "")
        if not pin or pin == self._config.version:
            self._update_blocker = None
            return
        if not force:
            blocker = self._inflight_blocker(None)
            if blocker is not None:
                if self._update_blocker != blocker:
                    self._log(f"product update to {pin} deferred ({blocker})")
                self._update_blocker = blocker
                return
        self._update_blocker = None
        self._product_attempted = True
        try:
            self._install_product(pin)
        except Exception as exc:
            self._log(f"product update to {pin} failed: {exc} (retrying next pass)")
            self._emit_error(type(exc).__name__, f"product update to {pin} failed: {exc}")

    def _install_product(self, pin: str) -> None:
        """Stop the tree, install the pinned version, re-exec in place
        (ADR-0013 §8). Release pins install by name from the package index;
        a served artifact set (the developer build path) is pulled from the
        Control Node and installed from local wheel files — nodes never
        pull source and never build."""
        self._supervisor.stop_all(grace_seconds=self._stop_grace_seconds())
        artifacts = self._desired.get("product_artifacts")
        if isinstance(artifacts, list) and artifacts and self._client is not None:
            targets = self._pull_artifacts(pin, [str(a) for a in artifacts])
        else:
            targets = [f"{name}=={pin}" for name in UPDATE_PACKAGES]
        proc = self._update_runner(
            [sys.executable, "-m", "pip", "install", "--upgrade", *targets],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pip install failed: {(proc.stderr or '').strip()[-300:]}")
        self._update_done = True
        self._log(f"updated to {pin}; re-exec")
        self._execv(sys.executable, [sys.executable, "-m", "theozolith_nodedaemon", *sys.argv[1:]])

    def _pull_artifacts(self, version: str, names: list[str]) -> list[str]:
        """Download the served wheels into the state dir; local file paths
        become the pip install targets."""
        wheel_dir = self._config.state_dir / "update-wheels"
        if wheel_dir.is_dir():
            for stale in wheel_dir.iterdir():
                stale.unlink()
        wheel_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for name in names:
            base = Path(name).name  # a served filename is never a path
            target = wheel_dir / base
            target.write_bytes(self._client.fetch_artifact(version, base))
            paths.append(str(target))
        return paths

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
                self._emit_error(type(exc).__name__, f"image {name}: build failed: {exc}")
        desired = self._stacks()
        # Converge runtime objects that desired state no longer names BEFORE
        # converging the ones it does — so a rename (old name torn down, new
        # name started) and a deletion both settle in one pass (ADR-0044).
        # A backstop around the whole sweep (each object is already isolated
        # inside it): even a failure enumerating the live inventory must not
        # stop the desired Stacks from converging or the orphan reap from running.
        try:
            self._reconcile_removed(desired)
        except Exception as exc:
            self._log(f"removed-runtime sweep failed: {exc}")
            self._emit_error(type(exc).__name__, f"removed-runtime sweep failed: {exc}")
        for stack in desired:
            want_running = stack.state == "running" and stack.name not in self._drained
            try:
                if stack.kind == "process":
                    self._converge_process(stack, want_running)
                else:
                    self._converge_container(stack, want_running)
            except Exception as exc:
                # Config-apply and container-start failures both land here.
                self._log(f"stack {stack.name}: reconcile failed: {exc}")
                self._emit_error(type(exc).__name__, f"stack {stack.name}: reconcile failed: {exc}")
        try:
            self._reap_orphans()
        except Exception as exc:
            self._log(f"orphan reap failed: {exc}")
            self._emit_error(type(exc).__name__, f"orphan reap failed: {exc}")

    def _reconcile_removed(self, desired: list[WireStack]) -> None:
        """Runtime objects with no matching desired Stack are torn down like an
        explicit stopped desire (ADR-0044 amendment): removal from desired
        state is a deletion — immediate teardown. Covers a deleted Stack, a
        renamed Stack (its old name gone), and any transition that also changed
        the Stack name. Names still present in desired state are the converge
        step's business (including a kind flip under the same name) and are
        never touched here; unrelated Stacks are preserved.

        Discovery differs by durability. Single-image container Stacks carry
        persistent Docker labels, so a stale one is reaped even across a daemon
        restart. Supervised process children and compose projects are tracked
        in memory — children never outlive the daemon and ``_applied_compose``
        likewise — so their orphan cleanup is scoped to this daemon's lifetime;
        the compose limitation is spelled out below."""
        desired_names = {s.name for s in desired}
        # Each stale object's teardown is isolated: one failure is logged and
        # emitted, its retry state is preserved, and the sweep continues to the
        # next object and on to converging the desired Stacks (ADR-0044).
        for name in self._supervisor.names():
            if name not in desired_names:
                self._log(f"stack {name}: absent from desired state; stopping process child")
                self._isolated(
                    f"stack {name}: absent process child",
                    lambda name=name: self._stop_process_child(name),
                )
        for row in self._docker.stack_containers():
            container_name = str(row.get("name", ""))
            if not container_name.startswith(STACK_CONTAINER_PREFIX):
                continue
            stack_name = container_name[len(STACK_CONTAINER_PREFIX) :]
            if stack_name not in desired_names:
                self._log(f"stack {stack_name}: absent from desired state; removing container")
                self._isolated(
                    f"stack {stack_name}: absent single-image container",
                    lambda cn=container_name: self._docker.remove(cn),
                )
        # Compose projects this daemon started and desired state no longer
        # names are composed down with their RETAINED files (never an empty file
        # list). LIMITATION (stated, not silently skipped): there is no host-side
        # compose-project discovery, so this reaps only projects in
        # ``_applied_compose`` (this daemon lifetime). A compose Stack started by
        # a PRIOR daemon and then deleted from desired state is not discoverable
        # here and would keep running until a running-form discovery mechanism (a
        # labeled `compose ls`) is added — a broader change deferred out of scope.
        for name in list(self._applied_compose):
            if name not in desired_names:
                self._log(f"stack {name}: absent from desired state; composing down")
                self._isolated(
                    f"stack {name}: absent compose project",
                    lambda name=name: self._compose_down(name),
                )

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
            self._emit_error(
                type(exc).__name__, f"stack {stack.name}: cannot deploy, secrets unavailable: {exc}"
            )
            return False

    def _process_env(self, stack: WireStack) -> dict[str, str]:
        """The full effective environment a process Stack's child launches with.
        This is also the convergence input: the resolved worker-type env
        (repository/adapter/model/run-image tag) and the secret <ENV>_FILE
        mappings all live here, so a change to any of them changes this dict and
        drives a restart (ADR-0044 amendment)."""
        env = {
            "THEOZOLITH_NODE_NAME": self._config.node,
            # Per-process-Stack identity (ADR-0044): the Stack name becomes the
            # theozolith.owner label on the run containers this driver creates,
            # so _reap_orphans matches them to their supervisor. Control-authored
            # Stack env still wins (a driver's THEOZOLITH_STACK, if declared).
            "THEOZOLITH_STACK": stack.name,
            # The dedicated per-Stack jobs directory (ADR-0019); an explicit
            # env value in the Stack definition wins.
            "THEOZOLITH_JOBS_DIR": str(stack_jobs_dir(stack)),
            **stack.env,
        }
        # The control channel for node-resident drivers (ADR-0023): they
        # authenticate as the node that supervises them — the daemon hands
        # its own per-node token down instead of a hand-configured shared
        # token. Stack env wins (daemon-less dev keeps its own settings).
        if self._config.control_url:
            env.setdefault("CONTROL_NODE_URL", self._config.control_url)
            env.setdefault("THEOZOLITH_NODE_TOKEN", self._config.node_token)
            if self._config.tls_ca:
                env.setdefault("THEOZOLITH_TLS_CA", self._config.tls_ca)
        # THEOZOLITH_RUN_IMAGE now arrives in the control-authored Stack env
        # (resolved from the worker type, ADR-0044); the daemon no longer maps a
        # removed wire field to a built tag.
        env_files = secret_env_files(stack, self._config.secrets_dir)
        env.update({f"{name}_FILE": path for name, path in env_files.items()})
        return env

    def _converge_process(self, stack: WireStack, want_running: bool) -> None:
        if not want_running:
            # Stopped/drained: tear down EVERY form under this name, not just a
            # live process child. A desire that flipped to a stopped process
            # while a container/compose form is actually running must leave
            # nothing behind (ADR-0044 amendment).
            self._teardown_all_forms(stack.name)
            return
        env = self._process_env(stack)
        argv = shlex.split(stack.command) if stack.command else []
        alive = self._supervisor.alive(stack.name)
        # Fail closed on old/incomplete built-in-driver desired state (ADR-0044
        # amendment, Sean's ruling — no backward compatibility): a built-in
        # driver must not launch without a control-authored THEOZOLITH_RUN_IMAGE.
        # Its absence means old control or an old cached document; refuse to run
        # against the worker package's default run image, stop any already-live
        # instance, and raise so the error surfaces — reconcile continues with
        # the other Stacks (a generic process Stack stays legal without it).
        if argv and argv[0] in BUILTIN_DRIVER_COMMANDS and not env.get("THEOZOLITH_RUN_IMAGE"):
            if alive:
                self._stop_process_child(stack.name)
            raise RuntimeError(
                f"built-in driver {argv[0]!r} has no control-authored"
                " THEOZOLITH_RUN_IMAGE — incompatible/incomplete desired state"
                " (a coordinated control upgrade is required, ADR-0044); refusing"
                " to launch against the default run image"
            )
        if alive and not self._supervisor.needs_restart(stack.name, stack.command, env):
            return  # already live with this exact effective spec — no churn
        # A (re)start is due for a live child ONLY because its effective spec
        # changed; that must never interrupt an in-flight Run (ADR-0044
        # amendment). If the running child is mid-Run, leave it and its Run
        # containers untouched and retry on a later pass — restarting exactly
        # once after the Run ends. The signal is read from the jobs dir the
        # RUNNING child was launched with, so a THEOZOLITH_JOBS_DIR change still
        # detects a Run under the old path. A dead child never blocks (its dirs
        # are orphans); stopped/drained desire took the immediate path above.
        if alive:
            blocker = self._inflight_blocker([stack.name])
            if blocker is not None:
                self._log(f"stack {stack.name}: restart deferred ({blocker})")
                return
        # Never restart onto a run image that is not built: if the driver's
        # declared derived run image is missing (its recipe failed or has not
        # built yet this pass), keep the current child running and try again
        # next pass rather than tear a working driver down onto an unavailable
        # image (ADR-0044 amendment).
        image_tag = env.get("THEOZOLITH_RUN_IMAGE", "")
        declared_tags = {img.get("tag") for img in self._images().values()}
        if image_tag in declared_tags and not self._docker.image_exists(image_tag):
            self._log(
                f"stack {stack.name}: deferring {'restart' if alive else 'start'} —"
                f" run image {image_tag} not built yet"
            )
            return
        # Preflight every replacement secret BEFORE stopping the old child: if
        # the new effective spec's secrets are unavailable and uncached, keep
        # the current child running and retry later rather than tear a working
        # driver down onto a spec that cannot launch (ADR-0044 amendment). Only
        # once all prerequisites are ready do we kill the tree and relaunch.
        if not self._pull_stack_secrets(stack):
            return
        if alive:
            self._stop_process_child(stack.name)  # kill-the-tree before relaunching
        # container->process: a Stack that flipped from a container form to a
        # process leaves a stale Stack container / compose project under this
        # name. Now that the process's prerequisites (run image, secrets) are
        # ready, remove it so the two forms never coexist (ADR-0044 amendment);
        # a no-op on an ordinary start.
        self._teardown_container_forms(stack.name)
        self._supervisor.ensure_running(stack.name, stack.command, env)
        self._applied_jobs_dir[stack.name] = str(stack_jobs_dir(stack))

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

    def _compose_fingerprint(self, stack: WireStack) -> str:
        """All effective compose inputs, so any change reconciles (ADR-0044)."""
        return json.dumps(
            {
                "compose_files": list(stack.compose_files),
                "env": stack.env,
                "secrets": stack.secrets,
            },
            sort_keys=True,
        )

    def _container_fingerprint(self, stack: WireStack) -> str:
        """The effective runtime spec of a single-image container Stack: image
        tag, command, env, secret mapping, ports, volumes. A change to any of
        these replaces the running container. Secret VALUES never enter this —
        only the mapping (ENV -> secret name) does (ADR-0044 amendment)."""
        return hashlib.sha256(
            json.dumps(
                {
                    "image": stack.image,
                    "command": stack.command,
                    "env": stack.env,
                    "secrets": stack.secrets,
                    "ports": list(stack.ports),
                    "volumes": list(stack.volumes),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def _converge_container(self, stack: WireStack, want_running: bool) -> None:
        # A process child still tracked under this name means the Stack flipped
        # process->container: its teardown is deferred until the container's
        # prerequisites are ready, and never happens while its Run is in flight.
        child_lingers = stack.name in self._supervisor.names()
        if not want_running:
            # Stopped/drained: tear down EVERY form under this name (process
            # child, single-image container, tracked compose project, owned Run
            # containers), whatever kind the desired-but-stopped Stack declares
            # — a compose->stopped-single-image or single-image->stopped-compose
            # desire must not strand the currently running form (ADR-0044).
            self._teardown_all_forms(stack.name)
            # Post-restart safety: a compose project for a Stack STILL named in
            # desired state (now stopped/drained) but not tracked this lifetime —
            # its in-memory record was lost across a daemon restart — is downed
            # with the DESIRED compose files (a valid, non-empty teardown), so a
            # stopped compose Stack does not keep running unseen. An ABSENT Stack
            # has no files and stays under the stated prior-daemon discovery
            # limitation (handled, tracked-only, in _reconcile_removed).
            if (
                stack.compose_files
                and stack.name not in self._applied_compose
                and self._docker.compose_ps(f"ozolith-{stack.name}")
            ):
                self._log(f"stack {stack.name}: downing untracked compose project (post-restart)")
                self._docker.compose(
                    f"ozolith-{stack.name}", self._compose_paths(stack), "down"
                )
            return
        # process->container: a live child's in-flight Run defers the WHOLE
        # transition (read from its applied jobs dir — the desired kind is no
        # longer process, so _inflight_blocker would not see it). A dead child
        # never blocks and is torn down once prerequisites are ready.
        if child_lingers and self._supervisor.alive(stack.name):
            blocker = self._child_inflight_blocker(stack.name)
            if blocker is not None:
                self._log(f"stack {stack.name}: process->container transition deferred ({blocker})")
                return
        if stack.compose_files:
            self._converge_compose(stack, child_lingers)
            return
        self._converge_single_image(stack, child_lingers)

    def _converge_compose(self, stack: WireStack, child_lingers: bool) -> None:
        """Bring the compose form up as the SOLE runtime form under this name.
        Handles a single-image->compose transition too: the losing single-image
        container is retained until the compose prerequisites (secrets, valid
        materialized files) are ready, then removed BEFORE compose up so the two
        container forms never coexist (ADR-0044 amendment)."""
        fingerprint = self._compose_fingerprint(stack)
        single_present = bool(self._docker.stack_containers(stack.name))
        applied = self._applied_compose.get(stack.name)
        if (
            not child_lingers
            and not single_present
            and applied is not None
            and applied.fingerprint == fingerprint
        ):
            return  # compose is already the only form, at this exact spec — no churn
        if not self._pull_stack_secrets(stack):
            return
        files = self._compose_paths(stack)  # materialize + validate before any teardown
        if child_lingers:  # process->compose: retire the process form
            self._stop_process_child(stack.name)
        if single_present:  # single-image->compose: retire the single-image form first
            self._log(
                f"stack {stack.name}: single-image->compose;"
                " removing the single-image container before compose up"
            )
            self._remove_single_image(stack.name)
        self._docker.compose(f"ozolith-{stack.name}", files, "up")
        # Retain the materialized paths so a later down is valid (never empty).
        self._applied_compose[stack.name] = AppliedCompose(fingerprint, [str(f) for f in files])

    def _converge_single_image(self, stack: WireStack, child_lingers: bool) -> None:
        """Run the single-image form as the SOLE runtime form under this name.
        Handles a compose->single-image transition too: the desired image and
        secrets are preflighted and the old compose project is RETAINED until
        they are ready, then composed down (with its retained files) BEFORE
        docker run so the two forms never coexist (ADR-0044 amendment)."""
        want = self._container_fingerprint(stack)
        compose_tracked = stack.name in self._applied_compose
        rows = self._docker.stack_containers(stack.name)
        running = [r for r in rows if r.get("state") == "running"]
        if (
            not child_lingers
            and not compose_tracked
            and running
            and running[0].get(LABEL_STACK_SPEC, "") == want
        ):
            return  # verifiably the only form, at the current effective spec — no churn
        # Never replace (or first-create) onto a derived image our OWN recipes
        # declare but that is not built yet (a failed or not-yet build): keep any
        # current container OR the old compose project alive, emit a deferral,
        # and retry next pass — replacing exactly once the image appears. An
        # arbitrary external image reference (not one of our recipe tags) is
        # Docker's to pull normally, so it is not gated here (ADR-0044 amendment).
        declared_tags = {img.get("tag") for img in self._images().values()}
        if stack.image in declared_tags and not self._docker.image_exists(stack.image):
            self._log(
                f"stack {stack.name}: deferring container "
                f"{'replacement' if (running or compose_tracked) else 'create'} —"
                f" image {stack.image} not built yet"
            )
            return
        if running and not running[0].get(LABEL_STACK_SPEC):
            # A pre-existing container with no trustworthy applied-spec record
            # (older daemon, a manual start, or a degraded recovery) must not be
            # silently assumed current: reconcile it once so the fingerprint is
            # recovered from here on (ADR-0044 amendment).
            self._log(
                f"stack {stack.name}: running container has no applied-spec label;"
                " reconciling once"
            )
        if not self._pull_stack_secrets(stack):
            return
        if child_lingers:  # prerequisites ready: retire the process form before the container
            self._stop_process_child(stack.name)
        if compose_tracked:  # compose->single-image: retire the compose form first
            self._log(
                f"stack {stack.name}: compose->single-image;"
                " composing the old project down before docker run"
            )
            self._compose_down(stack.name)
        self._docker.run_stack_container(
            stack.name,
            stack.image,
            env_files=secret_env_files(stack, self._config.secrets_dir),
            env=stack.env,
            ports=list(stack.ports),
            volumes=list(stack.volumes),
            # Optional docker-run command for the single-image form — how
            # the Flight Deck starts its named tmux session (ADR-0019).
            command=shlex.split(stack.command) if stack.command else None,
            spec=want,
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
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["provision"]:
        # The one node-side human interaction (ADR-0023): paste the join
        # string `theozolith join-token create` printed. Everything else on
        # a node is this daemon.
        from theozolith_nodedaemon.provisioning import main as provision_main

        return provision_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="theozolith-nodedaemon",
        description=(
            "TheOzolith Node Daemon: heartbeat to the Control Node with this node's"
            " provisioned token, reconcile declarative Stacks, build derived images,"
            " reap orphans. 'provision <join-string>' joins a fresh box (ADR-0023)."
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
