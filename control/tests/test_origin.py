"""The canonical origin (ADR-0019): slug generation, validation, the
origin-init CLI verb, and the production serve requirement."""

from __future__ import annotations

import pytest
from theozolith_control import origin
from theozolith_control.cli import main as cli_main
from theozolith_control.origin import OriginError
from theozolith_control.tls import provision

BASE32_ALPHABET = set("abcdefghijklmnopqrstuvwxyz234567")


def test_generated_slugs_carry_128_bits_as_26_base32_chars():
    slugs = {origin.generate_slug() for _ in range(32)}
    assert len(slugs) == 32  # cheap non-collision sanity
    for slug in slugs:
        assert len(slug) == 26 and set(slug) <= BASE32_ALPHABET


def test_compose_write_read_roundtrip(tmp_path):
    host = origin.compose_host(origin.generate_slug(), "theozolith.com")
    origin.validate_canonical_host(host)
    path = origin.write_canonical_host(tmp_path, host)
    assert path.read_text().strip() == host
    assert origin.read_canonical_host(tmp_path) == host


def test_validation_rejects_low_entropy_and_malformed_hosts():
    for bad in (
        "",
        "control.local",  # a guessable name is exactly what this forbids
        "short.theozolith.com",
        "a" * 26,  # no base domain
        f"{'A' * 26}.theozolith.com",  # uppercase is not the canonical form
        f"{'a' * 26}.bad_domain",
        f"{'a' * 26}.-x.com",
        f"{'a' * 25}.theozolith.com",  # one char short of the entropy floor
    ):
        with pytest.raises(OriginError):
            origin.validate_canonical_host(bad)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("THEOZOLITH_NODE_TOKEN", "node-token")
    monkeypatch.setenv("THEOZOLITH_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("THEOZOLITH_CONTROL_DATA", str(tmp_path / "data"))
    return tmp_path / "data"


def test_origin_init_is_persistent_until_forced(cli_env):
    assert cli_main(["origin-init"]) == 0
    first = origin.read_canonical_host(cli_env)
    origin.validate_canonical_host(first)
    assert first.endswith(".theozolith.internal")  # the self-contained default

    with pytest.raises(SystemExit, match="persistent"):
        cli_main(["origin-init"])
    assert origin.read_canonical_host(cli_env) == first

    assert cli_main(["origin-init", "--force", "--base-domain", "theozolith.com"]) == 0
    second = origin.read_canonical_host(cli_env)
    assert second != first and second.endswith(".theozolith.com")


def test_production_serve_requires_the_canonical_origin(cli_env):
    """Acceptance 6: with TLS material present but no provisioned origin,
    production startup refuses with the origin-init instruction."""
    provision(cli_env / "tls", ["control.lan"])
    with pytest.raises(SystemExit, match="origin-init"):
        cli_main(["serve"])


def test_tls_init_includes_the_canonical_host(cli_env):
    assert cli_main(["origin-init"]) == 0
    assert cli_main(["tls-init"]) == 0  # no --host needed once provisioned
    from cryptography import x509

    cert = x509.load_pem_x509_certificate((cli_env / "tls" / "server.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert origin.read_canonical_host(cli_env) in san.get_values_for_type(x509.DNSName)
