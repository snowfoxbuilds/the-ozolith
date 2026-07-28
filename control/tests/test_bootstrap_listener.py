"""The plaintext bootstrap listener (ADR-0023/0026): exactly three inert
public values on a dedicated port; every other path and method refused."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest
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


def test_detect_host_ip_answers_a_dialable_address():
    ip = detect_host_ip()
    assert ip.count(".") == 3 and all(part.isdigit() for part in ip.split("."))
