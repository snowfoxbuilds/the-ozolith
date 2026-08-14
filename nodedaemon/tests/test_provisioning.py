"""Join-string provisioning (ADR-0023/0025, acceptances 9/10/14).

The happy path runs the REAL flow over real sockets: a genuine TLS Control
Node (uvicorn), the plaintext bootstrap listener, and `provision` with its
default stdlib fetchers. The MITM cases assert ZERO bytes reach the control
channel with an instrumented listener — the same trap the ControlClient
redirect-policy rigs point a TLS 3xx at. The parser is pinned byte-for-byte
to control's composer by round-trip (no shared import crosses the
component boundary).
"""

from __future__ import annotations

import ast
import http.server
import json
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from theozolith_control import joinstring, tls
from theozolith_control.app import create_app
from theozolith_control.bootstrap import BootstrapServer
from theozolith_control.crypto import SecretBox, generate_key
from theozolith_control.secretstore import SecretStore
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store
from theozolith_nodedaemon import provisioning
from theozolith_nodedaemon.config import load_daemon_config
from theozolith_nodedaemon.controlclient import ControlClient, ControlError
from theozolith_nodedaemon.provisioning import ProvisionError, parse_join_string

FINGERPRINT = "ab" * 32


def _compose(**overrides) -> str:
    values = dict(addr="192.0.2.7:6965", ca_sha256=FINGERPRINT, token=b"t" * 16, expires_at=4000)
    values.update(overrides)
    return joinstring.compose(**values)


# -- the wire format, pinned across the component boundary ----------------------


def test_cross_component_roundtrip():
    payload = parse_join_string(_compose())
    assert payload.addr == "192.0.2.7:6965"
    assert payload.host == "192.0.2.7" and payload.bootstrap_port == 6965
    assert payload.ca_sha256 == FINGERPRINT
    assert payload.token == b"t" * 16
    assert payload.expires_at == 4000
    # Portless addr: the fixed bootstrap default rides implicitly
    # (ADR-0026 — never http-80; the join string dials the listener).
    assert parse_join_string(_compose(addr="10.0.0.9")).bootstrap_port == 6965


def test_the_paste_stays_terminal_sized():
    assert len(_compose(addr="192.168.100.200:6965")) <= 130  # ~120 chars by design


