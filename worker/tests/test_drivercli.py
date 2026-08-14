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


# -- custom drivers (ADR-0042) --------------------------------------------------

import json  # noqa: E402
import sys  # noqa: E402

from theozolith_worker import api  # noqa: E402

# A minimal, valid custom Driver: exports a top-level Driver subclassing
# api.Worker with the required role attribute.
DRIVER_SRC = (
    "from theozolith_worker import api\n"
    "\n"
    "class Driver(api.Worker):\n"
    "    role = 'demo'\n"
    "    def fetch_work(self):\n"
    "        return []\n"
    "    def execute(self, item):\n"
    "        return 0\n"
)


class _FakeSink:
    """Records emitted events (the crash-at-start channel)."""

    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event) -> bool:
        self.events.append(event)
        return True


@pytest.fixture
def clean_import():
    """Custom-driver import mutates global sys.path / sys.modules; snapshot and
    restore so tests do not leak `drivers.*` state into one another."""

    def purge():
        for key in list(sys.modules):
            if key == "drivers" or key.startswith("drivers."):
                del sys.modules[key]

    saved_path = list(sys.path)
    purge()
    yield
    sys.path[:] = saved_path
    purge()


def _write_dist(
    root: Path, name: str, source: str, *, built_against="0.3.0", package=False
) -> Path:
    """Build an unpacked config-distribution root (drivers/ + config-dist.json)
    holding one driver, as the daemon would hand to the launcher."""
    drivers = root / "drivers"
    drivers.mkdir(parents=True, exist_ok=True)
    if package:
        pkg = drivers / name
        pkg.mkdir()
        (pkg / "__init__.py").write_text(source, encoding="utf-8")
    else:
        (drivers / f"{name}.py").write_text(source, encoding="utf-8")
    (root / "config-dist.json").write_text(
        json.dumps({"format": 1, "drivers_hash": "x" * 64, "built_against": built_against}),
        encoding="utf-8",
    )
    return root


def _env(dist_root: Path, **extra) -> dict[str, str]:
    return {
        "THEOZOLITH_DRIVERS_DIR": str(dist_root),
        "THEOZOLITH_NODE_NAME": "box1",
        "THEOZOLITH_STACK": "custom-impl",
        **extra,
    }


@pytest.fixture
def captured_run(monkeypatch):
    """run_driver replaced at the seam the launcher calls: the custom-driver
    path resolves and imports the real Driver, then hands it here."""
    calls: list[tuple[type, bool]] = []

    def fake_run_driver(driver_cls, *, once=False):
        calls.append((driver_cls, once))
        return 0

    monkeypatch.setattr(drivercli, "run_driver", fake_run_driver)
    return calls


def test_file_form_custom_driver_imports_and_runs(tmp_path, clean_import, captured_run):
    root = _write_dist(tmp_path / "dist", "foo", DRIVER_SRC)
    logs: list[str] = []
    assert (
        drivercli._run_custom_driver("drivers/foo", once=True, environ=_env(root), log=logs.append)
        == 0
    )
    (driver_cls, once) = captured_run[0]
    assert once is True
    assert issubclass(driver_cls, api.Worker) and driver_cls.__name__ == "Driver"


def test_package_form_custom_driver_imports_and_runs(tmp_path, clean_import, captured_run):
    root = _write_dist(tmp_path / "dist", "bar", DRIVER_SRC, package=True)
    assert drivercli._run_custom_driver("drivers/bar", once=False, environ=_env(root)) == 0
    (driver_cls, once) = captured_run[0]
    assert once is False and issubclass(driver_cls, api.Worker)


def test_main_routes_a_drivers_ref_to_the_custom_path(
    tmp_path, clean_import, captured_run, monkeypatch
):
    root = _write_dist(tmp_path / "dist", "foo", DRIVER_SRC)
    monkeypatch.setattr(drivercli.os, "environ", _env(root))
    assert drivercli.main(["drivers/foo", "--once"]) == 0
    assert captured_run and captured_run[0][1] is True


def test_missing_drivers_dir_exits_1(tmp_path, clean_import, captured_run):
    assert drivercli._run_custom_driver("drivers/foo", once=True, environ={}) == 1
    assert not captured_run  # never reached run_driver


def test_drivers_dir_without_drivers_subdir_exits_1(tmp_path, clean_import, captured_run):
    (tmp_path / "empty").mkdir()
    code = drivercli._run_custom_driver("drivers/foo", once=True, environ=_env(tmp_path / "empty"))
    assert code == 1 and not captured_run


@pytest.mark.parametrize("bad", ["drivers/Bad-Name", "drivers/1abc", "drivers/a-b"])
def test_invalid_custom_driver_name_exits_1(bad, clean_import, captured_run):
    assert drivercli._run_custom_driver(bad, once=True, environ={}) == 1
    assert not captured_run


def test_missing_driver_export_refused(tmp_path, clean_import, captured_run):
    root = _write_dist(tmp_path / "dist", "noexport", "X = 1\n")
    assert drivercli._run_custom_driver("drivers/noexport", once=True, environ=_env(root)) == 1
    assert not captured_run


