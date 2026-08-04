"""TLS on the channel (acceptance 7): minted CA + server cert, a real
uvicorn server, the real node-side client verifying against the CA — and
the renewal lifecycle (PR #15): Apple-compliant leaves, the expiry-warning
policy, and interruption-safe re-minting."""

from __future__ import annotations

import datetime
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from controlrig import ADMIN_TOKEN, make_rig
from theozolith_control import tls as tls_module
from theozolith_control.tls import (
    ca_fingerprint_sha256,
    leaf_expiry_warning,
    pair_matches,
    provision,
    remint_server_cert,
)
from theozolith_nodedaemon.controlclient import ControlClient

SENTINEL = "tls-transported-secret-value"

WORKER_STACK = (
    'kind = "process"\nnode = "box1"\ncommand = "theozolith-worker"\n'
    '[secrets]\nWORKER_GITHUB_TOKEN = "github-worker"\n'
)


class LiveServer:
    """The app on a real socket (uvicorn in a thread), TLS optional."""

    def __init__(self, app, certfile: str | None = None, keyfile: str | None = None):
        self._config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="error",
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
        )
        self.server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)
        self.scheme = "https" if certfile else "http"

    def __enter__(self) -> LiveServer:
        self._thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"{self.scheme}://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self._thread.join(10)


def test_provision_mints_ca_and_server_material(tmp_path: Path):
    ca, cert, key = provision(tmp_path / "tls", ["controlnode.lan", "127.0.0.1"])
    assert ca.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert cert.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert (key.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "tls" / "ca.key").stat().st_mode & 0o777) == 0o600


def test_server_cert_meets_apple_trust_requirements(tmp_path: Path):
    """The optional browser green lock (ADR-0034) is only reachable if the
    leaf satisfies Apple's trust stack: a serverAuth EKU and validity of at
    most 825 days — a 10-year leaf is rejected by Safari/iOS even under a
    trusted CA. The CA itself may stay long-lived (roots are exempt)."""
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    ca_path, cert_path, _key = provision(tmp_path / "tls", ["127.0.0.1"])
    leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    leaf_days = (leaf.not_valid_after_utc - leaf.not_valid_before_utc).days
    assert leaf_days <= 825
    ca_cert = x509.load_pem_x509_certificate(ca_path.read_bytes())
    assert (ca_cert.not_valid_after_utc - ca_cert.not_valid_before_utc).days > 825

    # The recover re-mint issues through the same path: same properties.
    from theozolith_control.tls import remint_server_cert

    new_cert, _ = remint_server_cert(tmp_path / "tls", ["127.0.0.1"])
    reminted = x509.load_pem_x509_certificate(new_cert.read_bytes())
    eku = reminted.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    assert (reminted.not_valid_after_utc - reminted.not_valid_before_utc).days <= 825


def test_secrets_transit_tls_end_to_end(tmp_path: Path):
    """CLI-entered value -> encrypted store -> node-scoped pull, all over a
    genuinely TLS channel verified against the minted CA."""
    ca, cert, key = provision(tmp_path / "tls", ["127.0.0.1"])
    rig = make_rig(tmp_path, secrets_channel_ok=True)
    rig.write_config("stacks/worker.toml", WORKER_STACK)

    with LiveServer(rig.client.app, certfile=str(cert), keyfile=str(key)) as live:
        # Admin entry over TLS (what `theozolith secret set` does).
        request = urllib.request.Request(
            f"{live.url}/api/v1/secrets/github-worker",
            data=json.dumps({"value": SENTINEL}).encode(),
            method="PUT",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"},
        )
        context = ssl.create_default_context(cafile=str(ca))
        with urllib.request.urlopen(request, context=context) as resp:
            assert resp.status == 200

        # The REAL node-side client, pinned to the CA (THEOZOLITH_TLS_CA).
        client = ControlClient(live.url, rig.node_token(), ca=str(ca))
        assert client.pull_secrets("box1", ["github-worker"]) == {"github-worker": SENTINEL}

        # Without the CA the handshake itself fails: nothing transits.
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(f"{live.url}/api/v1/healthz")


def test_plain_http_server_refuses_secret_traffic(tmp_path: Path):
    """A Control Node serving plaintext (no --insecure-dev) refuses both
    secret entry and pulls: TLS is mandatory for values, end to end."""
    rig = make_rig(tmp_path, secrets_channel_ok=False)
    rig.write_config("stacks/worker.toml", WORKER_STACK)

    with LiveServer(rig.client.app) as live:
        request = urllib.request.Request(
            f"{live.url}/api/v1/secrets/github-worker",
            data=json.dumps({"value": SENTINEL}).encode(),
            method="PUT",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request)
        assert denied.value.code == 403

        client = ControlClient(live.url, rig.node_token(), insecure_dev=True)  # client would try…
        with pytest.raises(Exception, match="403"):
            client.pull_secrets("box1", ["github-worker"])  # …the server still refuses


def test_wildcard_hosts_are_refused(tmp_path):
    """ADR-0019 acceptance 9: one TLS identity per deployment — the tooling
    will never mint a shareable wildcard key."""
    with pytest.raises(ValueError, match="wildcard"):
        provision(tmp_path / "tls", ["*.theozolith.com"])


# -- the renewal lifecycle (PR #15) ----------------------------------------------


def _load_cert(path: Path):
    from cryptography import x509

    return x509.load_pem_x509_certificate(path.read_bytes())


