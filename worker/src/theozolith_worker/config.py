"""Driver configuration from the environment.

Every variable honors the VAR_FILE convention (NODE-SUBSTRATE.md): if
``<NAME>_FILE`` is set, the value is read from that file. This is how secrets
arrive when the Control Node materializes them to /run/secrets in M3+; with a
plain exported variables (daemon-less dev) they work unchanged.

The clone URL defaults to the tokenless HTTPS remote: the PAT never lands in
a checkout's ``.git/config`` — the driver authenticates via environment-level
git config at clone/fetch/push time only (see ``gitops.auth_env``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """The environment does not describe a runnable driver."""


def env_value(environ: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    """Read ``name`` from the environment, honoring ``<name>_FILE``."""
    file_path = environ.get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"could not read {name}_FILE={file_path!r}: {exc}") from exc
    value = environ.get(name)
    if value is not None and value != "":
        return value
    return default


def _first(environ: Mapping[str, str], *names: str, default: str | None = None) -> str | None:
    """The first of ``names`` present in the environment (VAR_FILE honored).

    Lookup chains let one shared environment configure both drivers: role-prefixed
    variables (WORKER_GITHUB_TOKEN, REVIEWER_MODEL, …) win over the generic
    THEOZOLITH_* names.
    """
    for name in names:
        value = env_value(environ, name)
        if value:
            return value
    return default


def _required(environ: Mapping[str, str], *names: str) -> str:
    value = _first(environ, *names)
    if not value:
        raise ConfigError(f"set {names[-1]} (or {names[-1]}_FILE)")
    return value


def _float(environ: Mapping[str, str], *names: str, default: str) -> float:
    raw = _first(environ, *names, default=default) or default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{names[0]} must be a number, got {raw!r}") from exc


def _volumes(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse ``name:/path,name:/path`` into named-volume mounts."""
    pairs = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, path = item.partition(":")
        if not sep or not name or not path.startswith("/"):
            raise ConfigError(f"THEOZOLITH_CACHE_VOLUMES entry {item!r} is not name:/path")
        pairs.append((name, path))
    return tuple(pairs)


@dataclass(frozen=True)
class DriverConfig:
    """Shared configuration for the Worker and Reviewer drivers."""

    role: str  # "worker" | "reviewer"
    repo: str  # target repo as owner/name
    token: str  # this driver's machine-user PAT (never enters containers)
    api_url: str
    clone_url: str  # tokenless git remote for the target repo
    model: str  # model the run-container agent runs
    adapter: str  # harness adapter name (M2: "claude")
    run_image: str  # the run-container image the driver launches
    stack: str  # theozolith.owner label on containers this driver creates
    poll_seconds: float
    control_node_url: str  # claim dispatch + events (required, ADR-0017)
    control_token: str  # node bearer token for the Control Node channel
    control_ca: str | None  # CA bundle pinning a self-signed Control Node
    progress_seconds: float  # run-progress telemetry cadence (0 disables)
    node_name: str  # the physical node, for events (M3 heartbeat identity)
    worker_id: str
    jobs_dir: Path  # where per-Run job directories live
    agent_timeout_seconds: float
    cache_volumes: tuple[tuple[str, str], ...]  # warm caches as named volumes
    agent_env: dict[str, str]  # env the run container gets (model API key)
    container_user: str | None  # uid:gid for run containers; None = image user


DEFAULT_CACHE_VOLUMES = "theozolith-cache:/home/ozolith/.cache"


def load_config(environ: Mapping[str, str] | None = None, *, role: str) -> DriverConfig:
    """Build the driver config for ``role`` ("worker" or "reviewer")."""
    environ = os.environ if environ is None else environ
    prefix = role.upper()  # WORKER_* / REVIEWER_* win over the generic names
    repo = _required(environ, "THEOZOLITH_REPO")
    if "/" not in repo:
        raise ConfigError(f"THEOZOLITH_REPO must be owner/name, got {repo!r}")
    token = _required(environ, f"{prefix}_GITHUB_TOKEN", "GITHUB_TOKEN")
    api_url = env_value(environ, "THEOZOLITH_API_URL", "https://api.github.com") or ""

    default_model = "claude-sonnet-5" if role == "worker" else "claude-fable-5"
    model = _first(environ, f"{prefix}_MODEL", "THEOZOLITH_MODEL", default=default_model) or ""

    clone_url = env_value(environ, "THEOZOLITH_CLONE_URL") or f"https://github.com/{repo}.git"

    agent_env: dict[str, str] = {}
    api_key = env_value(environ, "ANTHROPIC_API_KEY")
    if api_key:
        agent_env["ANTHROPIC_API_KEY"] = api_key

    worker_id = (
        _first(environ, "THEOZOLITH_WORKER_ID", "WORKER_ID", default=os.uname().nodename) or role
    )
    # ADR-0017: claims dispatch through the Control Node — there is no
    # second claim path, so a driver without one cannot run. The daemon-less
    # dev shape is `theozolith serve` on the same box.
    control_node_url = env_value(environ, "CONTROL_NODE_URL")
    if not control_node_url:
        raise ConfigError(
            "set CONTROL_NODE_URL — drivers request work from the Control Node (ADR-0017);"
            " for local dev run 'theozolith serve' and point at it"
        )
    stack = _first(environ, f"{prefix}_STACK", "THEOZOLITH_STACK", default=role) or role
    # Per-Stack default matching the Node Daemon's injection and control's
    # uniqueness check (ADR-0019): each driver owns a distinct jobs dir so
    # queue-behind never observes another Stack's Runs. Under the daemon
    # THEOZOLITH_JOBS_DIR is always injected; this default only applies to the
    # daemon-less dev shape, where it keeps worker/reviewer separate.
    default_jobs_dir = f"/var/tmp/theozolith/jobs/{stack}"
    return DriverConfig(
        role=role,
        repo=repo,
        token=token,
        api_url=api_url,
        clone_url=clone_url,
        model=model,
        adapter=env_value(environ, "THEOZOLITH_ADAPTER", "claude") or "claude",
        run_image=env_value(environ, "THEOZOLITH_RUN_IMAGE", "theozolith-run-claude:local")
        or "theozolith-run-claude:local",
        stack=stack,
        poll_seconds=_float(environ, "THEOZOLITH_POLL_SECONDS", "POLL_SECONDS", default="60"),
        control_node_url=control_node_url,
        control_token=env_value(environ, "THEOZOLITH_NODE_TOKEN") or "",
        control_ca=env_value(environ, "THEOZOLITH_TLS_CA"),
        progress_seconds=_float(environ, "THEOZOLITH_PROGRESS_SECONDS", default="30"),
        node_name=env_value(environ, "THEOZOLITH_NODE_NAME") or os.uname().nodename,
        worker_id=worker_id,
        jobs_dir=Path(
            env_value(environ, "THEOZOLITH_JOBS_DIR", default_jobs_dir) or default_jobs_dir
        ),
        agent_timeout_seconds=_float(environ, "THEOZOLITH_AGENT_TIMEOUT_SECONDS", default="3600"),
        cache_volumes=_volumes(
            env_value(environ, "THEOZOLITH_CACHE_VOLUMES", DEFAULT_CACHE_VOLUMES)
            or DEFAULT_CACHE_VOLUMES
        ),
        agent_env=agent_env,
        container_user=env_value(environ, "THEOZOLITH_CONTAINER_USER"),
    )
