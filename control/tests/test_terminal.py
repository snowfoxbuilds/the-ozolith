"""The PTY bridge's hardening contracts (ADR-0019): identifier validation,
argv substitution, and the fast-producer/slow-client resource bounds —
exercised directly against PtyBridge with scriptable fake websockets."""

from __future__ import annotations

import asyncio

import pytest
from theozolith_control.web.terminal import (
    READ_CHUNK,
    AttachError,
    PtyBridge,
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


def test_out_of_range_resize_does_not_kill_the_session():
    """A hostile/fat-fingered resize (cols past ushort range) must clamp,
    not raise struct.error and tear the session down. Without the clamp,
    the socket pump dies with an 'error: ...' reason on the bad frame."""
    socket = ScriptedSocket([{"text": '{"resize": {"cols": 100000, "rows": -5}}'}])
    bridge = PtyBridge(socket, ["sh", "-c", "sleep 0.3"])
    reason = asyncio.run(bridge.run())
    # The client hangs up right after the resize; the pump survived it.
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
