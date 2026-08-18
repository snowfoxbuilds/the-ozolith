"""``init --with-local-node`` (ADR-0037): pre-flight refusals, the
node-unit drift guard against install-nodedaemon.sh, the internal join
orchestration (standard provision grammar, loopback addr, token consumed,
join string never shown), and the stage-don't-deploy scaffold."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from controlrig import make_settings
from theozolith_control import localnode
from theozolith_control.configrepo import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
NODEDAEMON_EXEC = "/opt/theozolith/bin/theozolith-nodedaemon"


def _ok(*args, **kwargs):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


# -- pre-flight (before any state is written) ------------------------------------


def test_preconditions_require_docker_and_the_daemon_cli():
    with pytest.raises(SystemExit, match="docker"):
        localnode.ensure_preconditions(which=lambda name: None)
    # docker present, daemon CLI missing: refusal with remediation — a root
    # setup path never pip-installs on its own.
    with pytest.raises(SystemExit, match=r"build\.py"):
        localnode.ensure_preconditions(
            which=lambda name: "/usr/bin/docker" if name == "docker" else None
        )


def test_with_local_node_refuses_inside_a_container(tmp_path, monkeypatch):
    from theozolith_control.cli import main as cli_main

    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr("theozolith_control.cli._running_in_container", lambda: True)
    with pytest.raises(SystemExit, match="bare-metal root"):
        cli_main(["init", "--ip", "127.0.0.1", "--with-local-node"])
    assert not (tmp_path / "home" / "secrets").exists()  # refused before any state


# -- the node unit: one body, two writers, drift-tested --------------------------


def test_node_unit_matches_the_installer_heredoc():
    """install-nodedaemon.sh (remote boxes) and localnode (the local one)
    write the same unit; ExecStart is the parameterized seam. Drift in
    either direction fails here."""
    installer = (REPO_ROOT / "deploy" / "install-nodedaemon.sh").read_text(encoding="utf-8")
    match = re.search(r"<<'UNIT'\n(.*?)\nUNIT\n", installer, re.DOTALL)
    assert match, "install-nodedaemon.sh no longer embeds the unit heredoc"
    heredoc = match.group(1) + "\n"
    assert localnode.render_node_unit(NODEDAEMON_EXEC) == heredoc


def test_install_node_daemon_lays_down_user_dir_and_unit(tmp_path, monkeypatch):
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return _ok()

    unit_path = tmp_path / "theozolith-nodedaemon.service"
    localnode.install_node_daemon(
        NODEDAEMON_EXEC,
        runner=runner,
        unit_path=unit_path,
        state_dir=tmp_path / "state",
        log=lambda _: None,
    )
    assert calls[0][:2] == ["useradd", "--system"]
    assert ["usermod", "-aG", "docker", "ozolith"] in calls
    assert any(c[0] == "install" and str(tmp_path / "state") in c for c in calls)
    assert calls[-1] == ["systemctl", "daemon-reload"]
    unit = unit_path.read_text()
    assert f"ExecStart={NODEDAEMON_EXEC}" in unit
    assert "KillMode=control-group" in unit


# -- the internal join: unmodified flow, machine-consumed ------------------------


NOW = 1_000_000.0
JOIN_STRING = "ozjoin1:MACHINE-ONLY-SECRET"
FAKE_CA = b"--fake ca pem--"


class Harness:
    """Fake runner + API for bootstrap_local_node: records everything and
    answers like a healthy serve with a server clock. ``pre_row`` seeds the
    node row the reconcile phase sees BEFORE any provisioning (None =
    absent; a dict with version/last_seen otherwise); after a recorded
    successful provision or a daemon restart, the node answers as freshly
    heartbeating. ``write_identity`` lays down the provisioned on-disk
    layout the reconcile phase validates."""

    def __init__(
        self,
        tmp_path,
        *,
        consume_token: bool = True,
        provision_rc: int = 0,
        provision_stderr: str = "CA fingerprint mismatch: possible MITM, or a stale join string",
        pre_row: dict | None = None,
        mint_response: tuple[int, dict] | None = None,
    ):
        self.settings = make_settings(tmp_path)
        self.settings.tls_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.tls_dir / "ca.pem").write_bytes(FAKE_CA)
        self.state_dir = tmp_path / "state"
        self.calls: list[list[str]] = []
        self.fetches: list[tuple[str, str, dict | None]] = []
        self.logs: list[str] = []
        self.minted_addr = ""
        self.consume_token = consume_token
        self.provision_rc = provision_rc
        self.provision_stderr = provision_stderr
        self.pre_row = pre_row
        self.mint_response = mint_response

    def write_identity(
        self,
        *,
        ca: bytes = FAKE_CA,
        name: str = "localbox",
        control_url: str = "https://127.0.0.1",
        token_value: str = "per-node-token-value",
    ):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "control-url").write_text(control_url + "\n")
        (self.state_dir / "node-name").write_text(name + "\n")
        (self.state_dir / "node-token").write_text(token_value + "\n")
        (self.state_dir / "node-token").chmod(0o600)
        (self.state_dir / "ca.pem").write_bytes(ca)

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[1:2] == ["provision"]:
            return SimpleNamespace(
                returncode=self.provision_rc, stdout="", stderr=self.provision_stderr
            )
        return _ok()

    def restarted_daemon(self) -> bool:
        return ["systemctl", "restart", "theozolith-nodedaemon.service"] in self.calls

    def _node_rows(self):
        provisioned = any(c[1:2] == ["provision"] and self.provision_rc == 0 for c in self.calls)
        if provisioned or self.restarted_daemon():
            return [{"name": "localbox", "version": "0.3.0", "last_seen": NOW}]
        if self.pre_row is None:
            return []
        return [{"name": "localbox", **self.pre_row}]

    def fetch(self, method, url, *, token, ca, body=None):
        assert token == self.settings.admin_token
        self.fetches.append((method, url, body))
        if url.endswith("/api/v1/healthz"):
            return 200, {"ok": True}
        if method == "POST" and url.endswith("/api/v1/join-tokens"):
            self.minted_addr = body["addr"]
            if self.mint_response is not None:
                return self.mint_response
            return 200, {"id": "tok-1", "join_string": JOIN_STRING}
        if method == "DELETE" and "/api/v1/join-tokens/" in url:
            return 200, {"revoked": True}
        if method == "GET" and url.endswith("/api/v1/join-tokens"):
            tokens = [] if self.consume_token else [{"id": "tok-1"}]
            return 200, {"tokens": tokens}
        if url.endswith("/api/v1/state"):
            return 200, {"now": NOW, "nodes": self._node_rows()}
        raise AssertionError(f"unexpected fetch: {method} {url}")

    def revokes(self) -> list[str]:
        return [u for m, u, _ in self.fetches if m == "DELETE"]

    def bootstrap(self, tmp_path):
        localnode.bootstrap_local_node(
            self.settings,
            node_name="localbox",
            nodedaemon_exec=NODEDAEMON_EXEC,
            runner=self.runner,
            fetch=self.fetch,
            sleep=lambda seconds: None,
            unit_path=tmp_path / "nodedaemon.service",
            state_dir=self.state_dir,
            log=self.logs.append,
        )


def test_bootstrap_runs_the_standard_provision_grammar(tmp_path, monkeypatch):
    """M8 acceptance 1: the internal flow starts the real service, mints
    the join token through the standard endpoint with a loopback addr, and
    invokes the standard provision implementation — the installed CLI's
    exact grammar, no reimplementation. The join string is never shown."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path)
    harness.bootstrap(tmp_path)

    # Early serve start via the systemd unit init installed (ADR-0037).
    start_index = harness.calls.index(["systemctl", "start", "theozolith-control.service"])
    provision_call = next(c for c in harness.calls if c[1:2] == ["provision"])
    assert provision_call == [
        NODEDAEMON_EXEC,
        "provision",
        JOIN_STRING,
        "--node",
        "localbox",
    ]
    assert harness.calls.index(provision_call) > start_index

    # The join-string addr is the temporary loopback listener.
    host, _, port = harness.minted_addr.partition(":")
    assert host == "127.0.0.1" and int(port) > 0

    # Minted and consumed was verified through the standard endpoints.
    methods = [(m, u.rsplit("/", 1)[-1].split("?")[0]) for m, u, _ in harness.fetches]
    assert ("POST", "join-tokens") in methods
    assert ("GET", "join-tokens") in methods

    # The human never sees the join string (acceptance 1).
    assert all("ozjoin1:" not in line for line in harness.logs)
    assert any("registered and heartbeating" in line for line in harness.logs)


