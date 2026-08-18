"""`theozolith config migrate` (ADR-0048 amendment): the code-assisted upgrade
path for pre-ingest deployments. The command reads the legacy configs tree
(never modifying it), writes a human Config Repo without the retired
knowledge_source/knowledge_pin fields, keeps the machine-owned control.toml
[control] block OUT of the Config Repo — and the migrated repo then ingests
onto the legacy tree with machine state, product pin, stacks, and worker
types preserved."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
from theozolith_control import configrepo, controltoml
from theozolith_control.ingest import ingest
from theozolith_control.migrate import MigrateError, migrate_legacy

DIGEST = "a" * 64
BASE = f"ghcr.io/snowfoxbuilds/theozolith-run-claude:1.2@sha256:{DIGEST}"

LEGACY_WORKER_TYPE = f"""# The Implementer worker type (operator comments survive migration).
driver = "builtin:implementer"
adapter = "claude"
model = "claude-sonnet-5"
workspace = "acme/sandbox"
base = "{BASE}"
knowledge_source = "https://github.com/acme/knowledge.git"
knowledge_pin = "abc123def4567890"

[secrets]
GITHUB_TOKEN = "github-implementer"
KNOWLEDGE_GIT_TOKEN = "knowledge-git-token"
"""

LEGACY_CONTROL_TOML = """[control]
control_ip = "192.0.2.7"
control_port = 9443

[settings]
heartbeat_seconds = 30
"""


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def write(root: Path, relpath: str, text: str) -> None:
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def legacy_tree(tmp_path: Path) -> Path:
    """A committed pre-ADR-0048 configs tree: git-backed, hand-edited, with
    the machine-written control address in place."""
    legacy = tmp_path / "configs"
    write(legacy, "worker-types/claude-dev.toml", LEGACY_WORKER_TYPE)
    write(
        legacy,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\nstate = "stopped"\n',
    )
    write(legacy, "drivers/hello.py", "class Driver:  # pragma: no cover\n    pass\n")
    write(legacy, "product.toml", '[product]\nversion = "0.2.0"\n')
    write(legacy, "control.toml", LEGACY_CONTROL_TOML)
    _git(legacy, "init", "-q")
    _git(legacy, "add", "-A")
    _git(legacy, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", "legacy")
    return legacy


def test_migrate_strips_legacy_knowledge_and_preserves_everything_else(tmp_path):
    legacy = legacy_tree(tmp_path)
    dest = tmp_path / "config-src"
    report = migrate_legacy(legacy, dest, log=lambda *_: None)

    migrated = (dest / "worker-types/claude-dev.toml").read_text()
    assert "knowledge_source =" not in migrated.replace("#   knowledge_source", "")
    assert "knowledge_pin =" not in migrated.replace("#   knowledge_pin", "")
    # The old values survive as a MIGRATION comment; operator content survives.
    assert "MIGRATION(ADR-0048)" in migrated
    assert "https://github.com/acme/knowledge.git" in migrated
    assert "operator comments survive migration" in migrated
    assert 'GITHUB_TOKEN = "github-implementer"' in migrated
    # Config files ride; machine-owned files do not.
    assert (dest / "stacks/implementer.toml").is_file()
    assert (dest / "drivers/hello.py").is_file()
    assert (dest / "product.toml").read_text() == '[product]\nversion = "0.2.0"\n'
    # control.toml is reduced to the operator [settings] surface: no [control]
    # table (ingest would refuse a Config Repo that authored one).
    control = tomllib.loads((dest / "control.toml").read_text())
    assert "control" not in control
    assert control["settings"] == {"heartbeat_seconds": 30.0}
    # The follow-ups are named: knowledge placement and the retired token.
    assert any("knowledge_source" in note for note in report.notes)
    assert any("KNOWLEDGE_GIT_TOKEN" in note for note in report.notes)
    # The migrated repo is committed and clean; the legacy tree is untouched.
    assert _git(dest, "status", "--porcelain") == ""
    assert _git(legacy, "status", "--porcelain") == ""


def test_migrated_repo_ingests_onto_the_legacy_tree_preserving_machine_state(tmp_path):
    """End to end: migrate, then ingest the migrated repo with the LEGACY tree
    as the pinned build — the upgrade sequence. Machine-owned control.toml
    address, product pin, stacks, and worker types all survive; the upgraded
    load_config (which refuses knowledge_source) accepts the result."""
    legacy = legacy_tree(tmp_path)
    with pytest.raises(configrepo.ConfigRepoError, match=r"retired \(ADR-0048\)"):
        configrepo.load_config(legacy)  # the incompatibility being migrated away

    dest = tmp_path / "config-src"
    migrate_legacy(legacy, dest, log=lambda *_: None)
    report = ingest(str(dest), legacy, log=lambda *_: None)
    assert report.changed

    config = configrepo.load_config(legacy)
    assert config.worker_types["claude-dev"].knowledge == ""
    assert config.worker_types["claude-dev"].workspace == "acme/sandbox"
    assert [s.name for s in config.stacks] == ["implementer"]
    assert config.product_version == "0.2.0"
    # The machine-written [control] block was preserved from the pinned side;
    # the migrated [settings] surface arrived from the Config Repo.
    assert controltoml.read_control_ip(legacy) == "192.0.2.7"
    assert controltoml.read_control_port(legacy) == 9443
    assert controltoml.read_values(legacy)["heartbeat_seconds"] == 30.0
    # The legacy git history is still there, extended — not replaced.
    assert "legacy" in _git(legacy, "log", "--format=%s")


def test_migrate_notes_the_retired_writable_clone_deck_pattern(tmp_path):
    legacy = tmp_path / "configs"
    write(
        legacy,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\n'
        'command = "/usr/local/bin/flightdeck-start"\n'
        'setup = ["theozolith-knowledge clone-init --source x --target /home/ozolith/knowledge"]\n'
        'volumes = ["knowledge-flightdeck:/home/ozolith/knowledge"]\n',
    )
    report = migrate_legacy(legacy, tmp_path / "config-src", log=lambda *_: None)
    assert any("writable-clone" in note for note in report.notes)


def test_migrate_refuses_bad_destinations(tmp_path):
    legacy = legacy_tree(tmp_path)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "x").write_text("x")
    with pytest.raises(MigrateError, match="not empty"):
        migrate_legacy(legacy, occupied, log=lambda *_: None)
    with pytest.raises(MigrateError, match="must differ"):
        migrate_legacy(legacy, legacy, log=lambda *_: None)
    with pytest.raises(MigrateError, match="not a directory"):
        migrate_legacy(tmp_path / "nope", tmp_path / "dest", log=lambda *_: None)


def test_migrate_without_legacy_knowledge_is_a_plain_copy(tmp_path):
    legacy = tmp_path / "configs"
    write(
        legacy,
        "worker-types/claude-dev.toml",
        f'driver = "builtin:implementer"\nadapter = "claude"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{BASE}"\n',
    )
    report = migrate_legacy(legacy, tmp_path / "config-src", log=lambda *_: None)
    migrated = (tmp_path / "config-src/worker-types/claude-dev.toml").read_text()
    assert "MIGRATION" not in migrated
    assert not any("knowledge_source" in note for note in report.notes)
