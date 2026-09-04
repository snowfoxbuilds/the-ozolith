"""The GitHub Relay's hostile-ingress HTTP/1.1 parser (ADR-0057 item 6).

Any process in the run container can reach the relay socket, so nothing here
assumes the client is ``gh``: one request is read from the reader, validated
in a fixed order with the first failure naming the refusal's stage, and never
a byte is read past the declared body end. The request-target has two
grammars — the path is percent-decoded once and refused on any ambiguous
spelling, the query is decoded once with form semantics and re-encoded
canonically — and the upstream request is rebuilt from validated parts only,
so exactly one parser's opinion of the request ever reaches the wire.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from theozolith_worker.relay.reasons import Budgets, MethodClass, Reason, Stage

UPSTREAM_HOST = "api.github.com"

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_SUB_DELIMS = frozenset("!$&'()*+,;=")
# RFC 3986 pchar: what a canonical path segment may hold once decoded. A
# decoded ``/``, ``\\``, ``%``, ``?``, ``#``, space, control, or non-ASCII byte
# is an ambiguous spelling and refuses; a raw ``/`` is the segment separator.
_PATH_DECODED = _UNRESERVED | _SUB_DELIMS | {":", "@"}
_PATH_RAW = _PATH_DECODED | {"/", "%"}
# The RFC 3986 query character set, checked before decoding.
_QUERY_RAW = _UNRESERVED | _SUB_DELIMS | {":", "@", "/", "?", "%"}
_HEX = frozenset("0123456789abcdefABCDEF")
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_DIGITS = re.compile(r"[0-9]+")
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}")
_CHUNK_SIZE_LINE_LIMIT = 34
_CONTENT_TYPE_JSON = re.compile(
    r"application/json[ \t]*(?:;[ \t]*charset=[A-Za-z0-9._-]+[ \t]*)?", re.IGNORECASE
)
_FORWARDED_VALUE_LIMIT = 1024
_ADMITTED_METHODS = frozenset({MethodClass.GET, MethodClass.HEAD, MethodClass.POST})
_WRITE_METHODS = frozenset({MethodClass.PUT, MethodClass.PATCH, MethodClass.DELETE})
_KNOWN_METHODS = {member.value: member for member in MethodClass if member is not MethodClass.other}
_BODYLESS_METHODS = frozenset({MethodClass.GET, MethodClass.HEAD})

# Client headers forwarded upstream, in the order they are sent forward; every
# other client header is stripped and, where the upstream needs one, rebuilt.
CLIENT_HEADER_ALLOWLIST = (
    "Accept",
    "Content-Type",
    "X-GitHub-Api-Version",
    "GraphQL-Features",
    "If-None-Match",
    "If-Modified-Since",
)
_CLIENT_ALLOWLIST_LOWER = tuple(name.lower() for name in CLIENT_HEADER_ALLOWLIST)
_FRAMING_FORBIDDEN = ("expect", "upgrade", "te", "trailer")

RESPONSE_HEADER_ALLOWLIST = (
    "Content-Type",
    "Date",
    "ETag",
    "Last-Modified",
    "Cache-Control",
    "Vary",
    "Link",
    "Retry-After",
    "X-GitHub-Request-Id",
    "X-GitHub-Media-Type",
    "X-GitHub-Api-Version-Selected",
)
_RESPONSE_ALLOWLIST_LOWER = frozenset(name.lower() for name in RESPONSE_HEADER_ALLOWLIST)
_RESPONSE_ALLOWED_PREFIX = "x-ratelimit-"


class Reader(Protocol):
    """What ``read_request`` needs of its reader: the two methods of
    ``io.BufferedReader`` it calls, so in-memory and slow stubs qualify."""

    def readline(self, size: int = -1, /) -> bytes: ...

    def read(self, size: int = -1, /) -> bytes: ...


@dataclass(frozen=True)
class QueryPair:
    """One decoded query pair. ``name`` holds the decoded name bytes as a
    latin-1 string (one code point per byte); ``value`` is ``None`` for a
    bare name that carried no ``=``."""

    name: str
    value: bytes | None


@dataclass(frozen=True)
class CanonicalTarget:
    path: str
    query: tuple[QueryPair, ...]
    raw_len: int
    raw_sha256: str


@dataclass(frozen=True)
class ParsedRequest:
    method: MethodClass
    target: CanonicalTarget
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class IngressRefusal:
    """A refused request line, total over every input: ``stage`` names the
    request-line check that failed (and ``target`` is ``None``); once the
    target validated, a header, framing, or body failure carries it and
    ``stage`` is ``None``."""

    stage: Stage | None
    reason: Reason
    status: int
    method: MethodClass
    method_len: int | None
    method_sha256: str | None
    raw_target_len: int
    raw_target_sha256: str
    target: CanonicalTarget | None


@dataclass(frozen=True)
class NoRequestLine:
    """No request line was ever seen: the peer closed, the reader timed out,
    or the request-line limit was reached before a line end. Not a refusal —
    no record is written and no sequence number is spent."""

    cause: str


@dataclass(frozen=True)
class UpstreamRequest:
    method: str
    request_target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def percent_encode(data: bytes) -> str:
    """The canonical re-encoding: every byte outside the unreserved set
    becomes uppercase ``%XX``, so the result reads identically under RFC
    3986 and form decoding."""
    return "".join(chr(byte) if chr(byte) in _UNRESERVED else f"%{byte:02X}" for byte in data)


def canonical_query(pairs: Iterable[QueryPair]) -> str:
    parts = []
    for pair in pairs:
        part = percent_encode(pair.name.encode("latin-1"))
        if pair.value is not None:
            part += "=" + percent_encode(pair.value)
        parts.append(part)
    return "&".join(parts)


def _target_bytes(raw_target: str) -> bytes:
    """The raw request-target bytes: the parser hands over latin-1 text (one
    code point per byte); any wider text is digested as UTF-8."""
    try:
        return raw_target.encode("latin-1")
    except UnicodeEncodeError:
        return raw_target.encode("utf-8", "surrogatepass")


def _is_origin_form(raw_target: str) -> bool:
    return raw_target.startswith("/") and "#" not in raw_target


def _canonical_path(raw_path: str, budgets: Budgets) -> str | None:
    if len(raw_path) > budgets.path_limit:
        return None
    decoded: list[str] = []
    i, n = 0, len(raw_path)
    while i < n:
        char = raw_path[i]
        if char == "%":
            escape = raw_path[i + 1 : i + 3]
            if len(escape) < 2 or escape[0] not in _HEX or escape[1] not in _HEX:
                return None
            char = chr(int(escape, 16))
            if char not in _PATH_DECODED:
                return None
            i += 3
        elif char in _PATH_RAW:
            i += 1
        else:
            return None
        decoded.append(char)
    path = "".join(decoded)
    if path == "/":
        return path
    if any(segment in ("", ".", "..") for segment in path.split("/")[1:]):
        return None
    return path


def _decode_query_component(text: str) -> bytes | None:
    out = bytearray()
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "%":
            escape = text[i + 1 : i + 3]
            if len(escape) < 2 or escape[0] not in _HEX or escape[1] not in _HEX:
                return None
            byte = int(escape, 16)
            i += 3
        else:
            byte = 0x20 if char == "+" else ord(char)
            i += 1
        if byte < 0x20 or byte == 0x7F:
            return None
        out.append(byte)
    return bytes(out)


def _canonical_query_pairs(raw_query: str, budgets: Budgets) -> tuple[QueryPair, ...] | None:
    if len(raw_query) > budgets.query_limit or raw_query == "":
        return None
    if any(char not in _QUERY_RAW for char in raw_query):
        return None
    parts = raw_query.split("&")
    if len(parts) > budgets.query_pairs:
        return None
    pairs = []
    for part in parts:
        name, equals, value = part.partition("=")
        if name == "":
            return None
        name_bytes = _decode_query_component(name)
        if name_bytes is None:
            return None
        value_bytes: bytes | None = None
        if equals:
            value_bytes = _decode_query_component(value)
            if value_bytes is None:
                return None
        pairs.append(QueryPair(name_bytes.decode("latin-1"), value_bytes))
    return tuple(pairs)


def canonicalize_target(raw_target: str, budgets: Budgets) -> CanonicalTarget | IngressRefusal:
    """The one canonicalizer for a request-target string: origin-form, then
    the path grammar, then the query grammar. A refusal carries the stage,
    reason, status, and the raw target's length and digest with
    ``target=None``; its method fields are placeholders (``other``, no
    length or digest) for the caller to fill."""
    raw = _target_bytes(raw_target)

    def refuse(stage: Stage, reason: Reason) -> IngressRefusal:
        return IngressRefusal(
            stage=stage,
            reason=reason,
            status=400,
            method=MethodClass.other,
            method_len=None,
            method_sha256=None,
            raw_target_len=len(raw),
            raw_target_sha256=sha256_hex(raw),
            target=None,
        )

    if not _is_origin_form(raw_target):
        return refuse(Stage.TARGET_FORM, Reason.TARGET_FORM)
    raw_path, question, raw_query = raw_target.partition("?")
    path = _canonical_path(raw_path, budgets)
    if path is None:
        return refuse(Stage.PATH, Reason.PATH)
    pairs: tuple[QueryPair, ...] = ()
    if question:
        decoded = _canonical_query_pairs(raw_query, budgets)
        if decoded is None:
            return refuse(Stage.QUERY, Reason.QUERY)
        pairs = decoded
    return CanonicalTarget(path, pairs, len(raw), sha256_hex(raw))


def _classify_method(token: str) -> tuple[MethodClass, int | None, str | None]:
    known = _KNOWN_METHODS.get(token)
    if known is not None:
        return known, None, None
    raw = token.encode("latin-1")
    return MethodClass.other, len(raw), sha256_hex(raw)


def _read_exact(reader: Reader, size: int) -> bytes | None:
    chunks = []
    remaining = size
    while remaining:
        chunk = reader.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse_header_line(line: bytes) -> tuple[str, str] | None:
    if not line.endswith(b"\r\n"):
        return None
    content = line[:-2]
    if any(byte >= 0x7F or (byte < 0x20 and byte != 0x09) for byte in content):
        return None
    if content[:1] in (b" ", b"\t"):
        return None
    name, colon, value = content.partition(b":")
    if not colon or _TOKEN.fullmatch(name.decode("ascii")) is None:
        return None
    return name.decode("ascii"), value.strip(b" \t").decode("ascii")


def _read_chunked(reader: Reader, budgets: Budgets) -> bytes | Reason:
    parts: list[bytes] = []
    total = 0
    while True:
        line = reader.readline(_CHUNK_SIZE_LINE_LIMIT)
        if not line.endswith(b"\n"):
            return Reason.FRAMING if len(line) >= _CHUNK_SIZE_LINE_LIMIT else Reason.BODY
        if not line.endswith(b"\r\n") or _CHUNK_SIZE.fullmatch(line[:-2]) is None:
            return Reason.FRAMING
        size = int(line[:-2], 16)
        if size == 0:
            terminator = _read_exact(reader, 2)
            if terminator is None:
                return Reason.BODY
            return b"".join(parts) if terminator == b"\r\n" else Reason.FRAMING
        total += size
        if total > budgets.request_body_limit:
            return Reason.FRAMING
        data = _read_exact(reader, size + 2)
        if data is None:
            return Reason.BODY
        if not data.endswith(b"\r\n"):
            return Reason.FRAMING
        parts.append(data[:-2])


def read_request(
    reader: Reader, budgets: Budgets
) -> ParsedRequest | IngressRefusal | NoRequestLine:
    """Read exactly one request. Socket timeouts are the caller's and surface
    as ``TimeoutError`` from the reader; a byte past the declared body end is
    never read, so the caller's next read sees any pipelined tail."""
    line_limit = budgets.request_line_limit
    try:
        line = reader.readline(line_limit + 2)
    except TimeoutError:
        return NoRequestLine("timeout")
    if not line.endswith(b"\n"):
        return NoRequestLine("over-long" if len(line) >= line_limit + 2 else "closed")
    content = line[:-1]
    crlf = content.endswith(b"\r")
    if crlf:
        content = content[:-1]
    text = content.decode("latin-1")
    method_token, first_sp, rest = text.partition(" ")
    raw_target, second_sp, version = rest.partition(" ")
    method, method_len, method_sha256 = _classify_method(method_token)
    raw = _target_bytes(raw_target)

    def refuse_line(stage: Stage, reason: Reason, status: int) -> IngressRefusal:
        return IngressRefusal(
            stage=stage,
            reason=reason,
            status=status,
            method=method,
            method_len=method_len,
            method_sha256=method_sha256,
            raw_target_len=len(raw),
            raw_target_sha256=sha256_hex(raw),
            target=None,
        )

    if not text or not crlf or not first_sp or not second_sp:
        return refuse_line(Stage.REQUEST_LINE, Reason.REQUEST_LINE, 400)
    if version != "HTTP/1.1":
        return refuse_line(Stage.VERSION, Reason.VERSION, 505)
    if not _is_origin_form(raw_target):
        return refuse_line(Stage.TARGET_FORM, Reason.TARGET_FORM, 400)
    if method in _WRITE_METHODS:
        return refuse_line(Stage.METHOD, Reason.MUTATION, 403)
    if method not in _ADMITTED_METHODS:
        return refuse_line(Stage.METHOD, Reason.METHOD, 405)
    target = canonicalize_target(raw_target, budgets)
    if isinstance(target, IngressRefusal):
        return replace(target, method=method, method_len=method_len, method_sha256=method_sha256)

    def refuse(reason: Reason) -> IngressRefusal:
        return IngressRefusal(
            stage=None,
            reason=reason,
            status=400,
            method=method,
            method_len=method_len,
            method_sha256=method_sha256,
            raw_target_len=len(raw),
            raw_target_sha256=sha256_hex(raw),
            target=target,
        )

    fields: list[tuple[str, str]] = []
    head_bytes = 0
    while True:
        try:
            line = reader.readline(budgets.header_field + 2)
        except TimeoutError:
            return refuse(Reason.HEADERS)
        if not line.endswith(b"\n"):
            return refuse(Reason.HEADERS)
        if line == b"\r\n":
            break
        head_bytes += len(line)
        if len(fields) >= budgets.header_count or head_bytes > budgets.headers_total:
            return refuse(Reason.HEADERS)
        field = _parse_header_line(line)
        if field is None:
            return refuse(Reason.HEADERS)
        fields.append(field)

    by_name: dict[str, list[str]] = {}
    for name, value in fields:
        by_name.setdefault(name.lower(), []).append(value)
    if any(name in by_name for name in _FRAMING_FORBIDDEN):
        return refuse(Reason.FRAMING)
    for value in by_name.get("connection", ()):
        if any(token.strip().lower() == "upgrade" for token in value.split(",")):
            return refuse(Reason.FRAMING)
    content_lengths = by_name.get("content-length", [])
    transfer_encodings = by_name.get("transfer-encoding", [])
    if len(content_lengths) > 1 or len(transfer_encodings) > 1:
        return refuse(Reason.FRAMING)
    if content_lengths and transfer_encodings:
        return refuse(Reason.FRAMING)
    body_length = 0
    if content_lengths:
        if _DIGITS.fullmatch(content_lengths[0]) is None:
            return refuse(Reason.FRAMING)
        body_length = int(content_lengths[0])
        if body_length > budgets.request_body_limit:
            return refuse(Reason.FRAMING)
    chunked = False
    if transfer_encodings:
        if transfer_encodings[0].lower() != "chunked":
            return refuse(Reason.FRAMING)
        chunked = True
    if method in _BODYLESS_METHODS and (chunked or body_length):
        return refuse(Reason.BODY)

    body = b""
    try:
        if chunked:
            result = _read_chunked(reader, budgets)
            if isinstance(result, Reason):
                return refuse(result)
            body = result
        elif body_length:
            data = _read_exact(reader, body_length)
            if data is None:
                return refuse(Reason.BODY)
            body = data
    except TimeoutError:
        return refuse(Reason.BODY)

    headers = tuple(
        (name, value) for name, value in fields if name.lower() in _CLIENT_ALLOWLIST_LOWER
    )
    return ParsedRequest(method, target, headers, body)


