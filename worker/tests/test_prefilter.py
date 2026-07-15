"""Control Node claim pre-filter: advisory, optional, never authoritative."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from theozolith_worker.prefilter import ControlNodePrefilter, NullPrefilter, make_prefilter


@pytest.fixture
def control_node():
    """A tiny Control Node answering claim intents from a scripted queue."""
    answers: list[dict] = []
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/api/v1/claim-intents"
            length = int(self.headers.get("Content-Length", "0"))
            seen.append(json.loads(self.rfile.read(length)))
            body = json.dumps(answers.pop(0) if answers else {}).encode()
            self.send_response(200)
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


def test_explicit_veto_is_honored(control_node):
    url, answers, seen = control_node
    answers.append({"allow": False})
    assert ControlNodePrefilter(url).allows(7, "worker-a") is False
    assert seen == [{"issue": 7, "worker": "worker-a"}]


def test_allow_and_noncommittal_answers_pass(control_node):
    url, answers, _ = control_node
    answers.extend([{"allow": True}, {}, {"something": "else"}])
    prefilter = ControlNodePrefilter(url)
    assert prefilter.allows(1, "w") is True
    assert prefilter.allows(2, "w") is True
    assert prefilter.allows(3, "w") is True


def test_unreachable_control_node_is_cleanly_skipped():
    prefilter = ControlNodePrefilter("http://127.0.0.1:1", timeout=0.2)
    assert prefilter.allows(1, "w") is True  # GitHub remains the only authority


def test_make_prefilter_defaults_to_null():
    assert isinstance(make_prefilter(None), NullPrefilter)
    assert isinstance(make_prefilter(""), NullPrefilter)
    assert isinstance(make_prefilter("http://cn:8080"), ControlNodePrefilter)
