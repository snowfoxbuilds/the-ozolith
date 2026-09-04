"""The driver-side relay supervisor (ADR-0057 items 8 and 10) driving a real
``python -m theozolith_worker.relay`` child: the layout it creates, the
loud pre-existing-entry failures, the credential's one route through the
pipe, the incremental ``gh_calls`` scan, the four terminations classified
from the exit report and status together with what the driver observed,
and the cleanup that never touches the sink."""

from __future__ import annotations

import os
import signal
import socket
import stat
import time
from pathlib import Path

import pytest
from theozolith_worker.relay import audit
from theozolith_worker.relay.__main__ import parse_args
from theozolith_worker.relay.audit import SINK_NAME, SPOOL_DIR, parse_records
from theozolith_worker.relay.reasons import Budgets
from theozolith_worker.relay.supervisor import (
    CONTAINER_SOCKET_PATH,
    SOCKET_NAME,
    RelayExit,
    RelayRun,
    RelayStartError,
)
from theozolith_worker.relay.upstream import Live, NoUpstream

CREDENTIAL = "ghp_supervisor-test-credential-9f3a2b1c"
SMALL = Budgets(head_read_seconds=1.0, body_read_seconds=1.0)


def call(socket_path: Path, raw: bytes, timeout: float = 5.0) -> bytes:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    conn.connect(str(socket_path))
    out = b""
    try:
        conn.sendall(raw)
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return out
            out += chunk
    finally:
        conn.close()


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def files_under(*roots: Path) -> list[Path]:
    return [p for root in roots for p in root.rglob("*") if p.is_file() and not p.is_symlink()]


ADMIN_READ = b"GET /orgs/x HTTP/1.1\r\nHost: api.github.com\r\n\r\n"
MUTATION = b"PUT /repos/o/r HTTP/1.1\r\nHost: api.github.com\r\n\r\n"
READ = b"GET /repos/o/r HTTP/1.1\r\nHost: api.github.com\r\n\r\n"


@pytest.fixture
def relays():
    """Every started run is stopped at teardown, whatever the test did."""
    started: list[RelayRun] = []
    yield started
    for run in started:
        run.stop(timeout=2.0)


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    job = tmp_path / "job"
    jobs = tmp_path / "jobs"
    job.mkdir()
    jobs.mkdir()
    return job, jobs


def start(relays, dirs, *, run_id="r1", upstream=None, budgets=SMALL, log=None) -> RelayRun:
    job, jobs = dirs
    run = RelayRun.start(
        job=job,
        jobs_dir=jobs,
        run_id=run_id,
        upstream=NoUpstream() if upstream is None else upstream,
        log=log if log is not None else (lambda line: None),
        budgets=budgets,
    )
    relays.append(run)
    return run


# -- start: layout and readiness ---------------------------------------------


def test_start_lays_out_relay_dir_sink_spool_and_socket(relays, dirs):
    job, jobs = dirs
    run = start(relays, dirs)
    assert run.relay_dir == jobs / audit.RELAY_PARENT / "r1"
    assert mode(run.relay_dir) == 0o700
    assert mode(run.relay_dir / SINK_NAME) == 0o600
    assert mode(run.relay_dir / SPOOL_DIR) == 0o700
    assert run.socket_path == job / SOCKET_NAME
    assert stat.S_ISSOCK(os.lstat(run.socket_path).st_mode)
    assert mode(run.socket_path) == 0o666
    assert CONTAINER_SOCKET_PATH == "/job/" + SOCKET_NAME
    # Ready means the child answers on the socket.
    reply = call(run.socket_path, ADMIN_READ)
    assert reply.startswith(b"HTTP/1.1 403 ") and b'"reason":"admin-read"' in reply
    assert run.poll() is None


def test_pre_existing_socket_path_entry_fails_loud_and_is_never_unlinked(relays, dirs):
    job, _ = dirs
    planted = job / SOCKET_NAME
    planted.write_text("planted")
    with pytest.raises(RelayStartError):
        start(relays, dirs)
    assert planted.read_text() == "planted"
    # The relay dir and sink were created before the check; the loud failure
    # leaves them for the operator, and a retry is a new run_id anyway.


def test_pre_existing_socket_path_symlink_fails_loud_and_is_never_unlinked(relays, dirs, tmp_path):
    job, _ = dirs
    target = tmp_path / "elsewhere"
    target.write_text("elsewhere")
    (job / SOCKET_NAME).symlink_to(target)
    with pytest.raises(RelayStartError):
        start(relays, dirs)
    assert (job / SOCKET_NAME).is_symlink()
    assert target.read_text() == "elsewhere"


def test_pre_existing_relay_dir_fails_loud(relays, dirs):
    job, jobs = dirs
    audit.create_relay_dir(jobs, "r1")
    with pytest.raises(RelayStartError):
        start(relays, dirs)
    assert not (job / SOCKET_NAME).exists()


