"""Join-string provisioning (ADR-0023/0025, acceptances 9/10/14).

The happy path runs the REAL flow over real sockets: a genuine TLS Control
Node (uvicorn), the plaintext bootstrap listener, and `provision` with its
default stdlib fetchers. The MITM case asserts ZERO bytes reach the control
channel with an instrumented listener. The parser is pinned byte-for-byte
to control's composer by round-trip (no shared import crosses the
component boundary).
"""

from __future__ import annotations

import ast
import socket
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
from theozolith_nodedaemon.controlclient import ControlClient
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


def test_expired_token_rejects_after_tls_with_nothing_persisted(tmp_path):
    with LiveControl(tmp_path) as live:
        join = live.join_string(ttl=0.001)
        time.sleep(0.05)
        state = tmp_path / "state"
        with pytest.raises(ProvisionError, match="expired, consumed, or revoked"):
            provisioning.provision(join, state_dir=state, node_name="late", enable_systemd=False)
        assert not state.exists()
        assert live.secret_store.provisioned_nodes() == []


# -- acceptance 14: the node distribution stays stdlib-only ----------------------


def test_node_distribution_is_stdlib_only_including_provisioning():
    """Every import anywhere in theozolith_nodedaemon (function-scope
    included) resolves to the stdlib or the package itself."""
    package_dir = Path(provisioning.__file__).parent
    allowed = set(sys.stdlib_module_names) | {"theozolith_nodedaemon"}
    for source in sorted(package_dir.glob("*.py")):
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
