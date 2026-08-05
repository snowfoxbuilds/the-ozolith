"""The browser origin (ADR-0034, made lazy by ADR-0036).

The slug origin is retired: the default browser origin is the same
persisted address nodes dial — ``https://<control_ip>[:<control_port>]``
(ADR-0031's read-only control.toml fields). Since ADR-0036 the browser
surface is opt-in: ``origin-init`` persists ``browser_origin`` (offering
the IP origin as the default; an operator who runs their own DNS may enter
a hostname origin instead — the one hostname re-entry point). The server
certificate carries the IP SANs from init, so a device that trusts the
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

Both entry points fail closed: ``derive_origin`` refuses a non-IP control
address or an out-of-range port; ``parse_browser_origin`` additionally
refuses every URL shape ADR-0022 refused (credentials, path, query,
fragment, wildcard, non-https) — the guard is never armed with garbage
expectations.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


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


# One DNS label: letter/digit first and last, inner hyphens (ADR-0022's
# hostname shape, minus the retired slug specifics). No wildcards.
_DNS_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _hostname_or_ip(host: str) -> str:
    """The canonical Host spelling for ``host``, or OriginError. IP
    literals canonicalize through ipaddress (bracketed if IPv6); hostnames
    lowercase and must be dot-joined DNS labels, at most 253 characters."""
    stripped = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        parsed = ipaddress.ip_address(stripped)
    except ValueError:
        pass
    else:
        return f"[{parsed}]" if parsed.version == 6 else str(parsed)
    lowered = host.lower().rstrip(".")
    if not lowered or len(lowered) > 253:
        raise OriginError(f"hostname {host!r} is empty or too long")
    for label in lowered.split("."):
        if not _DNS_LABEL.match(label):
            raise OriginError(
                f"hostname {host!r} is not a valid DNS name (label {label!r};"
                " wildcards are refused — every deployment gets one exact origin)"
            )
    return lowered


def parse_browser_origin(text: str) -> BrowserOrigin:
    """One explicit, complete https origin (ADR-0036: the origin-init
    prompt) — ``https://<ip-or-hostname>[:<port>]``, nothing else. Fails
    closed on non-https schemes, credentials, any path beyond ``/``,
    queries, fragments, malformed ports, and malformed hosts (ADR-0022's
    parsing posture; the hostname case is the one hostname re-entry point
    left after ADR-0034)."""
    candidate = (text or "").strip()
    if not candidate:
        raise OriginError("empty browser origin")
    parts = urlsplit(candidate)
    if parts.scheme != "https":
        raise OriginError(f"browser origin must be https://, got {candidate!r}")
    if parts.username or parts.password:
        raise OriginError("browser origin must not carry credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise OriginError("browser origin must be scheme://host[:port] with no path or query")
    try:
        port = parts.port
    except ValueError as exc:
        raise OriginError(f"browser origin port is malformed: {exc}") from exc
    if not parts.hostname:
        raise OriginError(f"browser origin {candidate!r} has no host")
    # urlsplit lowercases and unbrackets the hostname; re-bracket IPv6 via
    # the shared canonicalizer so the Host spelling matches what browsers
    # send.
    host = _hostname_or_ip(parts.hostname)
    port = 443 if port is None else port
    if not 1 <= port <= 65535:
        raise OriginError(f"browser origin port {port!r} is out of range")
    origin = f"https://{host}" if port == 443 else f"https://{host}:{port}"
    return BrowserOrigin(origin=origin, host=host, port=port)


def san_host(browser_origin: BrowserOrigin) -> str:
    """The certificate-SAN spelling of the origin's host: unbracketed IP
    literal, or the hostname (tls.py picks IPAddress vs DNSName by parsing
    exactly this string)."""
    host = browser_origin.host
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host
