"""The browser origin: the control IP, exactly (ADR-0034).

The slug origin is retired: browsers dial the same persisted address nodes
dial — ``https://<control_ip>[:<control_port>]`` (ADR-0031's read-only
control.toml fields). No DNS record exists anywhere; the server certificate
has carried the IP in its SAN since ADR-0031, so a device that trusts the
per-deployment CA verifies cleanly, and every other device clicks through
the interstitial (the TrueNAS model — CA trust is the optional green-lock
upgrade, never a setup step).

Discovery is not the boundary and never was: the admin password and the
login rate limit (ADR-0027) stand alone. What survives from ADR-0022 is
exact-origin enforcement — cookie-authenticated state changes and
websockets must carry exactly the one Host and one Origin spelling derived
here, so DNS rebinding and lured clicks still fail closed. Browsers omit
the default https port (443) and include any other, which makes the
canonical spelling unambiguous.

Derivation fails closed: a value that is not an IP literal, or a port
outside 1-65535, raises OriginError — the guard is never armed with
garbage expectations.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass


class OriginError(ValueError):
    """The browser origin cannot be derived from the persisted address."""


@dataclass(frozen=True)
class BrowserOrigin:
    """The one canonical browser origin. ``origin`` is the exact Origin
    spelling browsers send; ``host_header`` is the exact Host spelling
    (port included only when it is not 443)."""

    origin: str
    host: str  # the IP literal, bracketed if IPv6 (URL/Host spelling)
    port: int

    @property
    def host_header(self) -> str:
        return self.host if self.port == 443 else f"{self.host}:{self.port}"


def derive_origin(control_ip: str, control_port: int = 443) -> BrowserOrigin:
    """The canonical origin for the persisted control address (ADR-0034)."""
    if not control_ip:
        raise OriginError("no persisted control IP — run 'theozolith init' first")
    try:
        parsed = ipaddress.ip_address(control_ip)
    except ValueError as exc:
        raise OriginError(f"control IP {control_ip!r} is not an IP address: {exc}") from exc
    if not isinstance(control_port, int) or isinstance(control_port, bool):
        raise OriginError(f"control port {control_port!r} is not an integer")
    if not 1 <= control_port <= 65535:
        raise OriginError(f"control port {control_port!r} is out of range")
    host = f"[{parsed}]" if parsed.version == 6 else str(parsed)
    origin = f"https://{host}" if control_port == 443 else f"https://{host}:{control_port}"
    return BrowserOrigin(origin=origin, host=host, port=control_port)