def test_bootstrap_fails_loud_when_the_token_survives(tmp_path, monkeypatch):
    """An unconsumed machine-only token is revoked before the failure
    surfaces — nothing outstanding survives an incomplete exchange."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path, consume_token=False)
    with pytest.raises(SystemExit, match="not consumed"):
        harness.bootstrap(tmp_path)
    assert any(url.endswith("/api/v1/join-tokens/tok-1") for url in harness.revokes())


def test_bootstrap_surfaces_a_provision_failure_class_and_revokes(tmp_path, monkeypatch):
    """A failed provision surfaces an ALLOWLISTED failure class — never the
    child's raw output — names the retry (no --force, no CA rotation), and
    revokes the token the human never saw."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path, provision_rc=1)
    with pytest.raises(SystemExit, match="CA fingerprint mismatch") as excinfo:
        harness.bootstrap(tmp_path)
    assert "re-run 'sudo theozolith init --with-local-node'" in str(excinfo.value)
    assert JOIN_STRING not in str(excinfo.value)
    assert any(url.endswith("/api/v1/join-tokens/tok-1") for url in harness.revokes())


def test_provision_output_containing_the_join_string_is_withheld(tmp_path, monkeypatch):
    """Amendment: provision stdout/stderr is never reprinted once a join
    string exists — an unrecognized failure quoting the exact join string
    (e.g. an echoed argv) surfaces as a withheld-output class."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(
        tmp_path,
        provision_rc=1,
        provision_stderr=f"usage: theozolith-nodedaemon provision {JOIN_STRING} --node localbox",
    )
    with pytest.raises(SystemExit, match="unrecognized failure") as excinfo:
        harness.bootstrap(tmp_path)
    message = str(excinfo.value)
    assert JOIN_STRING not in message
    assert "MACHINE-ONLY" not in message  # not even a fragment of it
    assert "withheld" in message
    assert any(url.endswith("/api/v1/join-tokens/tok-1") for url in harness.revokes())


def test_mint_without_an_id_is_rejected_without_leaking(tmp_path, monkeypatch):
    """A 200 mint missing its token id is unusable (nothing to revoke) and
    is rejected WITHOUT interpolating the response — the join string it
    carried never surfaces; the TTL backstop is named."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path, mint_response=(200, {"join_string": JOIN_STRING}))
    with pytest.raises(SystemExit, match="could not mint") as excinfo:
        harness.bootstrap(tmp_path)
    message = str(excinfo.value)
    assert JOIN_STRING not in message and "MACHINE-ONLY" not in message
    assert "TTL" in message
    assert harness.revokes() == []  # no id, nothing identifiable to revoke


