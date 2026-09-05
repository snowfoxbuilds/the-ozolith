"""RFC 3986 reference splitting and redirect ``Location`` classification for
the GitHub Relay (ADR-0057 items 5 and 8).

A redirect's ``Location`` is upstream-chosen text, so it is split by the
RFC 3986 Appendix B grammar alone — never ``urllib.parse`` — and reduced to
the closed scheme and host classifications the audit record carries. Nothing
here decides policy: the upstream client re-splits and re-classifies the
resolved reference and applies the origin pin itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from theozolith_worker.relay.audit import HostRepr
from theozolith_worker.relay.ingress import sha256_hex
from theozolith_worker.relay.reasons import HostStatus, Scheme

# RFC 3986 Appendix B. The last group's ``.`` never has to cross a line end:
# a reference carrying CR or LF is refused before the pattern runs.
_REFERENCE = re.compile(r"^(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_SUB_DELIMS = frozenset("!$&'()*+,;=")
_REG_NAME = _UNRESERVED | _SUB_DELIMS
_IP_LITERAL = _REG_NAME | {":"}
HOST_LIMIT = 253


@dataclass(frozen=True)
class UriParts:
    """One URI reference as Appendix B delimits it. ``authority`` is the text
    between ``//`` and the next ``/``, ``?``, or ``#`` — user-info and port
    included, exactly as delimited — and ``None`` when the reference has no
    ``//``; ``authority_raw`` names that same delimited text for the
    invalid-host digest."""

    scheme: str | None
    authority: str | None
    path: str
    query: str | None
    fragment: str | None

    @property
    def authority_raw(self) -> str | None:
        return self.authority


def _is_single_reference(text: str) -> bool:
    return all(0x21 <= ord(char) <= 0x7E for char in text)


def split_reference(text: str) -> UriParts | None:
    """The Appendix B split, or ``None`` when ``text`` is not one reference:
    it holds a space, tab, CR, LF, NUL, any other control byte, or a
    non-ASCII byte, or the grammar does not consume it whole."""
    if not _is_single_reference(text):
        return None
    match = _REFERENCE.fullmatch(text)
    if match is None:
        return None
    return UriParts(match.group(2), match.group(4), match.group(5), match.group(7), match.group(9))


def recompose(parts: UriParts) -> str:
    """RFC 3986 section 5.3 component recomposition."""
    out = ""
    if parts.scheme is not None:
        out += parts.scheme + ":"
    if parts.authority is not None:
        out += "//" + parts.authority
    out += parts.path
    if parts.query is not None:
        out += "?" + parts.query
    if parts.fragment is not None:
        out += "#" + parts.fragment
    return out


def classify_scheme(parts: UriParts) -> Scheme:
    """``https``/``http`` by name, ``other`` for any other scheme token, and
    ``invalid`` for a scheme part outside the token grammar — or for no
    scheme part at all: a relative or scheme-relative ``Location`` is not
    one the relay follows. ``absent`` is the caller's, for no header."""
    if parts.scheme is None or _SCHEME.fullmatch(parts.scheme) is None:
        return Scheme.INVALID
    lowered = parts.scheme.lower()
    if lowered == "https":
        return Scheme.HTTPS
    if lowered == "http":
        return Scheme.HTTP
    return Scheme.OTHER


@dataclass(frozen=True)
class Authority:
    """An authority parsed into its three parts; ``host`` keeps the IP
    literal's brackets. ``None`` from ``parse_authority`` means the text is
    not a well-formed authority at all."""

    userinfo: str | None
    host: str
    port: str | None


def parse_authority(authority: str) -> Authority | None:
    userinfo: str | None = None
    rest = authority
    if "@" in rest:
        userinfo, _, rest = rest.rpartition("@")
    if rest.startswith("["):
        close = rest.find("]")
        if close == -1:
            return None
        host, after = rest[: close + 1], rest[close + 1 :]
        if after == "":
            port: str | None = None
        elif after.startswith(":"):
            port = after[1:]
        else:
            return None
    else:
        host, colon, port_text = rest.partition(":")
        port = port_text if colon else None
    return Authority(userinfo, host, port)


def _host_in_character_set(host: str) -> bool:
    if host.startswith("["):
        return host.endswith("]") and len(host) > 2 and all(c in _IP_LITERAL for c in host[1:-1])
    return all(char in _REG_NAME for char in host)


def _digest_repr(status: HostStatus, text: str) -> HostRepr:
    """Length and digest of the text's bytes: latin-1 for the one-code-point-
    per-byte text the splitter admits, UTF-8 for anything wider a caller
    constructs directly."""
    try:
        data = text.encode("latin-1")
    except UnicodeEncodeError:
        data = text.encode("utf-8", "surrogatepass")
    return HostRepr(status, None, len(data), sha256_hex(data))


def classify_host(parts: UriParts) -> HostRepr:
    """The bounded host representation of the audit record: a literal only
    for a valid host of at most 253 bytes, a length and digest of the host
    when it is merely oversized, a length and digest of the whole delimited
    authority for anything else, nothing when there is no authority."""
    if parts.authority is None:
        return HostRepr(HostStatus.ABSENT)
    authority = parse_authority(parts.authority)
    if (
        authority is None
        or authority.userinfo is not None
        or authority.host == ""
        or "%" in parts.authority
        or (authority.port is not None and not authority.port.isdigit() and authority.port != "")
        or not _host_in_character_set(authority.host)
    ):
        return _digest_repr(HostStatus.INVALID, parts.authority_raw or "")
    if len(authority.host) > HOST_LIMIT:
        return _digest_repr(HostStatus.OVERSIZED, authority.host)
    return HostRepr(HostStatus.VALID, authority.host)


def _merge_paths(base: UriParts, relative_path: str) -> str:
    """RFC 3986 section 5.2.3: the base's last segment is replaced."""
    if base.authority is not None and base.path == "":
        return "/" + relative_path
    return base.path[: base.path.rfind("/") + 1] + relative_path


def resolve_location(base_target: str, location: str) -> str | None:
    """RFC 3986 section 5.2.2 reference resolution of ``location`` against
    the absolute URL of the request just sent, recomposed as one string for
    the caller to re-split and classify. Dot segments are kept, never
    removed: the ingress path grammar refuses them, so a ``Location`` that
    needs normalizing refuses instead of being fixed. ``None`` when
    ``location`` is not a single reference."""
    reference = split_reference(location)
    if reference is None:
        return None
    base = split_reference(base_target)
    if base is None or base.scheme is None:
        raise ValueError("base_target must be an absolute URL")
    if reference.scheme is not None:
        return recompose(reference)
    if reference.authority is not None:
        target = UriParts(
            base.scheme, reference.authority, reference.path, reference.query, reference.fragment
        )
    elif reference.path == "":
        query = base.query if reference.query is None else reference.query
        target = UriParts(base.scheme, base.authority, base.path, query, reference.fragment)
    elif reference.path.startswith("/"):
        target = UriParts(
            base.scheme, base.authority, reference.path, reference.query, reference.fragment
        )
    else:
        merged = _merge_paths(base, reference.path)
        target = UriParts(base.scheme, base.authority, merged, reference.query, reference.fragment)
    return recompose(target)
