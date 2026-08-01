"""The unified first run and recovery (ADR-0023/0024): init composes
everything and prints the handoff; the partition keeps every secret byte
under secrets/; cache.db is deletable; a restored folder recovers with one
command and zero node touches."""

from __future__ import annotations

import io
import shutil
import subprocess

import pytest
from theozolith_control import controltoml
from theozolith_control.cli import main as cli_main
from theozolith_control.crypto import SecretBox
from theozolith_control.passwords import parse_record
from theozolith_control.secretstore import SecretStore
from theozolith_control.settings import load_settings
from theozolith_control.store import Store

PASSWORD = "first-run-password"
SECRET_VALUE = "ghp_SUPERSECRETVALUE12345"


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "home"
    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(data))
    monkeypatch.delenv("THEOZOLITH_CONFIG_REPO", raising=False)
    monkeypatch.delenv("THEOZOLITH_PUBLIC_ORIGIN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    return data


def _init(home, *args) -> int:
    return cli_main(["init", "--ip", "127.0.0.1", *args])


def test_init_produces_the_partition_and_the_handoff(home, capsys):
    """Acceptance 2: the full partition layout plus the operator handoff —
    exact hosts line, CA URL, per-OS trust instructions."""
    assert _init(home) == 0
    settings = load_settings()

    # The partition (ADR-0024), durability class legible from the path.
    assert (home / "configs" / ".git").is_dir()
    assert (home / "configs" / "control.toml").is_file()
    assert (home / "secrets" / "master.key").is_file()
    assert (home / "secrets" / "admin-token").is_file()
    assert (home / "secrets" / "tls" / "ca.pem").is_file()
    assert (home / "secrets" / "tls" / "ca.key").is_file()
    assert (home / "secrets" / "tls" / "server.pem").is_file()
    assert (home / "cache").is_dir() and (home / "logs").is_dir()
    assert (home / "secrets").stat().st_mode & 0o777 == 0o700
    assert (home / "secrets" / "admin-token").stat().st_mode & 0o777 == 0o600

    # Only the scrypt hash of the password is stored.
    record = settings.admin_password_path.read_text()
    assert PASSWORD not in record
    parse_record(record)

    # The control address persists as the read-only fields (ADR-0031/0034):
    # the one address every mint surface — and every browser — will use.
    assert controltoml.read_control_ip(home / "configs") == "127.0.0.1"
    assert controltoml.read_control_port(home / "configs") == 443

    handoff = capsys.readouterr().out
    assert "dashboard: https://127.0.0.1" in handoff
    ca_url = f"http://127.0.0.1:{settings.bootstrap_port}/ca.pem"
    assert ca_url in handoff  # CA download URL
    # Steps are executable in printed order (acceptance 7 of the revision):
    # `serve` — which owns the bootstrap listener — comes before the CA
    # download it serves.
    assert handoff.index("theozolith serve") < handoff.index(ca_url)
    assert "static IP or DHCP reservation" in handoff  # the IP-only channel prerequisite
    # No DNS step exists (ADR-0034); the first visit clicks through the
    # interstitial, and CA trust is the optional green-lock upgrade.
    assert "DNS" not in handoff or "no DNS anywhere" in handoff
    assert "click through" in handoff
    assert "OPTIONAL" in handoff
    assert "security add-trusted-cert" in handoff  # macOS one-liner
    assert "update-ca-certificates" in handoff  # Linux one-liner
    assert "Firefox" in handoff and "iOS" in handoff
    assert "minus cache/" in handoff  # the backup doctrine, one line

    # The server cert carries the IP in the SAN (ADR-0023/0031) — what
    # nodes and CA-trusting browsers both verify against.
    from cryptography import x509

    cert = x509.load_pem_x509_certificate((home / "secrets" / "tls" / "server.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]


def test_init_rerun_requires_force_and_force_remints(home, monkeypatch):
    assert _init(home) == 0
    first_ca = (home / "secrets" / "tls" / "ca.pem").read_bytes()
    first_key = (home / "secrets" / "master.key").read_bytes()
    # The refusal names the most expensive consequence: fleet-wide CA
    # invalidation, one re-paste per node (review finding 4).
    with pytest.raises(SystemExit, match="EVERY provisioned node"):
        _init(home)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert _init(home, "--force") == 0
    # A new CA (outstanding join strings die by construction)…
    assert (home / "secrets" / "tls" / "ca.pem").read_bytes() != first_ca
    # …but the master key — and with it every stored secret — is untouched.
    assert (home / "secrets" / "master.key").read_bytes() == first_key


def test_init_refuses_to_autodetect_inside_a_container(home, monkeypatch, capsys):
    """ADR-0031: a containerized init must never silently ship the bridge
    IP — no --ip means refusal with the exact compose line to run."""
    monkeypatch.setattr("theozolith_control.cli._running_in_container", lambda: True)
    with pytest.raises(SystemExit, match="container"):
        cli_main(["init"])
    assert not (home / "secrets").exists()  # refused before any state landed
    # An explicit --ip provisions normally, container or not.
    assert _init(home) == 0
    assert controltoml.read_control_ip(home / "configs") == "127.0.0.1"


def test_filesystem_audit_no_secret_bytes_outside_secrets(home):
    """Acceptance 4: after init + one secret + one provisioned node, the
    configs/ tree is committable without leaking a byte, secrets/ is a
    sibling (absent, not git-ignored), and no secret material exists
    outside it."""
    assert _init(home) == 0
    settings = load_settings()
    box = SecretBox(settings.key_path.read_text().strip())
    secret_store = SecretStore(settings.store_db_path)
    secret_store.put_secret("github-worker", box.encrypt(SECRET_VALUE))
    node_token = secret_store.mint_node_token("box1")

    secret_bytes = [
        settings.key_path.read_bytes().strip(),
        PASSWORD.encode(),
        SECRET_VALUE.encode(),
        node_token.encode(),
        settings.admin_token_path.read_bytes().strip(),
        (home / "secrets" / "tls" / "ca.key").read_bytes(),
        (home / "secrets" / "tls" / "server.key").read_bytes(),
    ]
    secrets_dir = home / "secrets"
    for path in home.rglob("*"):
        if not path.is_file() or secrets_dir in path.parents:
            continue
        blob = path.read_bytes()
        for needle in secret_bytes:
            assert needle not in blob, f"secret material outside secrets/: {path}"

    # secrets/ is a sibling by decision (ADR-0024): not inside the repo,
    # and not merely git-ignored.
    assert secrets_dir not in (home / "configs").rglob("*")
    assert not (home / "configs" / ".gitignore").exists()
    status = subprocess.run(
        ["git", "-C", str(home / "configs"), "status", "--porcelain", "--ignored"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "secrets" not in status
    # The raw node token exists NOWHERE on disk (only its digest does).
    for path in home.rglob("*"):
        if path.is_file():
            assert node_token.encode() not in path.read_bytes(), path


def test_deleting_cache_db_costs_a_relogin_and_a_rewarm_only(home):
    """Acceptance 5: secrets and per-node tokens live in store.db; sessions,
    join tokens, and fleet state die with cache.db — and nothing else."""
    assert _init(home) == 0
    settings = load_settings()
    box = SecretBox(settings.key_path.read_text().strip())
    secret_store = SecretStore(settings.store_db_path)
    secret_store.put_secret("github-worker", box.encrypt(SECRET_VALUE))
    node_token = secret_store.mint_node_token("box1")

    store = Store(settings.cache_db_path)
    session = store.create_session(3600)
    store.create_join_token(ttl_seconds=3600, uses=1)
    store.touch_node("box1")
    store.close()

    settings.cache_db_path.unlink()  # always safe, by construction

    rebuilt = Store(settings.cache_db_path)
    assert not rebuilt.session_active(session)  # re-login…
    assert rebuilt.join_tokens() == []  # …re-mint join strings…
    assert rebuilt.fleet_state()["nodes"] == []  # …one heartbeat re-warms
    fresh = SecretStore(settings.store_db_path)
    assert fresh.node_for_token(node_token) == "box1"  # enrollment intact
    assert box.decrypt(fresh.get_secret_token("github-worker")) == SECRET_VALUE


def _backup(home, target):
    shutil.copytree(home, target, ignore=shutil.ignore_patterns("cache"))


def test_recovery_drill_restores_and_reminst_the_server_cert(home, tmp_path, capsys):
    """Acceptance 12: back up minus cache/, wipe, restore, recover — the
    server cert is re-minted from the restored CA (nodes reconnect
    untouched), and a node enrolled after the backup is exactly the
    re-provision worklist."""
    assert _init(home) == 0
    settings = load_settings()
    box = SecretBox(settings.key_path.read_text().strip())
    SecretStore(settings.store_db_path).put_secret("k", box.encrypt(SECRET_VALUE))
    early_token = SecretStore(settings.store_db_path).mint_node_token("box1")

    _backup(home, tmp_path / "backup")
    # Enrolled AFTER the copy: lost with the stale backup, surfaces as
    # unregistered after recovery (its heartbeats keep arriving).
    SecretStore(settings.store_db_path).mint_node_token("box2")

    old_cert = (home / "secrets" / "tls" / "server.pem").read_bytes()
    shutil.rmtree(home)
    shutil.copytree(tmp_path / "backup", home)

    assert cli_main(["recover", "--ip", "10.9.9.9"]) == 0
    out = capsys.readouterr().out
    assert "re-minted" in out and "10.9.9.9" in out
    # The IP changed (init used 127.0.0.1): the new address persists for
    # every future mint, and the output names the asymmetry — one re-paste
    # per node, and those nodes will NOT surface as unregistered (their
    # heartbeats go to the dead address and never arrive).
    assert controltoml.read_control_ip(home / "configs") == "10.9.9.9"
    assert "one join-string re-paste" in out.lower() or "join-string re-paste" in out
    assert "NOT appear in the" in out

    from cryptography import x509

    cert_bytes = (home / "secrets" / "tls" / "server.pem").read_bytes()
    assert cert_bytes != old_cert
    cert = x509.load_pem_x509_certificate(cert_bytes)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["10.9.9.9"]
    # The CA is byte-identical: nodes pinning it reconnect with no action.
    restored = SecretStore(settings.store_db_path)
    assert restored.node_for_token(early_token) == "box1"
    assert [n["node"] for n in restored.provisioned_nodes()] == ["box1"]  # box2 = worklist
    assert box.decrypt(restored.get_secret_token("k")) == SECRET_VALUE


def test_recover_enumerates_every_problem_in_one_pass(home, tmp_path, capsys):
    """Acceptance 13: a tampered/incomplete restore names EVERY missing or
    invalid artifact at once and exits nonzero."""
    assert _init(home) == 0
    settings = load_settings()
    box = SecretBox(settings.key_path.read_text().strip())
    SecretStore(settings.store_db_path).put_secret("k", box.encrypt(SECRET_VALUE))

    settings.key_path.unlink()  # missing master key
    (home / "secrets" / "tls" / "ca.key").unlink()  # missing CA private key
    settings.admin_password_path.write_text("garbage\n")  # corrupt hash record

    assert cli_main(["recover", "--ip", "10.9.9.9"]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "3 problem(s)" in out
    assert str(settings.key_path) in out
    assert "CA private key" in out
    assert "scrypt" in out  # the malformed password record, named
