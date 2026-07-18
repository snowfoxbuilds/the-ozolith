"""The canonical origin: one randomized hostname per deployment (ADR-0019).

``origin-init`` generates ``<slug>.<base-domain>`` where the slug encodes
128 bits from the OS CSPRNG as 26 lowercase base32 characters, and persists
it at ``<data-dir>/canonical-host``. The name is defense in depth for the
browser surface — an attacker who cannot name the host cannot aim a
browser at it — and never a substitute for the admin credential, the
private network, or exact-origin enforcement (all still required).

The hostname must resolve only on the trusted network (private DNS or
hosts entries); the Control Node keeps no public ingress path. The base
domain is configurable — the first-party deployment uses
``theozolith.com``; the default ``theozolith.internal`` stays inside the
ICANN-reserved private namespace so a self-contained deployment never
leaks resolution attempts to public DNS.

Production ``serve`` refuses to start without a persisted canonical host
whose slug meets the entropy format (>= 26 base32 chars); validation is a
format check — the generator is the only sanctioned source of slugs.
"""

from __future__ import annotations

import base64
import re
import secrets
from pathlib import Path

CANONICAL_HOST_FILE = "canonical-host"
DEFAULT_BASE_DOMAIN = "theozolith.internal"

SLUG_BYTES = 16  # 128 bits
_SLUG_CHARS = 26  # ceil(128 / 5) base32 characters
_SLUG_RE = re.compile(rf"^[a-z2-7]{{{_SLUG_CHARS},63}}$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


class OriginError(ValueError):
    """The canonical host is missing or does not meet the contract."""


def generate_slug() -> str:
    """26 lowercase base32 characters carrying 128 bits of CSPRNG output."""
    return base64.b32encode(secrets.token_bytes(SLUG_BYTES)).decode().rstrip("=").lower()


def compose_host(slug: str, base_domain: str) -> str:
    host = f"{slug}.{base_domain.strip('.').lower()}"
    validate_canonical_host(host)
    return host


def validate_canonical_host(host: str) -> None:
    """Raise OriginError unless ``host`` is ``<slug>.<base-domain>`` with a
    conforming slug and well-formed DNS labels."""
    if not host or len(host) > 253:
        raise OriginError(f"canonical host {host!r} is empty or too long")
    slug, dot, base = host.partition(".")
    if not dot or not base:
        raise OriginError(f"canonical host {host!r} has no base domain")
    if not _SLUG_RE.match(slug):
        raise OriginError(
            f"canonical host {host!r} lacks a conforming slug — the first label must be"
            f" >= {_SLUG_CHARS} base32 characters carrying at least 128 bits of entropy"
            " (run 'theozolith-control origin-init')"
        )
    for label in base.split("."):
        if not _LABEL_RE.match(label):
            raise OriginError(f"canonical host {host!r}: base-domain label {label!r} is not valid")


def host_path(data_dir: Path) -> Path:
    return data_dir / CANONICAL_HOST_FILE


def read_canonical_host(data_dir: Path) -> str:
    try:
        return host_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_canonical_host(data_dir: Path, host: str) -> Path:
    validate_canonical_host(host)
    path = host_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(host + "\n", encoding="utf-8")
    return path
