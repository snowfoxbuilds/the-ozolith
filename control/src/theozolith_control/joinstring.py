"""Join-string composition (ADR-0023): the one paste that provisions a node.

Format (version ``ozjoin1``)::

    ozjoin1:<base64url, unpadded, of payload || checksum>

    payload  = exp:u32be || ca_sha256:32 raw bytes || token:16 raw bytes
               || addr:utf-8 (the rest)
    checksum = first 4 bytes of SHA-256(b"ozjoin1" || payload)

``addr`` is where the bootstrap listener answers: ``host`` or ``host:port``
when the port is nonstandard — the HTTPS exchange port rides in the
canonical control URL the listener serves. The checksum exists so a
truncated paste fails as "malformed join string" on the node, never as a
confusing network error; it is integrity against copy-paste accidents, not
authentication (the CA fingerprint and the token are the security).

The node-side parser (theozolith_nodedaemon.provisioning) mirrors this
byte-for-byte, stdlib-only — no shared import crosses the component
boundary, so a cross-component round-trip test pins the two together.
"""

from __future__ import annotations

import base64
import hashlib

PREFIX = "ozjoin1"
TOKEN_BYTES = 16
_FIXED = 4 + 32 + TOKEN_BYTES  # exp + fingerprint + token, before addr


def compose(*, addr: str, ca_sha256: str, token: bytes, expires_at: float) -> str:
    """The complete join string. ``ca_sha256`` is the CA fingerprint hex
    (tls.ca_fingerprint_sha256); ``token`` is the 16 raw bytes minted by
    Store.create_join_token."""
    if len(token) != TOKEN_BYTES:
        raise ValueError(f"join token must be {TOKEN_BYTES} bytes, got {len(token)}")
    fingerprint = bytes.fromhex(ca_sha256)
    if len(fingerprint) != 32:
        raise ValueError("ca_sha256 must be a 32-byte SHA-256 hex digest")
    if not addr:
        raise ValueError("addr (the bootstrap listener address) is required")
    payload = int(expires_at).to_bytes(4, "big") + fingerprint + token + addr.encode("utf-8")
    checksum = hashlib.sha256(PREFIX.encode("ascii") + payload).digest()[:4]
    encoded = base64.urlsafe_b64encode(payload + checksum).rstrip(b"=").decode("ascii")
    return f"{PREFIX}:{encoded}"