def test_leaf_lifetime_is_exactly_the_policy(tmp_path: Path):
    """Initial and re-minted leaves both live the policy lifetime — the
    explicit tolerance is the 5-minute clock-skew backdate."""
    policy = datetime.timedelta(days=tls_module._SERVER_VALID_DAYS, minutes=5)
    _, cert_path, _ = provision(tmp_path / "tls", ["127.0.0.1"])
    initial = _load_cert(cert_path)
    assert initial.not_valid_after_utc - initial.not_valid_before_utc == policy
    new_cert, _ = remint_server_cert(tmp_path / "tls", ["127.0.0.1"])
    reminted = _load_cert(new_cert)
    assert reminted.not_valid_after_utc - reminted.not_valid_before_utc == policy


def test_remint_chains_to_the_original_ca_and_changes_no_trust_material(tmp_path: Path):
    """Routine renewal: the new leaf is signed by the ORIGINAL CA, matches
    its new key, and carries the requested SAN — while the CA certificate
    and its fingerprint (what every join string pins and every node and
    device trusts) are byte-identical. Zero node-side changes."""
    ca_path, cert_path, key_path = provision(tmp_path / "tls", ["127.0.0.1"])
    ca_before = ca_path.read_bytes()
    fingerprint_before = ca_fingerprint_sha256(ca_before)
    original_ca = _load_cert(ca_path)

    remint_server_cert(tmp_path / "tls", ["10.0.0.9"])  # recovery's new-IP case

    reminted = _load_cert(cert_path)
    reminted.verify_directly_issued_by(original_ca)  # raises on a chain break
    assert pair_matches(cert_path, key_path)
    from cryptography import x509

    san = reminted.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["10.0.0.9"]
    assert ca_path.read_bytes() == ca_before
    assert ca_fingerprint_sha256(ca_path.read_bytes()) == fingerprint_before


def test_expiry_warning_fires_at_and_inside_the_threshold_only(tmp_path: Path):
    _, cert_path, _ = provision(tmp_path / "tls", ["127.0.0.1"])
    expiry = _load_cert(cert_path).not_valid_after_utc
    threshold = datetime.timedelta(days=tls_module.LEAF_EXPIRY_WARNING_DAYS)

    assert (
        leaf_expiry_warning(cert_path, now=expiry - threshold - datetime.timedelta(days=1)) is None
    )
    at = leaf_expiry_warning(cert_path, now=expiry - threshold)
    inside = leaf_expiry_warning(cert_path, now=expiry - datetime.timedelta(days=5))
    expired = leaf_expiry_warning(cert_path, now=expiry + datetime.timedelta(days=1))
    assert at and inside and expired
    assert "EXPIRED" in expired

    # Self-contained operator guidance: date, remaining time, both renewal
    # commands, the restart, and the nodes-need-nothing guarantee.
    assert expiry.strftime("%Y-%m-%d") in inside
    assert "day(s) left" in inside
    assert "theozolith recover" in inside
    assert "docker compose" in inside
    assert "restart" in inside
    assert "CA is unchanged" in inside and "NO action" in inside


def test_staged_write_failures_preserve_the_live_pair(tmp_path: Path, monkeypatch):
    """A failure while staging either artifact leaves the previous pair
    byte-identical and no staged debris behind."""
    _, cert_path, key_path = provision(tmp_path / "tls", ["127.0.0.1"])
    before = (cert_path.read_bytes(), key_path.read_bytes())
    real_stage = tls_module._stage

    for victim in ("server.pem.staged", "server.key.staged"):

        def failing_stage(path, data, *, private=False, _victim=victim):
            if path.name == _victim:
                raise OSError("disk full")
            return real_stage(path, data, private=private)

        monkeypatch.setattr(tls_module, "_stage", failing_stage)
        with pytest.raises(ValueError, match="before taking effect"):
            remint_server_cert(tmp_path / "tls", ["127.0.0.1"])
        assert (cert_path.read_bytes(), key_path.read_bytes()) == before
        assert not list((tmp_path / "tls").glob("*.staged"))
        assert pair_matches(cert_path, key_path)


def test_promotion_failure_restores_the_live_pair(tmp_path: Path, monkeypatch):
    """A failure between the two promotions (the mixed-pair window) is
    rolled back: the previous pair comes back whole."""
    _, cert_path, key_path = provision(tmp_path / "tls", ["127.0.0.1"])
    before = (cert_path.read_bytes(), key_path.read_bytes())
    real_replace = os.replace
    calls = {"n": 0}

    def failing_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the key already promoted; the cert fails
            raise OSError("interrupted")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(ValueError, match="before taking effect"):
        remint_server_cert(tmp_path / "tls", ["127.0.0.1"])
    monkeypatch.setattr(os, "replace", real_replace)
    assert (cert_path.read_bytes(), key_path.read_bytes()) == before
    assert pair_matches(cert_path, key_path)
    assert not list((tmp_path / "tls").glob("*.staged"))


def test_renewed_pair_serves_real_tls(tmp_path: Path):
    """The whole point: after a renewal, the real TLS server loads the new
    pair and a client verifying against the UNCHANGED CA connects."""
    ca, _, _ = provision(tmp_path / "tls", ["127.0.0.1"])
    new_cert, new_key = remint_server_cert(tmp_path / "tls", ["127.0.0.1"])
    rig = make_rig(tmp_path)
    with LiveServer(rig.client.app, certfile=str(new_cert), keyfile=str(new_key)) as live:
        context = ssl.create_default_context(cafile=str(ca))
        with urllib.request.urlopen(f"{live.url}/api/v1/healthz", context=context) as resp:
            assert resp.status == 200
