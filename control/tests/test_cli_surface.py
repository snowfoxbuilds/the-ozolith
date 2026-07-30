"""The single human CLI (ADR-0032): one parser carries both halves, the
deprecated alias points at the same main, the fleet-operator error
contract (``ProductError`` -> 1) survives the fold, and the old command
spelling survives only in deliberate references."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from theozolith_control import product
from theozolith_control.cli import main as cli_main

# Where `theozolith-control` may still legitimately appear on a line
# (PR #10 review round 1): the component's distribution and docker-image
# names, the deprecated-alias console script, and deprecation notes that
# say "alias" in the same breath. Everything else is a missed sweep.
_ALLOWED_LINE = re.compile(
    r"theozolith-control:"  # docker image tags — the component, not the command
    r"|name = \"theozolith-control\""  # the pip distribution name
    r"|^theozolith-control = \"theozolith_control\.cli:main\"$"  # the alias script
    r"|^# theozolith-control$"  # the component README heading
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
# Not repo-authored: synced from Notion (AGENTS.md, CLAUDE.md) or generated
# (uv.lock names the distribution). The guard covers what the repo owns.
_SKIP_FILES = {"AGENTS.md", "CLAUDE.md", "uv.lock"}


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
