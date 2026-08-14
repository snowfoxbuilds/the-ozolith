"""Self-signed TLS provisioning for the control channel.

TLS is mandatory on the heartbeat/command channel because secrets transit it
(NODE-SUBSTRATE.md); a self-signed or install-provisioned CA is fine. This
module mints that CA and one server certificate; the install script copies
``ca.pem`` to every node, which pins it via THEOZOLITH_TLS_CA. No openssl
binary needed — the ``cryptography`` package is already a control/ dependency.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import ipaddress
import os
import secrets
import stat
from collections.abc import Iterator
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

# Every descendant directory between the trusted root and a TLS artifact is
# opened with these flags and then fstat-verified: a symlink planted at any
# component (``secrets``, ``secrets/tls``) fails the open with ELOOP instead
# of being followed, so a compromised service account that owns the secrets
# partition cannot steer a root-run mint into an arbitrary tree (OZ-02).
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_descendant(parent_fd: int, name: str, where: str) -> int:
    """Open ONE directory component relative to an already-verified
    descriptor. The component is created (0700) when missing; a symlink or
    non-directory at its name is refused, and the opened descriptor is
    fstat-verified to be a directory before it is trusted."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise OSError(
                f"refusing to traverse {where}: not a real directory ({exc.strerror})"
            ) from exc
    except OSError as exc:
        raise OSError(
            f"refusing to traverse {where}: it is a symlink or not a directory ({exc.strerror})"
        ) from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(f"refusing to traverse {where}: opened object is not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _trusted_tls_dir(tls_dir: Path, trust_root: Path) -> Iterator[int]:
    """A verified descriptor for ``tls_dir``, reached from ``trust_root``.

    ``trust_root`` is the Control data root — the directory the operator
    (not the service account) controls the location of. It is opened once,
    symlinks permitted at and above it (an operator's ``/var`` or Compose
    mount indirection is legitimate), and every descendant component down
    to ``tls_dir`` is opened via :func:`_open_descendant` so no
    service-owned link is ever followed. The relative route is lexical by
    construction (``relative_to``), never resolved through the filesystem."""
    try:
        relative = tls_dir.relative_to(trust_root)
    except ValueError as exc:
        raise ValueError(f"TLS dir {tls_dir} is not inside the trusted root {trust_root}") from exc
    parts = [part for part in relative.parts if part != "."]
    if ".." in parts:
        raise ValueError(f"TLS dir {tls_dir} escapes the trusted root {trust_root}")
    # tls-init on a fresh box may precede init: creating the trusted root
    # itself is safe (its parent is operator territory), descendants are not.
    os.makedirs(trust_root, mode=0o700, exist_ok=True)
    fds = [os.open(trust_root, os.O_RDONLY | os.O_DIRECTORY)]
    try:
        for depth, name in enumerate(parts):
            where = str(trust_root.joinpath(*parts[: depth + 1]))
            fds.append(_open_descendant(fds[-1], name, where))
        yield fds[-1]
    finally:
        for fd in fds:
            os.close(fd)


def _write_at(dir_fd: int, name: str, data: bytes, *, where: Path, private: bool = False) -> None:
    """Write one TLS artifact atomically, relative to a VERIFIED directory
    descriptor — no attacker-steerable path ever reaches an ``open`` or a
    rename. The temp file is created O_CREAT|O_EXCL|O_NOFOLLOW with an
    unguessable name inside the verified directory, its mode is set on the
    fd, and a dirfd-relative ``os.replace`` swaps it in: a symlink planted
    at the destination name is *replaced*, never written through (the
    pre-check refuses that shape loudly; the rename could not follow it
    regardless). Ownership is left to the caller's partition repair,
    matching the previous behaviour."""
    try:
        existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise OSError(f"refusing to write {where / name}: destination is not a regular file")
    tmp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(fd, 0o600 if private else 0o644)
            handle.write(data)
            handle.flush()
            os.fsync(fd)
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=dir_fd)
        raise


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def provision(tls_dir: Path, hosts: list[str], *, trust_root: Path) -> tuple[Path, Path, Path]:
    """Mint a CA and a server cert for ``hosts``; returns (ca, cert, key).

    ``trust_root`` anchors every write (OZ-02): it is the Control data root
    the operator placed, and the ``secrets/tls`` descendants beneath it are
    service-owned, so they are traversed by verified descriptor only."""
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
    with _trusted_tls_dir(tls_dir, trust_root) as dir_fd:
        _write_at(dir_fd, CA_FILE, ca_cert.public_bytes(serialization.Encoding.PEM), where=tls_dir)
        _write_at(
            dir_fd, CERT_FILE, server_cert.public_bytes(serialization.Encoding.PEM), where=tls_dir
        )
        _write_at(dir_fd, KEY_FILE, _pem_key(server_key), where=tls_dir, private=True)
        # The CA key stays for server-cert re-mints — recovery on a new box
        # re-issues the server certificate with the new IP in its SAN while
        # nodes keep trusting the pinned CA (ADR-0024) — locked down.
        _write_at(dir_fd, CA_KEY_FILE, _pem_key(ca_key), where=tls_dir, private=True)
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


def remint_server_cert(tls_dir: Path, hosts: list[str], *, trust_root: Path) -> tuple[Path, Path]:
    """Re-issue the server certificate from the EXISTING CA — the recovery
    move (ADR-0024): a restored Control Node lands on a new IP, the new SAN
    goes in the cert, and nodes reconnect untouched because they pin the CA,
    not the server certificate. Returns (cert, key) paths. Writes are
    anchored at ``trust_root`` exactly like :func:`provision` (OZ-02)."""
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
    with _trusted_tls_dir(tls_dir, trust_root) as dir_fd:
        _write_at(
            dir_fd, CERT_FILE, server_cert.public_bytes(serialization.Encoding.PEM), where=tls_dir
        )
        _write_at(dir_fd, KEY_FILE, _pem_key(server_key), where=tls_dir, private=True)
    return cert_path, key_path


def ca_fingerprint_sha256(ca_pem: bytes) -> str:
    """The CA certificate's SHA-256 fingerprint (over the DER encoding —
    the standard certificate fingerprint), hex. This is what the join
    string pins and what `provision` verifies before transmitting anything
    (ADR-0023); the node side computes the same digest stdlib-only."""
    cert = x509.load_pem_x509_certificate(ca_pem)
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
