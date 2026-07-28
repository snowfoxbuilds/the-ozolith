"""store.db: the durable half of the database split (ADR-0024).

Exactly two things live here — encrypted secret ciphertext (ADR-0015
mechanics unchanged: master.key + Fernet, single-transaction rotation) and
the per-node bearer tokens minted at provisioning (ADR-0023). Everything
else is cache.db's business and deletable. The file sits in
``~/.theozolith/secrets/`` beside master.key: backup is copying that folder,
recovery is restoring it — nodes hold their tokens and reconnect untouched.

Per-node tokens are stored as SHA-256 digests: verification needs only the
digest, so a copied store.db never yields a usable bearer token without the
node that holds it. Minting for an already-known node replaces the old
token (re-provisioning is the rotation story); revocation deletes the row,
after which that node's heartbeats 401 and surface as unregistered
sightings (cache.db).
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS node_tokens (
    node TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
"""

NODE_TOKEN_BYTES = 32


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SecretStore:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- secrets (encrypted values only; crypto lives in crypto.py) ----------

    def put_secret(self, name: str, token: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO secrets (name, token, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET token = ?, updated_at = ?",
                (name, token, self._clock(), token, self._clock()),
            )

    def get_secret_token(self, name: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT token FROM secrets WHERE name = ?", (name,)).fetchone()
        return row["token"] if row else None

    def secret_names(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT name FROM secrets ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    def replace_secret_tokens(self, tokens: dict[str, str]) -> None:
        """Key rotation: swap every stored token in one transaction."""
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE secrets SET token = ? WHERE name = ?",
                [(token, name) for name, token in tokens.items()],
            )

    # -- per-node tokens (ADR-0023: provisioning is registration) -------------

    def mint_node_token(self, node: str) -> str:
        """A fresh non-expiring bearer token for ``node``, replacing any
        previous one (re-provisioning rotates). Returns the one plaintext
        copy — it goes to the node and is never recoverable from here."""
        token = secrets.token_urlsafe(NODE_TOKEN_BYTES)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO node_tokens (node, token_hash, created_at) VALUES (?, ?, ?)"
                " ON CONFLICT (node) DO UPDATE SET token_hash = ?, created_at = ?",
                (node, _digest(token), self._clock(), _digest(token), self._clock()),
            )
        return token

    def node_for_token(self, token: str) -> str | None:
        """The node a presented bearer token belongs to, or None. The digest
        lookup is the comparison — no plaintext tokens exist to compare."""
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT node FROM node_tokens WHERE token_hash = ?", (_digest(token),)
            ).fetchone()
        return row["node"] if row else None

    def revoke_node_token(self, node: str) -> bool:
        """Remove a node from the fleet (explicit revocation, never lapse);
        True when a token existed. Its heartbeats 401 from now on."""
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM node_tokens WHERE node = ?", (node,))
        return cursor.rowcount > 0

    def provisioned_nodes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT node, created_at FROM node_tokens ORDER BY node"
            ).fetchall()
        return [dict(r) for r in rows]
