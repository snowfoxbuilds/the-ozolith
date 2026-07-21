"""The PTY bridge's hardening contracts (ADR-0019): identifier validation,
argv substitution, and the fast-producer/slow-client resource bounds —
exercised directly against PtyBridge with scriptable fake websockets."""

from __future__ import annotations

import asyncio
import fcntl
import gc
import os
import pty
import struct
import termios

import pytest
from theozolith_control.web.terminal import (
    READ_CHUNK,
    RESIZE_MAX_COLS,
    RESIZE_MAX_ROWS,
    RESIZE_MIN_COLS,
    RESIZE_MIN_ROWS,
    AttachError,
    PtyBridge,
    _resize,
    render_attach_argv,
    valid_container,
    valid_host,
)

TEMPLATE = ("ssh", "{host}", "-t", "docker", "exec", "-it", "{container}", "tmux", "attach")


# -- identifier validation (acceptances 2-3) -------------------------------------


def test_substitution_only_as_complete_arguments():
    argv = render_attach_argv(TEMPLATE, host="box1.lan", container="ozolith-run-r1")
    expected = ["ssh", "box1.lan", "-t", "docker", "exec", "-it", "ozolith-run-r1"]
    assert argv == [*expected, "tmux", "attach"]


def test_hostile_container_names_are_rejected():
    hostile = [
        "run;rm -rf /",
        "run$(reboot)",
        "run`x`",
        "run|x",
        "run x",
        "run\ttab",
        "run\nnl",
        "run'x",
        'run"x',
        "-oProxyCommand=evil",
        "--privileged",
        ".hidden",  # must start alphanumeric (the Docker name rule)
        "",
        "x" * 200,
    ]
    for name in hostile:
        assert not valid_container(name)
        with pytest.raises(AttachError):
            render_attach_argv(TEMPLATE, host="box1", container=name)


def test_placeholder_sets_stay_in_lockstep():
    """The parser's rejection set and the renderer's substitution set share
    named constants — a new placeholder can't be added to one alone."""
    from theozolith_control.configrepo import (
        ATTACH_CONTAINER,
        ATTACH_HOST,
        ATTACH_PLACEHOLDERS,
    )

    argv = render_attach_argv(
        (ATTACH_HOST, ATTACH_CONTAINER), host="box1", container="ozolith-run-r1"
    )
    assert argv == ["box1", "ozolith-run-r1"]
    assert set(ATTACH_PLACEHOLDERS) == {ATTACH_HOST, ATTACH_CONTAINER}


def test_hostile_host_names_are_rejected():
    hostile = ["box1;x", "-box", "box 1", "box_1", "box$(x)", "box..lan", "box1-", "", "a" * 254]
    for host in hostile:
        assert not valid_host(host)
        with pytest.raises(AttachError):
            render_attach_argv(TEMPLATE, host=host, container="ozolith-run-r1")
    for host in ("box1", "box1.lan", "10.0.0.5", "a-b.c-d.internal"):
        assert valid_host(host)


# -- resource bounds (acceptances 10-11) -----------------------------------------


class StuckSocket:
    """A client that never accepts a frame and never sends one."""

    async def send_bytes(self, data: bytes) -> None:
        await asyncio.sleep(3600)

    async def receive(self) -> dict:
        await asyncio.Event().wait()
        return {}


class ScriptedSocket:
    """A client that sends a fixed list of frames, then hangs up."""

    def __init__(self, frames: list[dict]):
        self._frames = list(frames)
        self.received = bytearray()

    async def send_bytes(self, data: bytes) -> None:
        self.received += data
        await asyncio.sleep(0)

    async def receive(self) -> dict:
        if self._frames:
            await asyncio.sleep(0)
            return self._frames.pop(0)
        return {"type": "websocket.disconnect"}


class CollectingSocket:
    """A modest client: accepts every frame, yielding between them."""

    def __init__(self):
        self.received = bytearray()

    async def send_bytes(self, data: bytes) -> None:
        self.received += data
        await asyncio.sleep(0)

    async def receive(self) -> dict:
        await asyncio.Event().wait()
        return {}


def test_fast_producer_slow_client_stays_within_budget_and_is_terminated():
    """Acceptance 10 + 11: a flooding attach against a wedged client keeps
    buffering within the fixed budget, then the stall timeout kills only
    the attach process tree and reports the reason."""
    bridge = PtyBridge(
        StuckSocket(),
        ["yes", "budget-filler-line"],
        stall_seconds=0.5,
        buffer_high=32_768,
        buffer_low=4_096,
    )
    reason = asyncio.run(bridge.run())
    assert reason == "stalled"
    assert bridge.max_buffered <= 32_768 + READ_CHUNK  # the fixed budget held
    assert bridge.process is not None and bridge.process.returncode is not None


