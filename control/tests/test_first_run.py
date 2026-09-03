"""The unified first run and recovery (ADR-0023/0024, split by ADR-0036):
init composes the machine surface only and prints the handoff; origin-init
is the opt-in browser step; the partition keeps every secret byte under
secrets/; cache.db is deletable; a restored folder recovers with one
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
    return data


def _init(home, *args) -> int:
    return cli_main(["init", "--ip", "127.0.0.1", *args])


def _origin_init(monkeypatch, *args, origin_line: str = "") -> int:
    """Run origin-init with a piped stdin: the origin line (blank keeps the
    IP-origin default), then the password."""
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{origin_line}\n{PASSWORD}\n"))
    return cli_main(["origin-init", *args])


def _server_san(home) -> tuple[set[str], set[str]]:
    """(IP SANs, DNS SANs) of the current server certificate."""
    from cryptography import x509

    cert = x509.load_pem_x509_certificate((home / "secrets" / "tls" / "server.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return (
        {str(ip) for ip in san.get_values_for_type(x509.IPAddress)},
        set(san.get_values_for_type(x509.DNSName)),
    )


def test_init_produces_the_partition_and_the_handoff(home, capsys):
    """Acceptance 2: the full partition layout plus the operator handoff —
    machine surface only (ADR-0036): no password prompt, no browser steps,
    a pointer at origin-init."""
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

    # No browser credential exists until origin-init (ADR-0036).
    assert not settings.admin_password_path.exists()
    assert controltoml.read_browser_origin(home / "configs") == ""

    # The control address persists as the read-only fields (ADR-0031/0034):
    # the one address every mint surface will use.
    assert controltoml.read_control_ip(home / "configs") == "127.0.0.1"
    assert controltoml.read_control_port(home / "configs") == 443

    handoff = capsys.readouterr().out
    assert "control address: https://127.0.0.1" in handoff
    assert "static IP or DHCP reservation" in handoff  # the IP-only channel prerequisite
    assert "join-token create" in handoff
    assert "theozolith status" in handoff
    # The browser is one optional command away, and nothing more: no login
    # step, no CA-trust prose (that moved to origin-init).
    assert "origin-init" in handoff
    assert "log in" not in handoff
    assert "security add-trusted-cert" not in handoff
    assert "minus cache/" in handoff  # the backup doctrine, one line

    # The server cert carries the IP SANs (ADR-0023/0031/0037) — the box's
    # IP and loopback are the same here, deduplicated.
    ips, dns = _server_san(home)
    assert ips == {"127.0.0.1"}
    assert dns == set()


def test_init_san_includes_loopback_beside_the_lan_ip(home):
    """ADR-0037: loopback rides the SAN unconditionally — the local node
    and the Operator TUI dial 127.0.0.1 against this cert."""
    assert cli_main(["init", "--ip", "192.0.2.20"]) == 0
    ips, _ = _server_san(home)
    assert ips == {"192.0.2.20", "127.0.0.1"}


def test_init_rerun_requires_force_and_force_remints(home):
    assert _init(home) == 0
    first_ca = (home / "secrets" / "tls" / "ca.pem").read_bytes()
    first_key = (home / "secrets" / "master.key").read_bytes()
    # The refusal names the most expensive consequence: fleet-wide CA
    # invalidation, one re-paste per node (review finding 4).
    with pytest.raises(SystemExit, match="EVERY provisioned node"):
        _init(home)
    assert _init(home, "--force") == 0
    # A new CA (outstanding join strings die by construction)…
    assert (home / "secrets" / "tls" / "ca.pem").read_bytes() != first_ca
    # …but the master key — and with it every stored secret — is untouched.
    assert (home / "secrets" / "master.key").read_bytes() == first_key


def test_origin_init_enables_the_browser_surface(home, monkeypatch, capsys):
    """Acceptance 4 (M8): origin-init takes the origin (default: the IP
    origin) and the password together, persists the origin, re-mints the
    server cert from the SAME CA, and stores only the scrypt hash."""
    assert _init(home) == 0
    ca_before = (home / "secrets" / "tls" / "ca.pem").read_bytes()
    settings = load_settings()

    assert _origin_init(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "browser surface enabled: https://127.0.0.1" in out
    assert "re-minted" in out
    # CA-trust instructions live here now (moved out of init; ADR-0036).
    assert "OPTIONAL" in out and "security add-trusted-cert" in out
    # OZ-01: the trust instructions carry the CA's real SHA-256 to verify out
    # of band, and the openssl command that reproduces it — an unverified
    # plaintext install is no longer presented as safe.
    from theozolith_control.tls import ca_fingerprint_sha256

    digest = ca_fingerprint_sha256((home / "secrets" / "tls" / "ca.pem").read_bytes())
    colon_fp = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()
    assert colon_fp in out
    assert "openssl x509 -in ca.pem -noout -fingerprint -sha256" in out

    assert controltoml.read_browser_origin(home / "configs") == "https://127.0.0.1"
    record = settings.admin_password_path.read_text()
    assert PASSWORD not in record
    parse_record(record)
    # Same CA — nodes pin it; only the server cert was re-minted.
    assert (home / "secrets" / "tls" / "ca.pem").read_bytes() == ca_before

    # Re-run requires --force.
    with pytest.raises(SystemExit, match="--force"):
        _origin_init(monkeypatch)
    assert _origin_init(monkeypatch, "--force") == 0


def test_origin_init_hostname_origin_lands_in_the_san(home, monkeypatch):
    """The one hostname re-entry point (ADR-0036): an operator-entered
    hostname origin persists and its host joins the SAN as a DNS name,
    beside the IP SANs nodes rely on."""
    assert cli_main(["init", "--ip", "192.0.2.20"]) == 0
    assert _origin_init(monkeypatch, origin_line="https://ozolith.lan:8443") == 0
    assert controltoml.read_browser_origin(load_settings().config_repo) == (
        "https://ozolith.lan:8443"
    )
    ips, dns = _server_san(home)
    assert ips == {"192.0.2.20", "127.0.0.1"}
    assert dns == {"ozolith.lan"}


def test_init_force_preserves_an_enabled_browser_surface(home, monkeypatch):
    """A re-init after origin-init keeps the origin hostname in the new
    SAN set — the enabled surface outlives the re-init (ADR-0036)."""
    assert cli_main(["init", "--ip", "192.0.2.20"]) == 0
    assert _origin_init(monkeypatch, origin_line="https://ozolith.lan") == 0
    assert cli_main(["init", "--ip", "192.0.2.20", "--force"]) == 0
    assert controltoml.read_browser_origin(load_settings().config_repo) == "https://ozolith.lan"
    ips, dns = _server_san(home)
    assert ips == {"192.0.2.20", "127.0.0.1"}
    assert dns == {"ozolith.lan"}


def test_origin_init_before_init_refuses(home):
    with pytest.raises(SystemExit, match="theozolith init"):
        cli_main(["origin-init"])


def test_origin_init_force_invalidates_every_session(home, monkeypatch):
    """Forced replacement of the browser credentials kills every live
    session — the replaced password must not leave old cookies working."""
    assert _init(home) == 0
    assert _origin_init(monkeypatch) == 0
    settings = load_settings()
    store = Store(settings.cache_db_path)
    session = store.create_session(3600)
    store.close()
    assert _origin_init(monkeypatch, "--force") == 0
    assert not Store(settings.cache_db_path).session_active(session)


def test_password_record_write_is_atomic(home, monkeypatch):
    """A failed write can never leave a partial or missing record: the
    previous record survives byte-for-byte and no temp file litters the
    secrets dir (ADR-0036 amendment)."""
    from theozolith_control.cli import _write_private

    target = home / "secrets" / "admin-password"
    target.parent.mkdir(parents=True)
    _write_private(target, "first-record")
    assert target.read_text() == "first-record\n"

    import os as os_module

    def broken_fsync(fd):
        raise OSError("disk full")

    monkeypatch.setattr(os_module, "fsync", broken_fsync)
    with pytest.raises(OSError, match="disk full"):
        _write_private(target, "second-record")
    assert target.read_text() == "first-record\n"  # the old record survives
    assert [p.name for p in target.parent.iterdir()] == ["admin-password"]  # no litter


def test_init_refuses_hostname_san_input(home):
    """M8 amendment: init mints IP SANs only — a hostname --host is refused
    before any state lands; origin-init is the one hostname entry point."""
    with pytest.raises(SystemExit, match="origin-init"):
        cli_main(["init", "--ip", "127.0.0.1", "--host", "ozolith.lan"])
    assert not (home / "secrets").exists()  # refused before any state
    # Validated IP literals remain accepted, additively.
    assert cli_main(["init", "--ip", "192.0.2.20", "--host", "192.0.2.99"]) == 0
    ips, dns = _server_san(home)
    assert ips == {"192.0.2.20", "127.0.0.1", "192.0.2.99"}
    assert dns == set()


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


def test_filesystem_audit_no_secret_bytes_outside_secrets(home, monkeypatch):
    """Acceptance 4: after init + origin-init + one secret + one
    provisioned node, the configs/ tree is committable without leaking a
    byte, secrets/ is a sibling (absent, not git-ignored), and no secret
    material exists outside it."""
    assert _init(home) == 0
    assert _origin_init(monkeypatch) == 0
    settings = load_settings()
    box = SecretBox(settings.key_path.read_text().strip())
    secret_store = SecretStore(settings.store_db_path)
    secret_store.put_secret("github-implementer", box.encrypt(SECRET_VALUE))
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
    secret_store.put_secret("github-implementer", box.encrypt(SECRET_VALUE))
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
    assert box.decrypt(fresh.get_secret_token("github-implementer")) == SECRET_VALUE


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

    cert_bytes = (home / "secrets" / "tls" / "server.pem").read_bytes()
    assert cert_bytes != old_cert
    ips, _ = _server_san(home)
    assert ips == {"10.9.9.9", "127.0.0.1"}  # loopback rides every mint (ADR-0037)
    # The CA is byte-identical: nodes pinning it reconnect with no action.
    restored = SecretStore(settings.store_db_path)
    assert restored.node_for_token(early_token) == "box1"
    assert [n["node"] for n in restored.provisioned_nodes()] == ["box1"]  # box2 = worklist
    assert box.decrypt(restored.get_secret_token("k")) == SECRET_VALUE


def test_recover_enumerates_every_problem_in_one_pass(home, tmp_path, monkeypatch, capsys):
    """Acceptance 13: a tampered/incomplete restore names EVERY missing or
    invalid artifact at once and exits nonzero. The password record is
    validated because this deployment enabled the browser (ADR-0036)."""
    assert _init(home) == 0
    assert _origin_init(monkeypatch) == 0
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


def test_recover_without_browser_enablement_owes_no_password(home, capsys):
    """Browser credentials are optional-but-consistent (ADR-0036): a
    browserless deployment recovers with no password hash on disk; an
    enabled one missing its hash is named as a problem."""
    assert _init(home) == 0
    SecretStore(load_settings().store_db_path)  # store.db exists in any real backup
    assert cli_main(["recover"]) == 0  # no password hash exists, no problem
    controltoml.write_browser_origin(home / "configs", "https://127.0.0.1")
    assert cli_main(["recover"]) == 1  # enabled but hash missing -> named
    assert "origin-init --force" in capsys.readouterr().out


# -- the retired Control Node repo setting (ADR-0056) ------------------------------


def test_control_node_repo_setting_is_retired(tmp_path):
    """ADR-0056: THEOZOLITH_REPO is retired as a Control Node setting — a
    lingering export (and its _FILE spelling) fails settings load loudly, so
    it can never silently steer coordination; unset/empty loads clean, and a
    PAT alone (no repo) enables coordination now."""
    from theozolith_worker.config import ConfigError

    base = {"THEOZOLITH_DATA_DIR": str(tmp_path)}
    # Unset and empty both load clean (env_value treats "" as unset).
    assert load_settings(base).coordination_jobs_enabled is False
    assert load_settings({**base, "THEOZOLITH_REPO": ""}).github_token is None
    # The PAT alone is the enablement bit — no target repo setting exists.
    assert load_settings({**base, "CONTROL_GITHUB_TOKEN": "gh"}).coordination_jobs_enabled is True

    with pytest.raises(ConfigError, match="retired"):
        load_settings({**base, "THEOZOLITH_REPO": "owner/name"})
    # The _FILE spelling trips the same guard (env_value honors it).
    repo_file = tmp_path / "repo"
    repo_file.write_text("owner/name")
    with pytest.raises(ConfigError, match="retired"):
        load_settings({**base, "THEOZOLITH_REPO_FILE": str(repo_file)})
