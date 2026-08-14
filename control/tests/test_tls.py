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

# A neutral process Stack (the worker semantics are irrelevant to TLS
# transport); a built-in-driver command would be rejected (ADR-0044).
WORKER_STACK = (
    'kind = "process"\nnode = "box1"\ncommand = "sleep 30"\n'
    '[secrets]\nIMPLEMENTER_GITHUB_TOKEN = "github-implementer"\n'
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
    ca, cert, key = provision(
        tmp_path / "tls", ["controlnode.lan", "127.0.0.1"], trust_root=tmp_path
    )
    assert ca.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert cert.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert (key.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "tls" / "ca.key").stat().st_mode & 0o777) == 0o600


def test_secrets_transit_tls_end_to_end(tmp_path: Path):
    """CLI-entered value -> encrypted store -> node-scoped pull, all over a
    genuinely TLS channel verified against the minted CA."""
    ca, cert, key = provision(tmp_path / "tls", ["127.0.0.1"], trust_root=tmp_path)
    rig = make_rig(tmp_path, secrets_channel_ok=True)
    rig.write_config("stacks/worker.toml", WORKER_STACK)

    with LiveServer(rig.client.app, certfile=str(cert), keyfile=str(key)) as live:
        # Admin entry over TLS (what `theozolith secret set` does).
        request = urllib.request.Request(
            f"{live.url}/api/v1/secrets/github-implementer",
            data=json.dumps({"value": SENTINEL}).encode(),
            method="PUT",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"},
        )
        context = ssl.create_default_context(cafile=str(ca))
        with urllib.request.urlopen(request, context=context) as resp:
            assert resp.status == 200

        # The REAL node-side client, pinned to the CA (THEOZOLITH_TLS_CA).
        client = ControlClient(live.url, rig.node_token(), ca=str(ca))
        assert client.pull_secrets("box1", ["github-implementer"]) == {
            "github-implementer": SENTINEL
        }

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
            f"{live.url}/api/v1/secrets/github-implementer",
            data=json.dumps({"value": SENTINEL}).encode(),
            method="PUT",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request)
        assert denied.value.code == 403

        client = ControlClient(live.url, rig.node_token(), insecure_dev=True)  # client would try…
        with pytest.raises(Exception, match="403"):
            client.pull_secrets("box1", ["github-implementer"])  # …the server still refuses


def test_wildcard_hosts_are_refused(tmp_path):
    """ADR-0019 acceptance 9: one TLS identity per deployment — the tooling
    will never mint a shareable wildcard key."""
    with pytest.raises(ValueError, match="wildcard"):
        provision(tmp_path / "tls", ["*.theozolith.com"], trust_root=tmp_path)


# -- OZ-02: the trusted-directory-descriptor writer -----------------------------
#
# The layout under test mirrors the real partition (ADR-0024): the trusted
# Control data root, its service-owned `secrets` descendant, and `secrets/tls`
# below that. A compromised service account owns everything BELOW the root and
# may plant symlinks anywhere in it; the root-run mint must never follow one.


def _data_layout(tmp_path: Path) -> tuple[Path, Path]:
    """(data_root, tls_dir) with the `secrets` level pre-created, as init does."""
    data = tmp_path / "data"
    (data / "secrets").mkdir(parents=True)
    return data, data / "secrets" / "tls"


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


def test_symlinked_secrets_parent_is_refused_through_the_full_mint(tmp_path: Path):
    """`secrets` itself replaced by a symlink: the complete initialization
    flow refuses at the traversal and the external target directory receives
    no files and no modifications."""
    data = tmp_path / "data"
    data.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "tls").mkdir(parents=True)
    (elsewhere / "tls" / "ca.key").write_bytes(b"someone-elses-key\n")
    before = _snapshot(elsewhere / "tls")
    (data / "secrets").symlink_to(elsewhere)

    with pytest.raises(OSError, match="refusing to traverse"):
        provision(data / "secrets" / "tls", ["127.0.0.1"], trust_root=data)
    assert _snapshot(elsewhere / "tls") == before
    assert list((elsewhere / "tls").iterdir()) == [elsewhere / "tls" / "ca.key"]


def test_symlinked_tls_parent_is_refused_through_mint_and_remint(tmp_path: Path):
    """`secrets/tls` replaced by a symlink: the initial mint refuses with the
    empty external target receiving nothing, and the re-mint flow
    (origin-init / recover) — whose CA read succeeds through the link —
    still refuses at the write traversal, leaving every external byte
    exactly as it was."""
    import shutil

    from theozolith_control.tls import remint_server_cert

    data, tls_dir = _data_layout(tmp_path)
    empty_target = tmp_path / "empty-target"
    empty_target.mkdir()
    tls_dir.symlink_to(empty_target)
    with pytest.raises(OSError, match="refusing to traverse"):
        provision(tls_dir, ["127.0.0.1"], trust_root=data)
    assert list(empty_target.iterdir()) == []

    # Re-mint: a REAL CA sits behind the planted link (the read path follows
    # it — reads steer no writes), but the write traversal must refuse.
    tls_dir.unlink()
    provision(tls_dir, ["127.0.0.1"], trust_root=data)
    elsewhere = tmp_path / "elsewhere"
    shutil.move(tls_dir, elsewhere)
    before = _snapshot(elsewhere)
    tls_dir.symlink_to(elsewhere)
    with pytest.raises(OSError, match="refusing to traverse"):
        remint_server_cert(tls_dir, ["127.0.0.1"], trust_root=data)
    assert _snapshot(elsewhere) == before