def test_backpressure_resumes_without_losing_output():
    """The high/low-water pause-resume cycle delivers every byte."""
    socket = CollectingSocket()
    total = 300_000
    bridge = PtyBridge(
        socket,
        ["sh", "-c", f"head -c {total} /dev/zero"],
        buffer_high=16_384,
        buffer_low=4_096,
    )
    reason = asyncio.run(bridge.run())
    assert reason == "process-exited"
    assert len(socket.received) == total
    assert bridge.max_buffered <= 16_384 + READ_CHUNK


# -- teardown/robustness ---------------------------------------------------------


def _winsize(fd: int) -> tuple[int, int]:
    """(rows, cols) actually applied to the PTY."""
    rows, cols, _, _ = struct.unpack(
        "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    )
    return rows, cols


def test_resize_clamps_to_the_operational_bounds():
    """M5: resize clamps to 20-500 cols and 5-300 rows — nothing outside
    those bounds ever reaches TIOCSWINSZ."""
    master, slave = pty.openpty()
    try:
        _resize(master, 100_000, 100_000)
        assert _winsize(master) == (RESIZE_MAX_ROWS, RESIZE_MAX_COLS)  # (300, 500)
        _resize(master, 1, -5)
        assert _winsize(master) == (RESIZE_MIN_ROWS, RESIZE_MIN_COLS)  # (5, 20)
        _resize(master, 120, 40)  # in-range values apply verbatim
        assert _winsize(master) == (40, 120)
    finally:
        os.close(master)
        os.close(slave)


def test_malformed_resize_frames_never_kill_the_session():
    """Malformed, negative, missing, non-numeric, and absurd dimensions
    are dropped or clamped — the pumps survive every one of them (without
    the guards, a bad frame kills the socket pump with an 'error' reason)."""
    frames = [
        {"text": '{"resize": {"cols": 100000, "rows": -5}}'},  # clamped
        {"text": '{"resize": {"cols": 1e999, "rows": 24}}'},  # JSON inf: OverflowError
        {"text": '{"resize": {"cols": NaN}}'},  # JSON nan: ValueError
        {"text": '{"resize": {"cols": "abc", "rows": null}}'},  # non-numeric
        {"text": '{"resize": {}}'},  # missing dimensions
        {"text": '{"resize": null}'},
        {"text": '{"resize": [1, 2]}'},  # wrong shapes
        {"text": "[1, 2]"},
        {"text": '"just a string"'},
        {"text": "not json at all"},
        {"text": "[" * 20_000},  # parser RecursionError from deep nesting
    ]
    socket = ScriptedSocket(frames)
    bridge = PtyBridge(socket, ["sh", "-c", "sleep 0.3"])
    reason = asyncio.run(bridge.run())
    # The client hangs up right after the frames; the pumps survived all.
    assert reason == "client-closed"
    assert not reason.startswith("error")


def test_spawn_failure_detaches_cleanly_without_leaking_the_master():
    """A missing attach binary returns a spawn-failed reason (master fd
    closed on the failure path, not leaked)."""
    socket = ScriptedSocket([])
    bridge = PtyBridge(socket, ["this-binary-does-not-exist-x7", "arg"])
    reason = asyncio.run(bridge.run())
    assert reason.startswith("spawn-failed")
    assert bridge.process is None


def test_repeated_spawn_failures_do_not_accumulate_fds():
    """The fd-leak regression proof (M5): both spawn-failure classes —
    OSError (missing binary) and ValueError (a NUL in argv) — leave the
    process fd table exactly where it started, run after run. Asserting
    the reason alone would pass with the master fd leaking."""
    failing_argvs = [["this-binary-does-not-exist-x7"], ["sh\0bad-argv"]]

    async def scenario() -> None:
        # Warm-up: absorb any lazily created event-loop/subprocess fds.
        for argv in failing_argvs:
            await PtyBridge(ScriptedSocket([]), argv).run()
        gc.collect()  # settle unrelated to-be-GC'd descriptors first
        baseline = set(os.listdir("/proc/self/fd"))
        for _ in range(10):
            for argv in failing_argvs:
                reason = await PtyBridge(ScriptedSocket([]), argv).run()
                assert reason.startswith("spawn-failed")
        # No NEW descriptors may exist (a leak adds 2 per iteration = 40
        # here); unrelated fds closing concurrently is fine.
        leaked = set(os.listdir("/proc/self/fd")) - baseline
        assert leaked == set()

    asyncio.run(scenario())
