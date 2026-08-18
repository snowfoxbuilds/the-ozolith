"""The single human CLI (ADR-0032): one parser carries both halves, the
deprecated alias points at the same main, the fleet-operator error
contract (``ProductError`` -> 1) survives the fold, and the old command
spelling survives only in deliberate references."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from theozolith_control import product
from theozolith_control.cli import main as cli_main

# Where `theozolith-control` may still legitimately appear on a line
# (PR #10 review round 1): the component's distribution and docker-image
# names, the deprecated-alias console script, and deprecation notes that
# say "alias" in the same breath — plus, since ADR-0034, the systemd unit
# and the system data dir, which carry the component name, not the command.
# Everything else is a missed sweep.
_ALLOWED_LINE = re.compile(
    r"theozolith-control:"  # docker image tags — the component, not the command
    r"|name = \"theozolith-control\""  # the pip distribution name
    r"|^theozolith-control = \"theozolith_control\.cli:main\"$"  # the alias script
    r"|^# theozolith-control$"  # the component README heading
    r"|theozolith-control\.service"  # the systemd unit (ADR-0034)
    r"|/var/lib/theozolith-control"  # the root-mediated data dir (ADR-0034)
    r"|alias"  # deprecation notes
)
_SKIP_PARTS = {
    ".git",
    ".venv",
    ".claude",
    "docs",  # ADRs/specs are records; they may quote the old surface
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "node_modules",
}
# Not repo-authored: synced from Notion (AGENTS.md, CLAUDE.md, CONTEXT.md)
# or generated (uv.lock names the distribution). The guard covers what the
# repo owns — a stale spelling in a synced doc is a Notion follow-up, not a
# repo edit.
_SKIP_FILES = {"AGENTS.md", "CLAUDE.md", "CONTEXT.md", "uv.lock"}


def test_old_spelling_survives_only_in_deliberate_references():
    root = Path(__file__).resolve().parents[2]
    self_path = Path(__file__).resolve()
    offenders = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if set(rel.parts) & _SKIP_PARTS or any(p.endswith(".egg-info") for p in rel.parts):
            continue
        if path.name in _SKIP_FILES:
            continue
        if not path.is_file() or path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if "theozolith-control" in line and not _ALLOWED_LINE.search(line):
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert not offenders, "old command spelling outside deliberate references:\n" + "\n".join(
        offenders
    )


def test_operator_commands_dispatch_through_the_merged_parser(monkeypatch):
    seen = {}

    def fake_update(args):
        seen["version"] = args.version
        return 0

    monkeypatch.setattr(product, "_cmd_update", fake_update)
    assert cli_main(["update", "--version", "1.2.3"]) == 0
    assert seen == {"version": "1.2.3"}


def test_product_errors_keep_their_exit_code(monkeypatch, capsys):
    def explode(args):
        raise product.ProductError("boom")

    monkeypatch.setattr(product, "_cmd_update", explode)
    assert cli_main(["update"]) == 1
    assert "error: boom" in capsys.readouterr().err


def test_product_main_delegates_to_the_merged_cli(monkeypatch):
    monkeypatch.setattr(product, "_cmd_update", lambda args: 0)
    assert product.main(["update"]) == 0


def test_both_entry_points_share_one_main():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]
    assert scripts["theozolith"] == "theozolith_control.cli:main"
    assert scripts["theozolith-control"] == scripts["theozolith"]


def test_config_ingest_dispatches_with_the_default_source(monkeypatch, tmp_path):
    """`theozolith config ingest` (ADR-0048): no positional -> the scaffolded
    config-src beside the data dir; an explicit path/URL wins."""
    from theozolith_control import cli, ingest

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))
    calls = []
    monkeypatch.setattr(
        ingest, "ingest", lambda source, pinned, **kw: calls.append((source, pinned)) or None
    )
    monkeypatch.setattr(cli, "_repair_partition_ownership", lambda settings: None)
    assert cli_main(["config", "ingest"]) == 0
    assert cli_main(["config", "ingest", "https://example.invalid/config.git"]) == 0
    assert calls[0][0] == str(tmp_path / "home" / "config-src")
    assert calls[0][1] == tmp_path / "home" / "configs"
    assert calls[1][0] == "https://example.invalid/config.git"


def test_config_migrate_dispatches_with_deployment_defaults(monkeypatch, tmp_path):
    """`theozolith config migrate` (ADR-0048 amendment): no arguments ->
    legacy = the deployment's configs/ (the future pinned build), dest = the
    config-src location; explicit paths win."""
    from theozolith_control import cli, migrate

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))
    calls = []
    monkeypatch.setattr(
        migrate,
        "migrate_legacy",
        lambda legacy, dest, **kw: calls.append((legacy, dest)) or None,
    )
    monkeypatch.setattr(cli, "_repair_partition_ownership", lambda settings: None)
    assert cli_main(["config", "migrate"]) == 0
    assert cli_main(["config", "migrate", str(tmp_path / "new-src"), "--legacy", "/old"]) == 0
    assert calls[0] == (tmp_path / "home" / "configs", tmp_path / "home" / "config-src")
    assert calls[1] == (Path("/old"), tmp_path / "new-src")


def test_config_migrate_refusal_exits_nonzero(monkeypatch, tmp_path):
    from theozolith_control import migrate

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))

    def refuse(legacy, dest, **kw):
        raise migrate.MigrateError("migration destination is not empty")

    monkeypatch.setattr(migrate, "migrate_legacy", refuse)
    with pytest.raises(SystemExit, match="not empty"):
        cli_main(["config", "migrate"])


def test_config_ingest_refusal_exits_nonzero(monkeypatch, tmp_path, capsys):
    from theozolith_control import ingest

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))

    def refuse(source, pinned, **kw):
        raise ingest.IngestError("pinned build has uncommitted changes")

    monkeypatch.setattr(ingest, "ingest", refuse)
    with pytest.raises(SystemExit, match="uncommitted changes"):
        cli_main(["config", "ingest"])