def test_remint_over_a_planted_key_symlink_spares_the_target(tmp_path: Path):
    """The directories are real but the FILE name is a planted symlink: the
    re-mint refuses it and the external target keeps its exact bytes — the
    write is never steered through the link (OZ-02)."""
    from theozolith_control.tls import remint_server_cert

    data, tls_dir = _data_layout(tmp_path)
    provision(tls_dir, ["127.0.0.1"], trust_root=data)
    target = tmp_path / "outside-key"
    target.write_bytes(b"do-not-clobber\n")
    (tls_dir / "server.key").unlink()
    (tls_dir / "server.key").symlink_to(target)

    with pytest.raises(OSError, match="not a regular file"):
        remint_server_cert(tls_dir, ["127.0.0.1"], trust_root=data)
    assert target.read_bytes() == b"do-not-clobber\n"


def test_remint_replaces_existing_files_and_tightens_loose_permissions(tmp_path: Path):
    """The re-mint flow over existing material: files are REPLACED atomically
    with fresh content, and a pre-existing loose-mode key lands 0600 again —
    the old O_TRUNC path kept the wider mode (issue #46)."""
    from theozolith_control.tls import remint_server_cert

    data, tls_dir = _data_layout(tmp_path)
    provision(tls_dir, ["127.0.0.1"], trust_root=data)
    stale_cert = (tls_dir / "server.pem").read_bytes()
    (tls_dir / "server.key").chmod(0o644)

    cert, key = remint_server_cert(tls_dir, ["127.0.0.1"], trust_root=data)
    assert cert.read_bytes() != stale_cert
    assert key.stat().st_mode & 0o777 == 0o600


def test_unprivileged_mint_owns_its_files_and_makes_expected_modes(tmp_path: Path):
    """Unprivileged execution (dev, Compose): the writer never chowns — every
    artifact belongs to the invoking user with 0644 certs / 0600 keys, and
    created directories are owner-only."""
    import os

    data = tmp_path / "data"  # not pre-created: tls-init on a fresh box
    tls_dir = data / "secrets" / "tls"
    ca, cert, key = provision(tls_dir, ["127.0.0.1"], trust_root=data)
    for path, mode in ((ca, 0o644), (cert, 0o644), (key, 0o600), (tls_dir / "ca.key", 0o600)):
        info = path.stat()
        assert info.st_uid == os.getuid()
        assert info.st_mode & 0o777 == mode
    assert (data / "secrets").stat().st_mode & 0o777 == 0o700


def test_trust_root_reached_through_an_operator_symlink_still_works(tmp_path: Path):
    """Symlinks AT OR ABOVE the trusted root are the operator's (a Compose
    mount, /var indirection) and legitimate; only service-owned descendants
    are held to O_NOFOLLOW."""
    real = tmp_path / "real-data"
    real.mkdir()
    linked = tmp_path / "data-link"
    linked.symlink_to(real)

    ca, _cert, _key = provision(linked / "secrets" / "tls", ["127.0.0.1"], trust_root=linked)
    assert (real / "secrets" / "tls" / "ca.pem").read_bytes() == ca.read_bytes()


def test_interrupted_write_keeps_the_old_file_and_the_next_mint_recovers(
    tmp_path: Path, monkeypatch
):
    """A crash mid-write (fsync boundary) must leave the previous artifact
    intact and no half-written destination; a later mint succeeds even with a
    stale temp file parked in the directory."""
    import os as os_module

    from theozolith_control import tls as tls_module

    data, tls_dir = _data_layout(tmp_path)
    provision(tls_dir, ["127.0.0.1"], trust_root=data)
    before = _snapshot(tls_dir)

    real_fsync = os_module.fsync
    calls = {"n": 0}

    def failing_fsync(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk pulled")
        return real_fsync(fd)

    monkeypatch.setattr(tls_module.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="disk pulled"):
        tls_module.remint_server_cert(tls_dir, ["127.0.0.1"], trust_root=data)
    monkeypatch.setattr(tls_module.os, "fsync", real_fsync)
    assert _snapshot(tls_dir) == before  # old material intact, temp cleaned up

    (tls_dir / ".server.key.deadbeef.tmp").write_bytes(b"stale-crash-leftover")
    cert, key = tls_module.remint_server_cert(tls_dir, ["127.0.0.1"], trust_root=data)
    assert key.read_bytes() != before["server.key"]
    assert cert.read_bytes() != before["server.pem"]


def test_tls_dir_outside_the_trust_root_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="not inside the trusted root"):
        provision(tmp_path / "outside" / "tls", ["127.0.0.1"], trust_root=tmp_path / "data")
