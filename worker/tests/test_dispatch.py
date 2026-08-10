"""The drivers' dispatch client (ADR-0017): one endpoint, clean pauses."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
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