def test_pre_existing_sink_fails_loud(relays, dirs, monkeypatch):
    job, jobs = dirs
    existing = audit.create_relay_dir(jobs, "pre")
    os.close(audit.open_sink(existing))
    monkeypatch.setattr(audit, "create_relay_dir", lambda jobs_dir, run_id: existing)
    with pytest.raises(RelayStartError):
        start(relays, dirs)
    assert (existing / SINK_NAME).exists()
    assert not (job / SOCKET_NAME).exists()


# -- the credential's one route ----------------------------------------------


def proc_bytes(pid: int, name: str) -> bytes:
    return Path(f"/proc/{pid}/{name}").read_bytes()


@pytest.mark.parametrize("source", ["credential", "credential_file"])
def test_credential_travels_only_through_the_pipe(relays, dirs, tmp_path, source):
    job, jobs = dirs
    if source == "credential":
        upstream = Live(credential=CREDENTIAL)
    else:
        token = tmp_path / "tok"
        token.write_text(CREDENTIAL + "\n")
        upstream = Live(credential_file=token)
    logs: list[str] = []
    run = start(relays, dirs, upstream=upstream, log=logs.append)
    secret = CREDENTIAL.encode()
    assert secret not in proc_bytes(run.pid, "cmdline")
    assert secret not in proc_bytes(run.pid, "environ")
    # Two requests refused before any upstream contact: the child is live,
    # holds the credential, and still writes nothing that contains it.
    assert b'"reason":"admin-read"' in call(run.socket_path, ADMIN_READ)
    assert b'"reason":"mutation"' in call(run.socket_path, MUTATION)
    exit_ = run.stop()
    assert exit_.termination == "clean"
    for path in files_under(job, jobs):
        assert secret not in path.read_bytes(), path
    assert not any(CREDENTIAL in line for line in logs)


def test_live_refuses_both_and_neither_credential_source(tmp_path):
    with pytest.raises(ValueError):
        Live()
    with pytest.raises(ValueError):
        Live(credential="x", credential_file=tmp_path / "tok")


# -- gh_calls ----------------------------------------------------------------


def test_gh_calls_counts_intent_records_incrementally(relays, dirs):
    run = start(relays, dirs)
    assert run.gh_calls == 0
    call(run.socket_path, READ)  # none mode: refused no-upstream, one intent
    call(run.socket_path, ADMIN_READ)
    call(run.socket_path, MUTATION)
    assert run.gh_calls == 3
    assert run.gh_calls == 3
    call(run.socket_path, b"\r\n")  # an empty request line: seen, refused, recorded
    assert run.gh_calls == 4
    call(run.socket_path, b"")  # nothing sent: no request line, no record
    assert run.gh_calls == 4
    exit_ = run.stop()
    assert exit_.termination == "clean"
    parsed = parse_records((run.relay_dir / SINK_NAME).read_bytes())
    assert parsed.counts_by_kind == {"intent": 4, "terminal": 1}
    assert run.gh_calls == 4  # the terminal record is not a call


# -- termination classification ---------------------------------------------


def assert_cleaned(run: RelayRun) -> None:
    assert not os.path.lexists(run.socket_path)
    assert list((run.relay_dir / SPOOL_DIR).iterdir()) == []
    assert (run.relay_dir / SINK_NAME).exists()


def test_clean_exit_after_the_driver_sends_sigterm(relays, dirs):
    run = start(relays, dirs)
    call(run.socket_path, READ)
    assert run.poll() is None
    exit_ = run.stop()
    assert exit_ == RelayExit(
        "clean", 0, {"event": "exit", "reason": "agent-exit", "audit": "ok"}, None
    )
    assert_cleaned(run)
    parsed = parse_records((run.relay_dir / SINK_NAME).read_bytes())
    assert parsed.terminal == audit.TERMINAL_PRESENT
    assert parsed.records[-1]["reason"] == "agent-exit"


def test_exhausted_is_read_from_the_exit_report_by_poll_then_stop(relays, dirs):
    run = start(relays, dirs, budgets=Budgets(connection_budget=3, head_read_seconds=1.0))
    for _ in range(3):
        call(run.socket_path, READ)
    exit_ = wait_for(run.poll)
    assert exit_.termination == "exhausted"
    assert exit_.exit_status == 0
    assert exit_.exit_report == {
        "event": "exit",
        "reason": "connection-budget-exhausted",
        "audit": "ok",
    }
    assert exit_.audit_failure is None
    # poll cleans nothing up; stop does, signals nothing, and agrees.
    assert not os.path.lexists(run.socket_path)  # the child's own unlink
    stopped = run.stop()
    assert stopped == exit_
    assert run.stop() is stopped
    assert_cleaned(run)
    parsed = parse_records((run.relay_dir / SINK_NAME).read_bytes())
    terminal = parsed.records[-1]
    assert terminal["kind"] == "terminal"
    assert terminal["reason"] == "connection-budget-exhausted"
    assert terminal["connection_budget_exhausted"] is True
    assert terminal["accepted"] == 3


