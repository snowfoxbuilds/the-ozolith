"""control.toml (ADR-0023/0029): defaults, env overrides, the fixed-schema
settings write path, and the read-only control address (ADR-0031/0034)."""

from __future__ import annotations

import subprocess

import pytest
from theozolith_control import controltoml
from theozolith_control.settings import load_settings
from theozolith_worker.config import ConfigError

CONTROL_IP = "192.0.2.30"


def _git(config_repo, *args) -> str:
    proc = subprocess.run(
        ["git", "-C", str(config_repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


def _git_repo(tmp_path):
    repo = tmp_path / "configs"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    return repo


def test_every_setting_ships_a_default_and_the_file_is_optional(tmp_path):
    """Deletion test (acceptance 3, settings half): with no Config Repo and
    an empty environment, everything loads at its shipped default — no
    tokens, no .env, no required variables."""
    settings = load_settings({"THEOZOLITH_DATA_DIR": str(tmp_path / "home")})
    assert settings.heartbeat_seconds == 60.0
    assert settings.zombie_grace_seconds == 600.0
    assert settings.tail_budget_bytes == 10 * 1024**3
    assert settings.terminal_session_cap == 8
    assert settings.session_days == 30.0
    assert settings.bootstrap_port == 6965
    assert settings.config_repo == tmp_path / "home" / "configs"
    assert settings.store_db_path == tmp_path / "home" / "secrets" / "store.db"
    assert settings.cache_db_path == tmp_path / "home" / "cache" / "cache.db"
    assert settings.terminal_audit_path == tmp_path / "home" / "logs" / "terminal-audit.log"


def test_control_toml_values_load_and_env_overrides_win(tmp_path):
    repo = tmp_path / "configs"
    repo.mkdir()
    (repo / "control.toml").write_text(
        "[settings]\nheartbeat_seconds = 30\nterminal_session_cap = 4\n"
    )
    environ = {
        "THEOZOLITH_DATA_DIR": str(tmp_path / "home"),
        "THEOZOLITH_CONFIG_REPO": str(repo),
    }
    settings = load_settings(environ)
    assert settings.heartbeat_seconds == 30.0
    assert settings.terminal_session_cap == 4
    # The expert escape hatch: a validated env override beats the file.
    settings = load_settings({**environ, "THEOZOLITH_TERMINAL_SESSION_CAP": "2"})
    assert settings.terminal_session_cap == 2
    with pytest.raises(ConfigError, match="must be a number"):
        load_settings({**environ, "THEOZOLITH_HEARTBEAT_SECONDS": "fast"})


def test_unknown_and_malformed_keys_fail_closed(tmp_path):
    repo = tmp_path / "configs"
    repo.mkdir()
    (repo / "control.toml").write_text("[settings]\nheartbaet_seconds = 30\n")
    with pytest.raises(controltoml.ControlTomlError, match="heartbaet_seconds"):
        controltoml.read_values(repo)
    (repo / "control.toml").write_text("[settings]\nheartbeat_seconds = -1\n")
    with pytest.raises(controltoml.ControlTomlError, match="positive"):
        controltoml.read_values(repo)


def test_set_value_commits_only_control_toml_with_the_convention(tmp_path):
    """Acceptance 7 (storage half): one fixed-schema commit per save,
    touching only control.toml, fixed author identity, and the value takes
    effect on the next settings load."""
    repo = _git_repo(tmp_path)
    controltoml.write_control_address(repo, CONTROL_IP)
    controltoml.set_value(repo, "heartbeat_seconds", "30")

    assert controltoml.read_values(repo)["heartbeat_seconds"] == 30.0
    settings = load_settings(
        {"THEOZOLITH_DATA_DIR": str(tmp_path / "home"), "THEOZOLITH_CONFIG_REPO": str(repo)}
    )
    assert settings.heartbeat_seconds == 30.0

    subject = _git(repo, "log", "-1", "--format=%s")
    assert subject.strip() == "theozolith: settings: heartbeat_seconds = 30"
    author = _git(repo, "log", "-1", "--format=%an <%ae>").strip()
    assert author == "theozolith <theozolith@invalid>"
    touched = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert touched == ["control.toml"]
    # A no-op save produces no commit.
    before = _git(repo, "rev-parse", "HEAD")
    controltoml.set_value(repo, "heartbeat_seconds", "30")
    assert _git(repo, "rev-parse", "HEAD") == before


def test_set_value_refuses_unknown_keys_and_the_control_address(tmp_path):
    repo = _git_repo(tmp_path)
    controltoml.write_control_address(repo, CONTROL_IP)
    for key in ("control_ip", "control_port", "public_origin", "made_up"):
        with pytest.raises(controltoml.ControlTomlError, match="unknown or read-only"):
            controltoml.set_value(repo, key, "10.6.6.6")
    assert controltoml.read_control_ip(repo) == CONTROL_IP


def test_address_writes_preserve_committed_settings(tmp_path):
    repo = _git_repo(tmp_path)
    controltoml.write_control_address(repo, CONTROL_IP, port=9443)
    controltoml.set_value(repo, "session_days", "7")
    controltoml.write_control_address(repo, "192.0.2.31")
    assert controltoml.read_control_ip(repo) == "192.0.2.31"
    assert controltoml.read_control_port(repo) == 9443  # recover keeps the port
    assert controltoml.read_values(repo)["session_days"] == 7.0