def test_mint_with_an_id_but_no_join_string_is_revoked(tmp_path, monkeypatch):
    """The inverse defect: an id without a join string is unusable but
    identifiable — cleanup revokes it on the way out."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path, mint_response=(200, {"id": "tok-1"}))
    with pytest.raises(SystemExit, match="could not mint"):
        harness.bootstrap(tmp_path)
    assert any(url.endswith("/api/v1/join-tokens/tok-1") for url in harness.revokes())


def test_mint_failure_never_interpolates_the_response(tmp_path, monkeypatch):
    """A refused mint whose body carries a join string (whatever the server
    echoed) is reported by status alone — the response is never
    interpolated into the diagnostic."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path, mint_response=(500, {"detail": "boom", "join_string": JOIN_STRING}))
    with pytest.raises(SystemExit, match="could not mint") as excinfo:
        harness.bootstrap(tmp_path)
    message = str(excinfo.value)
    assert JOIN_STRING not in message and "MACHINE-ONLY" not in message
    assert "HTTP 500" in message
    assert harness.revokes() == []  # a non-200 mint carries no trusted id


def _service_user(monkeypatch, uid: int | None = None):
    """Fake the ozolith service user as PRESENT: identity validation
    resolves it, and the install phase's useradd is skipped (irrelevant to
    reconcile tests). Default uid = the test's own files' owner."""
    import os
    import pwd

    resolved = os.getuid() if uid is None else uid
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_name=name, pw_uid=resolved, pw_gid=os.getgid()),
    )


def test_reconcile_fresh_node_with_valid_identity_is_a_noop(tmp_path, monkeypatch):
    """A node whose heartbeat is FRESH on the server clock, backed by a
    valid on-disk identity, is reconciled without a restart or a join."""
    _service_user(monkeypatch)
    harness = Harness(tmp_path, pre_row={"version": "0.3.0", "last_seen": NOW - 10})
    harness.write_identity()
    harness.bootstrap(tmp_path)
    assert not any(c[1:2] == ["provision"] for c in harness.calls)
    assert not harness.restarted_daemon()
    assert not any("join-tokens" in url for _, url, _ in harness.fetches)
    assert any("(fresh)" in line for line in harness.logs)


