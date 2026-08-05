"""build.py, the bootstrap shim (ADR-0023/0030, acceptance 1): no build
logic of its own — the same implementation `theozolith build` wraps — and,
since ADR-0041, the owner of the bootstrap environment (managed venv,
re-exec, entry-point links)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
from theozolith_control import product

REPO_ROOT = Path(__file__).parents[2]


def _load_shim():
    spec = importlib.util.spec_from_file_location("build_shim", REPO_ROOT / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_py_wraps_the_exact_same_implementation():
    """One implementation, two entry paths — identity-asserted so a
    copy-paste fork fails here, not in a deployment."""
    shim = _load_shim()
    assert shim.build_distribution is product.build_distribution
    # The shim finishes by installing the built wheels (the entry points),
    # and adds no second build pipeline.
    source = (REPO_ROOT / "build.py").read_text()
    assert "pip" in source and "install" in source
    assert "pip wheel" not in source  # building is product.py's job alone


def test_the_shared_module_is_importable_without_dependencies():
    """The bare-checkout property: theozolith_control.product imports
    stdlib-only at module import time (also pinned by the separability test
    in test_product.py) — a bare interpreter can run the bootstrap build."""
    source = (REPO_ROOT / "control" / "src" / "theozolith_control" / "product.py").read_text()
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            module = line.split()[1]
            assert not module.startswith(("fastapi", "uvicorn", "cryptography", "jinja2")), line


# -- the managed environment (ADR-0041) ----------------------------------------


def _fake_venv(path: Path) -> None:
    (path / "bin").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("")
    (path / "bin" / "python").write_text("")


def test_managed_bootstrap_requires_root():
    """The managed default writes /opt and /usr/local/bin — refused without
    root, naming both sudo and the --venv escape hatch."""
    shim = _load_shim()
    with pytest.raises(SystemExit, match=r"sudo.*--venv"):
        shim.ensure_environment(
            Path("/opt/theozolith"),
            [],
            managed=True,
            geteuid=lambda: 1000,
            runner=None,
            execv=None,
            environ={},
        )


def test_bootstrap_refuses_a_target_that_is_not_a_venv(tmp_path):
    """An existing non-venv directory at the target is never touched."""
    shim = _load_shim()
    target = tmp_path / "opt"
    target.mkdir()
    with pytest.raises(SystemExit, match="not a virtual environment"):
        shim.ensure_environment(
            target, [], managed=True, geteuid=lambda: 0, runner=None, execv=None, environ={}
        )


def test_bootstrap_refuses_without_ensurepip(tmp_path):
    """A venv-incapable interpreter refuses with the exact OS remediation —
    the shim never package-manages on its own (ADR-0037 posture)."""
    shim = _load_shim()
    with pytest.raises(SystemExit, match="python3-venv"):
        shim.ensure_environment(
            tmp_path / "venv",
            [],
            managed=False,
            geteuid=lambda: 1000,
            runner=None,
            execv=None,
            environ={},
            find_spec=lambda name: None,
        )


def test_bootstrap_creates_the_venv_and_reexecs(tmp_path):
    """The core ADR-0041 flow: create the venv, then re-execute this same
    shim with the venv's interpreter and the original argv."""
    shim = _load_shim()
    target = tmp_path / "venv"
    created, execs, environ = [], [], {}

    def runner(argv, check=False):
        _fake_venv(target)
        created.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    shim.ensure_environment(
        target,
        ["--venv", str(target)],
        managed=False,
        geteuid=lambda: 1000,
        runner=runner,
        execv=lambda path, argv: execs.append((path, argv)),
        environ=environ,
        find_spec=lambda name: object(),
    )
    assert created == [[shim.sys.executable, "-m", "venv", str(target)]]
    python = str(target / "bin" / "python")
    shim_path = str(shim.REPO_ROOT / "build.py")
    assert execs == [(python, [python, shim_path, "--venv", str(target)])]
    assert environ[shim.REEXEC_MARKER] == "1"


def test_bootstrap_reuses_an_existing_venv(tmp_path):
    """A venv already at the target (a node-shaped install, or a re-run) is
    reused, never recreated."""
    shim = _load_shim()
    target = tmp_path / "venv"
    _fake_venv(target)
    created, execs = [], []
    shim.ensure_environment(
        target,
        [],
        managed=False,
        geteuid=lambda: 1000,
        runner=lambda argv, check=False: created.append(argv),
        execv=lambda path, argv: execs.append(path),
        environ={},
    )
    assert created == []
    assert execs == [str(target / "bin" / "python")]


def test_reexec_marker_stops_an_exec_loop(tmp_path):
    """A venv whose interpreter does not identify as the venv would exec
    forever — the marker turns the second pass into a hard error."""
    shim = _load_shim()
    with pytest.raises(SystemExit, match="does not identify"):
        shim.ensure_environment(
            tmp_path / "venv",
            [],
            managed=False,
            geteuid=lambda: 1000,
            runner=None,
            execv=None,
            environ={shim.REEXEC_MARKER: "1"},
        )


def test_link_entry_points_is_idempotent_and_complete(tmp_path):
    """The human CLI (and the daemon CLI init's pre-flight resolves from
    PATH, ADR-0037) linked into the bin dir; re-linking is last-write-wins;
    a missing entry point is a hard error, not a silent gap."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "bin"
    (venv / "bin").mkdir(parents=True)
    for name in shim.LINKED_ENTRY_POINTS:
        (venv / "bin" / name).write_text("")
    for _ in range(2):  # second pass exercises the unlink-then-relink path
        shim.link_entry_points(venv, link_dir=bin_dir)
        for name in shim.LINKED_ENTRY_POINTS:
            link = bin_dir / name
            assert link.is_symlink() and link.resolve() == (venv / "bin" / name).resolve()
    (venv / "bin" / "theozolith-nodedaemon").unlink()
    with pytest.raises(SystemExit, match="theozolith-nodedaemon"):
        shim.link_entry_points(venv, link_dir=bin_dir)