def test_non_worker_driver_refused(tmp_path, clean_import, captured_run):
    root = _write_dist(tmp_path / "dist", "notworker", "class Driver:\n    pass\n")
    assert drivercli._run_custom_driver("drivers/notworker", once=True, environ=_env(root)) == 1
    assert not captured_run


def test_sys_path_is_appended_not_prepended(tmp_path, clean_import, captured_run):
    root = _write_dist(tmp_path / "dist", "foo", DRIVER_SRC)
    assert drivercli._run_custom_driver("drivers/foo", once=True, environ=_env(root)) == 0
    # APPEND, never prepend: the distribution root lands at the END of sys.path,
    # so a driver tree can never shadow stdlib or the product packages (ADR-0042).
    assert sys.path[-1] == str(root)
    assert sys.path[0] != str(root)


def test_stamp_mismatch_logs_advisory_and_proceeds(tmp_path, clean_import, captured_run):
    root = _write_dist(tmp_path / "dist", "foo", DRIVER_SRC, built_against="0.0.0-not-running")
    logs: list[str] = []
    assert (
        drivercli._run_custom_driver("drivers/foo", once=True, environ=_env(root), log=logs.append)
        == 0
    )
    assert any("built against 0.0.0-not-running" in line and "advisory" in line for line in logs)
    assert captured_run  # advisory never blocks the run


def test_absent_stamp_logs_no_advisory(tmp_path, clean_import, captured_run):
    root = tmp_path / "dist"
    (root / "drivers").mkdir(parents=True)
    (root / "drivers" / "foo.py").write_text(DRIVER_SRC, encoding="utf-8")
    (root / "config-dist.json").write_text(json.dumps({"format": 1}), encoding="utf-8")
    logs: list[str] = []
    assert (
        drivercli._run_custom_driver("drivers/foo", once=True, environ=_env(root), log=logs.append)
        == 0
    )
    assert not any("advisory" in line for line in logs)  # unstamped: no noise


def test_startup_crash_emits_error_through_sink_and_exits_1(
    tmp_path, clean_import, captured_run, capsys
):
    root = _write_dist(tmp_path / "dist", "broken", "def boom(:\n")  # syntax error
    sink = _FakeSink()
    code = drivercli._run_custom_driver("drivers/broken", once=True, environ=_env(root), sink=sink)
    assert code == 1 and not captured_run
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event["type"] == "theozolith.error"
    assert event["component"] == "driver-host"
    assert event["node"] == "box1" and event["stack"] == "custom-impl"
    assert "drivers/broken" in event["message"]
    assert "Traceback" in capsys.readouterr().err  # journal-visible traceback


def test_fork_header_is_logged(tmp_path, clean_import, captured_run):
    source = "# forked-from: builtin:implementer @ 0.3.0\n" + DRIVER_SRC
    root = _write_dist(tmp_path / "dist", "forked", source)
    logs: list[str] = []
    assert (
        drivercli._run_custom_driver(
            "drivers/forked", once=True, environ=_env(root), log=logs.append
        )
        == 0
    )
    assert any("forked-from: builtin:implementer @ 0.3.0" in line for line in logs)


def test_example_driver_runs_a_pass_against_fakes(tmp_path, clean_import):
    """Integration (ADR-0042 acceptance 5): the shipped example custom driver is
    a working Worker — a single run pass idles and logs, no product source read."""
    import importlib.util
    import shutil

    src = Path(__file__).parents[2] / "deploy" / "configs-example" / "drivers" / "hello_logger.py"
    # Load a tmp COPY (exec_module writes bytecode next to the source): never
    # pollute the shipped configs-example tree, which other tests read verbatim.
    path = tmp_path / "hello_logger.py"
    shutil.copyfile(src, path)
    spec = importlib.util.spec_from_file_location("hello_logger_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    Driver = module.Driver
    assert issubclass(Driver, api.Worker)

    (tmp_path / "jobs").mkdir()
    config = api.load_config(
        {
            "THEOZOLITH_REPO": "acme/sandbox",
            "GITHUB_TOKEN": "tok",
            "CONTROL_NODE_URL": "https://control.invalid:8443",
            "THEOZOLITH_JOBS_DIR": str(tmp_path / "jobs"),
        },
        role=Driver.role,
    )
    logs: list[str] = []

    class _Login:
        def viewer_login(self):
            return "ozolith-demo"

    class _Dispatch:
        last_unreachable = False

        def request_work(self, *args):
            return None

        def review_targets(self, *args):
            return None

    worker = Driver(
        config,
        client=_Login(),
        dispatch=_Dispatch(),
        sink=_FakeSink(),
        session_factory=lambda spec, job, manifest: None,
        log=logs.append,
    )
    assert worker.run(once=True) == 0  # claims nothing
    # on_idle is the production loop's idle-pass hook (not exercised by --once):
    # its heartbeat is the proof the custom driver is alive.
    worker.on_idle()
    assert any("hello from the hello_logger custom driver" in line for line in logs)
