"""Process supervision: kill-the-tree against REAL processes, plus the
wire-model and materialization units."""

from __future__ import annotations

import os
import time
from pathlib import Path

from theozolith_nodedaemon.stacks import (
    ProcessSupervisor,
    WireStack,
    materialize_secrets,
    secret_env_files,
)


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
    from theozolith_nodedaemon.stacks import spec_fingerprint

    a = spec_fingerprint("cmd", {"THEOZOLITH_NODE_TOKEN": "tok-1", "X": "1"})
    b = spec_fingerprint("cmd", {"THEOZOLITH_NODE_TOKEN": "tok-2", "X": "1"})
    assert a == b
    # And a real spec change is still detected.
    assert spec_fingerprint("cmd", {"X": "1"}) != spec_fingerprint("cmd", {"X": "2"})
