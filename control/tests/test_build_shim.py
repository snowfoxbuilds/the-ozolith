"""build.py, the bootstrap shim (ADR-0023/0030, acceptance 1): no build
logic of its own — the same implementation `theozolith build` wraps — and,
since ADR-0041, the owner of the bootstrap environment (managed venv,
health-checked re-exec, atomically published entry-point links that never
overwrite foreign state)."""

from __future__ import annotations

import errno
import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest
from theozolith_control import product

REPO_ROOT = Path(__file__).parents[2]

# A real executable interpreter stand-in: executable semantics (X_OK, exec)
# are part of the production contract, so an empty file will not do.
FAKE_PYTHON = "#!/bin/sh\nexit 0\n"


def _load_shim():
    spec = importlib.util.spec_from_file_location("build_shim", REPO_ROOT / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_venv(path: Path, *, executable: bool = True) -> None:
    (path / "bin").mkdir(parents=True, exist_ok=True)
    (path / "pyvenv.cfg").write_text("")
    python = path / "bin" / "python"
    python.write_text(FAKE_PYTHON)
    if executable:
        python.chmod(0o755)
    # A bin/pip wrapper is always present: its existence proves nothing (the
    # health check asks `python -m pip`, the operation the install runs).
    pip = path / "bin" / "pip"
    pip.write_text(FAKE_PYTHON)
    pip.chmod(0o755)


def _entry_points(shim, venv: Path) -> None:
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    for name in shim.LINKED_ENTRY_POINTS:
        script = venv / "bin" / name
        script.write_text(FAKE_PYTHON)
        script.chmod(0o755)


def _refuse(*_args, **_kwargs):
    raise AssertionError("must not be reached")


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
            runner=_refuse,
            execv=_refuse,
            environ={},
        )