def test_reconcile_stale_historical_version_restarts_and_demands_a_heartbeat(tmp_path, monkeypatch):
    """Amendment defect: a historical non-empty version is NOT liveness. A
    stale last_seen (dead daemon, old heartbeats) is restarted and resume
    succeeds only after a heartbeat newer than the stale baseline arrives,
    fresh on the server clock."""
    _service_user(monkeypatch)
    harness = Harness(tmp_path, pre_row={"version": "0.3.0", "last_seen": NOW - 10_000})
    harness.write_identity()
    harness.bootstrap(tmp_path)
    assert harness.restarted_daemon()
    assert not any(c[1:2] == ["provision"] for c in harness.calls)  # never re-provisioned
    assert not any("(fresh)" in line for line in harness.logs)  # stale never read as fresh
    assert any("not demonstrably" in line for line in harness.logs)
    assert any("registered and heartbeating" in line for line in harness.logs)


def test_reconcile_restarts_a_provisioned_but_silent_node(tmp_path, monkeypatch):
    """A node the exchange registered but whose daemon never heartbeat
    (version empty, registration touch recent) is restarted — never
    deleted, never re-provisioned."""
    _service_user(monkeypatch)
    harness = Harness(tmp_path, pre_row={"version": "", "last_seen": NOW - 5})
    harness.write_identity()
    harness.bootstrap(tmp_path)
    assert not any(c[1:2] == ["provision"] for c in harness.calls)
    assert ["systemctl", "enable", "theozolith-nodedaemon.service"] in harness.calls
    assert harness.restarted_daemon()
    assert any("registered and heartbeating" in line for line in harness.logs)


def test_reconcile_fails_explicitly_on_missing_or_corrupt_identity(tmp_path, monkeypatch):
    """A registered row is trusted only on top of a valid local identity:
    missing state fails explicitly with recovery/reprovision instructions
    — never a success report, never a restart of a daemon that cannot be
    the registered node, never an automatic deletion."""
    _service_user(monkeypatch)
    # Missing identity entirely — even with a FRESH row.
    harness = Harness(tmp_path, pre_row={"version": "0.3.0", "last_seen": NOW - 10})
    with pytest.raises(SystemExit, match="cannot back it") as excinfo:
        harness.bootstrap(tmp_path)
    assert "Nothing is deleted automatically" in str(excinfo.value)
    assert not harness.restarted_daemon()
    assert not any(c[1:2] == ["provision"] for c in harness.calls)

    # Corrupt identity (wrong CA pin) on a STALE row: same explicit refusal.
    harness = Harness(tmp_path, pre_row={"version": "0.3.0", "last_seen": NOW - 10_000})
    harness.write_identity(ca=b"--a different ca--")
    with pytest.raises(SystemExit, match="not this deployment's CA"):
        harness.bootstrap(tmp_path)
    assert not harness.restarted_daemon()


def test_identity_dial_address_is_parsed_structurally():
    """Round-4 defect: a prefix check accepted deceptive URL shapes. The
    structural parser requires exactly the https loopback origin on the
    expected port — no userinfo, deceptive suffixes, path, query, or
    fragment."""
    problem = localnode._dial_address_problem
    assert problem("https://127.0.0.1", 443) is None
    assert problem("https://127.0.0.1/", 443) is None
    assert problem("https://127.0.0.1:9443", 9443) is None
    assert "not exactly 127.0.0.1" in problem("https://127.0.0.1.example.com", 443)
    assert "userinfo" in problem("https://127.0.0.1@remote.example", 443)
    assert "port" in problem("https://127.0.0.1:9443", 443)  # wrong loopback port
    assert "port" in problem("https://127.0.0.1", 9443)  # implicit 443 vs expected
    assert "not https" in problem("http://127.0.0.1", 443)
    assert "path" in problem("https://127.0.0.1/api", 443)
    assert "path" in problem("https://127.0.0.1?x=1", 443)
    assert "path" in problem("https://127.0.0.1#f", 443)
    assert "malformed port" in problem("https://127.0.0.1:abc", 443)


def test_identity_rejects_deceptive_dial_addresses(tmp_path, monkeypatch):
    """Bootstrap-level: the deceptive-suffix and userinfo shapes fail the
    reconcile with the explicit cannot-back-it refusal."""
    _service_user(monkeypatch)
    for url, needle in (
        ("https://127.0.0.1.example.com", "not exactly 127.0.0.1"),
        ("https://127.0.0.1@remote.example", "userinfo"),
        ("https://127.0.0.1:9443", "port"),
    ):
        harness = Harness(tmp_path, pre_row={"version": "0.3.0", "last_seen": NOW - 10})
        harness.write_identity(control_url=url)
        with pytest.raises(SystemExit, match="cannot back it") as excinfo:
            harness.bootstrap(tmp_path)
        assert needle in str(excinfo.value)
        assert not harness.restarted_daemon()


