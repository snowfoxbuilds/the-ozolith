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
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from theozolith_nodedaemon import configdist
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

# The generic driver launcher every worker type resolves to control-side
# (`theozolith-driver <ref>`, ADR-0020) — one executable covers builtin:* and
# future drivers/* refs, so the daemon recognizes drivers by argv[0] alone and
# never duplicates control's ref registry or parses a worker type. A driver
# only functions with a control-authored THEOZOLITH_RUN_IMAGE; seeing this
# command WITHOUT that env means old or incomplete desired state, and the
# daemon fails that Stack closed rather than launch it against the worker
# package's default run image (ADR-0044 amendment).
DRIVER_LAUNCHER = "theozolith-driver"

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


# AppliedCompose lifecycle states (ADR-0044 amendment: crash-consistent compose
# startup). PENDING is the write-ahead record persisted BEFORE compose up — its
# mere presence never claims the project is healthy (compose_ps is the liveness
# authority), it only guarantees a durable teardown/retry context if the daemon
# dies anywhere from the write-ahead through the post-up confirmation. APPLIED is
# the optional confirmation stamped after a successful up. Both states carry
# identical recovery power (name + fingerprint + files); the distinction is
# informational, so a lost confirmation degrades to a still-usable PENDING record.
COMPOSE_PENDING = "pending"
COMPOSE_APPLIED = "applied"


