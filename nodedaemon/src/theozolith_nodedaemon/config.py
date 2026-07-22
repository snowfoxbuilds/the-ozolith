"""Node Daemon configuration from the environment.

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
    def secrets_dir(self) -> Path:
        return self.runtime_dir / "secrets"


def load_daemon_config(environ: Mapping[str, str] | None = None) -> DaemonConfig:
    environ = os.environ if environ is None else environ
    control_url = env_value(environ, "CONTROL_NODE_URL")
    node_token = env_value(environ, "THEOZOLITH_NODE_TOKEN") or ""
    if control_url and not node_token:
        raise DaemonConfigError("set THEOZOLITH_NODE_TOKEN (or its _FILE form)")

    return DaemonConfig(
        node=env_value(environ, "THEOZOLITH_NODE_NAME") or socket.gethostname(),
        control_url=control_url,
        node_token=node_token,
        tls_ca=env_value(environ, "THEOZOLITH_TLS_CA"),
        state_dir=Path(env_value(environ, "THEOZOLITH_STATE_DIR", DEFAULT_STATE_DIR) or ""),
        runtime_dir=Path(env_value(environ, "THEOZOLITH_RUNTIME_DIR", DEFAULT_RUNTIME_DIR) or ""),
        heartbeat_seconds=_float(environ, "THEOZOLITH_HEARTBEAT_SECONDS", "60"),
        stop_grace_seconds=_float(environ, "THEOZOLITH_STOP_GRACE_SECONDS", "30"),
        insecure_dev=(env_value(environ, "THEOZOLITH_INSECURE_DEV") or "") == "1",
        version=running_product_version(),
    )


def running_product_version() -> str:
    """The RUNNING product version, from the installed distribution's real
    metadata (ADR-0015, 2026-07-22): a source build's ``+g<sha>[.dirty]``
    local version survives into heartbeats, so the dashboard can compare
    every node against the recorded pin."""
    import importlib.metadata

    try:
        return importlib.metadata.version("theozolith-nodedaemon")
    except importlib.metadata.PackageNotFoundError:
        from theozolith_nodedaemon import __version__

        return __version__
