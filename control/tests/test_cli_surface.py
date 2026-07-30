"""The single human CLI (ADR-0032): one parser carries both halves, the
deprecated alias points at the same main, and the fleet-operator error
contract (``ProductError`` -> 1) survives the fold."""

from __future__ import annotations

import tomllib
from pathlib import Path

from theozolith_control import product
from theozolith_control.cli import main as cli_main


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