def test_exhausted_first_observed_by_stop(relays, dirs):
    run = start(relays, dirs, budgets=Budgets(connection_budget=2, head_read_seconds=1.0))
    call(run.socket_path, READ)
    call(run.socket_path, READ)
    wait_for(lambda: not os.path.lexists(run.socket_path))
    exit_ = run.stop()
    assert exit_.termination == "exhausted"
    assert exit_.exit_report["reason"] == "connection-budget-exhausted"
    assert_cleaned(run)


def test_killed_when_the_bounded_wait_elapses(relays, dirs):
    run = start(relays, dirs)
    os.kill(run.pid, signal.SIGSTOP)
    started = time.monotonic()
    exit_ = run.stop(timeout=0.5)
    assert time.monotonic() - started < 5
    assert exit_.termination == "killed"
    assert exit_.exit_status == -signal.SIGKILL
    assert exit_.exit_report is None
    assert_cleaned(run)


def test_crashed_on_an_external_kill(relays, dirs):
    run = start(relays, dirs)
    os.kill(run.pid, signal.SIGKILL)
    exit_ = wait_for(run.poll)
    assert exit_.termination == "crashed"
    assert exit_.exit_status == -signal.SIGKILL
    assert exit_.exit_report is None
    assert exit_.audit_failure is None
    assert run.stop() == exit_
    assert_cleaned(run)


def test_crashed_on_an_unsolicited_agent_exit_report(relays, dirs):
    run = start(relays, dirs)
    call(run.socket_path, READ)
    os.kill(run.pid, signal.SIGTERM)  # not the driver's: the supervisor sent nothing
    exit_ = wait_for(run.poll)
    assert exit_.exit_status == 0
    assert exit_.exit_report["reason"] == "agent-exit"
    assert exit_.termination == "crashed"
    assert run.stop() == exit_
    assert_cleaned(run)


def test_stop_is_idempotent_and_never_restarts(relays, dirs):
    run = start(relays, dirs)
    planted = run.relay_dir / SPOOL_DIR / "response-leftover"
    planted.write_bytes(b"spooled")
    sink_before = (run.relay_dir / SINK_NAME).read_bytes()
    first = run.stop()
    second = run.stop()
    assert second is first
    assert not planted.exists()
    assert_cleaned(run)
    sink_after = (run.relay_dir / SINK_NAME).read_bytes()
    assert sink_after.startswith(sink_before)
    parsed = parse_records(sink_after)
    assert parsed.counts_by_kind.get("terminal") == 1
    with pytest.raises((FileNotFoundError, ConnectionRefusedError)):
        call(run.socket_path, READ)
    # A third call after the socket is gone still returns the same record.
    assert run.stop() is first
    assert run.poll() is first


def test_a_flood_and_an_injected_crash_take_only_the_child(relays, dirs):
    run = start(relays, dirs, budgets=Budgets(head_read_seconds=1.0, body_read_seconds=1.0))
    garbage = b"\x16\x03\x01PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
    for index in range(200):
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(1.0)
            conn.connect(str(run.socket_path))
            conn.sendall(ADMIN_READ if index % 2 else garbage)
            conn.close()
        except OSError:
            pass
        if index == 100:
            os.kill(run.pid, signal.SIGSEGV)
    exit_ = wait_for(run.poll)
    assert exit_.termination == "crashed"
    assert exit_.exit_status == -signal.SIGSEGV
    assert exit_.exit_report is None
    assert run.stop() == exit_
    assert_cleaned(run)
    # The supervisor process is the test itself: still here, still counting.
    assert run.gh_calls >= 0


def test_stderr_of_the_child_reaches_log(relays, dirs):
    logs: list[str] = []
    run = start(relays, dirs, log=logs.append)
    # Nothing is logged in a healthy run; the stderr pump simply ends.
    run.stop()
    assert all(isinstance(line, str) for line in logs)


# -- the child entry's argv ---------------------------------------------------


def test_child_argv_parses_the_documented_shape():
    live = parse_args(
        [
            "--listen-fd",
            "3",
            "--sink-fd",
            "4",
            "--run-id",
            "r1",
            "--credential-fd",
            "5",
            "--spool-dir",
            "/x/spool",
            "--budgets",
            '{"connection_budget": 7}',
        ]
    )
    assert (live.listen_fd, live.sink_fd, live.run_id) == (3, 4, "r1")
    assert live.credential_fd == 5 and live.no_upstream is False
    assert live.spool_dir == "/x/spool" and live.budgets == '{"connection_budget": 7}'
    none = parse_args(
        [
            "--listen-fd",
            "3",
            "--sink-fd",
            "4",
            "--run-id",
            "r1",
            "--no-upstream",
            "--spool-dir",
            "/s",
        ]
    )
    assert none.credential_fd is None and none.no_upstream is True and none.budgets is None


@pytest.mark.parametrize(
    "mode_args",
    [[], ["--credential-fd", "5", "--no-upstream"]],
    ids=["neither", "both"],
)
def test_child_argv_requires_exactly_one_upstream_mode(mode_args):
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--listen-fd",
                "3",
                "--sink-fd",
                "4",
                "--run-id",
                "r1",
                "--spool-dir",
                "/s",
                *mode_args,
            ]
        )
