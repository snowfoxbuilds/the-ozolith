"""Actor configuration from the environment.

Every variable honors the VAR_FILE convention (NODE-SUBSTRATE.md): if
``<NAME>_FILE`` is set, the value is read from that file. This is how secrets
arrive when the Control Node materializes them to /run/secrets in M3+; with a
plain ``.env`` (M2 deploys) the direct variables work unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """The environment does not describe a runnable actor."""


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


def _required(environ: Mapping[str, str], name: str) -> str:
    value = env_value(environ, name)
    if not value:
        raise ConfigError(f"set {name} (or {name}_FILE)")
    return value


@dataclass(frozen=True)
class ActorConfig:
    """Shared configuration for the Worker and Reviewer actors."""

    repo: str  # target repo as owner/name
    token: str  # this actor's machine-user PAT
    api_url: str
    clone_url: str  # git remote for the target repo
    model: str  # model the adapter runs
    adapter: str  # adapter name (M2: "claude")
    poll_seconds: float
    recycle_runs: int  # Worker: exit after N Runs; Reviewer ignores
    control_node_url: str | None  # optional claim pre-filter; None = skipped
    worker_id: str
    workdir: Path  # where disposable Run checkouts live


def load_config(environ: Mapping[str, str] | None = None, *, role: str) -> ActorConfig:
    """Build the actor config for ``role`` ("worker" or "reviewer")."""
    environ = os.environ if environ is None else environ
    repo = _required(environ, "THEOZOLITH_REPO")
    if "/" not in repo:
        raise ConfigError(f"THEOZOLITH_REPO must be owner/name, got {repo!r}")
    token = _required(environ, "GITHUB_TOKEN")
    api_url = env_value(environ, "THEOZOLITH_API_URL", "https://api.github.com") or ""

    default_model = "claude-sonnet-5" if role == "worker" else "claude-fable-5"
    model = env_value(environ, "THEOZOLITH_MODEL", default_model) or default_model

    clone_url = env_value(environ, "THEOZOLITH_CLONE_URL") or (
        f"https://x-access-token:{token}@github.com/{repo}.git"
    )

    poll_seconds = float(env_value(environ, "THEOZOLITH_POLL_SECONDS", "60") or "60")
    recycle_runs = int(env_value(environ, "THEOZOLITH_RECYCLE_RUNS", "10") or "10")

    return ActorConfig(
        repo=repo,
        token=token,
        api_url=api_url,
        clone_url=clone_url,
        model=model,
        adapter=env_value(environ, "THEOZOLITH_ADAPTER", "claude") or "claude",
        poll_seconds=poll_seconds,
        recycle_runs=recycle_runs,
        control_node_url=env_value(environ, "CONTROL_NODE_URL"),
        worker_id=env_value(environ, "THEOZOLITH_WORKER_ID", os.uname().nodename) or "worker",
        workdir=Path(env_value(environ, "THEOZOLITH_WORKDIR", "/tmp/theozolith-runs") or ""),
    )
