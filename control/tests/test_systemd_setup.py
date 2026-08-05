"""ADR-0034 root-mediated bare-metal setup: unit rendering, the idempotent
installer shared by init and recover, its skip conditions, and service-user
executable reachability. Root, systemd, pwd, and every subprocess are faked
— the suite itself runs unprivileged in a container."""

from __future__ import annotations

import io
import os
import pwd as pwd_module
import subprocess as subprocess_module
import types

import pytest
from theozolith_control import cli, controltoml
from theozolith_control import settings as settings_module
from theozolith_control.cli import main as cli_main
from theozolith_control.crypto import SecretBox
from theozolith_control.passwords import parse_record
from theozolith_control.secretstore import SecretStore
from theozolith_control.settings import load_settings

PASSWORD = "systemd-password"
EXEC = "/usr/local/bin/theozolith"


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "home"
    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(data))
    monkeypatch.delenv("THEOZOLITH_CONFIG_REPO", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    return data


class _Recorder:
    """Stands in for subprocess.run: records argv, always succeeds."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def named(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == name]


class _RootMode:
    def __init__(self, recorder, unit_path, user_state):
        self.recorder = recorder
        self.unit_path = unit_path
        self.user_state = user_state  # {"exists": bool}


@pytest.fixture
def rootmode(tmp_path, monkeypatch) -> _RootMode:
    """Fake a root bare-metal systemd host with a fresh service user."""
    recorder = _Recorder()
    unit_path = tmp_path / "units" / cli.CONTROL_SERVICE_NAME
    unit_path.parent.mkdir()
    user_state = {"exists": False}

    def fake_getpwnam(name):
        if user_state["exists"]:
            return types.SimpleNamespace(pw_name=name)
        raise KeyError(name)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "_running_in_container", lambda: False)
    monkeypatch.setattr(cli, "_systemd_present", lambda: True)
    monkeypatch.setattr(cli, "CONTROL_UNIT_PATH", unit_path)
    monkeypatch.setattr(cli, "_service_executable", lambda: EXEC)
    # The chown gate compares against the dedicated system leaf; the fake
    # host's leaf is this test's data dir (the `home` fixture's path).
    monkeypatch.setattr(settings_module, "DEFAULT_ROOT_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(subprocess_module, "run", recorder)
    monkeypatch.setattr(pwd_module, "getpwnam", fake_getpwnam)
    return _RootMode(recorder, unit_path, user_state)


# -- the unit itself -------------------------------------------------------------


def test_unit_content_pins_user_data_dir_port_and_capabilities(tmp_path):
    text = cli._render_unit(EXEC, tmp_path / "data", 9443)
    assert f"User={cli.CONTROL_SERVICE_USER}" in text
    assert f"Group={cli.CONTROL_SERVICE_USER}" in text
    assert f"Environment=THEOZOLITH_DATA_DIR={tmp_path / 'data'}" in text
    assert f"ExecStart={EXEC} serve --port 9443" in text
    # The whole privilege story: one capability, bounded, no re-escalation.
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in text
    assert "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in text
    assert "NoNewPrivileges=yes" in text
    assert "User=root" not in text


# -- root init -------------------------------------------------------------------


def test_root_init_installs_and_enables_the_unit(home, rootmode, capsys):
    assert cli_main(["init", "--ip", "127.0.0.1"]) == 0

    unit = rootmode.unit_path.read_text()
    assert f"ExecStart={EXEC} serve --port 443" in unit
    assert f"Environment=THEOZOLITH_DATA_DIR={home}" in unit

    # Fresh box: the service user is created, the partition handed over,
    # systemd reloaded, the unit enabled.
    (useradd,) = rootmode.recorder.named("useradd")
    assert cli.CONTROL_SERVICE_USER in useradd and "--system" in useradd
    (chown,) = rootmode.recorder.named("chown")
    assert chown[-1] == str(home) and f"{cli.CONTROL_SERVICE_USER}:" in chown[-2]
    systemctl = rootmode.recorder.named("systemctl")
    assert ["systemctl", "daemon-reload"] in systemctl
    assert ["systemctl", "enable", cli.CONTROL_SERVICE_NAME] in systemctl

    # The handoff references the unit only because it was installed.
    handoff = capsys.readouterr().out
    assert f"sudo systemctl start {cli.CONTROL_SERVICE_NAME}" in handoff


def test_root_init_nonstandard_port_reaches_the_unit(home, rootmode):
    assert cli_main(["init", "--ip", "127.0.0.1", "--port", "9443"]) == 0
    assert f"ExecStart={EXEC} serve --port 9443" in rootmode.unit_path.read_text()


def test_installer_is_idempotent_with_an_existing_user_and_unit(home, rootmode):
    assert cli_main(["init", "--ip", "127.0.0.1"]) == 0
    first_unit = rootmode.unit_path.read_text()
    # Re-run with the user (and unit) already present: no second useradd,
    # ownership re-repaired, the unit rewritten identically, enable re-run.
    rootmode.user_state["exists"] = True
    assert cli._install_systemd_unit(load_settings(), 443) is True
    assert len(rootmode.recorder.named("useradd")) == 1
    assert len(rootmode.recorder.named("chown")) == 2
    assert rootmode.unit_path.read_text() == first_unit


# -- root recover ----------------------------------------------------------------


def test_root_recover_repairs_the_unit_without_a_new_ca(home, rootmode, capsys):
    assert cli_main(["init", "--ip", "127.0.0.1"]) == 0
    settings = load_settings()
    box = SecretBox(settings.key_path.read_text().strip())
    SecretStore(settings.store_db_path).put_secret("k", box.encrypt("v"))
    ca_before = (home / "secrets" / "tls" / "ca.pem").read_bytes()
    rootmode.unit_path.unlink()  # the replacement box has no unit yet
    rootmode.user_state["exists"] = True
    capsys.readouterr()

    assert cli_main(["recover"]) == 0
    out = capsys.readouterr().out
    # The printed instruction is truthful: the unit exists and was enabled
    # before recover suggested starting it — and the CA never rotates.
    assert rootmode.unit_path.is_file()
    assert ["systemctl", "enable", cli.CONTROL_SERVICE_NAME] in rootmode.recorder.named("systemctl")
    assert f"sudo systemctl start {cli.CONTROL_SERVICE_NAME}" in out
    assert (home / "secrets" / "tls" / "ca.pem").read_bytes() == ca_before


# -- skip conditions -------------------------------------------------------------


def test_installer_skips_unprivileged_container_and_no_systemd(home, tmp_path, monkeypatch, capsys):
    recorder = _Recorder()
    monkeypatch.setattr(subprocess_module, "run", recorder)
    settings = load_settings()

    # Unprivileged (the suite's real euid): skipped before any probe.
    assert cli._install_systemd_unit(settings, 443) is False

    # Root inside a container: the compose flow owns serving.
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "_running_in_container", lambda: True)
    assert cli._install_systemd_unit(settings, 443) is False

    # Root bare metal without systemd: skipped, with the manual line named.
    monkeypatch.setattr(cli, "_running_in_container", lambda: False)
    monkeypatch.setattr(cli, "_systemd_present", lambda: False)
    assert cli._install_systemd_unit(settings, 443) is False
    assert "systemd not detected" in capsys.readouterr().out
    assert recorder.calls == []  # nothing privileged ever ran


def test_unprivileged_init_handoff_never_references_the_unit(home, capsys):
    assert cli_main(["init", "--ip", "127.0.0.1"]) == 0
    handoff = capsys.readouterr().out
    assert "systemctl start" not in handoff
    assert "theozolith serve" in handoff


# -- the chown gate (ADR-0034 round 2): root only mutates its own leaf ----------


def test_root_install_refuses_an_overridden_data_dir(home, rootmode, monkeypatch, tmp_path):
    """THEOZOLITH_DATA_DIR pointing anywhere but the dedicated leaf must
    stop a root install before ANY mutation — a root `chown -R /var` is how
    a typo hands the host to the service user."""
    elsewhere = tmp_path / "var"  # stands in for /, /var, /var/lib…
    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(elsewhere))
    with pytest.raises(SystemExit, match="only manages"):
        cli._install_systemd_unit(load_settings(), 443)
    assert rootmode.recorder.calls == []  # no useradd, no chown, no systemctl
    assert not rootmode.unit_path.exists()


def test_root_init_refuses_an_overridden_data_dir_before_writing_state(
    home, rootmode, monkeypatch, tmp_path
):
    elsewhere = tmp_path / "var"
    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(elsewhere))
    with pytest.raises(SystemExit, match="only manages"):
        cli_main(["init", "--ip", "127.0.0.1"])
    assert not elsewhere.exists()  # refused before the partition was laid
    assert rootmode.recorder.calls == []


def test_root_install_refuses_a_symlinked_data_dir(home, rootmode, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    home.symlink_to(real)  # the leaf name points into the host
    with pytest.raises(SystemExit, match="symlink"):
        cli._install_systemd_unit(load_settings(), 443)
    assert rootmode.recorder.calls == []


# -- executable reachability (ADR-0034) ------------------------------------------


def _world_reachable(exe):
    """Grant o+x up the private pytest tmp ancestry (stops at /tmp)."""
    parent = exe.parent
    while str(parent) not in ("/", "/tmp"):
        parent.chmod(parent.stat().st_mode | 0o001)
        parent = parent.parent


def test_exec_reachability_accepts_a_world_reachable_path(tmp_path):
    exe = tmp_path / "opt" / "theozolith"
    exe.parent.mkdir(mode=0o755)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    _world_reachable(exe)
    assert cli._exec_unreachable_reason(exe) is None


def test_exec_reachability_rejects_private_paths(tmp_path):
    missing = tmp_path / "nowhere" / "theozolith"
    assert cli._exec_unreachable_reason(missing) == "no such file"

    unreadable = tmp_path / "bin" / "theozolith"
    unreadable.parent.mkdir(mode=0o755)
    unreadable.write_text("#!/bin/sh\n")
    unreadable.chmod(0o750)  # sudo resolves it; the service user cannot
    _world_reachable(unreadable)
    assert "not readable+executable by others" in cli._exec_unreachable_reason(unreadable)

    hidden = tmp_path / "homevenv" / "theozolith"
    hidden.parent.mkdir(mode=0o700)  # the ~/.venv-under-sudo case
    hidden.write_text("#!/bin/sh\n")
    hidden.chmod(0o755)
    assert "not traversable by others" in cli._exec_unreachable_reason(hidden)


def test_service_executable_rejects_unit_unsafe_characters(tmp_path, monkeypatch):
    """Paths that would break the unquoted ExecStart directive — spaces,
    systemd '%' specifiers — are refused at setup, not escaped."""
    for name in ("the ozolith", "theo%zolith"):
        exe = tmp_path / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        monkeypatch.setattr("shutil.which", lambda n, path=exe: str(path))
        with pytest.raises(SystemExit, match="unsafe for an unquoted systemd"):
            cli._service_executable()


def test_service_executable_fails_setup_with_remediation(tmp_path, monkeypatch):
    hidden = tmp_path / "root-home" / "theozolith"
    hidden.parent.mkdir(mode=0o700)
    hidden.write_text("#!/bin/sh\n")
    hidden.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda name: str(hidden))
    with pytest.raises(SystemExit, match=r"not reachable.*install TheOzolith"):
        cli._service_executable()


# -- browser credentials under sudo (ADR-0036 amendment) -------------------------


def test_origin_init_under_sudo_hands_the_partition_back(home, rootmode, monkeypatch, capsys):
    """M8 amendment: origin-init runs under sudo AFTER init's chown handed
    the partition to the service user — everything it writes (password
    record, re-minted server key, control.toml commit) must be readable by
    theozolith-control.service on its next restart, and by nobody else."""
    assert cli_main(["init", "--ip", "127.0.0.1"]) == 0
    before = len(rootmode.recorder.named("chown"))
    monkeypatch.setattr("sys.stdin", io.StringIO("\n" + PASSWORD + "\n"))
    assert cli_main(["origin-init"]) == 0

    settings = load_settings()
    record = settings.admin_password_path
    assert record.stat().st_mode & 0o777 == 0o600  # unrelated users locked out
    assert PASSWORD not in record.read_text()
    parse_record(record.read_text())  # a complete, parseable record
    # The partition was handed back to its owner (the service user on a
    # real box) with the same guarded chown the installer uses — the
    # service can read every artifact after its restart.
    chowns = rootmode.recorder.named("chown")
    assert len(chowns) == before + 1
    owner = home.stat()
    assert chowns[-1] == ["chown", "-R", f"{owner.st_uid}:{owner.st_gid}", str(home)]
    # No root-created cache database: none existed, none was created (the
    # service would otherwise be unable to write its own sessions).
    assert not settings.cache_db_path.exists()


def test_set_password_under_sudo_repairs_ownership_too(home, rootmode, monkeypatch):
    assert cli_main(["init", "--ip", "127.0.0.1"]) == 0
    before = len(rootmode.recorder.named("chown"))
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli_main(["set-password"]) == 0
    assert len(rootmode.recorder.named("chown")) == before + 1


def test_ownership_repair_never_touches_a_nondefault_path(tmp_path, monkeypatch):
    """The PR #12 blast-radius rule extends to the repair: a root recursive
    chown may target only the constant system leaf — an env-overridden or
    root-owned data dir is skipped, never chowned."""
    from theozolith_control.settings import ControlSettings

    recorder = _Recorder()
    monkeypatch.setattr(subprocess_module, "run", recorder)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    settings = ControlSettings(
        data_dir=elsewhere,
        config_repo=elsewhere / "configs",
        admin_token="",
        repo=None,
        github_token=None,
    )
    cli._repair_partition_ownership(settings)
    assert recorder.calls == []


def test_control_port_survives_recover_into_the_unit(home, rootmode, capsys):
    """recover preserves the persisted external port (never resets to 443)."""
    assert cli_main(["init", "--ip", "127.0.0.1", "--port", "9443"]) == 0
    settings = load_settings()
    box = SecretBox(settings.key_path.read_text().strip())
    SecretStore(settings.store_db_path).put_secret("k", box.encrypt("v"))
    rootmode.unit_path.unlink()
    rootmode.user_state["exists"] = True
    assert cli_main(["recover"]) == 0
    assert f"ExecStart={EXEC} serve --port 9443" in rootmode.unit_path.read_text()
    assert controltoml.read_control_port(home / "configs") == 9443