def test_malformed_pastes_fail_as_malformed_never_as_network_errors():
    good = _compose()
    for truncated in (good[:-1], good[: len(good) // 2], f"{joinstring.PREFIX}:", "ozjoin1"):
        with pytest.raises(ProvisionError, match=r"^malformed join string"):
            parse_join_string(truncated)
    # One flipped character mid-payload dies on the checksum.
    middle = len(good) // 2
    flipped = good[:middle] + ("A" if good[middle] != "A" else "B") + good[middle + 1 :]
    with pytest.raises(ProvisionError, match=r"^malformed join string"):
        parse_join_string(flipped)
    with pytest.raises(ProvisionError, match=r"^malformed join string"):
        parse_join_string("not a join string at all")


def test_future_versions_are_named_not_mangled():
    with pytest.raises(ProvisionError, match=r"unsupported join-string version 'ozjoin2'"):
        parse_join_string("ozjoin2:" + _compose().partition(":")[2])


def test_inspect_pretty_prints_without_acting():
    text = provisioning.inspect_text(parse_join_string(_compose()), now=1000)
    assert "192.0.2.7:6965" in text
    assert f"sha256:{FINGERPRINT}" in text
    assert "expires in 50m" in text
    expired = provisioning.inspect_text(parse_join_string(_compose()), now=5000)
    assert "EXPIRED (server enforces)" in expired


# -- live rigs -------------------------------------------------------------------


class LiveControl:
    """The real control app on a real TLS socket + the real bootstrap
    listener, exactly as `serve` arranges them."""

    def __init__(self, tmp_path: Path):
        self.tls_dir = tmp_path / "data" / "secrets" / "tls"
        self.ca_path, cert, key = tls.provision(self.tls_dir, ["127.0.0.1"])
        settings = ControlSettings(
            data_dir=tmp_path / "data",
            config_repo=tmp_path / "configs",
            admin_token="admin-token",
            repo=None,
            github_token=None,
            api_url="",
            # The persisted control address (ADR-0031/0034): the join
            # exchange echoes the address the node dialed, never this.
            control_ip="127.0.0.1",
            secrets_channel_ok=True,
        )
        self.store = Store(settings.cache_db_path)
        self.secret_store = SecretStore(settings.store_db_path)
        app = create_app(settings, self.store, self.secret_store, SecretBox(generate_key()))
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=0,
                log_level="error",
                ssl_certfile=str(cert),
                ssl_keyfile=str(key),
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.bootstrap: BootstrapServer | None = None

    def __enter__(self) -> LiveControl:
        self._thread.start()
        deadline = time.time() + 15
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("control node did not start")
            time.sleep(0.02)
        self.https_port = self._server.servers[0].sockets[0].getsockname()[1]
        self.bootstrap = BootstrapServer(
            ca_pem=self.ca_path.read_bytes(),
            origin="",
            control_url=f"https://127.0.0.1:{self.https_port}",
            port=0,
            host="127.0.0.1",
        )
        self.bootstrap.start()
        return self

    def __exit__(self, *exc) -> None:
        if self.bootstrap is not None:
            self.bootstrap.stop()
        self._server.should_exit = True
        self._thread.join(10)

    def join_string(self, *, ttl: float = 3600.0, uses: int = 1) -> str:
        _token_id, raw, expires_at = self.store.create_join_token(ttl_seconds=ttl, uses=uses)
        return joinstring.compose(
            addr=f"127.0.0.1:{self.bootstrap.port}",
            ca_sha256=tls.ca_fingerprint_sha256(self.ca_path.read_bytes()),
            token=raw,
            expires_at=expires_at,
        )


class TrapListener:
    """An instrumented TCP listener standing in for the control channel:
    records every accepted connection and byte (acceptance 10)."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        self.bytes_received = 0
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
                    self.bytes_received += len(data)
                    self.data += data
            except (TimeoutError, OSError):
                pass
            finally:
                conn.close()

    def stop(self):
        self._stop.set()
        self._thread.join(2)
        self._sock.close()


# -- acceptance 9: the happy path, one paste on a fresh box ----------------------


def test_provision_happy_path_end_to_end(tmp_path):
    state = tmp_path / "state"
    logs: list[str] = []
    with LiveControl(tmp_path) as live:
        join = live.join_string()
        provisioning.provision(
            join, state_dir=state, node_name="fresh-box", enable_systemd=False, log=logs.append
        )

        # Persisted under the daemon state dir: CA, control URL, name, token.
        # The control URL is the IP-based address the node just verified
        # (acceptance 1 of the 2026-07-28 ruling).
        assert (state / "ca.pem").read_bytes() == live.ca_path.read_bytes()
        persisted_url = (state / "control-url").read_text().strip()
        assert persisted_url == f"https://127.0.0.1:{live.https_port}"
        assert "theozolith.internal" not in persisted_url
        assert (state / "node-name").read_text().strip() == "fresh-box"
        assert (state / "node-token").stat().st_mode & 0o777 == 0o600
        node_token = (state / "node-token").read_text().strip()
        assert live.secret_store.node_for_token(node_token) == "fresh-box"

        # Provisioning IS registration: the node exists before any heartbeat.
        assert [n["name"] for n in live.store.fleet_state()["nodes"]] == ["fresh-box"]

        # The daemon boots env-free from the provisioned state (no .env).
        config = load_daemon_config({"THEOZOLITH_STATE_DIR": str(state)})
        assert config.control_url == f"https://127.0.0.1:{live.https_port}"
        assert config.node_token == node_token
        assert config.node == "fresh-box"
        assert config.tls_ca == str(state / "ca.pem")

        # …and genuinely heartbeats over TLS with its own token.
        client = ControlClient(config.control_url, config.node_token, ca=config.tls_ca)
        answer = client.heartbeat(
            {"node": "fresh-box", "stacks": [], "run_containers": [], "images": []}
        )
        assert "config" in answer

        # The join token was consumed: the second use is rejected cleanly
        # after TLS, with nothing persisted (acceptances 9 + 10).
        second_state = tmp_path / "state-2"
        with pytest.raises(ProvisionError, match="expired, consumed, or revoked"):
            provisioning.provision(
                join, state_dir=second_state, node_name="copycat", enable_systemd=False
            )
        assert not (second_state / "node-token").exists()
        assert "copycat" not in [n["node"] for n in live.secret_store.provisioned_nodes()]


def test_local_node_dial_address_survives_lan_renumbering(tmp_path):
    """M8 acceptance 2: the persisted dial address is loopback and comes
    ONLY from the daemon state dir — a LAN IP change (a new control_ip in
    the Config Repo, a re-minted server cert) never touches the local node
    of a Single-Node Deployment: it keeps dialing 127.0.0.1, which the
    unconditional loopback IP SAN keeps verifying (ADR-0036/0037)."""
    state = tmp_path / "state"
    with LiveControl(tmp_path) as live:
        provisioning.provision(
            live.join_string(), state_dir=state, node_name="localbox", enable_systemd=False
        )
    # No live control needed: the dial address is a local fact of the state
    # dir; nothing node-side ever reads the Config Repo's control_ip.
    config = load_daemon_config({"THEOZOLITH_STATE_DIR": str(state)})
    assert config.control_url.startswith("https://127.0.0.1")


def test_reprovisioning_rotates_the_token_and_replaces_state(tmp_path):
    """One re-paste per node is the IP-change recovery path (ADR-0023 §
    node channel addressing): a second provision of the SAME node rotates
    its per-node token, replaces the persisted state in place, and the
    node resumes heartbeating — while the old token goes dead."""
    state = tmp_path / "state"
    with LiveControl(tmp_path) as live:
        provisioning.provision(
            live.join_string(), state_dir=state, node_name="box1", enable_systemd=False
        )
        first_token = (state / "node-token").read_text().strip()

        provisioning.provision(
            live.join_string(), state_dir=state, node_name="box1", enable_systemd=False
        )
        second_token = (state / "node-token").read_text().strip()

        assert second_token != first_token  # rotated, not appended
        assert live.secret_store.node_for_token(first_token) is None  # old one is dead
        assert live.secret_store.node_for_token(second_token) == "box1"
        assert [n["node"] for n in live.secret_store.provisioned_nodes()] == ["box1"]

        # The daemon boots from the replaced state and heartbeats.
        config = load_daemon_config({"THEOZOLITH_STATE_DIR": str(state)})
        assert config.node_token == second_token
        client = ControlClient(config.control_url, config.node_token, ca=config.tls_ca)
        answer = client.heartbeat(
            {"node": "box1", "stacks": [], "run_containers": [], "images": []}
        )
        assert "config" in answer


# -- acceptance 10: failure modes fail closed and loud ---------------------------


def test_fingerprint_mismatch_aborts_with_zero_bytes_to_the_target(tmp_path):
    """A MITM (or rotated CA) serves a different certificate: provision
    aborts BEFORE any transmission — the instrumented control channel sees
    no connection, no byte, and nothing is persisted."""
    trap = TrapListener()
    evil_ca, _, _ = tls.provision(tmp_path / "evil-tls", ["127.0.0.1"])
    bootstrap = BootstrapServer(
        ca_pem=evil_ca.read_bytes(),  # NOT the CA the join string pins
        origin="",
        control_url=f"https://127.0.0.1:{trap.port}",
        port=0,
        host="127.0.0.1",
    )
    bootstrap.start()
    join = joinstring.compose(
        addr=f"127.0.0.1:{bootstrap.port}",
        ca_sha256=FINGERPRINT,  # the pinned (real) fingerprint
        token=b"t" * 16,
        expires_at=int(time.time()) + 3600,
    )
    state = tmp_path / "state"
    try:
        with pytest.raises(ProvisionError, match="possible MITM, or a stale join string"):
            provisioning.provision(join, state_dir=state, node_name="victim", enable_systemd=False)
        time.sleep(0.2)  # anything in flight would land by now
        assert trap.connections == 0
        assert trap.bytes_received == 0
        assert not state.exists()
    finally:
        bootstrap.stop()
        trap.stop()


def test_appended_ca_bundle_is_refused_before_fingerprinting(tmp_path):
    """The pin covers ONE certificate: a MITM that appends its own CA after
    the genuine one (fingerprint-matching first block, hostile trust anchor
    riding behind it) is refused before fingerprinting — zero bytes
    transmitted, nothing persisted."""
    trap = TrapListener()
    real_ca, _, _ = tls.provision(tmp_path / "real-tls", ["127.0.0.1"])
    evil_ca, _, _ = tls.provision(tmp_path / "evil-tls", ["127.0.0.1"])
    bootstrap = BootstrapServer(
        ca_pem=real_ca.read_bytes() + evil_ca.read_bytes(),
        origin="",
        control_url=f"https://127.0.0.1:{trap.port}",
        port=0,
        host="127.0.0.1",
    )
    bootstrap.start()
    join = joinstring.compose(
        addr=f"127.0.0.1:{bootstrap.port}",
        ca_sha256=tls.ca_fingerprint_sha256(real_ca.read_bytes()),  # the genuine pin
        token=b"t" * 16,
        expires_at=int(time.time()) + 3600,
    )
    state = tmp_path / "state"
    try:
        with pytest.raises(ProvisionError, match="refusing the bundle"):
            provisioning.provision(join, state_dir=state, node_name="victim", enable_systemd=False)
        time.sleep(0.2)  # anything in flight would land by now
        assert trap.connections == 0
        assert trap.bytes_received == 0
        assert not state.exists()
    finally:
        bootstrap.stop()
        trap.stop()


def test_pem_canonicalization_keeps_only_the_verified_certificate(tmp_path):
    """Bytes around the single block are tolerated but never trusted: the
    round-trip re-encodes exactly the fingerprinted DER, byte-identical to
    control's cryptography PEM output (the happy path pins the same via
    its persisted-ca equality)."""
    ca_path, _, _ = tls.provision(tmp_path / "tls", ["127.0.0.1"])
    pem = ca_path.read_bytes()
    der = provisioning.pem_to_der(b"# preamble\n" + pem + b"trailing noise\n")
    assert provisioning.der_to_pem(der) == pem
    # A second block — even a copy of the same certificate — is a bundle.
    with pytest.raises(ProvisionError, match="refusing the bundle"):
        provisioning.pem_to_der(pem + pem)


def test_non_https_control_url_is_never_persisted(tmp_path):
    """A hostile (or merely malformed) bootstrap /control-url dies BEFORE
    the join exchange: the exchange callable failing the test pins that
    nothing — the single-use join token included — went to the control
    channel, and no local state exists. Exact parsing, not a prefix check:
    a scheme merely beginning with https is refused too — and the parse is
    TOTAL: input that makes urlsplit itself raise ValueError (unmatched
    IPv6 brackets, NFKC-invalid netlocs) dies through the same
    ProvisionError, never as a leaked ValueError."""
    ca_path, _, _ = tls.provision(tmp_path / "tls", ["127.0.0.1"])
    pem = ca_path.read_bytes()

    def fail_post(url, body, ca):
        raise AssertionError("the join exchange must never run after a bad bootstrap URL")

    join = joinstring.compose(
        addr="127.0.0.1:6965",
        ca_sha256=tls.ca_fingerprint_sha256(pem),
        token=b"t" * 16,
        expires_at=int(time.time()) + 3600,
    )
    state = tmp_path / "state"
    for hostile in (
        b"http://198.51.100.9:6966\n",  # the plaintext downgrade
        b"httpsneak://198.51.100.9\n",  # prefix trick: startswith('https')
        b"https://\n",  # no usable hostname
        b"https://198.51.100.9:no-port\n",  # unparsable port
        b"https://[\n",  # unmatched IPv6 bracket: ValueError inside urlsplit
        b"https://[::1\n",  # same, with address content
        "https://evil\uff0fslash.test\n".encode(),  # NFKC-invalid netloc (full-width slash)
        b"https://198.51.100.9:0\n",  # explicit :0 — never rewritten to the default 443
    ):

        def fake_get(url, answer=hostile):
            return pem if url.endswith("/ca.pem") else answer

        with pytest.raises(ProvisionError, match="before the join exchange"):
            provisioning.provision(
                join,
                state_dir=state,
                node_name="victim",
                enable_systemd=False,
                http_get=fake_get,
                https_post=fail_post,
            )
        assert not state.exists()


def test_non_https_exchange_answer_is_never_persisted(tmp_path):
    """Malformed server output still fails closed AFTER the exchange: an
    answer whose control URL is not exactly https is refused before
    anything is persisted locally — and the error owns up that the
    exchange itself already ran (the join token is spent)."""
    ca_path, _, _ = tls.provision(tmp_path / "tls", ["127.0.0.1"])
    pem = ca_path.read_bytes()

    def fake_get(url):
        return pem if url.endswith("/ca.pem") else b""

    def fake_post(url, body, ca):
        answer = {"node_token": "tok-value", "control_url": "http://198.51.100.9:6966"}
        return 200, json.dumps(answer).encode()

    join = joinstring.compose(
        addr="127.0.0.1:6965",
        ca_sha256=tls.ca_fingerprint_sha256(pem),
        token=b"t" * 16,
        expires_at=int(time.time()) + 3600,
    )
    state = tmp_path / "state"
    with pytest.raises(ProvisionError, match="the exchange answered"):
        provisioning.provision(
            join,
            state_dir=state,
            node_name="victim",
            enable_systemd=False,
            http_get=fake_get,
            https_post=fake_post,
        )
    assert not state.exists()


def test_malformed_exchange_answer_url_is_provisionerror_never_valueerror(tmp_path):
    """The exchange-answer gate is TOTAL like the bootstrap one: a control
    URL that makes urlsplit itself raise ValueError (unmatched IPv6
    bracket, NFKC-invalid netloc) or names the undialable :0 becomes the
    documented ProvisionError — whose message owns up that the exchange
    already ran and the join token is spent — never a leaked ValueError,
    and never persisted state."""
    ca_path, _, _ = tls.provision(tmp_path / "tls", ["127.0.0.1"])
    pem = ca_path.read_bytes()

    def fake_get(url):
        return pem if url.endswith("/ca.pem") else b""

    join = joinstring.compose(
        addr="127.0.0.1:6965",
        ca_sha256=tls.ca_fingerprint_sha256(pem),
        token=b"t" * 16,
        expires_at=int(time.time()) + 3600,
    )
    state = tmp_path / "state"
    for hostile in (
        "https://[",
        "https://[::1",
        "https://evil\uff0fslash.test",
        "https://127.0.0.1:0",
    ):

        def fake_post(url, body, ca, answer=hostile):
            return 200, json.dumps({"node_token": "tok-value", "control_url": answer}).encode()

        with pytest.raises(ProvisionError, match="the exchange itself already ran"):
            provisioning.provision(
                join,
                state_dir=state,
                node_name="victim",
                enable_systemd=False,
                http_get=fake_get,
                https_post=fake_post,
            )
        assert not state.exists()


def test_expired_token_rejects_after_tls_with_nothing_persisted(tmp_path):
    with LiveControl(tmp_path) as live:
        join = live.join_string(ttl=0.001)
        time.sleep(0.05)
        state = tmp_path / "state"
        with pytest.raises(ProvisionError, match="expired, consumed, or revoked"):
            provisioning.provision(join, state_dir=state, node_name="late", enable_systemd=False)
        assert not state.exists()
        assert live.secret_store.provisioned_nodes() == []


def test_hostile_bootstrap_url_cannot_consume_the_join_token(tmp_path):
    """The early rejection costs nothing: after a hostile bootstrap served a
    plaintext control URL (behind the GENUINE CA, so the fingerprint pin
    alone would not catch it), the SAME join string still provisions
    through the genuine bootstrap — the single-use token was never consumed
    and the failed attempt registered no node."""
    state = tmp_path / "state"
    with LiveControl(tmp_path) as live:
        join = live.join_string()  # single-use
        payload = parse_join_string(join)
        hostile = BootstrapServer(
            ca_pem=live.ca_path.read_bytes(),  # the genuine CA: the pin passes
            origin="",
            control_url=f"http://127.0.0.1:{live.https_port}",  # the downgrade
            port=0,
            host="127.0.0.1",
        )
        hostile.start()
        try:
            hostile_join = joinstring.compose(
                addr=f"127.0.0.1:{hostile.port}",
                ca_sha256=payload.ca_sha256,
                token=payload.token,
                expires_at=payload.expires_at,
            )
            with pytest.raises(ProvisionError, match="before the join exchange"):
                provisioning.provision(
                    hostile_join, state_dir=state, node_name="victim", enable_systemd=False
                )
        finally:
            hostile.stop()
        assert not state.exists()
        assert live.secret_store.provisioned_nodes() == []  # nothing created remotely

        provisioning.provision(join, state_dir=state, node_name="survivor", enable_systemd=False)
        token = (state / "node-token").read_text().strip()
        assert live.secret_store.node_for_token(token) == "survivor"


# -- the bearer token's redirect policy (ControlClient, live sockets) ------------


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


def _redirector(location: str) -> type[_Quiet]:
    """Every request answered with a 302 to `location` — the cheapest shape
    a redirect attacker (or a misconfigured reverse proxy) can take."""

    class Handler(_Quiet):
        def do_GET(self):
            self._drain()
            self._answer(302, {"Location": location})

        do_POST = do_GET

    return Handler


def _tls_server(tmp_path: Path, handler: type[_Quiet]):
    """`handler` on a real TLS socket under the repository's test CA."""
    ca_path, cert, key = tls.provision(tmp_path / "redirect-tls", ["127.0.0.1"])
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, ca_path


def _stop_server(server, thread) -> None:
    server.shutdown()
    thread.join(2)
    server.server_close()


def test_authenticated_redirect_to_plaintext_target_gets_zero_bytes(tmp_path):
    """An HTTPS control endpoint answering 3xx toward a plaintext target:
    the transport refuses the hop BEFORE issuing the redirected request —
    on the bearer-authenticated POST and both authenticated GETs alike, the
    trap sees zero connections, zero bytes, and therefore zero
    Authorization headers."""
    trap = TrapListener()
    server, thread, ca_path = _tls_server(
        tmp_path, _redirector(f"http://127.0.0.1:{trap.port}/loot")
    )
    client = ControlClient(
        f"https://127.0.0.1:{server.server_address[1]}", "bearer-secret", ca=str(ca_path)
    )
    try:
        with pytest.raises(ControlError, match="refusing redirect"):
            client.emit_event({"kind": "probe"})  # POST
        with pytest.raises(ControlError, match="refusing redirect"):
            client.fetch_artifact("1.2.3", "wheel.whl")  # authenticated GET
        with pytest.raises(ControlError, match="refusing redirect"):
            client.fetch_config_artifact("c" * 64)  # the other authenticated GET
        time.sleep(0.2)  # anything in flight would land by now
        assert trap.connections == 0
        assert trap.bytes_received == 0
        assert b"Authorization" not in trap.data
    finally:
        _stop_server(server, thread)
        trap.stop()


def test_cross_origin_https_redirect_is_refused_before_dialing(tmp_path):
    """Same scheme, different origin: refused BEFORE the redirected request
    is issued. The target here does not even exist — a dial attempt would
    surface as ControlUnreachable, never this ControlError."""
    server, thread, ca_path = _tls_server(tmp_path, _redirector("https://127.0.0.1:1/elsewhere"))
    client = ControlClient(
        f"https://127.0.0.1:{server.server_address[1]}", "bearer-secret", ca=str(ca_path)
    )
    try:
        with pytest.raises(ControlError, match="refusing redirect"):
            client.heartbeat({"node": "box1", "stacks": []})
    finally:
        _stop_server(server, thread)


def test_malformed_redirect_location_is_controlerror_before_any_dial(tmp_path):
    """A Location that urljoin itself raises ValueError on (unmatched IPv6
    bracket) and one whose target parses to the undialable :0 both die as
    ControlError BEFORE any redirected request. The origin server sees
    exactly the one original request per call — so the Authorization header
    rode only the configured origin — and the asserted error type does the
    rest: a dial attempt at either target could only surface as
    ControlUnreachable, never these ControlErrors."""
    served: list[str] = []

    class Handler(_Quiet):
        def do_POST(self):
            served.append(self.path)
            self._drain()
            if self.path == "/api/v1/heartbeats":
                self._answer(302, {"Location": "https://[::1"})  # unmatched bracket
            else:
                self._answer(302, {"Location": "https://127.0.0.1:0/loot"})  # explicit :0

    server, thread, ca_path = _tls_server(tmp_path, Handler)
    client = ControlClient(
        f"https://127.0.0.1:{server.server_address[1]}", "bearer-secret", ca=str(ca_path)
    )
    try:
        with pytest.raises(ControlError, match="malformed Location"):
            client.heartbeat({"node": "box1"})
        with pytest.raises(ControlError, match="leaves the configured"):
            client.emit_event({"kind": "probe"})
        assert served == ["/api/v1/heartbeats", "/api/v1/events"]
    finally:
        _stop_server(server, thread)


def test_same_origin_https_redirect_still_works_and_loops_are_bounded(tmp_path):
    """The policy refuses hops that LEAVE the origin, not redirects per se:
    a same-origin HTTPS relocation is followed with method and body intact,
    and a same-origin redirect loop dies on the hop budget instead of
    spinning."""

    class Handler(_Quiet):
        def do_POST(self):
            body = self._drain()
            if self.path == "/api/v1/heartbeats":
                self._answer(307, {"Location": "/api/v1/heartbeats-moved"})
            elif self.path == "/api/v1/events":
                self._answer(302, {"Location": "/api/v1/events"})  # loops forever
            else:
                answer = {"config": {"relocated": True}, "echo": json.loads(body)}
                self._answer(200, {"Content-Type": "application/json"}, json.dumps(answer).encode())

    server, thread, ca_path = _tls_server(tmp_path, Handler)
    client = ControlClient(
        f"https://127.0.0.1:{server.server_address[1]}", "bearer-secret", ca=str(ca_path)
    )
    try:
        answer = client.heartbeat({"node": "box1"})
        assert answer["config"] == {"relocated": True}
        assert answer["echo"] == {"node": "box1"}  # the 307 re-sent the body
        with pytest.raises(ControlError, match="too many redirects"):
            client.emit_event({"kind": "probe"})
    finally:
        _stop_server(server, thread)


# -- acceptance 14: the node distribution stays stdlib-only ----------------------


def test_node_distribution_is_stdlib_only_including_provisioning():
    """Every import anywhere in theozolith_nodedaemon (function-scope
    included) resolves to the stdlib or the package itself."""
    package_dir = Path(provisioning.__file__).parent
    allowed = set(sys.stdlib_module_names) | {"theozolith_nodedaemon"}
    for source in sorted(package_dir.rglob("*.py")):  # rglob: subpackages cannot escape
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.partition(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [] if node.level else [(node.module or "").partition(".")[0]]
            else:
                continue
            for root in roots:
                assert root in allowed, f"{source.name}: non-stdlib import {root!r}"
