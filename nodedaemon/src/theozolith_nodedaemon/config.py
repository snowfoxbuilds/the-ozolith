"""Node Daemon configuration: provisioned state first, environment override.

Provisioning (ADR-0023) persists everything the daemon needs to speak to
its Control Node under the state dir — ``control-url``, ``node-token``
(the per-node bearer), ``node-name``, ``ca.pem`` — so a provisioned box
needs NO environment configuration (`.env` is deleted). Environment
variables remain as expert overrides and the daemon-less dev path.

Stdlib-only (ADR-0010: the daemon must install trivially on any host) and
VAR_FILE-honoring like every TheOzolith service. A daemon without a
CONTROL_NODE_URL is legal: it reconciles from its cached desired state
forever — degraded mode is a first-class mode, not an error (ADR-0006).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_DIR = "/var/lib/theozolith"
DEFAULT_RUNTIME_DIR = "/run/theozolith"
DEFAULT_HEARTBEAT_SECONDS = 60.0
DEFAULT_STOP_GRACE_SECONDS = 30.0


class DaemonConfigError(RuntimeError):
    """The environment does not describe a runnable daemon."""


def env_value(environ: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    """Read ``name`` honoring ``<name>_FILE`` (the VAR_FILE convention)."""
    file_path = environ.get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DaemonConfigError(f"could not read {name}_FILE={file_path!r}: {exc}") from exc
    value = environ.get(name)
    return value if value else default


def _float(environ: Mapping[str, str], name: str, default: str) -> float:
    raw = env_value(environ, name, default) or default
    try:
        return float(raw)
    except ValueError as exc:
        raise DaemonConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class DaemonConfig:
    node: str  # this node's name (heartbeats, Stack placement matching)
    control_url: str | None  # None = permanent degraded mode (cache only)
    node_token: str
    tls_ca: str | None  # CA bundle pinning the Control Node's self-signed CA
    state_dir: Path  # config cache + materialized compose files (disk)
    runtime_dir: Path  # secrets tmpfs (systemd RuntimeDirectory under /run)
    heartbeat_seconds: float
    stop_grace_seconds: float  # SIGTERM -> SIGKILL window (kill-the-tree)
    insecure_dev: bool  # allow secret pulls over plain http (dev/tests ONLY)
    version: str = ""

    @property
    def cache_path(self) -> Path:
        return self.state_dir / "config-cache.json"

    @property
    def drained_path(self) -> Path:
        return self.state_dir / "drained.json"

    @property
    def applied_compose_path(self) -> Path:
        # Non-secret applied compose metadata (fingerprint + materialized file
        # paths) that must survive a daemon restart so a compose project which
        # outlived the daemon is still torn down / recovered on a later
        # same-name transition (ADR-0044 amendment). Beside drained.json —
        # applied runtime state, not deletable cache.
        return self.state_dir / "applied-compose.json"

    @property
    def config_dist_dir(self) -> Path:
        # The unpacked config distributions (ADR-0042): one <hash>/ dir per
        # applied distribution (drivers/ + config-dist.json), plus a `current`
        # pointer file. Applied runtime state under the state dir — survives a
        # restart so a reconnecting node keeps reporting its applied hash.
        return self.state_dir / "config-dist"

    @property
    def config_dist_current(self) -> Path:
        return self.config_dist_dir / "current"

    @property
    def knowledge_export_dir(self) -> Path:
        # The STABLE deck-facing knowledge export (ADR-0048): one child dir
        # per compiled knowledge tree, copied from the applied distribution
        # and swapped whole on change. Flight Decks read-only bind-mount this
        # parent — its inode never moves, so a swap never recreates a
        # container; a restarted agent CLI picks up the new trees. Outside
        # config-dist/ so the distribution GC never sweeps it.
        return self.state_dir / "knowledge"

    @property
    def policy_export_dir(self) -> Path:
        # The STABLE deck-facing Agent Policy export (ADR-0055): one child
        # dir per policy tree, exported verbatim from the applied
        # distribution with the same whole-tree swap as knowledge. Flight
        # Decks read-only bind-mount this parent; the deck start script
        # links the managed drop-in dir into the selected child, so a
        # content swap lands on the next `claude` launch with no recreate.
        return self.state_dir / "policy"

    @property
    def cli_dir(self) -> Path:
        # The CLI Pin store (ADR-0055): per tool, fail-closed-installed
        # version dirs (<tool>/<version>/<published name>, the CLI archive
        # contract's closed published set) plus the per-worker-type
        # by-type/ records (.desired the moment config applies, .current only
        # post-install). Flight Decks read-only bind-mount this parent
        # (default /var/lib/theozolith/cli); the daemon creates it every pass
        # so Docker never auto-creates it root-owned with wrong perms. A
        # deletable binary CACHE, never backup state (ADR-0024): convergence
        # reconstructs it from the Pinned Build wire plus the registry.
        return self.state_dir / "cli"

    @property
    def secrets_dir(self) -> Path:
        return self.runtime_dir / "secrets"

    @property
    def docker_config_dir(self) -> Path:
        # DOCKER_CONFIG dir for build-time registry auth (ADR-0049): a tmpfs
        # sibling of secrets_dir under the runtime dir, holding one config.json
        # with the private-base pull credential. The daemon is the only reader
        # (it points a `docker build` at it via env), so 0700 dir / 0600 leaf —
        # no cross-UID 0444 like a container-mounted Stack secret.
        return self.runtime_dir / "docker"


def _provisioned(state_dir: Path, name: str) -> str | None:
    """One file persisted by `provision` (ADR-0023), or None."""
    try:
        return (state_dir / name).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def load_daemon_config(environ: Mapping[str, str] | None = None) -> DaemonConfig:
    environ = os.environ if environ is None else environ
    state_dir = Path(env_value(environ, "THEOZOLITH_STATE_DIR", DEFAULT_STATE_DIR) or "")
    # Provisioned state is the sanctioned source; the environment overrides
    # it (expert hatch / daemon-less dev). File names match provisioning.py.
    control_url = env_value(environ, "CONTROL_NODE_URL") or _provisioned(state_dir, "control-url")
    node_token = (
        env_value(environ, "THEOZOLITH_NODE_TOKEN") or _provisioned(state_dir, "node-token") or ""
    )
    if control_url and not node_token:
        raise DaemonConfigError(
            "no node token: run 'theozolith-nodedaemon provision <join-string>'"
            " (or set THEOZOLITH_NODE_TOKEN / its _FILE form for dev)"
        )
    tls_ca = env_value(environ, "THEOZOLITH_TLS_CA")
    if not tls_ca and (state_dir / "ca.pem").is_file():
        tls_ca = str(state_dir / "ca.pem")

    return DaemonConfig(
        node=env_value(environ, "THEOZOLITH_NODE_NAME")
        or _provisioned(state_dir, "node-name")
        or socket.gethostname(),
        control_url=control_url,
        node_token=node_token,
        tls_ca=tls_ca,
        state_dir=state_dir,
        runtime_dir=Path(env_value(environ, "THEOZOLITH_RUNTIME_DIR", DEFAULT_RUNTIME_DIR) or ""),
        heartbeat_seconds=_float(
            environ, "THEOZOLITH_HEARTBEAT_SECONDS", str(DEFAULT_HEARTBEAT_SECONDS)
        ),
        stop_grace_seconds=_float(
            environ, "THEOZOLITH_STOP_GRACE_SECONDS", str(DEFAULT_STOP_GRACE_SECONDS)
        ),
        insecure_dev=(env_value(environ, "THEOZOLITH_INSECURE_DEV") or "") == "1",
        version=running_product_version(),
    )


def running_product_version() -> str:
    """The RUNNING product version, from the installed distribution's real
    metadata (ADR-0015 as revised): a source build's ``+g<sha>`` local
    version survives into heartbeats, so pin convergence and the dispatch
    eligibility gate compare every node against the recorded pin."""
    import importlib.metadata

    try:
        return importlib.metadata.version("theozolith-nodedaemon")
    except importlib.metadata.PackageNotFoundError:
        from theozolith_nodedaemon import __version__

        return __version__
