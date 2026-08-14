"""The plaintext bootstrap listener (ADR-0023/0026): exactly three inert
public values on a dedicated port; every other path and method refused."""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request

import pytest
from theozolith_control import bootstrap as bootstrap_mod
from theozolith_control.bootstrap import BootstrapServer, detect_host_ip

CA_PEM = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
ORIGIN = f"https://{'a' * 26}.theozolith.internal"


@pytest.fixture
def listener():
    server = BootstrapServer(
        ca_pem=CA_PEM, origin=ORIGIN, control_url=ORIGIN, port=0, host="127.0.0.1"
    )
    server.start()
    yield server
    server.stop()


def _get(listener, path: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{listener.port}{path}") as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), ""


def test_serves_exactly_the_three_values(listener):
    status, body, content_type = _get(listener, "/ca.pem")
    assert (status, body) == (200, CA_PEM) and "pem" in content_type
    assert _get(listener, "/origin")[:2] == (200, ORIGIN.encode() + b"\n")
    assert _get(listener, "/control-url")[:2] == (200, ORIGIN.encode() + b"\n")


def test_every_other_path_404s(listener):
    """The route table is closed by decision, not convention."""
    for path in ("/", "/index.html", "/ca.pem/..", "/api/v1/state", "/secrets", "/ca.key"):
        assert _get(listener, path)[0] == 404, path


def test_non_get_methods_are_refused(listener):
    request = urllib.request.Request(
        f"http://127.0.0.1:{listener.port}/ca.pem", data=b"x", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request)
    assert refused.value.code == 405
    # HEAD serves headers only (fetchers probe with it).
    head = urllib.request.Request(f"http://127.0.0.1:{listener.port}/ca.pem", method="HEAD")
    with urllib.request.urlopen(head) as resp:
        assert resp.status == 200 and resp.read() == b""


def test_every_response_closes_its_connection(listener):
    """No keep-alive on the unauthenticated listener (OZ-04): an idle client
    cannot park a handler thread, since every response says Connection: close
    — on the served value, the 404, and the 405 alike."""
    with urllib.request.urlopen(f"http://127.0.0.1:{listener.port}/ca.pem") as resp:
        assert resp.headers.get("Connection", "").lower() == "close"
    with pytest.raises(urllib.error.HTTPError) as missing:
        urllib.request.urlopen(f"http://127.0.0.1:{listener.port}/nope")
    assert missing.value.headers.get("Connection", "").lower() == "close"
    request = urllib.request.Request(
        f"http://127.0.0.1:{listener.port}/ca.pem", data=b"x", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request)
    assert refused.value.headers.get("Connection", "").lower() == "close"


def test_a_partial_request_is_timed_out_not_held_forever(monkeypatch):
    """Slowloris defense (OZ-04): a client that opens a connection and never
    completes its request must be dropped by the read timeout, not left
    holding a handler thread and fd. With a short timeout the server closes
    the half-open request within the budget."""
    monkeypatch.setattr(bootstrap_mod, "BOOTSTRAP_READ_TIMEOUT", 0.5)
    server = BootstrapServer(
        ca_pem=CA_PEM, origin=ORIGIN, control_url=ORIGIN, port=0, host="127.0.0.1"
    )
    server.start()
    try:
        conn = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        conn.sendall(b"GET /ca.pem HTTP/1.1\r\n")  # no blank line: request never completes
        conn.settimeout(3)
        started = time.monotonic()
        leftover = conn.recv(4096)  # server times out and closes → clean EOF
        assert leftover == b"" and time.monotonic() - started < 2.5
        conn.close()
    finally:
        server.stop()


def test_connections_beyond_the_worker_cap_are_dropped(monkeypatch):
    """The pool is bounded (OZ-04): with one worker slot, a connection that
    parks the slot causes the next connection to be dropped outright — closed
    with no response — rather than spawning another unbounded handler
    thread."""
    monkeypatch.setattr(bootstrap_mod, "BOOTSTRAP_MAX_WORKERS", 1)
    monkeypatch.setattr(bootstrap_mod, "BOOTSTRAP_READ_TIMEOUT", 5.0)  # first client holds its slot
    server = BootstrapServer(
        ca_pem=CA_PEM, origin=ORIGIN, control_url=ORIGIN, port=0, host="127.0.0.1"
    )
    server.start()
    holder = None
    try:
        holder = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        holder.sendall(b"GET /ca.pem HTTP/1.1\r\n")  # partial: occupies the only slot
        time.sleep(0.3)  # let the handler acquire the slot
        dropped = socket.create_connection(("127.0.0.1", server.port), timeout=2)
        dropped.settimeout(2)
        assert dropped.recv(4096) == b""  # dropped: no handler, no response
        dropped.close()
    finally:
        if holder is not None:
            holder.close()
        server.stop()


def test_detect_host_ip_answers_a_dialable_address():
    ip = detect_host_ip()
    assert ip.count(".") == 3 and all(part.isdigit() for part in ip.split("."))
