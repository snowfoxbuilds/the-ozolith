"""The public origin (ADR-0019, amended by M5): slug generation, origin
parsing/validation, the origin-init CLI verb, the production serve
requirement (artifact or THEOZOLITH_PUBLIC_ORIGIN), bind-port
independence, and TLS SAN derivation."""

from __future__ import annotations

import socket

import pytest
from theozolith_control import controltoml, origin
from theozolith_control.cli import main as cli_main
from theozolith_control.origin import OriginError
from theozolith_control.passwords import hash_password
from theozolith_control.tls import provision

BASE32_ALPHABET = set("abcdefghijklmnopqrstuvwxyz234567")
GOOD_HOST = f"{'a' * 26}.theozolith.com"


def test_generated_slugs_carry_128_bits_as_26_base32_chars():
    slugs = {origin.generate_slug() for _ in range(32)}
    assert len(slugs) == 32  # cheap non-collision sanity
    for slug in slugs:
        assert len(slug) == 26 and set(slug) <= BASE32_ALPHABET


def test_compose_write_read_roundtrip(tmp_path):
    """The origin persists as the read-only [control] field of control.toml
    in the Config Repo (ADR-0024)."""
    text = origin.compose_origin(origin.generate_slug(), "theozolith.com")
    assert text.startswith("https://") and ":" not in text.removeprefix("https://")
    path = controltoml.write_public_origin(tmp_path, text)
    assert path.name == "control.toml" and text in path.read_text()
    assert controltoml.read_public_origin(tmp_path) == text


def test_parse_yields_portless_host_and_origin_for_default_https():
    """https://<slug>.theozolith.com => Host <slug>.theozolith.com and the
    same Origin — no :8443 or any other port anywhere."""
    parsed = origin.parse_public_origin(f"https://{GOOD_HOST}")
    assert parsed.origin == f"https://{GOOD_HOST}"
    assert parsed.host_header == GOOD_HOST
    assert parsed.hostname == GOOD_HOST
    assert parsed.port == 443


def test_parse_normalizes_case_default_port_and_bare_slash():
    parsed = origin.parse_public_origin(f"HTTPS://{'A' * 26}.TheOzolith.COM:443/")
    assert parsed.origin == f"https://{'a' * 26}.theozolith.com"
    assert parsed.host_header == f"{'a' * 26}.theozolith.com"


def test_parse_keeps_an_explicit_nonstandard_port():
    parsed = origin.parse_public_origin(f"https://{GOOD_HOST}:9443")
    assert parsed.origin == f"https://{GOOD_HOST}:9443"
    assert parsed.host_header == f"{GOOD_HOST}:9443"
    assert parsed.hostname == GOOD_HOST  # the SAN input never carries a port


