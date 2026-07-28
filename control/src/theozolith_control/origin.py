"""The public origin: one randomized browser origin per deployment (ADR-0019).

``origin-init`` (and unified ``init``, ADR-0023) generates
``https://<slug>.<base-domain>`` where the slug encodes 128 bits from the
OS CSPRNG as 26 lowercase base32 characters, persisted as the read-only
``[control] public_origin`` field of control.toml in the Config Repo
(ADR-0024 — storage only; semantics unchanged). The name is
defense in depth for the browser surface — an attacker who cannot name the
host cannot aim a browser at it — and never a substitute for the admin
credential, the private network, or exact-origin enforcement (all still
required).

The public origin is what browsers dial; it is independent of the Uvicorn
bind host and port (``serve --host/--port``), so changing where the server
listens never silently changes the accepted ``Host`` or ``Origin``. The
default HTTPS origin carries no port (browsers omit ``:443``); a
nonstandard *external* port is included explicitly and enforced exactly.

The hostname must resolve only on the trusted network (private DNS or
hosts entries); the Control Node keeps no public ingress path. The base
domain is configurable — the first-party deployment uses
``theozolith.com``; the default ``theozolith.internal`` stays inside the
ICANN-reserved private namespace so a self-contained deployment never
leaks resolution attempts to public DNS.

Production ``serve`` refuses to start without a public origin — from the
persisted artifact or the ``THEOZOLITH_PUBLIC_ORIGIN`` environment
override (an expert escape hatch: the value is format-checked, but a
server cannot measure entropy in text, so the operator supplying it is
responsible for a CSPRNG-generated slug; the generator is the only
sanctioned source). Origins must be https and carry nothing but the
randomized hostname and an optional port: credentials, paths, queries,
fragments, wildcard hosts, malformed ports, and nonconforming first
labels all fail closed.
"""

from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_BASE_DOMAIN = "theozolith.internal"

SLUG_BYTES = 16  # 128 bits
_SLUG_CHARS = 26  # ceil(128 / 5) base32 characters
_SLUG_RE = re.compile(rf"^[a-z2-7]{{{_SLUG_CHARS},63}}$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class OriginError(ValueError):
    """The public origin is missing or does not meet the contract."""


@dataclass(frozen=True)
class PublicOrigin:
    """A parsed, canonicalized public origin. ``origin`` is the one exact
    Origin spelling browsers send; ``hostname`` is what tls-init puts in
    the certificate SAN (never a port); ``host_header`` is the one exact
    Host spelling (port included only when it is not 443)."""

    origin: str
    hostname: str
    port: int

    @property
    def host_header(self) -> str:
        return self.hostname if self.port == 443 else f"{self.hostname}:{self.port}"


def generate_slug() -> str:
    """26 lowercase base32 characters carrying 128 bits of CSPRNG output."""
    return base64.b32encode(secrets.token_bytes(SLUG_BYTES)).decode().rstrip("=").lower()


def compose_origin(slug: str, base_domain: str, *, port: int | None = None) -> str:
    """The canonical origin for a slug + base domain (+ optional external
    port; 443 is the https default and is omitted)."""
    host = f"{slug}.{base_domain.strip('.').lower()}"
    text = f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"
    return parse_public_origin(text).origin


def validate_host(host: str) -> None:
    """Raise OriginError unless ``host`` is ``<slug>.<base-domain>`` with a
    conforming randomized slug and well-formed DNS labels."""
    if not host or len(host) > 253:
        raise OriginError(f"public-origin host {host!r} is empty or too long")
    slug, dot, base = host.partition(".")
    if not dot or not base:
        raise OriginError(f"public-origin host {host!r} has no base domain")
    if not _SLUG_RE.match(slug):
        raise OriginError(
            f"public-origin host {host!r} lacks a conforming slug — the first label must be"
            f" >= {_SLUG_CHARS} base32 characters carrying at least 128 bits of entropy"
            " (run 'theozolith-control origin-init')"
        )
    for label in base.split("."):
        if not _LABEL_RE.match(label):
            raise OriginError(f"public-origin host {host!r}: base-domain label {label!r} invalid")


def parse_public_origin(text: str) -> PublicOrigin:
    """Parse and validate one public origin, canonicalizing case and the
    default port. Everything nonconforming fails closed with OriginError:
    non-https schemes, credentials, non-empty paths (bare ``/`` tolerated),
    queries, fragments, wildcard hosts, malformed or zero ports, and hosts
    whose first label does not meet the slug entropy format."""
    raw = text.strip()
    if not raw:
        raise OriginError(
            "no public origin configured — run 'theozolith-control origin-init'"
            " or set THEOZOLITH_PUBLIC_ORIGIN"
        )
    try:
        split = urlsplit(raw)
    except ValueError as exc:
        raise OriginError(f"public origin {raw!r} does not parse: {exc}") from exc
    if split.scheme != "https":
        raise OriginError(
            f"public origin {raw!r} must be an https:// URL (production browsers reach"
            " the Control Node over TLS only)"
        )
    if split.username is not None or split.password is not None:
        raise OriginError(f"public origin {raw!r} must not carry credentials")
    if split.path not in ("", "/"):
        raise OriginError(f"public origin {raw!r} must not carry a path")
    if split.query or split.fragment:
        raise OriginError(f"public origin {raw!r} must not carry a query or fragment")
    try:
        port = split.port  # urlsplit defers port parsing to this property
    except ValueError as exc:
        raise OriginError(f"public origin {raw!r} has a malformed port") from exc
    if port == 0:
        raise OriginError(f"public origin {raw!r} has a malformed port")
    hostname = split.hostname or ""
    if "*" in hostname:
        raise OriginError(
            f"public origin {raw!r}: wildcard hosts are refused — one exact origin per deployment"
        )
    validate_host(hostname)
    port = port or 443
    origin = f"https://{hostname}" if port == 443 else f"https://{hostname}:{port}"
    return PublicOrigin(origin=origin, hostname=hostname, port=port)
