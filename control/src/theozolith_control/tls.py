"""Self-signed TLS provisioning for the control channel.

TLS is mandatory on the heartbeat/command channel because secrets transit it
(NODE-SUBSTRATE.md); a self-signed or install-provisioned CA is fine. This
module mints that CA and one server certificate; the install script copies
``ca.pem`` to every node, which pins it via THEOZOLITH_TLS_CA. No openssl
binary needed — the ``cryptography`` package is already a control/ dependency.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CA_FILE = "ca.pem"
CA_KEY_FILE = "ca.key"
CERT_FILE = "server.pem"
KEY_FILE = "server.key"

_VALID_DAYS = 3650  # home-lab CA: rotation story is re-running tls-init


def _write(path: Path, data: bytes, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def provision(tls_dir: Path, hosts: list[str]) -> tuple[Path, Path, Path]:
    """Mint a CA and a server cert for ``hosts``; returns (ca, cert, key)."""
    if not hosts:
        raise ValueError("at least one --host (DNS name or IP) is required")
    for host in hosts:
        if "*" in host:
            # Per-deployment TLS identity (ADR-0019): a wildcard key shared
            # across deployments would make any one compromise fleet-wide.
            raise ValueError(f"wildcard host {host!r} refused — one TLS identity per deployment")
    now = datetime.datetime.now(datetime.UTC)
    not_before = now - datetime.timedelta(minutes=5)
    not_after = now + datetime.timedelta(days=_VALID_DAYS)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TheOzolith Control CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        # Key identifiers: modern OpenSSL refuses to build a chain without them.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key, server_cert = _issue_server_cert(ca_name, ca_key, hosts, not_before, not_after)

    ca_path = tls_dir / CA_FILE
    cert_path = tls_dir / CERT_FILE
    key_path = tls_dir / KEY_FILE
    _write(ca_path, ca_cert.public_bytes(serialization.Encoding.PEM))
    _write(cert_path, server_cert.public_bytes(serialization.Encoding.PEM))
    _write(key_path, _pem_key(server_key), private=True)
    # The CA key stays for server-cert re-mints — recovery on a new box
    # re-issues the server certificate with the new IP in its SAN while
    # nodes keep trusting the pinned CA (ADR-0024) — locked down.
    _write(tls_dir / CA_KEY_FILE, _pem_key(ca_key), private=True)
    return ca_path, cert_path, key_path


def _issue_server_cert(
    ca_name: x509.Name,
    ca_key: ec.EllipticCurvePrivateKey,
    hosts: list[str],
    not_before: datetime.datetime,
    not_after: datetime.datetime,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    names: list[x509.GeneralName] = []
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return server_key, server_cert


def remint_server_cert(tls_dir: Path, hosts: list[str]) -> tuple[Path, Path]:
    """Re-issue the server certificate from the EXISTING CA — the recovery
    move (ADR-0024): a restored Control Node lands on a new IP, the new SAN
    goes in the cert, and nodes reconnect untouched because they pin the CA,
    not the server certificate. Returns (cert, key) paths."""
    if not hosts:
        raise ValueError("at least one host (DNS name or IP) is required")
    for host in hosts:
        if "*" in host:
            raise ValueError(f"wildcard host {host!r} refused — one TLS identity per deployment")
    try:
        ca_cert = x509.load_pem_x509_certificate((tls_dir / CA_FILE).read_bytes())
        ca_key = serialization.load_pem_private_key((tls_dir / CA_KEY_FILE).read_bytes(), None)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load the CA keypair from {tls_dir}: {exc}") from exc
    if not isinstance(ca_key, ec.EllipticCurvePrivateKey):
        raise ValueError(f"{tls_dir / CA_KEY_FILE} is not the expected EC private key")
    now = datetime.datetime.now(datetime.UTC)
    server_key, server_cert = _issue_server_cert(
        ca_cert.subject,
        ca_key,
        hosts,
        now - datetime.timedelta(minutes=5),
        min(now + datetime.timedelta(days=_VALID_DAYS), ca_cert.not_valid_after_utc),
    )
    cert_path, key_path = tls_dir / CERT_FILE, tls_dir / KEY_FILE
    _write(cert_path, server_cert.public_bytes(serialization.Encoding.PEM))
    _write(key_path, _pem_key(server_key), private=True)
    return cert_path, key_path


def ca_fingerprint_sha256(ca_pem: bytes) -> str:
    """The CA certificate's SHA-256 fingerprint (over the DER encoding —
    the standard certificate fingerprint), hex. This is what the join
    string pins and what `provision` verifies before transmitting anything
    (ADR-0023); the node side computes the same digest stdlib-only."""
    cert = x509.load_pem_x509_certificate(ca_pem)
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