def test_bootstrap_refuses_a_target_that_is_not_a_venv(tmp_path):
    """An existing non-venv directory at the target is never touched."""
    shim = _load_shim()
    target = tmp_path / "opt"
    target.mkdir()
    with pytest.raises(SystemExit, match="not a virtual environment"):
        shim.ensure_environment(
            target, [], managed=True, geteuid=lambda: 0, runner=_refuse, execv=_refuse, environ={}
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
            runner=_refuse,
            execv=_refuse,
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
    """A venv whose interpreter runs but does not identify as the venv
    would exec forever — the marker turns the second pass into a hard
    error. (Other broken-venv shapes are caught by their own checks.)"""
    shim = _load_shim()
    with pytest.raises(SystemExit, match="does not identify"):
        shim.ensure_environment(
            tmp_path / "venv",
            [],
            managed=False,
            geteuid=lambda: 1000,
            runner=_refuse,
            execv=_refuse,
            environ={shim.REEXEC_MARKER: "1"},
        )


# -- broken and partial venvs refuse with remediation, never a traceback -------


def test_a_failed_venv_creation_is_cleaned_up_and_reported(tmp_path):
    """A failed `python -m venv` exits with the command's status in the
    message and removes the partial tree it left, so the re-run starts
    from scratch instead of treating debris as a healthy venv."""
    shim = _load_shim()
    target = tmp_path / "venv"

    def failing_runner(argv, check=False):
        (target / "bin").mkdir(parents=True)  # the partial debris of a failed create
        (target / "pyvenv.cfg").write_text("")
        return subprocess.CompletedProcess(argv, 1)

    with pytest.raises(SystemExit, match="exit status 1"):
        shim.ensure_environment(
            target,
            [],
            managed=False,
            geteuid=lambda: 1000,
            runner=failing_runner,
            execv=_refuse,
            environ={},
            find_spec=lambda name: object(),
        )
    assert not target.exists()


def test_a_venv_without_an_interpreter_is_refused(tmp_path):
    shim = _load_shim()
    target = tmp_path / "venv"
    _fake_venv(target)
    (target / "bin" / "python").unlink()
    with pytest.raises(SystemExit, match="no bin/python"):
        shim.ensure_environment(
            target, [], managed=False, geteuid=lambda: 1000, execv=_refuse, environ={}
        )


def test_a_non_executable_interpreter_is_refused_with_remediation(tmp_path):
    """A bin/python regular file is not enough — it must be executable;
    the refusal names repair and removal."""
    shim = _load_shim()
    target = tmp_path / "venv"
    _fake_venv(target, executable=False)
    with pytest.raises(SystemExit, match=r"not executable.*repair|not executable.*delete"):
        shim.ensure_environment(
            target, [], managed=False, geteuid=lambda: 1000, execv=_refuse, environ={}
        )


def test_the_pip_preflight_asks_the_module_the_install_runs(tmp_path):
    """The install invokes `python -m pip`, so the preflight runs exactly
    that module path (`--version`) — it never consults a bin/pip wrapper on
    disk, whose existence proves nothing about the module."""
    shim = _load_shim()
    python = tmp_path / "venv" / "bin" / "python"
    calls = []

    def runner(argv, check=False, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    shim.check_pip(python, runner=runner)
    assert calls == [[str(python), "-m", "pip", "--version"]]


def test_a_broken_pip_module_is_a_remediation_with_the_cause(tmp_path):
    """A nonzero `python -m pip --version` (the wrapper may well exist) is
    refused with the failure's own last line and repair-or-delete
    remediation — never a traceback."""
    shim = _load_shim()

    def runner(argv, check=False, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No module named pip\n")

    with pytest.raises(SystemExit, match="No module named pip") as excinfo:
        shim.check_pip(tmp_path / "venv" / "bin" / "python", runner=runner)
    assert "repair its pip module or delete the venv" in str(excinfo.value)


def test_a_pip_preflight_exec_error_is_a_remediation_not_a_traceback(tmp_path):
    """The interpreter failing to run at all for the pip check (EACCES,
    ENOEXEC, …) gets the same repair-or-delete remediation."""
    shim = _load_shim()

    def runner(argv, check=False, **kwargs):
        raise OSError(errno.EACCES, "Permission denied", argv[0])

    with pytest.raises(SystemExit, match="repair its pip module or delete the venv"):
        shim.check_pip(tmp_path / "venv" / "bin" / "python", runner=runner)


def test_a_failing_exec_is_a_remediation_not_a_traceback(tmp_path):
    """execv can fail even on an executable file (ENOEXEC, EACCES, I/O
    errors) — the shim reports the venv as broken instead of crashing."""
    shim = _load_shim()
    target = tmp_path / "venv"
    _fake_venv(target)

    def failing_execv(path, argv):
        raise OSError(errno.ENOEXEC, "Exec format error", path)

    with pytest.raises(SystemExit, match="venv is broken"):
        shim.ensure_environment(
            target, [], managed=False, geteuid=lambda: 1000, execv=failing_execv, environ={}
        )


# -- entry-point publication: validate everything, overwrite nothing -----------


def test_link_entry_points_is_idempotent_and_complete(tmp_path):
    """The human CLI (and the daemon CLI init's pre-flight resolves from
    PATH, ADR-0037) linked into the bin dir; a re-run over its own links
    converges; a missing entry point is a hard error, not a silent gap."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "bin"
    _entry_points(shim, venv)
    for _ in range(2):  # second pass republishes over this installation's own links
        shim.link_entry_points(venv, link_dir=bin_dir)
        for name in shim.LINKED_ENTRY_POINTS:
            link = bin_dir / name
            assert link.is_symlink() and link.resolve() == (venv / "bin" / name).resolve()
    (venv / "bin" / "theozolith-nodedaemon").unlink()
    with pytest.raises(SystemExit, match="theozolith-nodedaemon"):
        shim.link_entry_points(venv, link_dir=bin_dir)


def test_a_non_executable_entry_point_is_refused(tmp_path):
    """A source entry point must be a regular executable file — a stray
    non-executable file in the venv's bin is an incomplete install."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "bin"
    _entry_points(shim, venv)
    (venv / "bin" / "theozolith").chmod(0o644)
    with pytest.raises(SystemExit, match="no executable"):
        shim.link_entry_points(venv, link_dir=bin_dir)
    assert not bin_dir.exists()


def test_a_symlink_entry_point_is_refused(tmp_path):
    """The source contract is a NON-symlink regular executable file: a
    symlinked entry point is refused even when it resolves to a valid
    executable — is_file() alone would happily follow it."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "bin"
    _entry_points(shim, venv)
    script = venv / "bin" / "theozolith"
    real = venv / "bin" / "somewhere-else"
    script.rename(real)
    script.symlink_to(real)
    with pytest.raises(SystemExit, match="no executable regular file"):
        shim.link_entry_points(venv, link_dir=bin_dir)
    assert not bin_dir.exists()


def test_the_link_dir_is_created_only_after_validation_succeeds(tmp_path):
    """An absent link dir (a bare box's /usr/local/bin) may be created only
    once every source and destination has validated — a refused run leaves
    no directory behind; a valid one creates it and publishes."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "deep" / "bin"
    _entry_points(shim, venv)
    script = venv / "bin" / shim.LINKED_ENTRY_POINTS[-1]
    script.chmod(0o644)
    with pytest.raises(SystemExit, match="no executable"):
        shim.link_entry_points(venv, link_dir=bin_dir)
    assert not bin_dir.exists() and not bin_dir.parent.exists()
    script.chmod(0o755)
    shim.link_entry_points(venv, link_dir=bin_dir)
    for name in shim.LINKED_ENTRY_POINTS:
        assert os.readlink(bin_dir / name) == str(venv / "bin" / name)


@pytest.mark.parametrize("collision", ["file", "directory", "foreign-symlink"])
def test_a_collision_is_refused_by_name_and_left_untouched(tmp_path, collision):
    """An unrelated path at a destination is never unlinked: the refusal
    names the conflicting path, and no other link is published first —
    validation of every source and destination precedes any mutation."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "bin"
    _entry_points(shim, venv)
    bin_dir.mkdir()
    # Collide on the LAST name: under an unsafe implementation the earlier
    # names would already have been published when the refusal fires.
    colliding = bin_dir / shim.LINKED_ENTRY_POINTS[-1]
    foreign = tmp_path / "somebody-elses-install" / "bin" / colliding.name
    if collision == "file":
        colliding.write_text("#!/bin/sh\n# somebody else's tool\n")
    elif collision == "directory":
        colliding.mkdir()
    else:
        foreign.parent.mkdir(parents=True)
        foreign.write_text(FAKE_PYTHON)
        colliding.symlink_to(foreign)
    with pytest.raises(SystemExit, match=re.escape(str(colliding))) as excinfo:
        shim.link_entry_points(venv, link_dir=bin_dir)
    assert "refusing" in str(excinfo.value)
    # The collision survives untouched …
    if collision == "file":
        assert colliding.is_file() and "somebody else's" in colliding.read_text()
    elif collision == "directory":
        assert colliding.is_dir()
    else:
        assert colliding.is_symlink() and os.readlink(colliding) == str(foreign)
    # … and none of the other destinations was published before the refusal.
    for name in shim.LINKED_ENTRY_POINTS[:-1]:
        assert not (bin_dir / name).exists() and not (bin_dir / name).is_symlink()


@pytest.mark.parametrize("occupant", ["file", "directory", "symlink"])
def test_a_foreign_object_at_a_candidate_temp_name_survives(tmp_path, monkeypatch, occupant):
    """Temporary names are owned by exclusive creation, not by pattern: an
    object already sitting at a candidate temp name — whatever it is — makes
    the shim pick another name; it is never removed and survives the whole
    publication byte for byte."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "bin"
    _entry_points(shim, venv)
    bin_dir.mkdir()
    tokens = {"n": 0}

    def fake_token_hex(nbytes):
        tokens["n"] += 1
        return "collide" if tokens["n"] == 1 else f"free{tokens['n']}"

    monkeypatch.setattr(shim.secrets, "token_hex", fake_token_hex)
    # Occupy the exact first candidate name the shim will generate.
    candidate = bin_dir / f".{shim.LINKED_ENTRY_POINTS[0]}.collide.tmp"
    elsewhere = tmp_path / "elsewhere"
    if occupant == "file":
        candidate.write_bytes(b"somebody else's bytes")
    elif occupant == "directory":
        candidate.mkdir()
        (candidate / "keep").write_bytes(b"somebody else's bytes")
    else:
        candidate.symlink_to(elsewhere)  # dangling: symlink(2) still refuses EEXIST
    shim.link_entry_points(venv, link_dir=bin_dir)
    # Publication succeeded around the occupant …
    for name in shim.LINKED_ENTRY_POINTS:
        assert os.readlink(bin_dir / name) == str(venv / "bin" / name)
    # … which survives byte for byte.
    if occupant == "file":
        assert candidate.read_bytes() == b"somebody else's bytes"
    elif occupant == "directory":
        assert (candidate / "keep").read_bytes() == b"somebody else's bytes"
    else:
        assert os.readlink(candidate) == str(elsewhere)


def test_a_leftover_at_a_predictable_temp_name_is_foreign(tmp_path):
    """No path is "ours by name": a file squatting on the old predictable
    temp-name shape is somebody's until proven created by this invocation —
    publication succeeds around it and leaves it untouched."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "bin"
    _entry_points(shim, venv)
    bin_dir.mkdir()
    squatter = bin_dir / f".{shim.LINKED_ENTRY_POINTS[0]}.theozolith-tmp"
    squatter.write_bytes(b"not actually ours")
    shim.link_entry_points(venv, link_dir=bin_dir)
    assert squatter.read_bytes() == b"not actually ours"
    for name in shim.LINKED_ENTRY_POINTS:
        assert os.readlink(bin_dir / name) == str(venv / "bin" / name)


def test_a_failed_rename_cleans_only_its_own_temp(tmp_path, monkeypatch):
    """A failed rename removes the temp symlink this invocation created —
    nothing else in the directory — and the previously published links
    survive the failed re-publication intact."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "bin"
    _entry_points(shim, venv)
    shim.link_entry_points(venv, link_dir=bin_dir)  # a valid installation
    bystander = bin_dir / ".unrelated-dotfile"
    bystander.write_bytes(b"leave me be")

    def failing_replace(src, dst):
        raise OSError(errno.EIO, "I/O error", str(dst))

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(SystemExit, match="could not publish"):
        shim.link_entry_points(venv, link_dir=bin_dir)
    expected = {bystander.name, *shim.LINKED_ENTRY_POINTS}
    assert {p.name for p in bin_dir.iterdir()} == expected  # no temp survived
    assert bystander.read_bytes() == b"leave me be"
    for name in shim.LINKED_ENTRY_POINTS:
        assert os.readlink(bin_dir / name) == str(venv / "bin" / name)


def test_an_interrupted_first_installation_converges_on_rerun(tmp_path, monkeypatch):
    """The three destinations are published sequentially — atomic each, not
    transactional as a set. Failing on the second leaves the first link
    valid, the others absent, and no temp path of this invocation's behind;
    the re-run completes the set."""
    shim = _load_shim()
    venv, bin_dir = tmp_path / "venv", tmp_path / "bin"
    _entry_points(shim, venv)
    first, second, third = shim.LINKED_ENTRY_POINTS
    real_replace, renames = os.replace, []

    def replace_failing_second(src, dst):
        renames.append(dst)
        if len(renames) == 2:
            raise OSError(errno.EIO, "I/O error", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_failing_second)
    with pytest.raises(SystemExit, match="could not publish"):
        shim.link_entry_points(venv, link_dir=bin_dir)
    # The published subset is valid, the rest absent, no temp remains.
    assert os.readlink(bin_dir / first) == str(venv / "bin" / first)
    for name in (second, third):
        assert not (bin_dir / name).exists() and not (bin_dir / name).is_symlink()
    assert [p.name for p in bin_dir.iterdir()] == [first]
    monkeypatch.setattr(os, "replace", real_replace)
    shim.link_entry_points(venv, link_dir=bin_dir)  # the re-run converges
    for name in shim.LINKED_ENTRY_POINTS:
        assert os.readlink(bin_dir / name) == str(venv / "bin" / name)


def test_the_documented_uninstall_covers_every_owned_link():
    """The cleanup contract: the deploy README's deletion procedure must
    account for every link the bootstrap owns — guarded by readlink against
    the managed venv so a foreign binary at those names is never deleted.
    Growing LINKED_ENTRY_POINTS fails here until the procedure grows too."""
    shim = _load_shim()
    readme = (REPO_ROOT / "deploy" / "README.md").read_text()
    start = readme.index("## Cleanup / deletion test")
    end = readme.find("\n## ", start + 1)
    section = readme[start:] if end == -1 else readme[start:end]
    for name in shim.LINKED_ENTRY_POINTS:
        assert name in section, f"cleanup procedure misses the {name} link"
    assert "readlink" in section, "cleanup must check links point at the managed venv"
    assert str(shim.MANAGED_VENV / "bin") in section


# -- main(): the whole orchestration, both passes ------------------------------


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """A shim wired to a sandboxed /opt and /usr/local/bin with the process
    boundary (euid, execv, environ, subprocess, build) faked out."""
    shim = _load_shim()
    opt = tmp_path / "opt" / "theozolith"
    bin_dir = tmp_path / "usr-local-bin"
    monkeypatch.setattr(shim, "MANAGED_VENV", opt)
    monkeypatch.setattr(shim, "LINK_DIR", bin_dir)
    state = {
        "environ": {},
        "execs": [],
        "created": [],
        "pip": [],
        "pip_checks": [],
        "pip_check_rc": 0,
        "publishes": [],
        "publish_rc": 0,
        "euid": 0,
    }
    monkeypatch.setattr(os, "environ", state["environ"])
    monkeypatch.setattr(os, "geteuid", lambda: state["euid"])
    monkeypatch.setattr(os, "execv", lambda path, argv: state["execs"].append((path, argv)))

    def fake_run(argv, check=False, **kwargs):
        if argv[1:3] == ["-m", "venv"]:
            _fake_venv(Path(argv[3]))
            state["created"].append(argv[3])
        elif argv[1:] == ["-m", "pip", "--version"]:
            state["pip_checks"].append(argv)
            stderr = "No module named pip\n" if state["pip_check_rc"] else ""
            return subprocess.CompletedProcess(argv, state["pip_check_rc"], "", stderr)
        elif argv[1:4] == ["-m", "pip", "install"]:
            state["pip"].append(argv)
        elif argv[0].endswith("/bin/theozolith") and argv[1:2] == ["build"]:
            state["publishes"].append(argv)
            return subprocess.CompletedProcess(argv, state["publish_rc"])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        shim, "build_distribution", lambda root, out: ("1.2.3+gtest", ["a.whl", "b.whl"])
    )
    return shim, opt, bin_dir, state


def test_main_managed_first_install_creates_and_reexecs(sandbox):
    """Pass one of `sudo python3 build.py` on a bare box: the managed venv
    is created and the shim re-executes itself with the venv's interpreter
    and the original argv."""
    shim, opt, bin_dir, state = sandbox
    assert shim.main([]) == 0
    assert state["created"] == [str(opt)]
    python = str(opt / "bin" / "python")
    assert state["execs"] == [(python, [python, str(shim.REPO_ROOT / "build.py")])]
    assert state["environ"][shim.REEXEC_MARKER] == "1"
    assert state["pip"] == [] and not bin_dir.exists()  # pass one never installs or links


def test_main_managed_second_pass_builds_installs_and_links(sandbox):
    """Pass two (now inside the venv): build via the shared implementation,
    pip-install the built wheels, and publish all three links."""
    shim, opt, bin_dir, state = sandbox
    _fake_venv(opt)
    _entry_points(shim, opt)  # what the real pip install would have produced
    shim.inside = lambda venv: True  # this interpreter "is" the venv's
    assert shim.main([]) == 0
    assert state["pip_checks"]  # `python -m pip` pre-flighted before the build
    (pip_argv,) = state["pip"]
    assert "--upgrade" in pip_argv
    assert [a for a in pip_argv if a.endswith(".whl")] == [
        str(shim.REPO_ROOT / "dist" / "a.whl"),
        str(shim.REPO_ROOT / "dist" / "b.whl"),
    ]
    for name in shim.LINKED_ENTRY_POINTS:
        assert os.readlink(bin_dir / name) == str(opt / "bin" / name)


def test_main_managed_rerun_reuses_the_venv_and_converges(sandbox):
    """A second `sudo python3 build.py` over a valid installation reuses
    the venv (no re-create) and republishes the same links — idempotent."""
    shim, opt, bin_dir, state = sandbox
    _fake_venv(opt)
    _entry_points(shim, opt)
    assert shim.main([]) == 0  # pass one: reuse, re-exec
    assert state["created"] == [] and len(state["execs"]) == 1
    shim.inside = lambda venv: True
    for _ in range(2):  # pass two, twice: the upgrade path converges
        assert shim.main([]) == 0
    for name in shim.LINKED_ENTRY_POINTS:
        assert os.readlink(bin_dir / name) == str(opt / "bin" / name)


def test_main_a_failing_pip_module_check_stops_before_any_build(sandbox):
    """The bin/pip wrapper exists, `python -m pip` is broken: the in-venv
    pass (which the direct `sudo /opt/theozolith/bin/python build.py` entry
    also lands on) refuses with remediation before any build, install, or
    link publication begins."""
    shim, opt, bin_dir, state = sandbox
    _fake_venv(opt)  # bin/pip wrapper present — and proves nothing
    _entry_points(shim, opt)
    shim.inside = lambda venv: True
    shim.build_distribution = _refuse  # the build must never be reached
    state["pip_check_rc"] = 1
    with pytest.raises(SystemExit, match="repair its pip module or delete the venv"):
        shim.main([])
    assert state["pip"] == [] and not bin_dir.exists()


def test_main_managed_requires_root(sandbox):
    shim, _, _, state = sandbox
    state["euid"] = 1000
    with pytest.raises(SystemExit, match=r"sudo.*--venv"):
        shim.main([])


def test_main_managed_second_pass_still_requires_root(sandbox):
    """The documented `sudo /opt/theozolith/bin/python build.py` spelling
    short-circuits ensure_environment — the root pre-flight must hold on
    the second pass too, before anything is built or written."""
    shim, opt, bin_dir, state = sandbox
    _fake_venv(opt)
    state["euid"] = 1000
    shim.inside = lambda venv: True
    with pytest.raises(SystemExit, match=r"sudo.*--venv"):
        shim.main([])
    assert state["pip"] == [] and not bin_dir.exists()


def test_main_unmanaged_venv_needs_no_root_and_links_nothing(sandbox, tmp_path):
    """The --venv escape hatch end to end: unprivileged, original argv
    preserved across the re-exec, and no /usr/local/bin links ever."""
    shim, _, bin_dir, state = sandbox
    state["euid"] = 1000  # never root — the escape hatch must not need it
    dev = tmp_path / "dev-venv"
    assert shim.main(["--venv", str(dev)]) == 0
    assert state["created"] == [str(dev)]
    python = str(dev / "bin" / "python")
    assert state["execs"] == [
        (python, [python, str(shim.REPO_ROOT / "build.py"), "--venv", str(dev)])
    ]
    shim.inside = lambda venv: True
    assert shim.main(["--venv", str(dev)]) == 0
    assert state["pip"] and not bin_dir.exists()  # installed, but no links anywhere


# -- main(): the chained publish (ADR-0051) ------------------------------------


def test_main_publishes_via_the_venv_cli_after_install(sandbox):
    """ADR-0051: pass two chains into the fleet publish — the JUST-INSTALLED
    venv CLI as a subprocess (never an in-process import: checkout modules
    and installed modules must not mix), reusing this run's dist/ wheels,
    with the uninitialized-box skip living inside the CLI
    (--if-initialized), never in the shim."""
    shim, opt, _bin_dir, state = sandbox
    _fake_venv(opt)
    _entry_points(shim, opt)
    shim.inside = lambda venv: True
    assert shim.main([]) == 0
    cli = str(opt / "bin" / "theozolith")
    assert state["publishes"] == [
        [
            cli,
            "build",
            "--source",
            str(shim.REPO_ROOT),
            "--dist",
            str(shim.REPO_ROOT / "dist"),
            "--if-initialized",
        ]
    ]
    assert state["pip"]  # the publish chains AFTER the install, never replaces it


def test_main_no_publish_skips_the_subprocess(sandbox):
    shim, opt, _bin_dir, state = sandbox
    _fake_venv(opt)
    _entry_points(shim, opt)
    shim.inside = lambda venv: True
    assert shim.main(["--no-publish"]) == 0
    assert state["publishes"] == []
    assert state["pip"]  # the install is unchanged — only the publish is deferred


def test_main_a_failed_publish_fails_the_bootstrap_loudly(sandbox, capsys):
    """An initialized box that fails to publish fails the whole command with
    the subprocess's exit code: the wheels ARE installed, but the operator
    must know the fleet was NOT updated (ADR-0051 — never a silent
    half-success)."""
    shim, opt, bin_dir, state = sandbox
    _fake_venv(opt)
    _entry_points(shim, opt)
    shim.inside = lambda venv: True
    state["publish_rc"] = 3
    assert shim.main([]) == 3
    err = capsys.readouterr().err
    assert "the fleet was NOT updated" in err
    assert "sudo theozolith build" in err
    for name in shim.LINKED_ENTRY_POINTS:  # the install itself completed first
        assert os.readlink(bin_dir / name) == str(opt / "bin" / name)


def test_main_unmanaged_venv_publishes_through_its_own_cli(sandbox, tmp_path):
    """The --venv escape hatch chains the same publish through ITS venv's
    CLI: an unconfigured dev box skips inside the CLI (TargetError ->
    --if-initialized), an env-configured one publishes — and still no
    /usr/local/bin links either way."""
    shim, _, bin_dir, state = sandbox
    state["euid"] = 1000  # never root — the escape hatch must not need it
    dev = tmp_path / "dev-venv"
    _fake_venv(dev)
    shim.inside = lambda venv: True
    assert shim.main(["--venv", str(dev)]) == 0
    (publish,) = state["publishes"]
    assert publish[0] == str(dev / "bin" / "theozolith")
    assert "--if-initialized" in publish and "--dist" in publish
    assert not bin_dir.exists()
