"""Secret-store cryptography: Fernet with a file-held master key (ADR-0015).

Values are encrypted per-secret before they touch SQLite; the database alone
reveals nothing. The master key lives in one 0600 file beside the database
(or arrives via THEOZOLITH_MASTER_KEY(_FILE)) and never transits the channel.
Rotation re-encrypts every stored value under a fresh key in one transaction
(``theozolith-control rotate-key``).
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    """A secret could not be decrypted (wrong key or corrupted store)."""


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def ensure_key_file(path: Path) -> str:
    """Read the master key, generating it (mode 0600) on first start."""
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    return key


class SecretBox:
    """Encrypt/decrypt secret values under the master key."""

    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise CryptoError(f"invalid master key: {exc}") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise CryptoError("secret cannot be decrypted with this master key") from exc