def test_parse_rejects_nonconforming_origins():
    """Invalid public-origin URL forms fail closed (OriginError)."""
    for bad in (
        "",
        GOOD_HOST,  # bare host: the artifact is a complete origin now
        f"http://{GOOD_HOST}",  # production origins require https
        f"ftp://{GOOD_HOST}",
        f"https://user@{GOOD_HOST}",  # credentials
        f"https://user:pw@{GOOD_HOST}",
        f"https://{GOOD_HOST}/dashboard",  # path
        f"https://{GOOD_HOST}?q=1",  # query
        f"https://{GOOD_HOST}#frag",  # fragment
        "https://*.theozolith.com",  # wildcard host
        f"https://{GOOD_HOST}:0",  # malformed ports
        f"https://{GOOD_HOST}:99999",
        f"https://{GOOD_HOST}:8a",
        "https://control.local",  # a guessable name is exactly what this forbids
        f"https://{'a' * 25}.theozolith.com",  # one char short of the entropy floor
        f"https://{'a' * 26}",  # no base domain
        f"https://{'a' * 26}.bad_domain",
        f"https://{'a' * 26}.-x.com",
        "https://10.0.0.5",  # an IP literal is not a randomized hostname
    ):
        with pytest.raises(OriginError):
            origin.parse_public_origin(bad)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("THEOZOLITH_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("THEOZOLITH_DATA_DIR", str(data))
    # Production serve requires the init-written password hash; the
    # bootstrap listener gets a per-test free port.
    monkeypatch.setenv("THEOZOLITH_BOOTSTRAP_PORT", str(_free_port()))
    monkeypatch.delenv("THEOZOLITH_PUBLIC_ORIGIN", raising=False)
    (data / "secrets").mkdir(parents=True)
    (data / "secrets" / "admin-password").write_text(hash_password("pw") + "\n")
    return data


def _stored_origin(data):
    return controltoml.read_public_origin(data / "configs")


def test_origin_init_is_persistent_until_forced(cli_env):
    assert cli_main(["origin-init"]) == 0
    first = _stored_origin(cli_env)
    origin.parse_public_origin(first)
    assert first.endswith(".theozolith.internal")  # the self-contained default, no port

    with pytest.raises(SystemExit, match="persistent"):
        cli_main(["origin-init"])
    assert _stored_origin(cli_env) == first

    assert cli_main(["origin-init", "--force", "--base-domain", "theozolith.com"]) == 0
    second = _stored_origin(cli_env)
    assert second != first and second.endswith(".theozolith.com")


def test_origin_init_can_pin_a_nonstandard_external_port(cli_env):
    assert cli_main(["origin-init", "--port", "9443"]) == 0
    text = _stored_origin(cli_env)
    assert text.endswith(".theozolith.internal:9443")
    assert origin.parse_public_origin(text).host_header.endswith(":9443")
    # :443 is the https default and is normalized away.
    assert cli_main(["origin-init", "--force", "--port", "443"]) == 0
    assert ":" not in _stored_origin(cli_env).removeprefix("https://")


def test_production_serve_requires_the_public_origin(cli_env):
    """Acceptance 6: with TLS material present but no provisioned origin
    (and no env override), production startup refuses with instructions."""
    provision(cli_env / "secrets" / "tls", ["control.lan"])
    with pytest.raises(SystemExit, match="origin-init"):
        cli_main(["serve"])


def _capture_serve(monkeypatch) -> dict:
    """Run `serve` up to (a fake) uvicorn.run and capture the built app."""
    import uvicorn

    captured: dict = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return captured


def test_serve_accepts_the_persisted_artifact_and_ignores_the_bind_port(cli_env, monkeypatch):
    """Production boots from the origin-init artifact; a different internal
    serve --port changes only the bind, never the accepted Host/Origin."""
    assert cli_main(["origin-init"]) == 0
    assert cli_main(["tls-init"]) == 0
    text = _stored_origin(cli_env)
    captured = _capture_serve(monkeypatch)
    assert cli_main(["serve", "--port", "9090"]) == 0
    guard = captured["app"].state.browser_guard
    assert guard.expected_origin == text
    assert guard.expected_host == text.removeprefix("https://")
    assert ":" not in guard.expected_host  # the bind port leaked nowhere
    assert captured["kwargs"]["port"] == 9090  # …but uvicorn does bind it


def test_serve_accepts_the_env_override_in_production(cli_env, monkeypatch):
    """THEOZOLITH_PUBLIC_ORIGIN alone (no artifact) satisfies production —
    the expert escape hatch; format-checked, entropy is the operator's job."""
    provision(cli_env / "secrets" / "tls", ["control.lan"])
    monkeypatch.setenv("THEOZOLITH_PUBLIC_ORIGIN", f"https://{GOOD_HOST}")
    captured = _capture_serve(monkeypatch)
    assert cli_main(["serve"]) == 0
    guard = captured["app"].state.browser_guard
    assert guard.expected_host == GOOD_HOST
    assert guard.expected_origin == f"https://{GOOD_HOST}"


def test_serve_fails_closed_on_an_invalid_env_override(cli_env, monkeypatch):
    provision(cli_env / "secrets" / "tls", ["control.lan"])
    for bad in (f"http://{GOOD_HOST}", "https://control.local", f"https://{GOOD_HOST}:8a"):
        monkeypatch.setenv("THEOZOLITH_PUBLIC_ORIGIN", bad)
        with pytest.raises(SystemExit, match="error"):
            cli_main(["serve"])


def test_tls_init_includes_the_hostname_but_never_a_port(cli_env):
    """TLS SAN derives from the public origin's hostname alone, even when
    the origin pins an explicit external port."""
    assert cli_main(["origin-init", "--port", "9443"]) == 0
    assert cli_main(["tls-init"]) == 0  # no --host needed once provisioned
    from cryptography import x509

    hostname = origin.parse_public_origin(_stored_origin(cli_env)).hostname
    cert = x509.load_pem_x509_certificate((cli_env / "secrets" / "tls" / "server.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = san.get_values_for_type(x509.DNSName)
    assert hostname in dns_names
    assert all(":" not in name for name in dns_names)
