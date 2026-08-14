"""OZ-03: the control-side bearer transport keeps the admin/node token off a
plaintext non-loopback wire and off any cross-origin or downgrade redirect —
the same invariants the node daemon's ControlClient carries, over live
sockets. Plain http is allowed only to a loopback address (local/dev), which
is what the CLI/status/TUI tests use and where the token never reaches a wire.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest
from theozolith_control import bearerhttp


class _Trap:
    """An instrumented TCP listener: records every connection and byte, so a
    leaked redirect hop is provable (zero bytes, zero Authorization)."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        self.data = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            self.connections += 1
            conn.settimeout(0.2)
            try:
                while data := conn.recv(4096):
                    self.data += data
            except (TimeoutError, OSError):
                pass
            finally:
                conn.close()

    def stop(self):
        self._stop.set()
        self._thread.join(2)
        self._sock.close()


class _Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _drain(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _answer(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


def _server(handler: type[_Quiet]):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def _stop(server, thread) -> None:
    server.shutdown()
    thread.join(2)
    server.server_close()


def _bearer(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        method="POST",
        data=b"{}",
        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
    )


def test_non_loopback_http_target_is_refused_before_any_dial():
    """A plain-http URL to a non-loopback host never leaves the process: the
    token would ride the wire in cleartext, so the transport refuses it
    structurally — no connection attempted."""
    with pytest.raises(bearerhttp.BearerTransportError, match="must ride https"):
        bearerhttp.open_bearer(_bearer("http://198.51.100.9/api"), ca=None, timeout=1)


def test_cross_origin_redirect_gets_zero_bytes_at_the_trap():
    """A control endpoint that answers 302 toward another origin: the hop is
    refused BEFORE the redirected request, so the trap on the other port sees
    zero connections, zero bytes, zero Authorization."""
    trap = _Trap()

    class Handler(_Quiet):
        def do_POST(self):
            self._drain()
            self._answer(302, {"Location": f"http://127.0.0.1:{trap.port}/loot"})

    server, thread, port = _server(Handler)
    try:
        with pytest.raises(bearerhttp.BearerTransportError, match="leaves the bearer origin"):
            bearerhttp.open_bearer(_bearer(f"http://127.0.0.1:{port}/api"), ca=None, timeout=2)
        import time

        time.sleep(0.2)  # anything in flight would have landed
        assert trap.connections == 0
        assert b"Authorization" not in trap.data
    finally:
        _stop(server, thread)
        trap.stop()


def test_same_origin_redirect_is_followed_with_method_and_body():
    """The policy refuses hops that LEAVE the origin, not redirects per se: a
    same-origin relocation is followed, method and body intact."""

    class Handler(_Quiet):
        def do_POST(self):
            body = self._drain()
            if self.path == "/api":
                self._answer(307, {"Location": "/api/moved"})
            else:
                self._answer(
                    200, {"Content-Type": "application/json"}, json.dumps(json.loads(body)).encode()
                )

    server, thread, port = _server(Handler)
    try:
        status, raw = bearerhttp.open_bearer(
            urllib.request.Request(
                f"http://127.0.0.1:{port}/api",
                method="POST",
                data=b'{"ok": true}',
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            ),
            ca=None,
            timeout=2,
        )
        assert status == 200
        assert json.loads(raw) == {"ok": True}
    finally:
        _stop(server, thread)


def test_loopback_http_happy_path_returns_body():
    class Handler(_Quiet):
        def do_POST(self):
            self._drain()
            self._answer(200, {"Content-Type": "application/json"}, b'{"pong": 1}')

    server, thread, port = _server(Handler)
    try:
        status, raw = bearerhttp.open_bearer(
            _bearer(f"http://127.0.0.1:{port}/api"), ca=None, timeout=2
        )
        assert status == 200 and json.loads(raw) == {"pong": 1}
    finally:
        _stop(server, thread)


def test_http_error_is_reraised_for_the_caller_to_map():
    class Handler(_Quiet):
        def do_POST(self):
            self._drain()
            self._answer(404, {}, b"nope")

    server, thread, port = _server(Handler)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            bearerhttp.open_bearer(_bearer(f"http://127.0.0.1:{port}/api"), ca=None, timeout=2)
        assert exc.value.code == 404
    finally:
        _stop(server, thread)


def test_origin_parse_is_exact_and_distinguishes_scheme():
    # A prefix trick must not pass for https; a downgrade is a different origin.
    assert bearerhttp._origin("httpsevil://h/x") is None
    assert bearerhttp._origin("https://h:8443/x") != bearerhttp._origin("http://h:8443/x")
    assert bearerhttp._origin("https://[::1/x") is None  # unmatched bracket → None, not a raise
    assert bearerhttp._is_loopback("127.0.0.1") and bearerhttp._is_loopback("localhost")
    assert not bearerhttp._is_loopback("198.51.100.9")