@dataclass
class AppliedCompose:
    """The non-secret recovery record for a compose project this daemon is
    bringing up or has brought up: its effective fingerprint (change detection),
    the materialized compose/overlay file PATHS, and a lifecycle ``state``
    (``pending`` write-ahead / ``applied`` confirmed). Retaining the paths lets a
    later ``down`` run with real teardown context — a valid ``docker compose
    --file … down`` — rather than an empty file list (ADR-0044 amendment). Secret
    VALUES never enter this record: it holds file paths, and the compose documents
    carry secret references (the VAR_FILE convention), not values.

    The record is persisted to disk (``applied_compose_path``) and reloaded on
    boot, so a compose project that outlived the daemon is still distinguishable
    and tearable down during a later same-name kind/form transition — the paths
    survive because they are references, and the fingerprint carries only compose
    documents (secret references, not values), env, and the secret NAME mapping.

    The referenced files live in a generation-addressed directory keyed by the
    fingerprint (``_compose_paths``/``_compose_generation``), so a record's files
    are IMMUTABLE for its lifetime: a new spec materializes a new generation and
    never rewrites the bytes this record points at. Obsolete generations are
    reclaimed only after a transition is confirmed applied or a project is downed
    (ADR-0044 amendment: immutable/versioned materialization).

    The record is written AHEAD of compose up (state ``pending``) so a crash after
    Docker creates the project but before confirmation still leaves durable
    recovery metadata; a successful up promotes it to ``applied`` best-effort."""

    fingerprint: str
    files: list[str]
    state: str = COMPOSE_APPLIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "files": list(self.files),
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: Any) -> AppliedCompose | None:
        """Validate a persisted record before its file paths are trusted for a
        teardown: it must be a dict with a NON-EMPTY list of non-empty file paths
        (an empty file list could only yield an invalid, empty ``compose … down``,
        which is exactly what retaining the paths exists to avoid). A missing
        ``state`` reads as ``applied`` — the only records the pre-write-ahead code
        wrote were post-up, so treating them as confirmed is crash-correct."""
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            return None
        files = [str(f) for f in data["files"] if str(f)]
        if not files:
            return None
        return cls(
            fingerprint=str(data.get("fingerprint", "")),
            files=files,
            state=str(data.get("state", "") or COMPOSE_APPLIED),
        )


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
        # Compose projects this daemon is bringing up or has brought up, name ->
        # recovery record. The record retains the materialized compose/overlay
        # paths so a later down (change, transition, stopped/drained,
        # absent-from-desired) is a valid `docker compose --file … down`, never an
        # empty file list (ADR-0044). It is written AHEAD of compose up (state
        # pending) and confirmed after (state applied), and is persisted to disk
        # and reloaded here: a compose project can outlive — or crash-orphan
        # itself past — the daemon, so the record must survive a restart for a
        # later same-name kind/form transition to distinguish and tear it down
        # (ADR-0044 amendment). Only non-secret metadata is stored — fingerprint,
        # file paths, and lifecycle state — never secret values.
        self._applied_compose: dict[str, AppliedCompose] = self._load_applied_compose()
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
        # Config-distribution convergence (ADR-0042): the drivers-hash is
        # desired state, checked every pass and applied at most once per pass.
        self._drivers_attempted = False
        self._drivers_blocker: str | None = None
        # The verified applied drivers-hash, memoized for the duration of ONE
        # pass (reset in ``once``). The ``current`` pointer alone is never proof:
        # each pass re-derives the applied hash by recomputing the manifest over
        # the pointed-at tree (``_verify_applied_drivers``), so a missing tree, a
        # malformed pointer, or a mutated tree reads as non-converged and is
        # repaired on the next fetch. None = not yet computed this pass; a swap
        # or retirement sets it directly to the freshly verified value.
        self._verified_drivers_hash: str | None = None
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
        self._drivers_attempted = False  # at most one config-dist swap per pass
        # Re-verify the applied distribution from scratch each pass — startup
        # (the first pass) and every heartbeat thereafter — so a partial restore
        # or a runtime-mutated tree is detected rather than trusted (ADR-0042).
        self._verified_drivers_hash = None
        commands = self._exchange_heartbeat()
        for command in commands:
            if not self._execute(command):
                # Queue-behind blocks the QUEUE: commands after a deferred
                # one wait too, or a later drain could land before the
                # recycle it was issued after and be undone by it.
                break
        self._converge_product()
        # After product convergence, before reconcile: a new distribution stops
        # its affected driver Stacks so THIS pass's _reconcile restarts them on
        # the new tree (ADR-0042).
        self._converge_drivers()
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
            # The applied config distribution (ADR-0042): the hash the node has
            # actually converged onto, and the product version its artifact was
            # stamped against. Both "" when none is applied.
            "drivers_hash": self._current_drivers_hash(),
            "drivers_built_against": self._current_built_against(),
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

    def _load_applied_compose(self) -> dict[str, AppliedCompose]:
        """Recover the persisted applied-compose records on boot. Malformed or
        missing state degrades to an empty map (a corrupt file must never crash
        the daemon); each record is validated by ``AppliedCompose.from_dict``."""
        raw = _read_json(self._config.applied_compose_path, {})
        result: dict[str, AppliedCompose] = {}
        if isinstance(raw, dict):
            for name, record in raw.items():
                applied = AppliedCompose.from_dict(record)
                if applied is not None:
                    result[str(name)] = applied
        return result

    def _persist_applied_compose(self) -> None:
        """Write the applied-compose map atomically. Called after every mutation
        (an up records, a down drops) so the on-disk record always mirrors what
        is actually applied — the fact a restart needs to tear a surviving
        compose project down. Only fingerprint + file paths are written; secret
        values never reach this file (ADR-0044 amendment)."""
        _atomic_json(
            self._config.applied_compose_path,
            {name: record.to_dict() for name, record in sorted(self._applied_compose.items())},
        )

    def _write_ahead_compose(self, name: str, record: AppliedCompose) -> None:
        """Adopt and atomically persist a compose recovery record BEFORE the
        compose up it describes (ADR-0044 amendment). On a persist failure the
        in-memory map is rolled back to its prior entry — so memory and disk never
        diverge — and the error re-raises, which stops the caller from invoking
        compose up: no project is created without a durable record able to tear it
        down. The record's files must be non-empty (a compose Stack always
        materializes at least one document)."""
        if not record.files:
            raise RuntimeError(f"stack {name}: refusing a compose recovery record with no files")
        prior = self._applied_compose.get(name)
        self._applied_compose[name] = record
        try:
            self._persist_applied_compose()
        except Exception:
            if prior is None:
                self._applied_compose.pop(name, None)
            else:
                self._applied_compose[name] = prior
            raise

    def _compose_running(self, name: str) -> bool:
        """Whether the deterministic compose project has running workload — the
        SAME liveness rule the heartbeat status reports (a ``compose_ps`` row in
        the running state). An AppliedCompose fingerprint proves which spec was
        applied, not that the project still runs, so convergence consults this
        before declaring a compose Stack already converged (ADR-0044 amendment)."""
        return any(
            row.get("state") == "running" for row in self._docker.compose_ps(f"ozolith-{name}")
        )

    def _compose_down(self, name: str) -> None:
        """Compose a tracked project down using the compose/overlay files it was
        brought up with (retained in ``_applied_compose``, across restarts) — a
        valid teardown, never an empty file list. The applied record is dropped
        (and the change persisted) only AFTER the down succeeds, so a failed down
        is retried on the next pass rather than forgotten (ADR-0044 amendment). A
        no-op for a name this daemon has no record of — the accepted prior-daemon
        compose-discovery limitation, now only when the persisted record is also
        gone."""
        applied = self._applied_compose.get(name)
        if applied is None:
            return
        self._docker.compose(f"ozolith-{name}", [Path(p) for p in applied.files], "down")
        self._applied_compose.pop(name, None)
        self._persist_applied_compose()
        # The downed generation is now referenced by no record; the per-pass
        # ``_sweep_compose_generations`` reclaims every unreferenced generation
        # dir for this (now recordless) name and retries any deletion that fails,
        # so cleanup is not tied to this teardown succeeding (ADR-0044 amendment).

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

    # -- config distribution (ADR-0042) -----------------------------------------------

    def _current_drivers_hash(self) -> str:
        """The VERIFIED applied config-distribution hash ('' when none applied) —
        what the heartbeat reports and what the swap compares against. The
        ``current`` pointer alone is not proof: the hash is only returned when
        the pointer is well-formed AND its tree exists AND that tree recomputes
        to the pointer (``_verify_applied_drivers``). Memoized per pass."""
        if self._verified_drivers_hash is None:
            self._verified_drivers_hash = self._verify_applied_drivers()
        return self._verified_drivers_hash

    def _verify_applied_drivers(self) -> str:
        """Recompute-and-verify the applied distribution (ADR-0042). Returns the
        applied hash only when the ``current`` pointer is a 64-hex value whose
        tree exists under ``config-dist/<hash>/``, holds ONLY the unpacked
        artifact shape (``_applied_tree_shape_error``), and recomputes to the
        pointer; a missing pointer, a malformed value, a missing tree, a rogue
        or irregular applied-tree entry, an unenumerable subtree, or a
        recompute mismatch all read as '' (non-converged), so
        ``_converge_drivers`` refetches and repairs it — while the old verified
        tree keeps being used until a replacement is fully verified and swapped
        in. Verification is FAIL CLOSED: ``manifest_hash_of_tree`` raises on
        symlinks, FIFO/socket/device entries, and enumeration failures rather
        than silently omitting them, so no unhashed executable entry can
        coexist with a converged report."""
        try:
            pointer = self._config.config_dist_current.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not pointer:
            return ""
        if not re.fullmatch(r"[0-9a-f]{64}", pointer):
            self._log(f"config distribution pointer {pointer[:20]!r} is malformed; not converged")
            return ""
        tree = self._config.config_dist_dir / pointer
        if not tree.is_dir():
            self._log(f"config distribution {pointer[:12]} has no applied tree; not converged")
            return ""
        shape_error = self._applied_tree_shape_error(tree)
        if shape_error is not None:
            self._log(f"config distribution {pointer[:12]} {shape_error}; not converged")
            return ""
        try:
            recomputed = configdist.manifest_hash_of_tree(tree)
        except (OSError, configdist.ConfigDistError) as exc:
            # An unreadable, irregular, or malformed applied tree reads as
            # non-converged, so the next pass refetches and repairs it
            # (ADR-0042) — logged so the repair trigger is observable.
            self._log(f"config distribution {pointer[:12]} failed verification: {exc}")
            return ""
        if recomputed != pointer:
            self._log(
                f"config distribution {pointer[:12]} tree recomputes to"
                f" {recomputed[:12] or '(empty)'}; not converged"
            )
            return ""
        return pointer

    def _applied_tree_shape_error(self, tree: Path) -> str | None:
        """The applied root ``config-dist/<hash>/`` may hold ONLY the unpacked
        artifact shape: the ``drivers`` directory (real, not a symlink) and the
        ``config-dist.json`` metadata file (regular, not a symlink) — either
        may be absent, but nothing else non-excluded may be present. The
        drivers manifest covers only ``drivers/``, so a rogue sibling at the
        applied root would otherwise be unhashed-but-present content riding a
        converged report (ADR-0042: the applied state directory is potentially
        malformed after a restore or local corruption). Returns a reason, or
        ``None`` when the shape is valid."""
        if tree.is_symlink():
            return "applied tree is a symlink"
        try:
            with os.scandir(tree) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            return f"applied tree cannot be enumerated ({exc})"
        for entry in entries:
            if configdist.excluded_part(entry.name):
                continue
            if entry.name == configdist.DRIVERS_DIR and entry.is_dir(follow_symlinks=False):
                continue
            if entry.name == configdist.ARTIFACT_METADATA and entry.is_file(follow_symlinks=False):
                continue
            return f"applied tree holds an unexpected or irregular entry {entry.name!r}"
        return None

    def _current_built_against(self) -> str:
        """The product version the applied distribution was built against, from
        its ``config-dist.json`` ('' when none). Advisory only, so a missing or
        unreadable stamp is simply '' — but ``config-dist.json`` is NOT trusted
        as proof of content: it is only read once the tree has verified, and its
        ``format`` and ``drivers_hash`` are checked against the verified hash
        before ``built_against`` is reported (a stale/foreign envelope yields
        '', never a wrong stamp)."""
        current = self._current_drivers_hash()
        if not current:
            return ""
        meta = _read_json(self._config.config_dist_dir / current / configdist.ARTIFACT_METADATA, {})
        if not isinstance(meta, dict):
            return ""
        if meta.get("format") != configdist.ARTIFACT_FORMAT or meta.get("drivers_hash") != current:
            return ""
        built = meta.get("built_against", "")
        return built if isinstance(built, str) else ""

    def _drivers_stack_names(self) -> list[str]:
        """Desired process Stacks whose command is ``theozolith-driver
        drivers/<ref>`` — the config-distribution drivers a swap must restart.
        Identified by the SAME parse as the landed fail-closed guard (argv[0]
        the launcher, argv[1] under ``drivers/``): a builtin:* driver Stack and
        a generic process Stack are untouched by a swap (ADR-0042)."""
        names = []
        for stack in self._stacks():
            if stack.kind != "process" or not stack.command:
                continue
            try:
                argv = shlex.split(stack.command)
            except ValueError:
                continue
            if len(argv) >= 2 and argv[0] == DRIVER_LAUNCHER and argv[1].startswith("drivers/"):
                names.append(stack.name)
        return names

    def _stop_drivers_stacks(self) -> None:
        for name in self._drivers_stack_names():
            if self._supervisor.alive(name):
                self._log(f"stack {name}: stopping for config-distribution swap")
                self._stop_process_child(name)

    def _write_current_pointer(self, drivers_hash: str) -> None:
        path = self._config.config_dist_current
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(drivers_hash, encoding="utf-8")
        os.replace(tmp, path)

    def _gc_config_dist(self, keep: set[str]) -> None:
        """Reclaim unpacked distributions not in ``keep`` (current + previous),
        and sweep stale dot-temps left by an interrupted unpack. The ``current``
        pointer file is never touched here."""
        root = self._config.config_dist_dir
        if not root.is_dir():
            return
        for child in sorted(root.iterdir()):
            if child.name == self._config.config_dist_current.name:
                continue
            if child.name.startswith("."):  # stale unpack temp
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                continue
            if child.is_dir() and child.name not in keep:
                shutil.rmtree(child, ignore_errors=True)

    def _apply_config_dist(self, desired: str, previous: str) -> None:
        """Fetch → verify in staging → stop affected drivers → exchange →
        publish the pointer (ADR-0042). Fetch a 409 → ControlError (retry next
        pass). The replacement is unpacked into a dot-prefixed STAGING sibling
        (every member name validated) and verified by recompute there, with the
        currently applied tree fully intact — on any fetch/verify failure
        NOTHING has been stopped or touched and the old tree keeps being used
        and reported. Only after verification are the affected driver Stacks
        stopped (never beneath a live child — a same-hash repair replaces the
        very tree the child runs from), the tree exchanged, and the ``current``
        pointer published. Each transition is crash-recoverable: a failure at
        any step leaves a pointer/tree state that reads as non-converged
        (evidence-based) and is retried honestly next pass."""
        data = self._client.fetch_config_artifact(desired)
        root = self._config.config_dist_dir
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f".{desired}.tmp"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            configdist.extract_zip(data, staging)
            recomputed = configdist.manifest_hash_of_tree(staging)
            if recomputed != desired:
                raise configdist.ConfigDistError(
                    f"config distribution hash mismatch: unpacked tree recomputes to"
                    f" {recomputed[:12] or '(empty)'}, expected {desired[:12]}"
                )
            # The replacement is fully verified — only now touch the running
            # world. Stop the affected driver Stacks BEFORE the active path is
            # removed or exchanged, so no interval exists where that path is
            # absent beneath a running process; this pass's _reconcile restarts
            # them on the published tree. Queue-behind was already honored by
            # _converge_drivers, so no active Run is killed here.
            self._stop_drivers_stacks()
            self._exchange_config_dist_tree(staging, root / desired)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        self._write_current_pointer(desired)
        # Memoize ONLY once the applied tree and the pointer are BOTH valid: a
        # failure anywhere above propagates first, so the heartbeat keeps
        # reporting the evidence-based value instead of the desired hash.
        self._verified_drivers_hash = desired
        self._log(f"config distribution converged to {desired[:12]}")
        self._gc_config_dist(keep={desired, previous} - {""})

    def _exchange_config_dist_tree(self, staging: Path, final: Path) -> None:
        """Publish a verified staging tree at ``final``, crash-recoverably. A
        directory cannot be renamed onto, so an existing ``final`` (a same-hash
        repair of a mutated/unreadable tree) is first renamed aside to a
        dot-prefixed sibling, the staging tree renamed in, then the retired
        tree reclaimed. Affected driver Stacks are already stopped by the
        caller, so no live process sits beneath the exchange. A crash between
        the renames leaves the pointer aimed at a missing tree — which reads as
        non-converged next pass and is refetched — and the dot-prefixed
        leftovers are swept by ``_gc_config_dist``."""
        retired = final.parent / f".{final.name}.retired"
        if retired.exists():
            shutil.rmtree(retired, ignore_errors=True)  # a crashed earlier exchange
        if final.exists():
            os.replace(final, retired)
        os.replace(staging, final)
        shutil.rmtree(retired, ignore_errors=True)

    def _retire_config_dist(self, previous: str) -> None:
        """Empty-desired: stop the config-distribution driver Stacks, clear the
        ``current`` pointer, and reclaim the unpacked trees (ADR-0042)."""
        self._stop_drivers_stacks()
        with contextlib.suppress(OSError):
            self._config.config_dist_current.unlink()
        self._gc_config_dist(keep=set())
        self._verified_drivers_hash = ""  # nothing applied now
        if previous:
            self._log("config distribution retired (none desired)")

    def _converge_drivers(self) -> None:
        """The drivers-hash is desired state (ADR-0042): every pass compares the
        applied distribution against the desired one and converges on mismatch.
        Queue-behind an in-flight Run like the product pin; a fetch/verify
        failure logs, emits, and retries next pass (control-side escalates
        persistence)."""
        if self._update_done or self._drivers_attempted:
            return  # a product re-exec is imminent, or already tried this pass
        desired = str(self._desired.get("drivers_hash", "") or "")
        current = self._current_drivers_hash()
        if desired == current:
            self._drivers_blocker = None
            return
        blocker = self._inflight_blocker(None)
        if blocker is not None:
            # The node keeps reporting the old hash and stays ineligible; the
            # Run finishes, then convergence applies (log once per blocker).
            if self._drivers_blocker != blocker:
                self._log(f"config distribution {desired[:12] or '(none)'} deferred ({blocker})")
            self._drivers_blocker = blocker
            return
        self._drivers_blocker = None
        if desired and self._client is None:
            return  # cache-only: cannot fetch a new distribution
        self._drivers_attempted = True
        try:
            if desired:
                self._apply_config_dist(desired, current)
            else:
                self._retire_config_dist(current)
        except Exception as exc:
            self._log(f"config distribution {desired[:12] or '(none)'} failed: {exc}")
            self._emit_error(
                type(exc).__name__,
                f"config distribution {desired[:12] or '(none)'} apply failed: {exc}",
            )

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
        # Reclaim obsolete compose generation dirs repo-wide, every pass, so a
        # deletion that failed earlier is retried and an orphaned generation is
        # discovered even for a Stack that transitioned form, stopped/drained, or
        # left desired state — none of which retains a record to trigger cleanup
        # (ADR-0044 amendment). Non-blocking: isolated per deletion, with a
        # backstop around enumeration so it never aborts the pass.
        try:
            self._sweep_compose_generations()
        except Exception as exc:
            self._log(f"compose generation sweep failed: {exc}")
            self._emit_error(type(exc).__name__, f"compose generation sweep failed: {exc}")

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
        # Compose projects whose applied record desired state no longer names are
        # composed down with their RETAINED files (never an empty file list). The
        # record now survives a daemon restart (``_applied_compose`` is persisted
        # and reloaded), so a project a PRIOR daemon started and desired state has
        # since dropped is reaped here too, as long as its persisted record
        # survives. LIMITATION (stated, not silently skipped, and unchanged in
        # kind): there is still no host-side compose-project discovery (a labeled
        # `compose ls`), so a project whose persisted record is also gone — the
        # deletable cache was cleared, or it predates persistence — remains
        # undiscoverable while absent from desired state; adding that discovery is
        # a broader change deferred out of scope. This residual limitation never
        # applies to a name STILL present in desired state: a same-name transition
        # or a stopped/drained desire recovers and tears the project down via the
        # persisted record (or, as a post-restart backstop, the desired files).
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
        # Fail closed on old/incomplete driver desired state (ADR-0044
        # amendment, Sean's ruling — no backward compatibility): a driver
        # Stack must not launch without a control-authored THEOZOLITH_RUN_IMAGE.
        # Its absence means old control or an old cached document; refuse to run
        # against the worker package's default run image, stop any already-live
        # instance, and raise so the error surfaces — reconcile continues with
        # the other Stacks (a generic process Stack stays legal without it).
        if argv and argv[0] == DRIVER_LAUNCHER and not env.get("THEOZOLITH_RUN_IMAGE"):
            if alive:
                self._stop_process_child(stack.name)
            raise RuntimeError(
                f"driver Stack {shlex.join(argv)!r} has no control-authored"
                " THEOZOLITH_RUN_IMAGE — incompatible/incomplete desired state"
                " (a coordinated control upgrade is required, ADR-0044); refusing"
                " to launch against the default run image"
            )
        if alive and not self._supervisor.needs_restart(stack.name, stack.command, env):
            # Already live at this exact effective spec. Declaring the successor
            # converged and returning must NOT mask a stale single-image container
            # or tracked compose project under this name (a predecessor whose
            # removal failed on an earlier pass): reap the stale form — without
            # churning the healthy child — then return. A teardown failure here
            # raises to reconcile (surfaced, retried next pass) and the child
            # stays untouched; two forms never coexist unnoticed (ADR-0044
            # amendment). The common case (process is the SOLE form) tears nothing
            # down and returns straight away.
            if self._docker.stack_containers(stack.name) or stack.name in self._applied_compose:
                self._teardown_container_forms(stack.name)
            return
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

    def _compose_generation(self, fingerprint: str) -> str:
        """The generation directory name for a compose fingerprint: a stable,
        filesystem-safe digest. Same effective spec -> same generation dir;
        any change (content, an added/removed/renamed file, an env or secret
        mapping change) yields a DIFFERENT generation, so a new spec never
        overwrites the bytes an existing record still references (ADR-0044
        amendment: immutable/versioned materialization)."""
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]

    def _compose_paths(self, stack: WireStack) -> list[Path]:
        """Materialize the inlined compose + overlay documents into a
        GENERATION-addressed directory — ``<state>/stacks/<name>/<generation>/`` —
        so a given applied/pending record's files are IMMUTABLE for that record's
        lifetime. A different effective spec resolves to a different generation
        and its own paths, so materializing a candidate never mutates the bytes
        the predecessor's retained record still points at (ADR-0044 amendment).

        Each document is written atomically (tmp + os.replace), then the COMPLETE
        candidate set is validated — a non-empty list whose every file reads back
        byte-for-byte — before it can be referenced by a pending record, so an
        interrupted write never exposes a half-written document as a referenced
        generation."""
        generation = self._compose_generation(self._compose_fingerprint(stack))
        base = self._config.state_dir / "stacks" / stack.name / generation
        root = base.resolve()
        paths = []
        for entry in stack.compose_files:
            target = (base / entry["name"]).resolve()
            if root not in target.parents:
                raise RuntimeError(f"compose path {entry['name']!r} escapes the stack dir")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_text(entry["content"], encoding="utf-8")
            os.replace(tmp, target)
            paths.append(target)
        if not paths:
            raise RuntimeError(f"stack {stack.name}: a compose Stack materialized no files")
        for path, entry in zip(paths, stack.compose_files, strict=True):
            if path.read_text(encoding="utf-8") != entry["content"]:
                raise RuntimeError(f"stack {stack.name}: compose file {path} failed validation")
        return paths

    def _gc_compose_generations(self, name: str, keep: set[str]) -> None:
        """Reclaim obsolete materialized compose directories for ONE Stack —
        every child dir whose name is not in ``keep``: the top-level entries
        under ``stacks/<name>/`` (generation dirs, or a legacy record's
        name-addressed roots such as ``compose/``) that some current record's
        ACTUAL file paths still reference; empty when none. Never removes a
        referenced entry, so a stopped/drained or absent Stack's still-recorded
        teardown context is preserved; the caller never invokes this at all for
        a Stack with a pending record (that Stack retains everything — see
        ``_sweep_compose_generations``). Each removal is isolated: a cleanup
        failure is logged, emitted as the normal capped error event, and left on
        disk so ``_sweep_compose_generations`` retries it on a later pass
        without blocking the rest of convergence (ADR-0044 amendment)."""
        stack_dir = self._config.state_dir / "stacks" / name
        if not stack_dir.is_dir():
            return
        for child in sorted(stack_dir.iterdir()):
            if child.is_dir() and child.name not in keep:
                self._isolated(
                    f"stack {name}: reclaiming obsolete compose generation {child.name}",
                    lambda child=child: shutil.rmtree(child),
                )

    def _sweep_compose_generations(self) -> None:
        """The repo-wide RETRY path for obsolete compose generation dirs, run once
        per reconcile pass (ADR-0044 amendment). It is keyed off the on-disk
        ``stacks/`` tree, NOT off a live compose project or a surviving
        AppliedCompose record, so a failed ``shutil.rmtree`` is retried every
        later pass and an orphaned generation is still discovered after the Stack
        transitions to another runtime form, is stopped/drained, or disappears
        from desired state entirely — none of which leaves a record to trigger the
        per-transition cleanup.

        What is retained follows two rules, both computed from EVERY current
        record (``_applied_compose`` reflects all of this pass's mutations by the
        time this runs at the end of ``_reconcile``):

        1. A record's ACTUAL file paths are the references — never a generation
           inferred from its fingerprint. Each path is normalized under
           ``stacks/`` and the top-level entry containing it is retained, which
           protects generation-addressed layouts AND a legacy pre-generation
           record's name-addressed paths (``stacks/<name>/compose/base.yml``) —
           paths a valid AppliedCompose record still needs for teardown. A path
           that cannot be safely mapped under ``stacks/`` conservatively retains
           EVERYTHING under that record's Stack name.

        2. A Stack whose record is PENDING retains ALL entries under its name.
           The pending write-ahead REPLACED the predecessor's applied record
           before compose up, so no surviving record references the predecessor
           generation — yet the predecessor may still be the running project if
           the up failed or the daemon crashed first. Predecessor generations are
           reclaimed only once the successor is confirmed applied (or the
           project is composed down and the record cleared).

        A name with no record keeps nothing: every generation dir under it is
        unreferenced and reclaimed. The sweep is bounded (one ``iterdir`` per
        Stack dir) and deterministic (sorted), stays per-Stack (a retained Stack
        never blocks another's cleanup), and each deletion is isolated via
        ``_gc_compose_generations`` — a failure logs, emits the capped error
        event, and stays retryable rather than blocking runtime convergence."""
        stacks_root = self._config.state_dir / "stacks"
        if not stacks_root.is_dir():
            return
        root = stacks_root.resolve()
        retain_all: set[str] = set()  # Stack dir names where nothing may be deleted
        keep_by_name: dict[str, set[str]] = {}  # dir name -> referenced top-level entries
        for stack_name, record in self._applied_compose.items():
            if record.state != COMPOSE_APPLIED:
                retain_all.add(stack_name)  # pending: predecessors are unrecorded
            for file in record.files:
                try:
                    parts = Path(file).resolve().relative_to(root).parts
                except (OSError, ValueError):
                    retain_all.add(stack_name)  # unmappable: preserve conservatively
                    continue
                if len(parts) >= 2:
                    keep_by_name.setdefault(parts[0], set()).add(parts[1])
                elif parts:
                    retain_all.add(parts[0])  # a bare stacks/<x> reference: keep x whole
                else:
                    retain_all.add(stack_name)
        for stack_dir in sorted(stacks_root.iterdir()):
            if not stack_dir.is_dir() or stack_dir.name in retain_all:
                continue
            self._gc_compose_generations(stack_dir.name, keep_by_name.get(stack_dir.name, set()))

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
                self._docker.compose(f"ozolith-{stack.name}", self._compose_paths(stack), "down")
                # No record references the just-materialized generation; the
                # per-pass sweep reclaims it (and retries on failure).
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
        Handles a single-image->compose (and process->compose) transition too: the
        losing form is retained until the compose prerequisites (secrets, valid
        materialized files) are ready AND the write-ahead recovery record has been
        durably persisted, then retired BEFORE compose up so the two runtime forms
        never coexist. Persisting the pending record ahead of the teardown means a
        failed write-ahead leaves the losing form running and touches nothing
        (ADR-0044 amendment).

        The candidate compose documents are materialized into a NEW
        generation-addressed directory (``_compose_paths``), distinct from any
        generation a predecessor's applied/pending record still references, so a
        failed write-ahead persist mutates none of the predecessor's recovery
        inputs. Obsolete generations are reclaimed only AFTER the transition is
        confirmed applied (ADR-0044 amendment: immutable/versioned
        materialization)."""
        fingerprint = self._compose_fingerprint(stack)
        single_present = bool(self._docker.stack_containers(stack.name))
        applied = self._applied_compose.get(stack.name)
        if (
            not child_lingers
            and not single_present
            and applied is not None
            and applied.state == COMPOSE_APPLIED
            and applied.fingerprint == fingerprint
        ):
            # Only a CONFIRMED (applied) record may take the healthy early return.
            # A pending record proves its spec was write-ahead-persisted, NOT that
            # compose up ever ran it: the deterministic project name
            # (ozolith-<name>) is identical across generations, so a compose_ps
            # running row cannot distinguish "B is up" from "the predecessor A is
            # still running and B never reached Docker." A crash between persisting
            # B's pending record and invoking compose up leaves exactly that
            # ambiguity — A running under a pending B record — and treating the
            # fingerprint+liveness match as converged would strand A while claiming
            # B. So a pending record ALWAYS falls through to compose up with its
            # retained immutable files (below), which converges the deterministic
            # project from A to B in place (never a down to resolve the ambiguity);
            # the post-up confirmation then promotes it to applied (ADR-0044
            # amendment: pending never claims specification convergence; compose_ps
            # stays the liveness authority, distinct from specification identity).
            #
            # Even for a confirmed record the fingerprint proves only which spec
            # was applied, not that the project still runs (it can die, or the
            # daemon can restart while it keeps running). Only skip as
            # already-converged when it is verifiably running; otherwise fall
            # through and compose up again with the retained/current files. No
            # churn when healthy.
            if self._compose_running(stack.name):
                return  # confirmed-applied, at this exact spec, and running
            self._log(
                f"stack {stack.name}: applied compose project not running; composing up again"
            )
        if not self._pull_stack_secrets(stack):
            return
        # Materialize + validate the candidate into its OWN generation dir before
        # any teardown — never overwriting the bytes the predecessor's record
        # still references (ADR-0044 amendment: immutable/versioned materialization).
        files = self._compose_paths(stack)
        # Write-ahead the recovery record BEFORE retiring the losing runtime form
        # AND before compose up (ADR-0044 amendment: crash-consistent startup). The
        # pending record (name + fingerprint + retained non-empty file paths) must
        # be durable BEFORE the predecessor teardown so no ordering — a crash or a
        # persist failure — can leave an old form retired with no record able to
        # bring the compose project up or tear it back down. If this persist FAILS,
        # nothing is retired: the process child and its owned Run containers, and
        # any single-image container, keep running; the previous applied record is
        # preserved (rolled back in _write_ahead_compose); and the raise reaches
        # _reconcile, which retries next pass. A compose spec change over a running
        # old project likewise keeps that project running — compose up is gated on
        # this persist, so a failed write-ahead never touches the live project.
        paths = [str(f) for f in files]
        self._write_ahead_compose(stack.name, AppliedCompose(fingerprint, paths, COMPOSE_PENDING))
        # Only after the write-ahead persists do we retire the predecessor form(s).
        # A teardown failure here raises BEFORE compose up, so the two runtime forms
        # never coexist and the durable pending record is retained for the retry
        # that completes the transition on a later pass (ADR-0044 amendment).
        if child_lingers:  # process->compose: retire the process form
            self._stop_process_child(stack.name)
        if single_present:  # single-image->compose: retire the single-image form first
            self._log(
                f"stack {stack.name}: single-image->compose;"
                " removing the single-image container before compose up"
            )
            self._remove_single_image(stack.name)
        self._docker.compose(f"ozolith-{stack.name}", files, "up")
        # Confirm APPLIED after a successful up — best-effort: a failure to persist
        # the confirmation must NOT drop the durable write-ahead record, so it is
        # isolated. compose_ps stays the liveness authority; the pending/applied
        # distinction is informational and a lingering pending record is harmless.
        self._applied_compose[stack.name].state = COMPOSE_APPLIED
        self._isolated(
            f"stack {stack.name}: confirm applied compose", self._persist_applied_compose
        )
        # The transition is complete and this record is now the only one that
        # matters for this name. The per-pass ``_sweep_compose_generations`` (end
        # of ``_reconcile``) reclaims every OTHER materialized generation — a
        # predecessor compose spec's dir, or an unreferenced candidate left by an
        # earlier failed write-ahead — keeping only the paths the current records
        # actually reference, and RETRIES any deletion that fails on later passes
        # even once this Stack early-returns healthy or leaves the compose form
        # entirely (ADR-0044 amendment: cleanup has a real retry path, not a
        # one-shot tied to reaching this line). Had this up FAILED, the record
        # would still be pending and the sweep would retain the predecessor
        # generation too — it is reclaimed only after this point.

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
                f"stack {stack.name}: running container has no applied-spec label; reconciling once"
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
