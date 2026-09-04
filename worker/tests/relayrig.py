"""Shared rig for the relay transport tests: a scriptable fake upstream on
loopback reached through ``connection_factory``, an in-process ``serve``
harness over a socket in ``tmp_path`` with a scriptable audit sink, and
byte-level client helpers. Everything the relay must never trust is
produced here on purpose — hostile ``Location`` values, truncated bodies,
stalls — so the tests state the outcome the relay must produce."""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from theozolith_worker.relay import audit
from theozolith_worker.relay.audit import AuditSink, ParseResult, parse_records
from theozolith_worker.relay.reasons import DEFAULT_BUDGETS, Budgets
from theozolith_worker.relay.server import serve
from theozolith_worker.relay.upstream import UpstreamClient

CREDENTIAL = "ghp_relay-test-credential-0123456789"


@dataclass
class Response:
    """One scripted upstream answer. ``raw`` is sent verbatim in place of
    everything else; ``stall_after``/``close_after`` cut the body short by
    sleeping or closing once that many body bytes are out."""

    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    chunked: bool = False
    declared_length: int | None = None
    stall_after: int | None = None
    stall_seconds: float = 0.0
    close_after: int | None = None
    raw: bytes | None = None


@dataclass(frozen=True)
class Seen:
    """One request as the fake upstream received it."""

    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None


Route = Response | Callable[[Seen], Response]


class FakeUpstream:
    """A loopback ``http.server`` the client reaches through its
    ``connection_factory``; the origin pin still runs on the relay side, only
    the socket target differs."""

    def __init__(self):
        rig = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                seen = Seen(self.command, self.path, tuple(self.headers.items()), body)
                with rig._lock:
                    rig.requests.append(seen)
                    route = rig.routes.get(self.path)
                    if route is None:
                        route = rig.routes.get(self.path.split("?", 1)[0])
                response = route(seen) if callable(route) else route
                if response is None:
                    response = Response(404, [("Content-Type", "text/plain")], b"not found")
                rig._write(self, seen, response)

            do_GET = do_HEAD = do_POST = do_PUT = _serve

        self._lock = threading.Lock()
        self.routes: dict[str, Route] = {}
        self.requests: list[Seen] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def _write(handler: BaseHTTPRequestHandler, seen: Seen, response: Response) -> None:
        out = handler.wfile
        if response.raw is not None:
            out.write(response.raw)
            out.flush()
            handler.close_connection = True
            return
        lines = [f"HTTP/1.1 {response.status} X"]
        lines += [f"{name}: {value}" for name, value in response.headers]
        if response.chunked:
            lines.append("Transfer-Encoding: chunked")
        else:
            length = (
                len(response.body) if response.declared_length is None else response.declared_length
            )
            lines.append(f"Content-Length: {length}")
        lines.append("Connection: close")
        out.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        out.flush()
        handler.close_connection = True
        if seen.method == "HEAD":
            return
        body = response.body
        if response.chunked:
            body = _chunk(body)
        cut = None
        if response.stall_after is not None:
            cut = response.stall_after
        if response.close_after is not None:
            cut = response.close_after if cut is None else min(cut, response.close_after)
        if cut is None:
            out.write(body)
            out.flush()
            return
        out.write(body[:cut])
        out.flush()
        if response.stall_after is not None and cut == response.stall_after:
            time.sleep(response.stall_seconds)
            out.write(body[cut:])
            out.flush()

    def start(self) -> FakeUpstream:
        self._thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def factory(self, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def route(self, target: str, response: Route) -> None:
        with self._lock:
            self.routes[target] = response

    def targets(self) -> list[str]:
        with self._lock:
            return [seen.target for seen in self.requests]

    def client(
        self,
        budgets: Budgets,
        spool_dir: Path,
        *,
        credential: str | None = CREDENTIAL,
        **kwargs,
    ) -> UpstreamClient:
        return UpstreamClient(
            credential, budgets, spool_dir, connection_factory=self.factory, **kwargs
        )

    def clients(self, budgets: Budgets, **kwargs) -> Callable[[Path], UpstreamClient]:
        """The ``ServerRig`` upstream argument: a client per spool dir."""
        return lambda spool_dir: self.client(budgets, spool_dir, **kwargs)


def _chunk(body: bytes, size: int = 1024) -> bytes:
    out = bytearray()
    for start in range(0, len(body), size):
        piece = body[start : start + size]
        out += f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
    out += b"0\r\n\r\n"
    return bytes(out)


def scripted_sink(
    fd: int,
    budgets: Budgets,
    *,
    fail_write: Callable[[bytes], bool] | None = None,
    fail_sync: Callable[[bytes], bool] | None = None,
    sync: bool = True,
) -> AuditSink:
    """An ``AuditSink`` whose write or ``fdatasync`` fails on the line a
    predicate selects; ``sync=False`` skips the real ``fdatasync`` for
    volume tests that would otherwise spend seconds in the disk."""
    last: list[bytes] = [b""]

    def write(target: int, line: bytes) -> int:
        last[0] = bytes(line)
        if fail_write is not None and fail_write(last[0]):
            raise OSError(28, "No space left on device")
        return os.write(target, line)

    def fdatasync(target: int) -> None:
        if fail_sync is not None and fail_sync(last[0]):
            raise OSError(5, "Input/output error")
        if sync:
            os.fdatasync(target)

    return AuditSink(fd, budgets, _write=write, _fdatasync=fdatasync)


def line_kind(line: bytes) -> str:
    return json.loads(line)["kind"]


def nth(kind: str, occurrence: int = 1) -> Callable[[bytes], bool]:
    """A predicate selecting the ``occurrence``-th record of ``kind``."""
    counter = {"n": 0}

    def select(line: bytes) -> bool:
        if line_kind(line) != kind:
            return False
        counter["n"] += 1
        return counter["n"] == occurrence

    return select


def hop_record(hop: int) -> Callable[[bytes], bool]:
    def select(line: bytes) -> bool:
        record = json.loads(line)
        return record["kind"] == "redirect-intent" and record["hop"] == hop

    return select


class ServerRig:
    """``serve`` in a thread over a socket under ``tmp_path``, with the
    relay dir and sink laid out as the supervisor lays them out."""

    def __init__(
        self,
        root: Path,
        *,
        upstream: Callable[[Path], UpstreamClient] | None,
        budgets: Budgets = DEFAULT_BUDGETS,
        sink_factory: Callable[[int, Budgets], AuditSink] | None = None,
        hooks=None,
        run_id: str = "run-1",
    ):
        self.root = root
        self.budgets = budgets
        self.job = root / "job"
        self.job.mkdir(exist_ok=True)
        (root / "jobs").mkdir(exist_ok=True)
        self.relay_dir = audit.create_relay_dir(root / "jobs", run_id)
        self.spool_dir = self.relay_dir / audit.SPOOL_DIR
        self.spool_dir.mkdir(mode=0o700)
        self.upstream = upstream(self.spool_dir) if upstream is not None else None
        self.sink_path = self.relay_dir / audit.SINK_NAME
        self.sink_fd = audit.open_sink(self.relay_dir)
        self.sink = sink_factory(self.sink_fd, budgets) if sink_factory else None
        self.hooks = hooks
        self.run_id = run_id
        self.socket_path = self.job / "gh.sock"
        self.report = io.StringIO()
        self.logs: list[str] = []
        self.agent_exit = threading.Event()
        self.exit_code: int | None = None
        self.error: BaseException | None = None
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(64)
        self.listen_fd = listener.detach()
        self._thread = threading.Thread(target=self._run, daemon=True, name="serve")

    def _run(self) -> None:
        try:
            self.exit_code = serve(
                self.listen_fd,
                self.sink_fd,
                upstream=self.upstream,
                budgets=self.budgets,
                report=self.report,
                run_id=self.run_id,
                log=self.logs.append,
                sink=self.sink,
                hooks=self.hooks,
                agent_exit=self.agent_exit,
            )
        except BaseException as exc:  # surfaced to the test, never swallowed
            self.error = exc

    def start(self) -> ServerRig:
        self._thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if '"ready"' in self.report.getvalue():
                return self
            if not self._thread.is_alive():
                break
            time.sleep(0.01)
        raise AssertionError(
            f"relay never reported ready: {self.report.getvalue()!r} {self.error!r}"
        )

    def stop(self, timeout: float = 15.0) -> int:
        """Agent exit, then the shutdown sequence to completion."""
        self.agent_exit.set()
        return self.join(timeout)

    def join(self, timeout: float = 15.0) -> int:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "serve did not return"
        if self.error is not None:
            raise self.error
        assert self.exit_code is not None
        return self.exit_code

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.report.getvalue().splitlines() if line]

    def records(self) -> ParseResult:
        return parse_records(self.sink_path.read_bytes())

    def sink_bytes(self) -> bytes:
        return self.sink_path.read_bytes()

    def connect(self, timeout: float = 10.0) -> socket.socket:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        conn.connect(str(self.socket_path))
        return conn

    def call(self, raw: bytes, timeout: float = 10.0, *, half_close: bool = True) -> bytes:
        """Send ``raw``, signal end-of-input unless told not to, and read
        the whole reply: what a client that has said everything looks like."""
        conn = self.connect(timeout)
        try:
            conn.sendall(raw)
            if half_close:
                conn.shutdown(socket.SHUT_WR)
            return read_all(conn)
        finally:
            conn.close()

    def request(self, raw: bytes, timeout: float = 10.0, *, half_close: bool = True) -> Reply:
        return parse_reply(self.call(raw, timeout, half_close=half_close))


