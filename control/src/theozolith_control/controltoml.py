"""control.toml: the durable home for tier-2 tunables (ADR-0023/0024).

The file lives in the Config Repo working home (``~/.theozolith/configs``)
beside ``stacks/``, ``images/``, and ``product.toml``. Every setting ships
a default, so the file is optional — the ADR-0004 deletion test boots with
no Config Repo at all — and ``THEOZOLITH_*`` environment variables remain
as validated expert overrides only.

Two tables, two write paths:

- ``[control]`` holds ``public_origin``, the one read-only field: written by
  init/origin-init, rendered read-only in the dashboard settings form, and
  refused by ``set_value`` — the dashboard cannot repoint the deployment.
- ``[settings]`` holds the tier-2 tunables. The dashboard settings form
  edits them through ``set_value``: a fixed-schema, single-file write that
  regenerates control.toml from the known key set and commits it (the
  ``product.toml`` pin-bump precedent — same author identity), never a
  free-form editor.

Unknown keys fail closed: a typo in a hand-edited file must surface as an
error, not silently configure nothing.
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTROL_TOML = "control.toml"

# The settings-form commit convention (the product.toml precedent).
COMMIT_AUTHOR_NAME = "theozolith"
COMMIT_AUTHOR_EMAIL = "theozolith@invalid"

DEFAULT_BOOTSTRAP_PORT = 6965
DEFAULT_INSTALLER_URL = (
    "https://github.com/snowfoxbuilds/the-ozolith/releases/latest/download/install-nodedaemon.sh"
)


class ControlTomlError(RuntimeError):
    """control.toml does not parse or violates the fixed schema."""


@dataclass(frozen=True)
class Setting:
    """One tier-2 tunable: its control.toml key, shipped default, and the
    THEOZOLITH_* environment variable that overrides it (expert hatch)."""

    key: str
    kind: type  # float | int | str
    default: Any
    env: str
    label: str  # one line for the dashboard settings form

    def coerce(self, raw: Any, *, source: str) -> Any:
        if self.kind is str:
            if not isinstance(raw, str) or not raw:
                raise ControlTomlError(f"{source}: {self.key!r} must be a non-empty string")
            return raw
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ControlTomlError(f"{source}: {self.key!r} must be a number, got {raw!r}")
        try:
            value = self.kind(raw)
        except (TypeError, ValueError) as exc:
            raise ControlTomlError(f"{source}: {self.key!r} must be a number, got {raw!r}") from exc
        if value <= 0:
            raise ControlTomlError(f"{source}: {self.key!r} must be positive, got {raw!r}")
        return value


SETTINGS: tuple[Setting, ...] = (
    Setting(
        "heartbeat_seconds",
        float,
        60.0,
        "THEOZOLITH_HEARTBEAT_SECONDS",
        "Node heartbeat interval (rides desired state to every node).",
    ),
    Setting(
        "zombie_grace_seconds",
        float,
        600.0,
        "THEOZOLITH_ZOMBIE_GRACE_SECONDS",
        "Silence on a live claim before the janitor escalates (ADR-0016).",
    ),
    Setting(
        "janitor_sweep_seconds",
        float,
        60.0,
        "THEOZOLITH_JANITOR_SWEEP_SECONDS",
        "Janitor sweep cadence.",
    ),
    Setting(
        "activation_window_seconds",
        float,
        60.0,
        "THEOZOLITH_ACTIVATION_WINDOW_SECONDS",
        "Grant with no claimed event inside this window is released (ADR-0017).",
    ),
    Setting(
        "tail_budget_bytes",
        int,
        10 * 1024**3,
        "THEOZOLITH_TAIL_BUDGET_BYTES",
        "Event-cache byte budget: progress/error telemetry evicted oldest-first.",
    ),
    Setting(
        "terminal_session_cap",
        int,
        8,
        "THEOZOLITH_TERMINAL_SESSION_CAP",
        "Concurrent web-terminal sessions (ADR-0019).",
    ),
    Setting(
        "session_days",
        float,
        30.0,
        "THEOZOLITH_SESSION_DAYS",
        "Absolute browser-session expiry, days (ADR-0023).",
    ),
    Setting(
        "offpin_beats",
        int,
        3,
        "THEOZOLITH_OFFPIN_BEATS",
        "Consecutive off-pin heartbeats before the restart escalation.",
    ),
    Setting(
        "stop_grace_seconds",
        float,
        30.0,
        "THEOZOLITH_STOP_GRACE_SECONDS",
        "SIGTERM -> SIGKILL window for kill-the-tree (rides desired state).",
    ),
    Setting(
        "bootstrap_port",
        int,
        DEFAULT_BOOTSTRAP_PORT,
        "THEOZOLITH_BOOTSTRAP_PORT",
        "Plaintext bootstrap listener port (CA cert, origin, control URL).",
    ),
    Setting(
        "installer_url",
        str,
        DEFAULT_INSTALLER_URL,
        "THEOZOLITH_INSTALLER_URL",
        "Where the printed join command fetches the node installer (HTTPS/scp only).",
    ),
)

_BY_KEY = {setting.key: setting for setting in SETTINGS}


def defaults() -> dict[str, Any]:
    return {setting.key: setting.default for setting in SETTINGS}


def control_toml_path(config_repo: Path) -> Path:
    return config_repo / CONTROL_TOML


def _load(config_repo: Path) -> dict[str, Any]:
    path = control_toml_path(config_repo)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ControlTomlError(f"{CONTROL_TOML}: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    return data


def _read_control_field(config_repo: Path, key: str) -> str:
    control = _load(config_repo).get("control", {})
    value = control.get(key, "") if isinstance(control, dict) else ""
    return value if isinstance(value, str) else ""


def read_public_origin(config_repo: Path) -> str:
    """The persisted public origin — a read-only [control] field written by
    init (ADR-0024 moved it here from the flat public-origin file)."""
    return _read_control_field(config_repo, "public_origin")


def read_control_ip(config_repo: Path) -> str:
    """The persisted control IP (ADR-0031): the LAN address nodes dial —
    the node channel is IP-only (ADR-0023 as amended 2026-07-28), so this
    read-only field feeds every join-string mint surface, the bootstrap
    listener's /control-url, and the certificate SAN. Confirmed once at
    init; it survives backup/recovery with the Config Repo."""
    return _read_control_field(config_repo, "control_ip")


def read_values(config_repo: Path) -> dict[str, Any]:
    """Tier-2 values: shipped defaults overlaid with the [settings] table.
    Unknown keys and malformed values fail closed with ControlTomlError."""
    values = defaults()
    table = _load(config_repo).get("settings", {})
    if not isinstance(table, dict):
        raise ControlTomlError(f"{CONTROL_TOML}: [settings] must be a table")
    unknown = sorted(set(table) - set(_BY_KEY))
    if unknown:
        raise ControlTomlError(
            f"{CONTROL_TOML}: unknown settings key(s): {', '.join(unknown)}"
            f" (known: {', '.join(sorted(_BY_KEY))})"
        )
    for key, raw in table.items():
        values[key] = _BY_KEY[key].coerce(raw, source=CONTROL_TOML)
    return values


def _toml_literal(value: Any) -> str:
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def render(origin: str, control_ip: str, overrides: dict[str, Any]) -> str:
    """The whole file, regenerated from the fixed schema: the read-only
    [control] table, then only the [settings] keys that differ from their
    shipped defaults (every setting has one — the file stays minimal and
    remains optional)."""
    lines = [
        "# TheOzolith control.toml (ADR-0023): tier-2 tunables, edited via the",
        "# dashboard settings form or by hand + commit. Every key has a shipped",
        "# default; delete a line to return to it. THEOZOLITH_* environment",
        "# variables override these (expert escape hatch).",
        "",
        "[control]",
        "# Read-only: written by 'theozolith-control init' (origin-init /",
        "# recover --ip); the settings form refuses to change them. The origin",
        "# is what browsers dial; the IP is what nodes dial (ADR-0023/0031).",
        f"public_origin = {_toml_literal(origin)}",
    ]
    if control_ip:
        lines.append(f"control_ip = {_toml_literal(control_ip)}")
    set_keys = [
        setting
        for setting in SETTINGS
        if overrides.get(setting.key, setting.default) != setting.default
    ]
    if set_keys:
        lines += ["", "[settings]"]
        for setting in set_keys:
            lines.append(f"{setting.key} = {_toml_literal(overrides[setting.key])}")
    return "\n".join(lines) + "\n"


def _commit(config_repo: Path, message: str, *, runner=subprocess.run, log=print) -> None:
    if not (config_repo / ".git").exists():
        return  # folder mode: the file itself is the record
    status = runner(
        ["git", "status", "--porcelain", CONTROL_TOML],
        cwd=str(config_repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode == 0 and not (status.stdout or "").strip():
        return
    for args in (
        ["git", "add", CONTROL_TOML],
        [
            "git",
            "-c",
            f"user.name={COMMIT_AUTHOR_NAME}",
            "-c",
            f"user.email={COMMIT_AUTHOR_EMAIL}",
            "commit",
            "-m",
            message,
        ],
    ):
        proc = runner(args, cwd=str(config_repo), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ControlTomlError(
                f"could not commit {CONTROL_TOML}: {' '.join(args[:2])} failed:"
                f" {(proc.stderr or proc.stdout or '').strip()[:300]}"
            )
    log(f"committed {CONTROL_TOML}: {message}")


def _write(config_repo: Path, origin: str, control_ip: str, overrides: dict[str, Any]) -> Path:
    config_repo.mkdir(parents=True, exist_ok=True)
    path = control_toml_path(config_repo)
    path.write_text(render(origin, control_ip, overrides), encoding="utf-8")
    return path


def write_public_origin(
    config_repo: Path, origin: str, *, runner=subprocess.run, log=print
) -> Path:
    """Persist the public origin (init/origin-init only), keeping the
    persisted control IP and every committed setting; committed with the
    fixed author identity."""
    from theozolith_control.origin import parse_public_origin

    canonical = parse_public_origin(origin).origin
    path = _write(config_repo, canonical, read_control_ip(config_repo), read_values(config_repo))
    _commit(config_repo, f"theozolith: public origin {canonical}", runner=runner, log=log)
    return path


def write_control_ip(config_repo: Path, ip: str, *, runner=subprocess.run, log=print) -> Path:
    """Persist the control IP (init / recover --ip only; ADR-0031), keeping
    the origin and every committed setting. Validated as an IP literal —
    the node channel never carries a hostname."""
    import ipaddress

    try:
        canonical = str(ipaddress.ip_address(ip))
    except ValueError as exc:
        raise ControlTomlError(f"control IP {ip!r} is not an IP address: {exc}") from exc
    path = _write(config_repo, read_public_origin(config_repo), canonical, read_values(config_repo))
    _commit(config_repo, f"theozolith: control ip {canonical}", runner=runner, log=log)
    return path


def set_value(config_repo: Path, key: str, raw: str, *, runner=subprocess.run, log=print) -> Any:
    """The settings-form write path: validate one known tier-2 key, rewrite
    control.toml from the fixed schema (touching nothing else), and commit
    ``theozolith: settings: <key> = <value>``. The public origin is not a
    settings key and is refused here by construction."""
    setting = _BY_KEY.get(key)
    if setting is None:
        raise ControlTomlError(
            f"unknown or read-only settings key {key!r} (known: {', '.join(sorted(_BY_KEY))})"
        )
    value = setting.coerce(raw, source="settings form")
    values = read_values(config_repo)
    values[key] = value
    _write(config_repo, read_public_origin(config_repo), read_control_ip(config_repo), values)
    # The message carries the exact TOML literal that was written.
    _commit(
        config_repo,
        f"theozolith: settings: {key} = {_toml_literal(value)}",
        runner=runner,
        log=log,
    )
    return value
