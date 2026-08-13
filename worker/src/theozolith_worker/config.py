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
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from theozolith_worker import jobdir


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
    variables (IMPLEMENTER_GITHUB_TOKEN, REVIEWER_MODEL, …) win over the generic
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


# Container trees holding a Claude configuration a Run must never persist:
# the home-scoped ~/.claude, and the project-scoped .claude of both session
# workspaces inside the mounted job dir. A cache volume mounted at any of
# these — or at any ANCESTOR (/, /home, /home/ozolith, /job, ...) — would
# carry the tree across Runs on a persistent named volume.
_PROTECTED_CLAUDE_TREES = (
    "/home/ozolith/.claude",
    f"{jobdir.CONTAINER_JOB_PATH}/{jobdir.CHECKOUT_DIR}/.claude",
    f"{jobdir.CONTAINER_JOB_PATH}/{jobdir.WORK_DIR}/.claude",
)


def _normalize_container_path(path: str) -> str:
    """Lexically normalize a container-side POSIX path, mirroring what the
    engine does with a mount destination (docker runs filepath.Clean on it):
    collapse ``//`` and ``/./``, resolve ``..`` segments."""
    norm = posixpath.normpath(path)
    if norm.startswith("//"):  # POSIX normpath preserves exactly two leading /
        norm = norm[1:]
    return norm


def _volumes(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse ``name:/path,name:/path`` into named-volume mounts.

    A cache volume that would persist a Claude configuration tree is refused
    (ADR-0043): live knowledge must never mount into a Run — it breaks pin
    reproducibility and opens a prompt-injection persistence channel. The
    shared-clone symlink carve-out is Flight-Deck-only; this closes the one
    config-reachable channel that could reintroduce it on a run container.
    Destinations are normalized first (the engine mounts the cleaned path, so
    ``/home//ozolith/./.claude`` and ``/home/ozolith/.claude`` are the same
    mount), then refused when any path segment is ``.claude`` (the tree itself
    or anything below it) or when the mount is an ancestor that would sweep a
    protected ``.claude`` tree onto the volume (``/home/ozolith``, ``/home``,
    ``/job``, ``/`` — the home- and workspace-scoped trees alike).
    """
    pairs = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, path = item.partition(":")
        if not sep or not name or not path.startswith("/"):
            raise ConfigError(f"THEOZOLITH_CACHE_VOLUMES entry {item!r} is not name:/path")
        dest = _normalize_container_path(path)
        if ".claude" in dest.split("/"):
            raise ConfigError(
                f"THEOZOLITH_CACHE_VOLUMES entry {item!r} targets a .claude path —"
                " live knowledge must never mount into a Run (ADR-0043); the"
                " shared-clone symlink carve-out is Flight-Deck-only"
            )
        covered = [
            tree
            for tree in _PROTECTED_CLAUDE_TREES
            if tree == dest or tree.startswith(dest.rstrip("/") + "/")
        ]
        if covered:
            raise ConfigError(
                f"THEOZOLITH_CACHE_VOLUMES entry {item!r} mounts an ancestor of the"
                f" protected .claude tree {covered[0]!r} — a persistent volume there"
                " would carry a Claude configuration across Runs (ADR-0043); the"
                " shared-clone symlink carve-out is Flight-Deck-only"
            )
        pairs.append((name, dest))
    return tuple(pairs)


@dataclass(frozen=True)
class DriverConfig:
    """Shared configuration for every worker type (ADR-0020)."""

    role: str  # the worker type / env prefix ("implementer" | "reviewer")
    repo: str  # target repo as owner/name
    token: str  # this driver's machine-user PAT (never enters containers)
    api_url: str
    clone_url: str  # tokenless git remote for the target repo
    model: str  # model the run-container agent runs
    adapter: str  # Agent adapter name (M2: "claude")
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
    agent_env: dict[str, str]  # model credentials (API key and/or OAuth token)
    container_user: str | None  # uid:gid for run containers; None = image user


DEFAULT_CACHE_VOLUMES = "theozolith-cache:/home/ozolith/.cache"


# Model-credential env vars each Agent adapter forwards into the otherwise
# credential-free run container (ADR-0013). For the Claude Code adapter the
# workspace API key and the subscription OAuth token are ALTERNATIVES:
#   ANTHROPIC_API_KEY        a workspace API key (the rotatable spend
#                            credential ADR-0013 describes)
#   CLAUDE_CODE_OAUTH_TOKEN  a subscription token from `claude setup-token`
# Supply whichever you have (both may be set — the Claude CLI decides
# precedence). A future adapter registers its own credential names here;
# nothing outside this map is forwarded, so a non-Claude worker never
# receives a Claude token.
_ADAPTER_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
}


def load_config(
    environ: Mapping[str, str] | None = None, *, role: str, default_model: str
) -> DriverConfig:
    """Build the driver config for worker type ``role`` (its env prefix).

    ``default_model`` is the type's shipped model, used when neither
    ``<ROLE>_MODEL`` nor ``THEOZOLITH_MODEL`` is set; each worker type passes
    its ``default_model`` class attribute (ADR-0020).
    """
    environ = os.environ if environ is None else environ
    prefix = role.upper()  # IMPLEMENTER_* / REVIEWER_* win over the generic names
    repo = _required(environ, "THEOZOLITH_REPO")
    if "/" not in repo:
        raise ConfigError(f"THEOZOLITH_REPO must be owner/name, got {repo!r}")
    token = _required(environ, f"{prefix}_GITHUB_TOKEN", "GITHUB_TOKEN")
    api_url = env_value(environ, "THEOZOLITH_API_URL", "https://api.github.com") or ""

    model = _first(environ, f"{prefix}_MODEL", "THEOZOLITH_MODEL", default=default_model) or ""

    clone_url = env_value(environ, "THEOZOLITH_CLONE_URL") or f"https://github.com/{repo}.git"

    adapter = env_value(environ, "THEOZOLITH_ADAPTER", "claude") or "claude"

    # Forward whichever of the adapter's model credentials are supplied. For
    # the Claude Code adapter the API key and the subscription OAuth token are
    # alternatives — either authenticates the headless session, and both may be
    # set. Only the resolved adapter's credentials are forwarded (ADR-0013).
    agent_env: dict[str, str] = {}
    for cred in _ADAPTER_CREDENTIAL_ENV.get(adapter, ()):
        value = env_value(environ, cred)
        if value:
            agent_env[cred] = value

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
    # worker_id keys the driver registry and per-Run job dirs; the default is
    # computed AFTER stack so it can disambiguate multiple workers on one node
    # (the same-node id collision #21 left open). worker_id is base-abstraction
    # vocabulary (ADR-0020) — the name stays across the identifier sweep.
    node_default = f"{stack}-{os.uname().nodename}"
    worker_id = (
        _first(environ, "THEOZOLITH_WORKER_ID", "WORKER_ID", default=node_default) or node_default
    )
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
        adapter=adapter,
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
