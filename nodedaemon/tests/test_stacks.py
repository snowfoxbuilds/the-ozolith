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


def test_materialized_secrets_are_0600_in_the_given_dir(tmp_path: Path):
    paths = materialize_secrets(tmp_path / "secrets", {"a": "value-a", "b": "value-b"})
    assert paths["a"].read_text() == "value-a"
    assert (paths["a"].stat().st_mode & 0o777) == 0o600
    assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700
    stack = WireStack.from_wire(
        {"name": "w", "kind": "process", "command": "c", "secrets": {"TOKEN": "a"}}
    )
    assert secret_env_files(stack, tmp_path / "secrets") == {"TOKEN": str(paths["a"])}
