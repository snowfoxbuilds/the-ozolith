"""The in-image materialization CLI (``theozolith-adapter``, ADR-0045):
validation backstop, scope behavior, and the exit-code contract. The CLI is
what the synthesized setup instruction runs during ``docker build``, so a
non-zero exit here IS the "build fails on an unmappable value" clause.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from theozolith_worker import adaptercli

WORKER_PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


def test_console_script_registered():
    # The synthesized setup instruction invokes ``theozolith-adapter`` by name
    # inside the image; the wheel must actually install it.
    scripts = tomllib.loads(WORKER_PYPROJECT.read_text())["project"]["scripts"]
    assert scripts["theozolith-adapter"] == "theozolith_worker.adaptercli:main"


def test_materialize_managed_writes_native_config(tmp_path, capsys):
    rc = adaptercli.main(
        [
            "materialize",
            "--adapter",
            "claude",
            "--model",
            "claude-sonnet-5",
            "--effort",
            "high",
            "--scope",
            "managed",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    settings = json.loads((tmp_path / "etc/claude-code/managed-settings.json").read_text())
    assert settings == {"model": "claude-sonnet-5", "effortLevel": "high"}
    assert (tmp_path / "etc/theozolith/model").read_text() == "claude-sonnet-5\n"
    assert "materialized" in capsys.readouterr().out


def test_materialize_interactive_writes_only_well_known_files(tmp_path):
    rc = adaptercli.main(
        [
            "materialize",
            "--adapter",
            "claude",
            "--model",
            "claude-opus-5",
            "--scope",
            "interactive",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    assert (tmp_path / "etc/theozolith/model").read_text() == "claude-opus-5\n"
    assert not (tmp_path / "etc/claude-code").exists()


def test_unknown_adapter_exits_2(tmp_path, capsys):
    rc = adaptercli.main(
        [
            "materialize",
            "--adapter",
            "pi",
            "--model",
            "x",
            "--scope",
            "managed",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 2
    assert "unknown Agent adapter" in capsys.readouterr().err


def test_unmappable_model_exits_2_with_base_bump_hint(tmp_path, capsys):
    rc = adaptercli.main(
        [
            "materialize",
            "--adapter",
            "claude",
            "--model",
            "gpt-5",
            "--scope",
            "managed",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    # The likeliest field failure inside a build is an old base whose adapter
    # registry predates the model — the message must say what to do about it.
    assert "cannot map model" in err and "bump the worker type's base" in err
    assert not (tmp_path / "etc").exists()  # nothing written on failure


def test_unmappable_effort_exits_2_listing_the_mappable_set(tmp_path, capsys):
    rc = adaptercli.main(
        [
            "materialize",
            "--adapter",
            "claude",
            "--model",
            "claude-sonnet-5",
            "--effort",
            "max",
            "--scope",
            "managed",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot map effort 'max'" in err
    assert "low" in err and "xhigh" in err


def test_nothing_to_materialize_exits_2(tmp_path, capsys):
    rc = adaptercli.main(
        ["materialize", "--adapter", "claude", "--scope", "managed", "--root", str(tmp_path)]
    )
    assert rc == 2
    assert "nothing to materialize" in capsys.readouterr().err


def test_corrupt_existing_managed_settings_fails_the_build(tmp_path, capsys):
    target = tmp_path / "etc/claude-code/managed-settings.json"
    target.parent.mkdir(parents=True)
    target.write_text("[]")
    rc = adaptercli.main(
        [
            "materialize",
            "--adapter",
            "claude",
            "--model",
            "claude-sonnet-5",
            "--scope",
            "managed",
            "--root",
            str(tmp_path),
        ]
    )
    assert rc == 1
    assert "materialize failed" in capsys.readouterr().err