def _forwardable(parsed: ParsedRequest, name_lower: str, value: str) -> bool:
    if len(value.encode("ascii")) > _FORWARDED_VALUE_LIMIT:
        return False
    if name_lower == "content-type":
        return parsed.method is MethodClass.POST and _CONTENT_TYPE_JSON.fullmatch(value) is not None
    return True


def build_upstream_request(
    parsed: ParsedRequest, *, authorization: str | None, user_agent: str
) -> UpstreamRequest:
    """The upstream request rebuilt from validated parts — never a raw client
    byte: the canonical target, the allowlisted headers (each at most once,
    the first occurrence deciding), and the relay's own framing."""
    query = canonical_query(parsed.target.query)
    request_target = parsed.target.path + (f"?{query}" if query else "")
    headers: list[tuple[str, str]] = [
        ("Host", UPSTREAM_HOST),
        ("User-Agent", user_agent),
        ("Accept-Encoding", "identity"),
    ]
    if authorization is not None:
        headers.append(("Authorization", authorization))
    for name_lower in _CLIENT_ALLOWLIST_LOWER:
        matching = (header for header in parsed.headers if header[0].lower() == name_lower)
        first = next(matching, None)
        if first is not None and _forwardable(parsed, name_lower, first[1]):
            headers.append(first)
    if parsed.method is MethodClass.POST:
        headers.append(("Content-Length", str(len(parsed.body))))
    return UpstreamRequest(parsed.method.value, request_target, tuple(headers), parsed.body)


def filter_response_headers(headers: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """The response headers delivered after the gate; framing is excluded
    because the server reframes, and everything not allowlisted is stripped."""
    kept = []
    for name, value in headers:
        lower = name.lower()
        if lower in _RESPONSE_ALLOWLIST_LOWER or lower.startswith(_RESPONSE_ALLOWED_PREFIX):
            kept.append((name, value))
    return tuple(kept)