def test_identity_rejects_files_the_service_user_cannot_read(tmp_path, monkeypatch):
    """'Validated identity' means the DAEMON can use it: artifacts owned by
    someone else (root-readable, ozolith-unreadable) are rejected."""
    import os

    _service_user(monkeypatch, uid=os.getuid() + 4242)  # files belong to 'root' stand-in
    harness = Harness(tmp_path, pre_row={"version": "0.3.0", "last_seen": NOW - 10})
    harness.write_identity()
    with pytest.raises(SystemExit, match="cannot back it") as excinfo:
        harness.bootstrap(tmp_path)
    assert "not owned" in str(excinfo.value)

    # Unit-level: the per-file contract names the unreadable artifact.
    token = harness.state_dir / "node-token"
    assert "not owned by" in localnode._artifact_problem(
        token, os.getuid() + 4242, confidential=True
    )
    token.chmod(0o644)  # owner-correct but world-readable secret
    assert "must be private" in localnode._artifact_problem(token, os.getuid(), confidential=True)


def test_identity_rejects_symlinked_artifacts(tmp_path, monkeypatch):
    _service_user(monkeypatch)
    harness = Harness(tmp_path, pre_row={"version": "0.3.0", "last_seen": NOW - 10})
    harness.write_identity()
    real = tmp_path / "elsewhere-ca.pem"
    real.write_bytes(FAKE_CA)
    (harness.state_dir / "ca.pem").unlink()
    (harness.state_dir / "ca.pem").symlink_to(real)
    with pytest.raises(SystemExit, match="symlink"):
        harness.bootstrap(tmp_path)


def test_nonsuccess_mint_with_an_id_is_revoked(tmp_path, monkeypatch):
    """Round-4 defect: a non-200 mint that still returned an identifiable
    token id must be revoked — and a response carrying both id and join
    string leaks neither into the diagnostic."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path, mint_response=(503, {"id": "tok-1", "join_string": JOIN_STRING}))
    with pytest.raises(SystemExit, match="could not mint") as excinfo:
        harness.bootstrap(tmp_path)
    message = str(excinfo.value)
    assert JOIN_STRING not in message and "MACHINE-ONLY" not in message
    assert "tok-1" not in message  # the id is for cleanup, never for display
    assert any(url.endswith("/api/v1/join-tokens/tok-1") for url in harness.revokes())


def test_hostile_token_id_shapes_are_never_captured(tmp_path, monkeypatch):
    """The id rides the revocation URL as a path segment: only the
    allowlisted shape is captured — a hostile id cannot steer the DELETE
    (and an uncapturable id means no revocation attempt, TTL backstop)."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(
        tmp_path,
        mint_response=(200, {"id": "../nodes/box1/revoke", "join_string": JOIN_STRING}),
    )
    with pytest.raises(SystemExit, match="could not mint"):
        harness.bootstrap(tmp_path)
    assert harness.revokes() == []


def test_interrupt_stops_the_listener_and_revokes(tmp_path, monkeypatch):
    """An interrupt between mint and consumption stops the temporary
    listener and revokes the machine-only token on the way out."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    listeners: list[SimpleNamespace] = []

    class FakeListener:
        def __init__(self, **kwargs):
            self.state = SimpleNamespace(started=False, stopped=False)
            self.port = 65001
            listeners.append(self.state)

        def start(self):
            self.state.started = True

        def stop(self):
            self.state.stopped = True

    monkeypatch.setattr(localnode, "BootstrapServer", FakeListener)
    harness = Harness(tmp_path)
    original_fetch = harness.fetch

    def interrupting_fetch(method, url, *, token, ca, body=None):
        if method == "GET" and url.endswith("/api/v1/join-tokens"):
            raise KeyboardInterrupt
        return original_fetch(method, url, token=token, ca=ca, body=body)

    harness.fetch = interrupting_fetch
    with pytest.raises(KeyboardInterrupt):
        harness.bootstrap(tmp_path)
    assert listeners and listeners[0].stopped  # temporary resource removed
    assert any(url.endswith("/api/v1/join-tokens/tok-1") for url in harness.revokes())


# -- the live transport: local bootstrap rides the ONE bearer client (OZ-03) ------


class _PlaintextTrap:
    """An instrumented plaintext TCP listener on its own port: a leaked
    redirect hop is provable — zero connections, zero bytes, and therefore
    zero Authorization headers."""

    def __init__(self):
        import socket
        import threading

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
            except OSError:
                pass
            finally:
                conn.close()

    def stop(self):
        self._stop.set()
        self._thread.join(2)
        self._sock.close()


def _quiet_handler(answers):
    """A BaseHTTPRequestHandler answering GET from ``answers`` —
    (status, headers, body) keyed by path, with a default."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            status, headers, body = answers.get(self.path, answers["*"])
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

    return Handler


