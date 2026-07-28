"""Admin password hashing: stdlib scrypt (ADR-0023).

The password is the human/browser credential — a sibling of, never derived
from, the admin bearer token (the machine credential). Only its hash is
stored, at ``~/.theozolith/secrets/admin-password``: memory-hard scrypt via
``hashlib``, no new dependency (Fernet is encryption, not password hashing,
and is not used here). The self-describing record format keeps parameter
upgrades a rehash-on-next-set, not a migration:

    scrypt$<n>$<r>$<p>$<salt-hex>$<hash-hex>
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 32
DKLEN = 64


class PasswordFormatError(ValueError):
    """The stored record is not a well-formed scrypt line."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DKLEN
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def parse_record(stored: str) -> tuple[int, int, int, bytes, bytes]:
    parts = stored.strip().split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise PasswordFormatError("admin-password record is not a scrypt line")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, derived = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError as exc:
        raise PasswordFormatError(f"admin-password record malformed: {exc}") from exc
    if n < 2 or (n & (n - 1)) or r < 1 or p < 1 or not salt or not derived:
        raise PasswordFormatError("admin-password record carries invalid scrypt parameters")
    return n, r, p, salt, derived


def verify_password(stored: str, password: str) -> bool:
    """Constant-time verification against one stored record; a malformed
    or empty record verifies nothing (fail closed)."""
    try:
        n, r, p, salt, derived = parse_record(stored)
    except PasswordFormatError:
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(derived)
    )
    return hmac.compare_digest(candidate, derived)
