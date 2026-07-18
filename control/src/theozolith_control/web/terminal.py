"""The web terminal's PTY bridge (ADR-0018, hardened by ADR-0019).

The Control Node runs a config-supplied attach argv per Stack (example
template: ``["ssh", "{host}", "-t", "docker", "exec", "-it", "{container}",
"tmux", "attach"]``) on a local pseudo-terminal and relays it over a
websocket to the xterm.js frontend. The argv is trusted command structure
from the Config Repo; ``{host}`` and ``{container}`` are the only untrusted
inputs and substitute exclusively as complete arguments, after validation
against identifier whitelists strict enough to survive the SSH
remote-command boundary (every permitted character is shell-inert).

Wire protocol (one websocket): binary frames are raw terminal bytes in
both directions; text frames are JSON control messages, currently only
``{"resize": {"cols": C, "rows": R}}`` (applied via TIOCSWINSZ). Keepalive
is the server's websocket ping (uvicorn default, 20s).

Resource bounds (ADR-0019): PTY output buffers at most ``BUFFER_HIGH``
bytes per session — past the high-water mark the bridge stops reading the
master fd, so the kernel PTY buffer fills and the attach process blocks on
write (real backpressure, no unbounded queue); reads resume below
``BUFFER_LOW``. A client that cannot accept a frame within the stall
timeout is declared dead: the bridge terminates the attach process tree
(never the Run container — killing the local ssh/docker-exec client leaves
the tmux session inside the container running) and records the reason.

Every session is audit-logged as JSON lines (attach and detach records:
actor, timestamp, target, argv, detach reason) to a file under the Control
Node data dir — a file, not the database, because the control database is
a cache that may drop anything (ADR-0016) and an audit log may not.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import re
import signal
import struct
import termios
import time
from collections import deque
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

READ_CHUNK = 65_536
# Per-session PTY output budget: pause reading past HIGH, resume below LOW.
BUFFER_HIGH = 512 * 1024
BUFFER_LOW = 64 * 1024
# A websocket send that cannot complete within this window means the client
# is gone or wedged: kill the attach, keep the Run.
SEND_STALL_SECONDS = 30.0

# Both identifier whitelists are strictly shell-inert: no whitespace, no
# control characters, no shell syntax, and no leading ``-`` (a value can
# never read as an option), so a forged heartbeat value cannot alter the
# command structure even after SSH hands the remote command to a shell.
#
# Hostname: DNS labels (letters, digits, inner hyphens) joined by dots.
_HOST_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)
# Docker container names: [a-zA-Z0-9][a-zA-Z0-9_.-]*, capped.
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class AttachError(ValueError):
    """An attach target identifier failed validation."""


def valid_host(value: str) -> bool:
    return len(value) <= 253 and _HOST_RE.match(value) is not None


def valid_container(value: str) -> bool:
    return _CONTAINER_RE.match(value) is not None


def render_attach_argv(template: tuple[str, ...], *, host: str, container: str) -> list[str]:
    """Substitute the validated identifiers into the trusted argv template.

    Placeholders substitute only as complete elements (the Config Repo
    parser already rejects embedded forms; this renderer never touches any
    other element, so there is no format-string surface at all)."""
    if not valid_host(host):
        raise AttachError(f"invalid attach host {host!r}")
    if not valid_container(container):
        raise AttachError(f"invalid container name {container!r}")
    substitutions = {"{host}": host, "{container}": container}
    return [substitutions.get(element, element) for element in template]


def audit(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}, sort_keys=True
            )
            + "\n"
        )


def _resize(master: int, cols: int, rows: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PtyBridge:
    """One attach session: relays a PTY over an accepted websocket until
    either side hangs up, within a fixed output-buffer budget; kills the
    whole attach process group on the way out. ``run()`` returns the detach
    reason for the audit log."""

    def __init__(
        self,
        websocket: WebSocket,
        argv: list[str],
        *,
        stall_seconds: float = SEND_STALL_SECONDS,
        buffer_high: int = BUFFER_HIGH,
        buffer_low: int = BUFFER_LOW,
    ):
        self._websocket = websocket
        self._argv = argv
        self._stall = stall_seconds
        self._high = buffer_high
        self._low = buffer_low
        self._chunks: deque[bytes] = deque()
        self.buffered = 0
        self.max_buffered = 0  # observability for the budget test
        self.process: asyncio.subprocess.Process | None = None
        self._reading = False
        self._eof = False
        self._readable = asyncio.Event()

    # -- PTY read side, with high/low-water backpressure --------------------

    def _on_readable(self, master: int, loop: asyncio.AbstractEventLoop) -> None:
        try:
            data = os.read(master, READ_CHUNK)
        except OSError:
            data = b""
        if not data:  # EOF: the attach command ended
            self._eof = True
            self._pause_reading(master, loop)
            self._readable.set()
            return
        self._chunks.append(data)
        self.buffered += len(data)
        self.max_buffered = max(self.max_buffered, self.buffered)
        if self.buffered >= self._high:
            # Past the budget: stop draining the PTY. The kernel buffer
            # fills and the attach process blocks on write — bounded memory
            # here, real backpressure there.
            self._pause_reading(master, loop)
        self._readable.set()

    def _pause_reading(self, master: int, loop: asyncio.AbstractEventLoop) -> None:
        if self._reading:
            loop.remove_reader(master)
            self._reading = False

    def _resume_reading(self, master: int, loop: asyncio.AbstractEventLoop) -> None:
        if not self._reading and not self._eof:
            loop.add_reader(master, self._on_readable, master, loop)
            self._reading = True

    async def _pty_to_socket(self, master: int, loop: asyncio.AbstractEventLoop) -> str:
        while True:
            while not self._chunks:
                if self._eof:
                    return "process-exited"
                self._readable.clear()
                await self._readable.wait()
            data = self._chunks.popleft()
            self.buffered -= len(data)
            try:
                await asyncio.wait_for(self._websocket.send_bytes(data), timeout=self._stall)
            except TimeoutError:
                return "stalled"
            except (WebSocketDisconnect, RuntimeError):
                # RuntimeError is starlette's send-after-close.
                return "client-closed"
            if self.buffered <= self._low:
                self._resume_reading(master, loop)

    # -- socket read side ---------------------------------------------------

    async def _socket_to_pty(self, master: int) -> str:
        while True:
            try:
                message = await self._websocket.receive()
            except WebSocketDisconnect:
                return "client-closed"
            if message.get("type") == "websocket.disconnect":
                return "client-closed"
            if message.get("bytes") is not None:
                # Off the loop: a full PTY buffer (Ctrl-S, stalled remote)
                # blocks os.write, and a stuck terminal must never stall
                # every other connection on this server.
                await asyncio.to_thread(os.write, master, message["bytes"])
            elif message.get("text"):
                with contextlib.suppress(ValueError, TypeError):
                    control = json.loads(message["text"])
                    resize = control.get("resize") or {}
                    _resize(master, int(resize.get("cols", 80)), int(resize.get("rows", 24)))

    # -- the session --------------------------------------------------------

    async def run(self) -> str:
        master, slave = pty.openpty()
        try:
            process = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,  # its own process group: the kill unit
                close_fds=True,
            )
        finally:
            os.close(slave)
        self.process = process

        loop = asyncio.get_running_loop()
        self._resume_reading(master, loop)
        pumps = [
            asyncio.ensure_future(self._pty_to_socket(master, loop)),
            asyncio.ensure_future(self._socket_to_pty(master)),
        ]
        reason = "error"
        cancelled = False
        try:
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is None:
                    reason = task.result()
                elif isinstance(exc, WebSocketDisconnect):
                    reason = "client-closed"
                else:  # a broken transport must still detach cleanly
                    reason = f"error: {exc}"
                break
        except asyncio.CancelledError:
            # The server tore this websocket task down (client hang-up or
            # shutdown). Under level-based cancellation every further await
            # re-raises, so the cleanup below must not await.
            cancelled = True
            raise
        finally:
            for pump in pumps:
                pump.cancel()
            self._pause_reading(master, loop)
            with contextlib.suppress(OSError):
                os.close(master)
            # Kill ONLY the attach process tree (ssh / docker exec client).
            # The Run container and its tmux session are untouched.
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGHUP)
            if cancelled:
                # No grace window is possible without awaiting: escalate now.
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, signal.SIGKILL)
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=10)
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, signal.SIGKILL)
        return reason


async def bridge(websocket: WebSocket, argv: list[str], **bounds) -> str:
    """Relay one attach argv's PTY over an accepted websocket; returns the
    detach reason (``process-exited`` | ``client-closed`` | ``stalled``)."""
    return await PtyBridge(websocket, argv, **bounds).run()