def _tls_loopback_server(tmp_path, answers):
    """``answers`` served on a REAL loopback TLS socket whose certificate is
    minted by the same ``tls.provision`` the init flow runs — the pinned
    loopback HTTPS origin the local bootstrap dials. Returns
    (server, thread, port, ca_path)."""
    import http.server
    import ssl
    import threading

    from theozolith_control import tls

    ca, cert, key = tls.provision(tmp_path / "live-tls", ["127.0.0.1"], trust_root=tmp_path)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _quiet_handler(answers))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1], str(ca)


def _stop_server(server, thread):
    server.shutdown()
    thread.join(2)
    server.server_close()


def test_local_bootstrap_redirect_trap_gets_zero_bytes_and_no_authorization(tmp_path):
    """LIVE: a hostile answer on the pinned loopback HTTPS origin redirects
    toward a different-port PLAINTEXT trap. The shared transport refuses the
    hop BEFORE any redirected request, the refusal surfaces as the mapped
    clean SystemExit (no token in the message), and the trap records zero
    connections, zero bytes, zero Authorization headers."""
    import time

    trap = _PlaintextTrap()
    answers = {"*": (302, {"Location": f"http://127.0.0.1:{trap.port}/steal"}, b"")}
    server, thread, port, ca = _tls_loopback_server(tmp_path, answers)
    try:
        with pytest.raises(SystemExit, match="local control channel refused") as excinfo:
            localnode._http(
                "GET", f"https://127.0.0.1:{port}/api/v1/state", token="admin-secret", ca=ca
            )
        assert "admin-secret" not in str(excinfo.value)
        time.sleep(0.2)  # anything in flight would have landed
        assert trap.connections == 0
        assert bytes(trap.data) == b""
    finally:
        _stop_server(server, thread)
        trap.stop()


def test_local_bootstrap_same_origin_https_requests_still_succeed(tmp_path):
    """LIVE: the happy path through the shared transport — loopback TLS
    verified against the minted CA, status and parsed JSON preserved, and an
    HTTP error answer mapped to (code, detail) exactly as before."""
    answers = {
        "/api/v1/state": (
            200,
            {"Content-Type": "application/json"},
            b'{"now": 1.0, "nodes": []}',
        ),
        "*": (404, {"Content-Type": "text/plain"}, b"nope"),
    }
    server, thread, port, ca = _tls_loopback_server(tmp_path, answers)
    try:
        status, answer = localnode._http(
            "GET", f"https://127.0.0.1:{port}/api/v1/state", token="admin-secret", ca=ca
        )
        assert status == 200 and answer == {"now": 1.0, "nodes": []}
        status, answer = localnode._http(
            "GET", f"https://127.0.0.1:{port}/api/v1/missing", token="admin-secret", ca=ca
        )
        assert status == 404 and answer == {"detail": "nope"}
    finally:
        _stop_server(server, thread)


