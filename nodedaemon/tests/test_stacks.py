"""Process supervision: kill-the-tree against REAL processes, plus the
wire-model and materialization units."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from theozolith_nodedaemon.stacks import (
    DRIVER_LAUNCHER,
    LauncherMissing,
    ProcessSupervisor,
    SecretNameError,
    WireStack,
    materialize_secrets,
    resolve_launcher,
    secret_env_files,
    spec_fingerprint,
    validate_stored_secret_name,
)


class _RecordingPopen:
    """Records the argv/env the supervisor launches; never runs anything."""

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, env=None, start_new_session=False, **_):
        self.calls.append((list(argv), dict(env or {})))

        class _Proc:
            pid = 4321

            def poll(self):
                return None

        return _Proc()


def _installed_launcher(dir_path: Path) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    launcher = dir_path / DRIVER_LAUNCHER
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    return launcher


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def test_stop_kills_the_whole_process_tree():
    """ADR-0013 kill-the-tree: recycling a driver terminates the driver AND
    everything it spawned — verified with a real shell spawning children."""
    supervisor = ProcessSupervisor(log=lambda *_: None)
    supervisor.ensure_running(
        "worker", "bash -c 'sleep 300 & sleep 300 & wait'", {"PATH": os.environ["PATH"]}
    )
    process = supervisor._children["worker"].process
    pgid = os.getpgid(process.pid)
    time.sleep(0.2)  # let the shell fork its children
    assert supervisor.alive("worker")

    supervisor.stop("worker", grace_seconds=2.0)

    assert not supervisor.alive("worker")
    deadline = time.monotonic() + 5
    while _pgid_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pgid_alive(pgid), "descendants outlived the stop (kill-the-tree violated)"


def test_sigterm_resistant_children_get_sigkill():
    supervisor = ProcessSupervisor(log=lambda *_: None)
    supervisor.ensure_running(
        "stubborn", "bash -c 'trap \"\" TERM; sleep 300'", {"PATH": os.environ["PATH"]}
    )
    time.sleep(0.3)  # let the trap install
    supervisor.stop("stubborn", grace_seconds=0.5)
    assert not supervisor.alive("stubborn")


def test_changed_command_recycles_the_child():
    supervisor = ProcessSupervisor(log=lambda *_: None)
    supervisor.ensure_running("s", "sleep 300", {"PATH": os.environ["PATH"]})
    first = supervisor._children["s"].process
    supervisor.ensure_running("s", "sleep 301", {"PATH": os.environ["PATH"]})
    assert supervisor._children["s"].process.pid != first.pid
    assert first.poll() is not None
    supervisor.stop_all(grace_seconds=2.0)


def test_status_reports_running_then_exit_code():
    supervisor = ProcessSupervisor(log=lambda *_: None)
    supervisor.ensure_running("s", "sleep 300", {"PATH": os.environ["PATH"]})
    state, detail = supervisor.status("s")
    assert state == "running" and detail.startswith("pid ")
    supervisor.stop("s", grace_seconds=2.0)
    state, _ = supervisor.status("s")
    assert state == "stopped"


def test_wire_stack_roundtrip_defaults():
    stack = WireStack.from_wire({"name": "w", "kind": "process", "command": "run me"})
    assert stack.state == "running"
    assert stack.secrets == {} and stack.compose_files == ()


def test_materialized_secrets_are_0444_leaves_behind_a_0700_dir(tmp_path: Path):
    """The cross-UID delivery boundary (ADR-0015 amendment): the DIRECTORY is
    the host-side barrier — 0700, service-user-owned, non-traversable by any
    other host user — while each LEAF is exactly 0444, so a container running
    as an arbitrary non-root uid can read the files bind-mounted into it. The
    leaf mode is fchmod-pinned: even the harshest service umask must not
    quietly produce an owner-only leaf that a uid-1000 container cannot read."""
    old_umask = os.umask(0o077)
    try:
        paths = materialize_secrets(tmp_path / "secrets", {"a": "value-a", "b": "value-b"})
    finally:
        os.umask(old_umask)
    assert paths["a"].read_text() == "value-a"
    assert (paths["a"].stat().st_mode & 0o777) == 0o444
    assert (paths["b"].stat().st_mode & 0o777) == 0o444
    assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700
    stack = WireStack.from_wire(
        {"name": "w", "kind": "process", "command": "c", "secrets": {"TOKEN": "a"}}
    )
    # The wiring carries PATHS only — the value has no route out but the file.
    assert secret_env_files(stack, tmp_path / "secrets") == {"TOKEN": str(paths["a"])}


def test_materialize_replaces_read_only_leaves_atomically(tmp_path: Path):
    """Updates stay temp-file-and-replace atomic over the read-only 0444
    leaves — including over a read-only temp file a crashed prior pass left
    behind — and no temp file survives a pass."""
    secrets_dir = tmp_path / "secrets"
    materialize_secrets(secrets_dir, {"a": "one"})
    stale_tmp = secrets_dir / ".a.tmp"
    stale_tmp.write_text("stale")
    stale_tmp.chmod(0o444)  # what a crash between fchmod and replace leaves
    paths = materialize_secrets(secrets_dir, {"a": "two"})
    assert paths["a"].read_text() == "two"
    assert (paths["a"].stat().st_mode & 0o777) == 0o444
    assert not stale_tmp.exists()
    assert [p.name for p in secrets_dir.iterdir()] == ["a"]


def test_materialize_repairs_a_wrong_typed_target_from_the_boot_race(tmp_path: Path):
    """A boot race (#114) can leave the secret leaf as a DIRECTORY: dockerd,
    restarting a Stack container before the daemon materializes secrets on the
    freshly-wiped tmpfs, auto-vivifies the missing bind source as one. os.replace
    onto a directory raises IsADirectoryError forever, so the writer must self-
    heal — remove any wrong-typed target — rather than stay permanently wedged.
    A non-empty stray directory is cleared too (dockerd could have written into
    it), and the leaf comes back a real 0444 file with the current value."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    stray = secrets_dir / "a"
    stray.mkdir()  # dockerd's auto-vivified bind source
    (stray / "leftover").write_text("junk")  # even non-empty, it must be cleared

    paths = materialize_secrets(secrets_dir, {"a": "value-a"})

    assert paths["a"].is_file()
    assert paths["a"].read_text() == "value-a"
    assert (paths["a"].stat().st_mode & 0o777) == 0o444
    assert [p.name for p in secrets_dir.iterdir()] == ["a"]


def test_materialize_repairs_a_symlink_target_without_following_it(tmp_path: Path):
    """A wrong-typed target that is a symlink is removed as the link it is —
    never followed to overwrite or recurse into whatever it points at. The
    secret leaf must be a genuine regular file at the leaf path, not a link."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("do-not-touch")
    (secrets_dir / "a").symlink_to(elsewhere)

    paths = materialize_secrets(secrets_dir, {"a": "value-a"})

    assert not paths["a"].is_symlink()
    assert paths["a"].read_text() == "value-a"
    assert elsewhere.read_text() == "do-not-touch"  # the link target is untouched


# -- stored secret name safety (#114) --------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "github-implementer",
        "admin-token",
        "anthropic-api-key",
        "a",
        "UPPER_case.mixed-1",  # a dot mid-name is fine
        ".env",  # an ordinary dotfile leaf — a leading dot alone is legitimate
        ".hidden",  # likewise
        "backup.tmp",  # a trailing '.tmp' without a leading dot is not the temp namespace
        "registry:ghcr.io",  # a colon is a legal leaf char; registry semantics guard elsewhere
        "registry:localhost:5000",
    ],
)
def test_validate_stored_secret_name_accepts_safe_leaves(name):
    assert validate_stored_secret_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        ".",  # the dir itself
        "..",  # the parent
        "a/b",  # a path component
        "/etc/passwd",  # absolute
        "../../etc/passwd",  # traversal
        "sub/dir",  # a separator anywhere
        "back\\slash",  # a Windows-style separator too
        ".a.tmp",  # collides with the writer's '.<name>.tmp' temporary namespace
        ".token.tmp",  # same reserved namespace, a realistic secret name
        ".env.tmp",  # even a dotfile-shaped name is refused inside the temp namespace
        "with\x00nul",  # an embedded NUL
    ],
)
def test_validate_stored_secret_name_rejects_unsafe_leaves(name):
    with pytest.raises(SecretNameError):
        validate_stored_secret_name(name)


def test_no_valid_name_can_collide_with_another_secrets_temp_path():
    """The writer materializes every value through '.<name>.tmp'. That whole shape
    is refused as a stored name, so no valid secret can BE another valid secret's
    temp path — the collision safety holds for every batch processing order (#114).
    Proven mechanically: for each valid leaf, its temp path is itself rejected."""
    for name in ("env", "hidden", "token", ".env", ".hidden", "backup.tmp"):
        validate_stored_secret_name(name)  # a valid stored name
        temp_path_name = f".{name}.tmp"  # exactly what materialize_secrets writes through
        with pytest.raises(SecretNameError):
            validate_stored_secret_name(temp_path_name)


@pytest.mark.parametrize("bad", [".", "..", "../evil", "sub/dir", ".a.tmp", "with\x00nul"])
def test_materialize_rejects_unsafe_names_before_any_filesystem_mutation(tmp_path: Path, bad):
    """The whole batch is prevalidated before a single write (#114): a `.`/`..`
    or any other unsafe name aborts materialization before any mkdir, open,
    chmod, rmtree, or replace runs — so an existing sibling secret and the parent
    runtime directory are left exactly as they were, never partially mutated."""
    secrets_dir = tmp_path / "secrets"
    materialize_secrets(secrets_dir, {"keep": "sibling-value"})  # a pre-existing sibling
    before = (secrets_dir / "keep").read_text()
    sentinel = tmp_path / "sentinel"  # a file in the PARENT, to prove no traversal ran
    sentinel.write_text("do-not-touch")

    with pytest.raises(SecretNameError):
        materialize_secrets(secrets_dir, {"OK_ENV": "new", bad: "attacker"})

    assert (secrets_dir / "keep").read_text() == before  # sibling untouched
    assert sentinel.read_text() == "do-not-touch"  # parent untouched
    # The safe name from the aborted batch was never written (prevalidation is
    # whole-batch, not first-unsafe): no partial materialization.
    assert not (secrets_dir / "OK_ENV").exists()


def test_materialize_valid_name_updates_atomically_and_stays_0444(tmp_path: Path):
    """A valid ordinary name materializes, re-materializes (update) atomically,
    and the leaf is 0444 both times — the safe-name path is unchanged by the
    prevalidation guard."""
    secrets_dir = tmp_path / "secrets"
    first = materialize_secrets(secrets_dir, {"tok": "one"})
    assert first["tok"].read_text() == "one"
    assert (first["tok"].stat().st_mode & 0o777) == 0o444
    second = materialize_secrets(secrets_dir, {"tok": "two"})
    assert second["tok"].read_text() == "two"
    assert (second["tok"].stat().st_mode & 0o777) == 0o444


def test_materialize_dot_names_write_correctly_and_never_collide_by_order(tmp_path: Path):
    """Ordinary dot-prefixed leaves ('.env', '.hidden') are legitimate secret
    names and materialize as real 0444 leaves beside plain names — and because no
    valid name is ever another's '.<name>.tmp' temp path, the batch is safe in ANY
    processing order (#114). Materialized twice with the dict order reversed, every
    value is correct and no '.tmp' residue survives."""
    secrets_dir = tmp_path / "secrets"
    forward = materialize_secrets(secrets_dir, {".env": "E1", ".hidden": "H1", "plain": "P1"})
    assert forward[".env"].read_text() == "E1"
    assert forward[".hidden"].read_text() == "H1"
    assert forward["plain"].read_text() == "P1"
    assert (forward[".env"].stat().st_mode & 0o777) == 0o444
    assert (forward[".hidden"].stat().st_mode & 0o777) == 0o444

    reverse = materialize_secrets(secrets_dir, {"plain": "P2", ".hidden": "H2", ".env": "E2"})
    assert reverse[".env"].read_text() == "E2"
    assert reverse[".hidden"].read_text() == "H2"
    assert reverse["plain"].read_text() == "P2"
    # Exactly the three leaves, no leftover temp file from either pass.
    assert sorted(p.name for p in secrets_dir.iterdir()) == [".env", ".hidden", "plain"]


def test_changed_env_recycles_even_with_the_same_command():
    """ADR-0044 amendment: convergence keys on the effective spec (command AND
    env), so a worker-type change that lands only in env still recycles."""
    supervisor = ProcessSupervisor(log=lambda *_: None)
    supervisor.ensure_running("s", "sleep 300", {"PATH": os.environ["PATH"], "MODEL": "a"})
    first = supervisor._children["s"].process
    supervisor.ensure_running("s", "sleep 300", {"PATH": os.environ["PATH"], "MODEL": "b"})
    assert supervisor._children["s"].process.pid != first.pid
    assert first.poll() is not None
    supervisor.stop_all(grace_seconds=2.0)


def test_same_effective_spec_does_not_recycle_despite_key_reordering():
    supervisor = ProcessSupervisor(log=lambda *_: None)
    supervisor.ensure_running("s", "sleep 300", {"PATH": os.environ["PATH"], "MODEL": "a"})
    first = supervisor._children["s"].process
    supervisor.ensure_running("s", "sleep 300", {"MODEL": "a", "PATH": os.environ["PATH"]})
    assert supervisor._children["s"].process is first  # reordering is not a change
    supervisor.stop("s", grace_seconds=2.0)


def test_needs_restart_tracks_command_and_env_and_liveness():
    supervisor = ProcessSupervisor(log=lambda *_: None)
    supervisor.ensure_running("s", "sleep 300", {"PATH": os.environ["PATH"], "M": "1"})
    assert not supervisor.needs_restart("s", "sleep 300", {"M": "1", "PATH": os.environ["PATH"]})
    assert supervisor.needs_restart("s", "sleep 300", {"PATH": os.environ["PATH"], "M": "2"})
    assert supervisor.needs_restart("s", "sleep 301", {"PATH": os.environ["PATH"], "M": "1"})
    supervisor.stop("s", grace_seconds=2.0)
    assert supervisor.needs_restart("s", "sleep 300", {"PATH": os.environ["PATH"], "M": "1"})


def test_node_token_value_is_redacted_from_the_fingerprint():
    """A secret value must not enter a fingerprint: the node control-channel
    token is redacted, so a token rotation neither churns the process nor
    leaks the value into the digest inputs (ADR-0044 amendment)."""
    a = spec_fingerprint("cmd", {"THEOZOLITH_NODE_TOKEN": "tok-1", "X": "1"})
    b = spec_fingerprint("cmd", {"THEOZOLITH_NODE_TOKEN": "tok-2", "X": "1"})
    assert a == b
    # And a real spec change is still detected.
    assert spec_fingerprint("cmd", {"X": "1"}) != spec_fingerprint("cmd", {"X": "2"})


# -- the Driver launcher resolves to an absolute path (ADR-0020/0041) ------------


def test_driver_launcher_resolves_to_the_venv_absolute_path(tmp_path: Path):
    """A `theozolith-driver <ref>` command launches from the launcher_dir's
    absolute path (systemd's default PATH never finds the console script);
    the ref and any flags ride through untouched."""
    launcher_dir = tmp_path / "bin"
    _installed_launcher(launcher_dir)
    popen = _RecordingPopen()
    supervisor = ProcessSupervisor(popen=popen, log=lambda *_: None, launcher_dir=launcher_dir)
    supervisor.ensure_running(
        "impl", "theozolith-driver builtin:implementer --loop", {"THEOZOLITH_RUN_IMAGE": "x:1"}
    )
    (argv, env) = popen.calls[0]
    assert argv == [str(launcher_dir / "theozolith-driver"), "builtin:implementer", "--loop"]
    assert env["THEOZOLITH_RUN_IMAGE"] == "x:1"  # env still flows through


def test_default_launcher_dir_is_the_venv_not_the_symlinked_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The DEFAULT launcher_dir (no injection) is the venv bin/, derived from
    sys.prefix — never the directory sys.executable's symlink resolves to.
    python3 -m venv makes bin/python a symlink to the system interpreter, so
    Path(sys.executable).resolve().parent lands in /usr/bin, where no launcher
    is installed (#94). Builds that real on-disk shape and asserts the default
    still resolves the launcher inside the venv."""
    venv = tmp_path / "venv"
    venv_bin = venv / "bin"
    _installed_launcher(venv_bin)  # <venv>/bin/theozolith-driver, executable
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.0\n")
    system_bin = tmp_path / "usr" / "bin"  # the interpreter lives OUTSIDE the venv
    system_bin.mkdir(parents=True)
    system_python = system_bin / "python3.12"
    system_python.write_text("")
    venv_python = venv_bin / "python"
    venv_python.symlink_to(system_python)  # exactly what `python -m venv` does

    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "executable", str(venv_python))
    # Guard: the symlink really does escape the venv, so this test would pass
    # trivially against the old .resolve()-based default only if that trap
    # were absent — it is not.
    assert Path(sys.executable).resolve().parent == system_bin != venv_bin

    supervisor = ProcessSupervisor(log=lambda *_: None)  # no launcher_dir
    argv = supervisor.resolve_command("theozolith-driver builtin:implementer --loop")
    assert argv == [str(venv_bin / "theozolith-driver"), "builtin:implementer", "--loop"]


def test_non_launcher_command_keeps_path_semantics(tmp_path: Path):
    """A generic process Stack is launched by bare name — resolve_launcher
    only ever rewrites the DRIVER_LAUNCHER argv[0]."""
    popen = _RecordingPopen()
    supervisor = ProcessSupervisor(popen=popen, log=lambda *_: None, launcher_dir=tmp_path / "bin")
    supervisor.ensure_running("batch", "my-batch --run", {})
    assert popen.calls[0][0] == ["my-batch", "--run"]


def test_fingerprint_uses_the_logical_command_not_the_resolved_path(tmp_path: Path):
    """The stored fingerprint is the LOGICAL PATH-relative command, so the
    resolved absolute launcher path never enters it — a driver child started
    under one launcher_dir does not need a restart just because the launcher
    lives elsewhere (a venv move must not churn a running Driver)."""
    launcher_dir = tmp_path / "bin"
    _installed_launcher(launcher_dir)
    popen = _RecordingPopen()
    command = "theozolith-driver builtin:implementer"
    env = {"THEOZOLITH_RUN_IMAGE": "x:1"}
    supervisor = ProcessSupervisor(popen=popen, log=lambda *_: None, launcher_dir=launcher_dir)
    supervisor.ensure_running("impl", command, env)
    assert supervisor._children["impl"].fingerprint == spec_fingerprint(command, env)
    assert not supervisor.needs_restart("impl", command, env)


def test_missing_launcher_raises_and_never_spawns(tmp_path: Path):
    """An absent launcher fails closed: LauncherMissing names the launcher
    path and the venv dir, and nothing is spawned."""
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()  # empty: no theozolith-driver inside
    popen = _RecordingPopen()
    supervisor = ProcessSupervisor(popen=popen, log=lambda *_: None, launcher_dir=launcher_dir)
    with pytest.raises(LauncherMissing) as excinfo:
        supervisor.ensure_running("impl", "theozolith-driver builtin:implementer", {})
    assert str(launcher_dir / "theozolith-driver") in str(excinfo.value)
    assert str(launcher_dir.parent) in str(excinfo.value)  # the venv dir is named
    assert popen.calls == []


def test_a_non_executable_launcher_is_launcher_missing(tmp_path: Path):
    launcher_dir = tmp_path / "bin"
    launcher = _installed_launcher(launcher_dir)
    launcher.chmod(0o644)  # present but not executable
    supervisor = ProcessSupervisor(log=lambda *_: None, launcher_dir=launcher_dir)
    with pytest.raises(LauncherMissing):
        supervisor.ensure_running("impl", "theozolith-driver builtin:implementer", {})


def test_a_missing_launcher_never_stops_a_live_child(tmp_path: Path):
    """The resolution runs BEFORE any teardown, so a launcher that vanishes
    while a child is live at the old spec leaves the running instance alone —
    a spec change that cannot launch never tears down what works."""
    launcher_dir = tmp_path / "bin"
    _installed_launcher(launcher_dir)
    popen = _RecordingPopen()
    supervisor = ProcessSupervisor(popen=popen, log=lambda *_: None, launcher_dir=launcher_dir)
    supervisor.ensure_running("impl", "theozolith-driver builtin:implementer", {"V": "1"})
    live = supervisor._children["impl"].process
    assert supervisor.alive("impl")
    (launcher_dir / DRIVER_LAUNCHER).unlink()  # launcher disappears
    with pytest.raises(LauncherMissing):
        # A changed effective spec would normally recycle the child.
        supervisor.ensure_running("impl", "theozolith-driver builtin:implementer", {"V": "2"})
    assert supervisor.alive("impl")  # the live child was never stopped
    assert supervisor._children["impl"].process is live
    assert len(popen.calls) == 1  # only the original launch ever happened


def test_resolve_launcher_returns_empty_argv_untouched(tmp_path: Path):
    assert resolve_launcher([], tmp_path) == []
