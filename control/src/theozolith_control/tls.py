"""Self-signed TLS provisioning for the control channel.

TLS is mandatory on the heartbeat/command channel because secrets transit it
(NODE-SUBSTRATE.md); a self-signed or install-provisioned CA is fine. This
module mints that CA and one server certificate; the install script copies
``ca.pem`` to every node, which pins it via THEOZOLITH_TLS_CA. No openssl
binary needed — the ``cryptography`` package is already a control/ dependency.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CA_FILE = "ca.pem"
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

    ca_path = tls_dir / CA_FILE
    cert_path = tls_dir / CERT_FILE
    key_path = tls_dir / KEY_FILE
    _write(ca_path, ca_cert.public_bytes(serialization.Encoding.PEM))
    _write(cert_path, server_cert.public_bytes(serialization.Encoding.PEM))
    _write(key_path, _pem_key(server_key), private=True)
    # The CA key signs exactly two certs and is never needed again unless
    # the operator re-provisions; keep it for that, locked down.
    _write(tls_dir / "ca.key", _pem_key(ca_key), private=True)
    return ca_path, cert_path, key_path