def test_revocation_swallows_a_transport_refusal_and_keeps_the_original_error(
    tmp_path, monkeypatch
):
    """``_http`` maps a transport policy refusal to SystemExit; when that
    happens during BEST-EFFORT revocation the original failure must keep
    unwinding — cleanup never replaces the error being reported (the token's
    one-hour TTL is the backstop)."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path)
    original_fetch = harness.fetch

    def refusing_cleanup_fetch(method, url, *, token, ca, body=None):
        if method == "DELETE":
            raise SystemExit("error: the local control channel refused a request: redirect trap")
        if method == "GET" and url.endswith("/api/v1/join-tokens"):
            raise KeyboardInterrupt  # the original failure, mid-join
        return original_fetch(method, url, token=token, ca=ca, body=body)

    harness.fetch = refusing_cleanup_fetch
    with pytest.raises(KeyboardInterrupt):  # NOT the cleanup SystemExit
        harness.bootstrap(tmp_path)


def test_heartbeat_timeout_keeps_the_provisioned_node(tmp_path, monkeypatch):
    """A first-heartbeat timeout deletes nothing: the message says so and
    names the resume command; the consumed token is not revoked."""
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path)
    original_rows = harness._node_rows

    def never_heartbeats():
        rows = original_rows()
        return [{**row, "version": ""} for row in rows]

    harness._node_rows = never_heartbeats
    ticks = iter([0.0] * 8 + [10_000.0] * 8)
    with pytest.raises(SystemExit, match=r"nothing was\s+deleted") as excinfo:
        localnode.bootstrap_local_node(
            harness.settings,
            node_name="localbox",
            nodedaemon_exec=NODEDAEMON_EXEC,
            runner=harness.runner,
            fetch=harness.fetch,
            sleep=lambda seconds: None,
            clock=lambda: next(ticks),
            unit_path=tmp_path / "nodedaemon.service",
            state_dir=tmp_path / "state",
            log=harness.logs.append,
        )
    assert "resume" in str(excinfo.value)
    assert harness.revokes() == []  # the consumed token is left alone


def test_bootstrap_times_out_when_serve_never_answers(tmp_path, monkeypatch):
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    harness = Harness(tmp_path)

    def dead_fetch(method, url, *, token, ca, body=None):
        raise OSError("connection refused")

    ticks = iter([0.0, 0.0, 1000.0, 2000.0, 3000.0])
    with pytest.raises(SystemExit, match="systemctl status"):
        localnode.bootstrap_local_node(
            harness.settings,
            node_name="localbox",
            nodedaemon_exec=NODEDAEMON_EXEC,
            runner=harness.runner,
            fetch=dead_fetch,
            sleep=lambda seconds: None,
            clock=lambda: next(ticks),
            unit_path=tmp_path / "nodedaemon.service",
            state_dir=tmp_path / "state",
            log=harness.logs.append,
        )


# -- the CLI retry path: resume without --force (ADR-0037 amendment) -------------


@pytest.fixture
def local_cli(tmp_path, monkeypatch):
    """A fake root bare-metal box wired for init --with-local-node: the
    privileged seams are stubbed, the partition is real."""
    import socket

    from theozolith_control import cli

    home = tmp_path / "home"
    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(home))
    monkeypatch.delenv("THEOZOLITH_CONFIG_REPO", raising=False)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(cli, "_running_in_container", lambda: False)
    monkeypatch.setattr(cli, "_systemd_present", lambda: True)
    monkeypatch.setattr(cli, "_validated_root_data_dir", lambda data_dir: data_dir)
    monkeypatch.setattr(cli, "_service_executable", lambda: "/usr/local/bin/theozolith")
    monkeypatch.setattr(cli, "_install_systemd_unit", lambda settings, port: True)
    monkeypatch.setattr(localnode, "ensure_preconditions", lambda: NODEDAEMON_EXEC)
    return SimpleNamespace(home=home, node=socket.gethostname())


def test_failed_bootstrap_retries_without_force_or_ca_rotation(local_cli, monkeypatch, capsys):
    """M8 amendment acceptance: a failed local bootstrap retries with the
    SAME command — no --force, no CA rotation, operator edits preserved,
    and the retry goes through the resume path."""
    from theozolith_control.cli import main as cli_main

    attempts: list[str] = []

    def failing_bootstrap(settings, *, node_name, **kwargs):
        attempts.append(node_name)
        raise SystemExit(f"error: local node provisioning failed: boom\n{localnode.RESUME_HINT}")

    monkeypatch.setattr(localnode, "bootstrap_local_node", failing_bootstrap)
    with pytest.raises(SystemExit, match="resume"):
        cli_main(["init", "--ip", "192.0.2.20", "--with-local-node"])
    ca_after_first = (local_cli.home / "secrets" / "tls" / "ca.pem").read_bytes()

    # The operator edits a scaffold file between attempts — in the Config
    # Repo, committed (ADR-0048: the pinned build is machine-owned; edits are
    # authored in config-src and re-ingested by the resume).
    stack = local_cli.home / "config-src" / "stacks" / "implementer.toml"
    edited = stack.read_text() + "\n# operator note\n"
    stack.write_text(edited)
    for argv in (
        ["git", "add", "-A"],
        ["git", "-c", "user.name=op", "-c", "user.email=op@invalid", "commit", "-q", "-m", "note"],
    ):
        subprocess.run(argv, cwd=str(stack.parent.parent), check=True, capture_output=True)

    def succeeding_bootstrap(settings, *, node_name, **kwargs):
        attempts.append("resumed")

    monkeypatch.setattr(localnode, "bootstrap_local_node", succeeding_bootstrap)
    assert cli_main(["init", "--with-local-node"]) == 0  # no --force, no --ip
    out = capsys.readouterr().out
    assert "resuming the local node in place" in out
    assert "Single-Node Deployment" in out
    assert (local_cli.home / "secrets" / "tls" / "ca.pem").read_bytes() == ca_after_first
    assert stack.read_text() == edited  # operator edit survived the retry
    # ...and the resume's re-ingest materialized it into the pinned build.
    assert (
        "# operator note"
        in (local_cli.home / "configs" / "stacks" / "implementer.toml").read_text()
    )
    assert attempts == [local_cli.node, "resumed"]


def test_resume_refuses_when_standard_init_state_is_missing(local_cli, monkeypatch):
    """Resume reconciles a partial local bootstrap, not a damaged
    partition: missing standard-init artifacts are named and pointed at
    recover / --force."""
    from theozolith_control.cli import main as cli_main

    monkeypatch.setattr(localnode, "bootstrap_local_node", lambda settings, **kwargs: None)
    assert cli_main(["init", "--ip", "192.0.2.20", "--with-local-node"]) == 0
    (local_cli.home / "secrets" / "tls" / "ca.key").unlink()
    with pytest.raises(SystemExit, match="cannot resume") as excinfo:
        cli_main(["init", "--with-local-node"])
    assert "ca.key" in str(excinfo.value)


def test_plain_rerun_still_requires_force_and_names_the_resume(local_cli, monkeypatch):
    """Without --with-local-node the initialized guard stands unchanged —
    and now names the resume alternative."""
    from theozolith_control.cli import main as cli_main

    monkeypatch.setattr(localnode, "bootstrap_local_node", lambda settings, **kwargs: None)
    assert cli_main(["init", "--ip", "192.0.2.20", "--with-local-node"]) == 0
    with pytest.raises(SystemExit, match="already initialized") as excinfo:
        cli_main(["init", "--ip", "192.0.2.20"])
    assert "init --with-local-node" in str(excinfo.value)


# -- the scaffold: complete, commented, stopped (acceptance 5) -------------------


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True
    ).stdout


def test_scaffold_is_complete_staged_and_committed(tmp_path):
    repo = tmp_path / "configs"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)

    written = localnode.write_scaffold(repo, "localbox", log=lambda _: None)
    assert sorted(written) == [
        "README.md",
        "stacks/implementer.toml",
        "worker-types/claude-dev.toml",
    ]

    # The scaffold parses under the real validator, staged stopped. The thin
    # Stack resolves through its worker type (ADR-0044).
    config = load_config(repo)
    worker = next(s for s in config.stacks if s.name == "implementer")
    assert worker.kind == "process" and worker.node == "localbox"
    assert worker.state == "stopped"
    assert worker.worker_type == "claude-dev"
    assert worker.env["THEOZOLITH_RUN_IMAGE"].startswith("theozolith/claude-dev:")
    assert set(worker.secrets.values()) == {"github-implementer", "anthropic-api-key"}
    assert "claude-dev" in config.worker_types  # placeholder digest passes validation

    # Stage-don't-deploy (ADR-0037): nothing rides desired state to build.
    assert config.desired_state_for("localbox")["images"] == []

    # Committed with the machine identity, like every machine write.
    assert _git(repo, "log", "--format=%an").strip() == "theozolith"
    assert "scaffold" in _git(repo, "log", "--format=%s")
    assert _git(repo, "status", "--porcelain").strip() == ""

    # The README names the finish line: digest pin, secrets, the flip.
    readme = (repo / "README.md").read_text()
    assert "docker inspect" in readme
    assert "theozolith secret set" in readme
    assert 'state = "running"' in readme
    assert "theozolith status" in readme


def test_scaffold_never_overwrites_operator_edits(tmp_path):
    repo = tmp_path / "configs"
    (repo / "stacks").mkdir(parents=True)
    edited = 'worker_type = "claude-dev"\nnode = "elsewhere"\n'
    (repo / "stacks" / "implementer.toml").write_text(edited)
    written = localnode.write_scaffold(repo, "localbox", log=lambda _: None)
    assert "stacks/implementer.toml" not in written
    assert (repo / "stacks" / "implementer.toml").read_text() == edited
