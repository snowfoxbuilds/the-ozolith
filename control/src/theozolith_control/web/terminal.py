"""The web terminal's PTY bridge (ADR-0018).

The Control Node runs a config-supplied attach command per Stack (default
template in the example configs: ``ssh {host} -t docker exec -it
{container} tmux attach``) on a local pseudo-terminal and relays it over a
websocket to the xterm.js frontend. No attach command configured = no
terminal for that Stack; attach targets are the live run containers
reported in heartbeats — no live container, no bridge.

Wire protocol (one websocket): binary frames are raw terminal bytes in
both directions; text frames are JSON control messages, currently only
``{"resize": {"cols": C, "rows": R}}`` (applied via TIOCSWINSZ). Keepalive
is the server's websocket ping (uvicorn default, 20s).

Every session is audit-logged as JSON lines (attach and detach records:
actor, timestamp, target, command) to a file under the Control Node data
dir — a file, not the database, because the control database is a cache
that may drop anything (ADR-0016) and an audit log may not.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import shlex
import signal
import struct
import termios
import time
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

READ_CHUNK = 65_536


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


async def bridge(websocket: WebSocket, command: str) -> None:
    """Relay one attach command's PTY over an accepted websocket until
    either side hangs up; kills the whole process group on the way out."""
    argv = shlex.split(command)
    master, slave = pty.openpty()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,  # its own process group: the kill unit
            close_fds=True,
        )
    finally:
        os.close(slave)

    loop = asyncio.get_running_loop()
    from_pty: asyncio.Queue[bytes] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(master, READ_CHUNK)
        except OSError:
            data = b""
        from_pty.put_nowait(data)

    loop.add_reader(master, on_readable)

    async def pty_to_socket() -> None:
        while True:
            data = await from_pty.get()
            if not data:  # EOF: the attach command ended
                break
            await websocket.send_bytes(data)

    async def socket_to_pty() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                os.write(master, message["bytes"])
            elif message.get("text"):
                with contextlib.suppress(ValueError, TypeError):
                    control = json.loads(message["text"])
                    resize = control.get("resize") or {}
                    _resize(master, int(resize.get("cols", 80)), int(resize.get("rows", 24)))

    pumps = [asyncio.ensure_future(pty_to_socket()), asyncio.ensure_future(socket_to_pty())]
    try:
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for pump in pumps:
            pump.cancel()
        loop.remove_reader(master)
        with contextlib.suppress(OSError):
            os.close(master)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGHUP)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=10)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
