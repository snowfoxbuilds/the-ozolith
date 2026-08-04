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
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_FILE = "ca.pem"
CA_KEY_FILE = "ca.key"
CERT_FILE = "server.pem"
KEY_FILE = "server.key"

_CA_VALID_DAYS = 3650  # home-lab CA: rotation story is re-running init
# Apple's trust stack refuses TLS server certificates with validity over
# 825 days — private CAs included — so a long-lived leaf can never earn the
# browser green lock on macOS/iOS (found on the first real Safari attempt,
# 2026-08-04). The margin below 825 absorbs clock skew. Renewal is the
# recover re-mint (same CA, nodes untouched).
_SERVER_VALID_DAYS = 820
# The renewal-warning policy: once the live leaf is inside this window the
# Control Node warns on every daily check (log line + a theozolith.error on
# the dashboard errors panel). 90 days is enough lead time for a hobbyist
# cadence, and the leaf's ~27-month life means the first warning arrives
# with the deployment still healthy, not expired.
LEAF_EXPIRY_WARNING_DAYS = 90

RENEWAL_COMMANDS = (
    "renew with 'sudo theozolith recover' (bare metal; compose:"
    " 'docker compose -f deploy/compose/control.yml run --rm control recover'),"
    " then restart the service ('sudo systemctl restart"
    " theozolith-control.service' / 'docker compose restart control')"
)


def leaf_expiry_warning(cert_path: Path, *, now: datetime.datetime | None = None) -> str | None:
    """The renewal warning when the live leaf is inside the policy window
    (or past it), else None. The message is self-contained operator
    guidance: what expires when, that the CA is untouched, the exact
    renewal commands per deployment shape, and that nodes need nothing."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return None  # no leaf to warn about; serve's own checks own that case
    now = datetime.datetime.now(datetime.UTC) if now is None else now
    remaining = cert.not_valid_after_utc - now
    if remaining > datetime.timedelta(days=LEAF_EXPIRY_WARNING_DAYS):
        return None
    expires = cert.not_valid_after_utc.strftime("%Y-%m-%d")
    state = (
        f"expires {expires} ({remaining.days} day(s) left)"
        if remaining > datetime.timedelta(0)
        else f"EXPIRED {expires}"
    )
    return (
        f"server certificate {state} — {RENEWAL_COMMANDS}. The CA is unchanged"
        " by renewal, so nodes and trusted devices need NO action while the CA"
        " and control IP stay the same."
    )


def pair_matches(cert_path: Path, key_path: Path) -> bool:
    """True when the live certificate and private key belong together —
    the startup guard against an interrupted renewal's mixed pair."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), None)
    except (OSError, ValueError):
        return False
    return cert.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


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
    not_after = now + datetime.timedelta(days=_CA_VALID_DAYS)
    server_not_after = now + datetime.timedelta(days=_SERVER_VALID_DAYS)

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

    server_key, server_cert = _issue_server_cert(
        ca_name, ca_key, hosts, not_before, server_not_after
    )

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
        # serverAuth EKU: required by Apple's trust stack (macOS 10.15+) —
        # without it Safari/iOS reject the leaf even under a trusted CA.
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
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


def _verify_leaf(
    cert: x509.Certificate,
    key: ec.EllipticCurvePrivateKey,
    ca_cert: x509.Certificate,
    hosts: list[str],
) -> None:
    """The pre-promotion gate: nothing touches the live pair until the
    freshly minted leaf provably chains to the existing CA, carries every
    requested SAN and the serverAuth EKU, respects the lifetime policy, and
    belongs to the freshly minted key."""
    cert.verify_directly_issued_by(ca_cert)  # raises on a chain break
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    covered = {str(v) for v in san.get_values_for_type(x509.IPAddress)} | set(
        san.get_values_for_type(x509.DNSName)
    )
    missing = [host for host in hosts if host not in covered]
    if missing:
        raise ValueError(f"minted leaf lacks SAN entries for: {', '.join(missing)}")
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if ExtendedKeyUsageOID.SERVER_AUTH not in eku:
        raise ValueError("minted leaf lacks the serverAuth EKU")
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    if lifetime > datetime.timedelta(days=_SERVER_VALID_DAYS, minutes=10):
        raise ValueError(
            f"minted leaf lifetime {lifetime} exceeds the {_SERVER_VALID_DAYS}d policy"
        )
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    if cert.public_key().public_bytes(
        serialization.Encoding.PEM, spki
    ) != key.public_key().public_bytes(serialization.Encoding.PEM, spki):
        raise ValueError("minted leaf does not match the minted private key")


def _stage(path: Path, data: bytes, *, private: bool = False) -> None:
    """A staged artifact: full bytes on disk (fsynced) with final
    permissions BEFORE any promotion touches the live pair."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def remint_server_cert(tls_dir: Path, hosts: list[str]) -> tuple[Path, Path]:
    """Re-issue the server certificate from the EXISTING CA — recovery's
    re-mint and the routine renewal move (same machinery): nodes reconnect
    untouched because they pin the CA, not the server certificate.

    Interruption-safe by protocol: the pair is minted and verified in
    memory, staged beside the live files with final permissions, and only
    then promoted; any failure before or during promotion restores the
    previous pair from the captured bytes. Two files cannot be replaced in
    one atomic step, so serve additionally refuses a mixed pair at startup
    (pair_matches) with re-running recover as the remediation — a crash in
    the promotion window can delay startup, never silently serve it.
    Returns (cert, key) paths."""
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
        min(now + datetime.timedelta(days=_SERVER_VALID_DAYS), ca_cert.not_valid_after_utc),
    )
    _verify_leaf(server_cert, server_key, ca_cert, hosts)

    cert_path, key_path = tls_dir / CERT_FILE, tls_dir / KEY_FILE
    staged_cert, staged_key = Path(f"{cert_path}.staged"), Path(f"{key_path}.staged")
    # The previous pair, captured for restore — promotion must leave either
    # this pair or the new one, never a mix.
    previous: dict[Path, tuple[bytes, bool]] = {}
    for path, private in ((cert_path, False), (key_path, True)):
        if path.is_file():
            previous[path] = (path.read_bytes(), private)
    try:
        _stage(staged_cert, server_cert.public_bytes(serialization.Encoding.PEM))
        _stage(staged_key, _pem_key(server_key), private=True)
        os.replace(staged_key, key_path)
        os.replace(staged_cert, cert_path)
        dir_fd = os.open(tls_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception as exc:
        for path, (data, private) in previous.items():
            _write(path, data, private=private)
        for staged in (staged_cert, staged_key):
            staged.unlink(missing_ok=True)
        raise ValueError(f"server-certificate renewal failed before taking effect: {exc}") from exc
    return cert_path, key_path


def ca_fingerprint_sha256(ca_pem: bytes) -> str:
    """The CA certificate's SHA-256 fingerprint (over the DER encoding —
    the standard certificate fingerprint), hex. This is what the join
    string pins and what `provision` verifies before transmitting anything
    (ADR-0023); the node side computes the same digest stdlib-only."""
    cert = x509.load_pem_x509_certificate(ca_pem)
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
