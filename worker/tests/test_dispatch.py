"""The drivers' dispatch client (ADR-0017): one endpoint, clean pauses."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from theozolith_worker import events
from theozolith_worker.dispatch import DispatchClient


@pytest.fixture
def control_node():
    """A tiny Control Node answering dispatch requests from a scripted queue
    of (status, body) pairs."""
    answers: list[tuple[int, dict]] = []
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/api/v1/dispatch"
            length = int(self.headers.get("Content-Length", "0"))
            seen.append(json.loads(self.rfile.read(length)))
            status, payload = answers.pop(0) if answers else (200, {})
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", answers, seen
    finally:
        server.shutdown()


def test_request_work_returns_the_granted_issue(control_node):
    url, answers, seen = control_node
    answers.append((200, {"issue": {"number": 7, "title": "T", "body": "B", "labels": []}}))
    granted = DispatchClient(url, "node-token").request_work("worker-a", "box1", "login-a")
    assert granted is not None and granted["number"] == 7
    # The request carries the driver's identity — registration included.
    assert seen == [
        {"role": "implementer", "driver": "worker-a", "node": "box1", "login": "login-a"}
    ]


def test_no_grant_and_refusals_answer_none(control_node):
    url, answers, _ = control_node
    answers.extend(
        [
            (200, {"issue": None}),
            (200, {"issue": None, "reason": "node quarantined: 2 consecutive failed Runs"}),
            (503, {"detail": "dispatch requires a control PAT"}),
        ]
    )
    logs: list[str] = []
    client = DispatchClient(url, "node-token", log=logs.append)
    assert client.request_work("w", "n", "l") is None
    assert client.request_work("w", "n", "l") is None
    assert client.request_work("w", "n", "l") is None
    assert any("quarantined" in line for line in logs)  # refusal reasons are surfaced
    assert any("HTTP 503" in line for line in logs)


def test_review_targets_and_pause(control_node):
    url, answers, seen = control_node
    answers.append((200, {"prs": [11, 13]}))
    client = DispatchClient(url, "node-token")
    assert client.review_targets("reviewer-1", "box1", "login-r") == [11, 13]
    assert seen[-1]["role"] == "reviewer"


def test_unreachable_control_node_pauses_cleanly():
    logs: list[str] = []
    client = DispatchClient("http://127.0.0.1:1", "t", timeout=0.2, log=logs.append)
    assert client.request_work("w", "n", "l") is None
    assert client.review_targets("w", "n", "l") is None
    assert any("unreachable" in line for line in logs)


def test_backoff_delay_doubles_to_the_cap_and_recovers():
    """ADR-0015 revision: capped exponential backoff while the Control Node
    is unreachable; streak 0 (recovered) is the plain poll interval."""
    from theozolith_worker.dispatch import backoff_delay

    assert [backoff_delay(60, streak) for streak in range(7)] == [
        60,
        60,
        120,
        240,
        300,
        300,
        300,
    ]
    assert backoff_delay(15, 3) == 60
    assert backoff_delay(0, 5) == 0  # a zero poll interval stays zero (tests)
    # An unbounded streak (a driver latched or unreachable for days) must
    # never overflow the int-to-float conversion and crash the loop it paces.
    assert backoff_delay(60, 100_000) == 300
    assert backoff_delay(60.0, 2_000) == 300.0


def test_unreachability_flag_tracks_the_last_pass(control_node):
    url, answers, _ = control_node
    client = DispatchClient(url, "node-token")
    answers.append((200, {"issue": None}))
    client.request_work("w", "n", "l")
    assert client.last_unreachable is False

    dead = DispatchClient("http://127.0.0.1:1", "t", timeout=0.2)
    dead.request_work("w", "n", "l")
    assert dead.last_unreachable is True

    answers.append((503, {"detail": "no PAT"}))
    client.request_work("w", "n", "l")
    assert client.last_unreachable is False  # a refusal is not unreachability


# -- OZ-03: the node token stays off a plaintext non-loopback wire ---------------


def test_off_box_http_url_is_refused_without_dialing():
    """A Stack-authored off-box http CONTROL_NODE_URL never gets the node
    token: the transport refuses it structurally (no DNS, no dial), the
    driver pauses and surfaces control-url-refused."""
    errors: list[tuple[str, str]] = []
    client = DispatchClient(
        "http://elsewhere.test", "node-token", on_error=lambda c, m: errors.append((c, m))
    )
    assert client.request_work("w", "n", "l") is None
    assert client.last_unreachable is True
    assert errors and errors[0][0] == "control-url-refused"


def test_cross_origin_redirect_never_hands_the_token_to_the_trap():
    """A control endpoint 302-ing to another origin: events.open_bearer (the
    shared worker transport) refuses the hop before issuing it, so a trap on
    the other port sees zero connections and zero Authorization."""
    trap_sock = socket.socket()
    trap_sock.bind(("127.0.0.1", 0))
    trap_sock.listen(4)
    trap_sock.settimeout(0.1)
    trap_port = trap_sock.getsockname()[1]
    seen = bytearray()
    stop = threading.Event()

    def _trap():
        while not stop.is_set():
            try:
                conn, _ = trap_sock.accept()
            except TimeoutError:
                continue
            conn.settimeout(0.2)
            try:
                while data := conn.recv(4096):
                    seen.extend(data)
            except (TimeoutError, OSError):
                pass
            finally:
                conn.close()

    trap_thread = threading.Thread(target=_trap, daemon=True)
    trap_thread.start()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{trap_port}/loot")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = events.control_request(
            f"http://127.0.0.1:{server.server_port}/api/v1/dispatch", "node-token", {"x": 1}
        )
        with pytest.raises(events.BearerTransportError, match="leaves the node-token origin"):
            events.open_bearer(request, ca=None, timeout=2)
        time.sleep(0.2)
        assert b"Authorization" not in seen and len(seen) == 0
    finally:
        server.shutdown()
        stop.set()
        trap_thread.join(2)
        trap_sock.close()


def test_dispatch_failures_fire_the_error_hook(control_node):
    """2026-07-21 grilling: dispatch failures surface as theozolith.error
    through the on_error hook the drivers wire to their event sink."""
    url, answers, _ = control_node
    answers.append((503, {"detail": "dispatch requires a control PAT"}))
    answers.append((200, {"issue": {"number": "not-an-int"}}))
    errors: list[tuple[str, str]] = []
    client = DispatchClient(url, "node-token", on_error=lambda c, m: errors.append((c, m)))

    assert client.request_work("w", "n", "l") is None
    assert client.request_work("w", "n", "l") is None
    unreachable = DispatchClient(
        "http://127.0.0.1:1", "t", timeout=0.2, on_error=lambda c, m: errors.append((c, m))
    )
    assert unreachable.request_work("w", "n", "l") is None

    classes = [error_class for error_class, _ in errors]
    assert classes == ["dispatch-refused", "malformed-grant", "control-unreachable"]
