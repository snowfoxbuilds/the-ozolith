"""tui.client (M9): every call is bearer JSON over HTTP against whatever
URL was resolved — exercised here against a REAL loopback socket, which is
exactly the SSH-forwarded shape (acceptance 2: the TUI works unmodified
when CONTROL_NODE_URL points at a forwarded port)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest
from theozolith_control.tui.client import ControlClient, ControlUnreachable


class _Recorder(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict]] = []
    responses: ClassVar[dict[str, tuple[int, dict]]] = {}

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(body) if body else None,
            }
        )
        status, answer = type(self).responses.get(self.path.split("?")[0], (200, {"ok": True}))
        payload = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = _handle

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture()
def server():
    _Recorder.requests = []
    _Recorder.responses = {}
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    thread.join(timeout=5)


def _client(server) -> ControlClient:
    # The forwarded-socket shape: a loopback URL on an ephemeral port, no
    # knowledge of the persisted control address anywhere.
    return ControlClient(f"http://127.0.0.1:{server.server_port}", "admin-token", None)


def test_reads_and_writes_carry_the_bearer_token(server):
    _Recorder.responses["/api/v1/state"] = (200, {"now": 1.0, "nodes": []})
    client = _client(server)
    assert client.state() == {"now": 1.0, "nodes": []}
    client.events(type="theozolith.error", since=100.0, limit=50)
    client.events(cursor="42")
    client.queue_command("box1", "recycle", "deck")
    client.release_quarantine("box1")
    client.put_secret("anthropic-api-key", "sk-value")

    assert all(r["authorization"] == "Bearer admin-token" for r in _Recorder.requests)
    by_path = [(r["method"], r["path"]) for r in _Recorder.requests]
    assert by_path == [
        ("GET", "/api/v1/state"),
        ("GET", "/api/v1/events?type=theozolith.error&since=100.0&limit=50"),
        ("GET", "/api/v1/events?cursor=42"),
        ("POST", "/api/v1/commands"),
        ("POST", "/api/v1/nodes/box1/quarantine/release"),
        ("PUT", "/api/v1/secrets/anthropic-api-key"),
    ]
    assert _Recorder.requests[3]["body"] == {"node": "box1", "verb": "recycle", "target": "deck"}
    assert _Recorder.requests[5]["body"] == {"value": "sk-value"}


def test_http_errors_fold_into_control_unreachable_with_the_class(server):
    _Recorder.responses["/api/v1/state"] = (401, {"detail": "admin token required"})
    with pytest.raises(ControlUnreachable) as caught:
        _client(server).state()
    assert caught.value.error_class == "HTTP 401"
    assert "admin token required" in caught.value.detail


def test_connection_refused_folds_into_control_unreachable():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # released on close: nothing listens here
    client = ControlClient(f"http://127.0.0.1:{port}", "admin-token", None, timeout=2.0)
    with pytest.raises(ControlUnreachable) as caught:
        client.state()
    assert caught.value.error_class == "ConnectionRefusedError"
    assert caught.value.dial_target == f"http://127.0.0.1:{port}"


def test_non_loopback_http_url_is_refused_before_dialing():
    """The forwarded-socket shape is loopback; a non-loopback http URL would
    put the admin token on a cleartext wire, so it is refused structurally
    (OZ-03) — folded into ControlUnreachable like any other failure."""
    with pytest.raises(ControlUnreachable) as caught:
        ControlClient("http://elsewhere.test", "admin-token", None, timeout=2.0).state()
    assert caught.value.error_class == "_BearerRefused"


def test_cross_origin_redirect_never_forwards_the_admin_token(server):
    """A 302 toward another origin is refused before the redirected request:
    a trap on the other port sees zero connections, zero Authorization."""
    import socket

    trap = socket.socket()
    trap.bind(("127.0.0.1", 0))
    trap.listen(4)
    trap.settimeout(0.1)
    trap_port = trap.getsockname()[1]
    seen = bytearray()
    stop = threading.Event()

    def _serve_trap():
        while not stop.is_set():
            try:
                conn, _ = trap.accept()
            except TimeoutError:
                continue
            try:
                while data := conn.recv(4096):
                    seen.extend(data)
            except (TimeoutError, OSError):
                pass
            finally:
                conn.close()

    trap_thread = threading.Thread(target=_serve_trap, daemon=True)
    trap_thread.start()
    _Recorder.responses["/api/v1/state"] = (200, {"ok": True})  # unused; redirect wins below

    class _Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{trap_port}/loot")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    redirect = HTTPServer(("127.0.0.1", 0), _Redirect)
    rthread = threading.Thread(target=redirect.serve_forever, daemon=True)
    rthread.start()
    try:
        client = ControlClient(f"http://127.0.0.1:{redirect.server_port}", "admin-token", None)
        with pytest.raises(ControlUnreachable) as caught:
            client.state()
        assert caught.value.error_class == "_BearerRefused"
        import time

        time.sleep(0.2)
        assert len(seen) == 0 and b"Authorization" not in seen
    finally:
        redirect.shutdown()
        rthread.join(timeout=5)
        stop.set()
        trap_thread.join(2)
        trap.close()