def read_all(conn: socket.socket) -> bytes:
    out = b""
    while True:
        try:
            chunk = conn.recv(65536)
        except (TimeoutError, ConnectionError):
            return out
        if not chunk:
            return out
        out += chunk


@dataclass(frozen=True)
class Reply:
    status: int | None
    headers: tuple[tuple[str, str], ...]
    body: bytes
    raw: bytes

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def json(self) -> dict:
        return json.loads(self.body)

    @property
    def reason(self) -> str | None:
        try:
            return self.json().get("reason")
        except ValueError:
            return None


def parse_reply(raw: bytes) -> Reply:
    """A minimal HTTP/1.1 response split; ``status=None`` for no response."""
    if not raw:
        return Reply(None, (), b"", raw)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = []
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers.append((name.decode("latin-1"), value.strip().decode("latin-1")))
    return Reply(status, tuple(headers), body, raw)


def get(target: str, *headers: str) -> bytes:
    extra = "".join(f"{header}\r\n" for header in headers)
    return f"GET {target} HTTP/1.1\r\nHost: api.github.com\r\n{extra}\r\n".encode()


def head(target: str) -> bytes:
    return f"HEAD {target} HTTP/1.1\r\nHost: api.github.com\r\n\r\n".encode()


def graphql(query: str, variables: dict | None = None) -> bytes:
    document: dict = {"query": query}
    if variables is not None:
        document["variables"] = variables
    body = json.dumps(document).encode()
    return (
        b"POST /graphql HTTP/1.1\r\nHost: api.github.com\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


def records_for(result: ParseResult, seq: int) -> list[dict]:
    return [record for record in result.records if record.get("seq") == seq]


def kinds_for(result: ParseResult, seq: int) -> list[str]:
    return [record["kind"] for record in records_for(result, seq)]
