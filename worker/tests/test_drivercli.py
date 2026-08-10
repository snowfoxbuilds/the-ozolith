"""The generic driver launcher (``theozolith-driver <ref> [--once]``, ADR-0020):
ref selection, ``--once`` plumbing, and the exit-code contract.

``run_driver`` is monkeypatched at the seam the CLI calls, so these tests pin
argument handling — which worker type a ref selects and what ``once`` value it
receives — without constructing a real dispatch loop.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from theozolith_worker import drivercli
from theozolith_worker.config import ConfigError
from theozolith_worker.implementer import Implementer
from theozolith_worker.reviewer import Reviewer

WORKER_PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


@pytest.fixture
def launched(monkeypatch):
    calls: list[tuple[type, bool]] = []

    def fake_run_driver(driver_cls, *, once=False):
        calls.append((driver_cls, once))
        return 0

    monkeypatch.setattr(drivercli, "run_driver", fake_run_driver)
    return calls


def test_builtin_implementer_once_selects_implementer(launched):
    assert drivercli.main(["builtin:implementer", "--once"]) == 0
    assert launched == [(Implementer, True)]


def test_builtin_reviewer_once_selects_reviewer(launched):
    assert drivercli.main(["builtin:reviewer", "--once"]) == 0
    assert launched == [(Reviewer, True)]


def test_known_ref_without_once_runs_the_loop(launched):
    assert drivercli.main(["builtin:implementer"]) == 0
    assert launched == [(Implementer, False)]


def test_unknown_ref_exits_2_and_lists_known_refs(launched, capsys):
    assert drivercli.main(["builtin:nonesuch"]) == 2
    assert launched == []  # nothing launched
    err = capsys.readouterr().err
    assert "builtin:nonesuch" in err
    assert "builtin:implementer" in err and "builtin:reviewer" in err


def test_config_error_exits_2_without_traceback(monkeypatch, capsys):
    def boom(driver_cls, *, once=False):
        raise ConfigError("THEOZOLITH_REPO is required")

    monkeypatch.setattr(drivercli, "run_driver", boom)
    assert drivercli.main(["builtin:implementer"]) == 2
    err = capsys.readouterr().err
    assert "THEOZOLITH_REPO is required" in err
    assert "Traceback" not in err


def test_keyboard_interrupt_is_a_clean_shutdown(monkeypatch, capsys):
    def interrupt(driver_cls, *, once=False):
        raise KeyboardInterrupt

    monkeypatch.setattr(drivercli, "run_driver", interrupt)
    assert drivercli.main(["builtin:implementer"]) == 0
    assert "implementer driver stopped" in capsys.readouterr().out


def test_console_script_names_the_drivercli_entry_point():
    scripts = tomllib.loads(WORKER_PYPROJECT.read_text())["project"]["scripts"]
    assert scripts["theozolith-driver"] == "theozolith_worker.drivercli:main"
