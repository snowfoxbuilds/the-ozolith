"""Control Node configuration from the environment.

Every variable honors the VAR_FILE convention (NODE-SUBSTRATE.md) via the
worker component's ``env_value``. Two static bearer tokens gate the API
(ADR-0015): the node token (Node Daemons and drivers) and the admin token
(the CLI and the dashboard). GitHub credentials are required for the
pipeline (ADR-0017: the Control Node writes every claim) — without them
claim dispatch answers 503, the janitor is disabled, and the Control Node
is a pure substrate observer, which is still a legal non-pipeline
deployment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from theozolith_worker.config import ConfigError, env_value

DEFAULT_CONFIG_REPO = "~/.theozolith/configs"
DEFAULT_DATA_DIR = "~/.theozolith/control"


def _float(environ: Mapping[str, str], name: str, default: str) -> float:
    raw = env_value(environ, name, default) or default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class ControlSettings:
    data_dir: Path  # SQLite DB, master key, TLS material
    config_repo: Path  # the Config Repo working home (ADR-0006)
    node_token: str  # bearer for Node Daemons and drivers
    admin_token: str  # the one admin credential: CLI, dashboard, terminal
    repo: str | None  # target repo (owner/name) for dispatch + janitor
    github_token: str | None  # the control PAT (claim writes, janitor)
    api_url: str
    zombie_grace_seconds: float
    janitor_sweep_seconds: float
    # ADR-0017: a grant with no claimed event inside this window is released.
    activation_window_seconds: float
    # ADR-0016 cache-not-archive: progress-telemetry byte budget (oldest-first
    # eviction; terminal events are never evicted).
    tail_budget_bytes: int
    # True only when the server terminates TLS itself or an operator
    # explicitly opted into insecure dev mode: gates the secret endpoints.
    secrets_channel_ok: bool = False
    # The one public origin browsers reach this deployment by (ADR-0019),
    # e.g. "https://<slug>.theozolith.internal" — empty only in dev;
    # production serve refuses to start without it. Parsed, it defines the
    # exact Host and Origin every cookie-authenticated state change must
    # carry. Independent of the Uvicorn bind host/port: changing serve
    # --port never changes the accepted Host or Origin.
    public_origin: str = ""
    # Concurrent web-terminal sessions; excess connects are refused before
    # any attach process launches (ADR-0019).
    terminal_session_cap: int = 8
    # True when serve terminates TLS: decides the session cookie's name and
    # Secure flag (__Host- + Secure over TLS; a plain dev cookie otherwise,
    # since browsers drop a Secure/__Host- cookie set over http).
    serve_tls: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "control.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "master.key"

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / "tls"

    @property
    def terminal_audit_path(self) -> Path:
        return self.data_dir / "terminal-audit.log"

    @property
    def artifacts_dir(self) -> Path:
        """Built distributions the developer path serves for node pulls
        (ADR-0015 amendment 2026-07-22): one directory per pinned version."""
        return self.data_dir / "artifacts"

    @property
    def coordination_jobs_enabled(self) -> bool:
        """Dispatch + janitor need a GitHub identity and a target repo."""
        return bool(self.repo and self.github_token)


def load_settings(environ: Mapping[str, str] | None = None) -> ControlSettings:
    from theozolith_control.origin import read_public_origin

    environ = os.environ if environ is None else environ

    node_token = env_value(environ, "THEOZOLITH_NODE_TOKEN")
    admin_token = env_value(environ, "THEOZOLITH_ADMIN_TOKEN")
    if not node_token or not admin_token:
        raise ConfigError(
            "set THEOZOLITH_NODE_TOKEN and THEOZOLITH_ADMIN_TOKEN (or their _FILE forms)"
        )

    repo = env_value(environ, "THEOZOLITH_REPO")
    if repo and "/" not in repo:
        raise ConfigError(f"THEOZOLITH_REPO must be owner/name, got {repo!r}")

    data_dir = Path(
        env_value(environ, "THEOZOLITH_CONTROL_DATA", DEFAULT_DATA_DIR) or DEFAULT_DATA_DIR
    ).expanduser()
    return ControlSettings(
        data_dir=data_dir,
        config_repo=Path(
            env_value(environ, "THEOZOLITH_CONFIG_REPO", DEFAULT_CONFIG_REPO) or DEFAULT_CONFIG_REPO
        ).expanduser(),
        node_token=node_token,
        admin_token=admin_token,
        repo=repo,
        github_token=env_value(environ, "CONTROL_GITHUB_TOKEN")
        or env_value(environ, "GITHUB_TOKEN"),
        api_url=env_value(environ, "THEOZOLITH_API_URL", "https://api.github.com") or "",
        zombie_grace_seconds=_float(environ, "THEOZOLITH_ZOMBIE_GRACE_SECONDS", "600"),
        janitor_sweep_seconds=_float(environ, "THEOZOLITH_JANITOR_SWEEP_SECONDS", "60"),
        activation_window_seconds=_float(environ, "THEOZOLITH_ACTIVATION_WINDOW_SECONDS", "60"),
        tail_budget_bytes=int(_float(environ, "THEOZOLITH_TAIL_BUDGET_BYTES", str(10 * 1024**3))),
        # The env override is an expert escape hatch (it wins over the
        # artifact); the sanctioned source is the origin-init file in the
        # data dir. Format-checked at serve/app startup — but entropy cannot
        # be inferred from text, so an operator overriding is responsible
        # for a CSPRNG-generated slug (origin.py).
        public_origin=env_value(environ, "THEOZOLITH_PUBLIC_ORIGIN")
        or read_public_origin(data_dir),
        terminal_session_cap=int(_float(environ, "THEOZOLITH_TERMINAL_SESSION_CAP", "8")),
    )
