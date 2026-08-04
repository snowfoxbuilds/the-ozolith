"""TLS on the channel (acceptance 7): minted CA + server cert, a real
uvicorn server, and the real node-side client verifying against the CA."""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from controlrig import ADMIN_TOKEN, make_rig
from theozolith_control.tls import provision
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
