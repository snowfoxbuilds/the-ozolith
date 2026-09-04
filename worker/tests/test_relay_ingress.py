"""The hostile-ingress parser, the two request-target grammars, upstream
reconstruction, and the header allowlists (ADR-0057 items 6 and 11), driven
by in-memory readers, a slow-reader stub, and the fixture corpus."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from theozolith_worker.relay.classify import classify_rest, is_admin_read
from theozolith_worker.relay.ingress import (
    CLIENT_HEADER_ALLOWLIST,
    RESPONSE_HEADER_ALLOWLIST,
    CanonicalTarget,
    IngressRefusal,
    NoRequestLine,
    ParsedRequest,
    QueryPair,
    build_upstream_request,
    canonical_query,
    canonicalize_target,
    filter_response_headers,
    read_request,
    sha256_hex,
)
from theozolith_worker.relay.reasons import DEFAULT_BUDGETS, Budgets, MethodClass, Reason, Stage

FIXTURES = Path(__file__).parent / "relay_fixtures"
INGRESS_CASES = sorted(p.name[: -len(".http")] for p in (FIXTURES / "ingress").glob("*.http"))
REST_HTTP_CASES = sorted(p.name[: -len(".http")] for p in (FIXTURES / "rest").glob("*.http"))
REST_TARGET_CASES = sorted(p.name[: -len(".target")] for p in (FIXTURES / "rest").glob("*.target"))


def expected(sub: str, name: str) -> dict:
    return json.loads((FIXTURES / sub / f"{name}.expected.json").read_text())


def reader(data: bytes) -> io.BufferedReader:
    return io.BufferedReader(io.BytesIO(data))


def parse(data: bytes, budgets: Budgets = DEFAULT_BUDGETS):
    return read_request(reader(data), budgets)


def request(line: bytes, *headers: bytes, body: bytes = b"") -> bytes:
    return line + b"\r\n" + b"".join(header + b"\r\n" for header in headers) + b"\r\n" + body


class SlowReader:
    """Hands out one byte per call and, once its data is spent, either raises
    ``TimeoutError`` (a socket read timeout) or returns EOF (a peer close)."""

    def __init__(self, data: bytes, *, then: str = "timeout"):
        self.data = data
        self.pos = 0
        self.then = then

    def _byte(self) -> bytes:
        if self.pos >= len(self.data):
            if self.then == "timeout":
                raise TimeoutError("read timed out")
            return b""
        self.pos += 1
        return self.data[self.pos - 1 : self.pos]

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        return self._byte()

    def readline(self, size: int = -1) -> bytes:
        limit = size if size >= 0 else len(self.data) + 1
        out = bytearray()
        while len(out) < limit:
            piece = self._byte()
            if not piece:
                break
            out += piece
            if piece == b"\n":
                break
        return bytes(out)


def refusal_triple(result) -> tuple[str | None, str, int]:
    assert isinstance(result, IngressRefusal), result
    return (
        None if result.stage is None else result.stage.value,
        result.reason.value,
        result.status,
    )


# ------------------------------------------------------------- the corpus


def test_corpus_is_present():
    assert len(INGRESS_CASES) >= 60 and len(REST_HTTP_CASES) >= 12 and len(REST_TARGET_CASES) >= 60


@pytest.mark.parametrize("name", INGRESS_CASES)
def test_hostile_ingress_corpus(name):
    data = (FIXTURES / "ingress" / f"{name}.http").read_bytes()
    want = expected("ingress", name)
    assert refusal_triple(parse(data)) == (want["stage"], want["reason"], want["status"]), name


@pytest.mark.parametrize("name", REST_HTTP_CASES)
def test_pinned_gh_requests_reconstruct_byte_exact(name):
    data = (FIXTURES / "rest" / f"{name}.http").read_bytes()
    want = expected("rest", name)
    parsed = parse(data)
    assert isinstance(parsed, ParsedRequest), name
    upstream = build_upstream_request(parsed, authorization="token x", user_agent="relay/1")
    assert upstream.method == want["method"]
    assert upstream.request_target == want["upstream_target"]
    # The one canonicalizer: the request line's target canonicalizes to the
    # same value whether it arrives through read_request or on its own.
    raw_target = data.split(b"\r\n", 1)[0].split(b" ")[1].decode("latin-1")
    assert canonicalize_target(raw_target, DEFAULT_BUDGETS) == parsed.target
    assert parsed.target.raw_len == len(raw_target)
    assert parsed.target.raw_sha256 == sha256_hex(raw_target.encode("latin-1"))


@pytest.mark.parametrize("name", REST_TARGET_CASES)
def test_request_target_grammar_corpus(name):
    raw = (FIXTURES / "rest" / f"{name}.target").read_bytes()
    want = expected("rest", name)
    raw_target = raw.decode("latin-1")
    result = canonicalize_target(raw_target, DEFAULT_BUDGETS)
    if "refusal" in want:
        assert isinstance(result, IngressRefusal), name
        assert refusal_triple(result) == (
            want["refusal"]["stage"],
            want["refusal"]["reason"],
            want["refusal"]["status"],
        ), name
        assert result.target is None
        assert (result.raw_target_len, result.raw_target_sha256) == (len(raw), sha256_hex(raw))
    else:
        assert isinstance(result, CanonicalTarget), name
        query = canonical_query(result.query)
        assert result.path + (f"?{query}" if query else "") == want["upstream_target"], name
    if b" " in raw or b"\r" in raw or b"\n" in raw or not raw:
        return
    # Through the request line, the same target yields the same outcome.
    via_line = parse(request(b"GET " + raw + b" HTTP/1.1"))
    if isinstance(result, IngressRefusal):
        assert refusal_triple(via_line) == refusal_triple(result)
        assert via_line.method is MethodClass.GET
    else:
        assert isinstance(via_line, ParsedRequest)
        assert via_line.target == result


# ------------------------------------------------------- request line stage


def test_request_line_exactly_at_the_limit_is_seen_and_one_byte_over_is_not():
    limit = DEFAULT_BUDGETS.request_line_limit
    line = b"GET /" + b"a" * 4095 + b"?q=" + b"a" * 4080 + b" HTTP/1.1"
    assert len(line) == limit
    parsed = parse(line + b"\r\n\r\n")
    assert isinstance(parsed, ParsedRequest) and len(parsed.target.path) == 4096
    over = parse(line.replace(b"?q=", b"?q=a") + b"\r\n\r\n")
    assert over == NoRequestLine("over-long")
    # Seen and refused, with a record producible from the refusal's fields.
    refused_line = b"GET /" + b"a" * (limit - 14) + b" HTTP/1.1"
    assert len(refused_line) == limit
    refusal = parse(refused_line + b"\r\n\r\n")
    assert refusal_triple(refusal) == ("path", "path", 400)
    assert refusal.raw_target_len == limit - 13


def test_no_request_line_on_close_timeout_and_over_long():
    assert parse(b"") == NoRequestLine("closed")
    assert parse(b"GET /user") == NoRequestLine("closed")
    assert read_request(SlowReader(b"GET /us"), DEFAULT_BUDGETS) == NoRequestLine("timeout")
    assert read_request(SlowReader(b"", then="eof"), DEFAULT_BUDGETS) == NoRequestLine("closed")
    assert parse(b"G" * 9000) == NoRequestLine("over-long")


def test_empty_request_line_is_seen_and_refused_at_the_request_line_stage():
    refusal = parse(b"\r\n\r\n")
    assert refusal_triple(refusal) == ("request-line", "request-line", 400)
    assert refusal.method is MethodClass.other
    assert (refusal.method_len, refusal.method_sha256) == (0, sha256_hex(b""))
    assert (refusal.raw_target_len, refusal.raw_target_sha256) == (0, sha256_hex(b""))
    assert refusal.target is None


def test_naming_stages_for_version_target_form_and_method():
    assert refusal_triple(parse(request(b"GET /user HTTP/1.0"))) == ("version", "version", 505)
    assert refusal_triple(parse(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")) == ("version", "version", 505)
    for target in (b"http://api.github.com/user", b"api.github.com:443", b"*", b"/user#f"):
        result = parse(request(b"GET " + target + b" HTTP/1.1"))
        assert refusal_triple(result) == ("target-form", "target-form", 400), target
        assert result.raw_target_len == len(target)
    for method in (b"CONNECT", b"OPTIONS", b"TRACE"):
        result = parse(request(method + b" /user HTTP/1.1"))
        assert refusal_triple(result) == ("method", "method", 405), method
        assert result.method is MethodClass(method.decode()) and result.method_len is None
    for method in (b"PUT", b"PATCH", b"DELETE"):
        result = parse(request(method + b" /user HTTP/1.1"))
        assert refusal_triple(result) == ("method", "mutation", 403), method
        assert result.method is MethodClass(method.decode())


def test_long_and_unknown_method_tokens_are_other_with_length_and_digest():
    token = b"X" * 5000
    line = token + b" /user HTTP/1.1"
    assert len(line) < DEFAULT_BUDGETS.request_line_limit
    result = parse(request(line))
    assert refusal_triple(result) == ("method", "method", 405)
    assert result.method is MethodClass.other
    assert (result.method_len, result.method_sha256) == (5000, sha256_hex(token))
    assert result.target is None
    upgrade = parse(request(b"BREW /user HTTP/1.1", b"Upgrade: h2c"))
    assert refusal_triple(upgrade) == ("method", "method", 405)
    assert upgrade.method is MethodClass.other and upgrade.method_len == 4
    # The literal token "other" is not the closed class.
    literal = parse(request(b"other /user HTTP/1.1"))
    assert literal.method is MethodClass.other and literal.method_len == 5


def test_post_off_graphql_validates_and_is_then_classified_a_mutation():
    parsed = parse(request(b"POST /repos/o/r/issues HTTP/1.1", b"Content-Length: 2", body=b"{}"))
    assert isinstance(parsed, ParsedRequest) and parsed.body == b"{}"
    assert classify_rest(parsed.method, parsed.target.path) is Reason.MUTATION
    graphql = parse(request(b"POST /graphql HTTP/1.1", b"Content-Length: 2", body=b"{}"))
    assert classify_rest(graphql.method, graphql.target.path) is None


# ------------------------------------------------------------ path stage


ENCODED_DENYLIST_MATRIX = [
    b"/repos/o/r%2Fhooks",
    b"/repos/o/r%5Chooks",
    b"/repos/o/%2e%2e/hooks",
    b"/repos/o/r/hooks%00",
    b"/repos/o/r%25/hooks",
    b"/repos/o/r%252Fhooks",
    b"/repos/o/r\\hooks",
    b"/repos/o/r//hooks",
    b"/repos/o/r/hooks/",
    b"/repos/o/r/./hooks",
    b"/repos/o/r/x/../hooks",
    b"/repos/o/r/hooks\xc3\xa9",
]


@pytest.mark.parametrize("raw", ENCODED_DENYLIST_MATRIX)
def test_encoded_denylist_spellings_refuse_at_the_path_stage_before_the_denylist(raw):
    result = parse(request(b"GET " + raw + b" HTTP/1.1"))
    assert refusal_triple(result) == ("path", "path", 400)
    assert result.target is None  # nothing was canonicalized for the denylist to see


def test_the_canonical_denylisted_spelling_is_refused_by_the_denylist_itself():
    parsed = parse(request(b"GET /repos/o/r/hooks HTTP/1.1"))
    assert isinstance(parsed, ParsedRequest)
    assert is_admin_read(parsed.target.path)
    assert classify_rest(parsed.method, parsed.target.path) is Reason.ADMIN_READ


def test_path_limit_exactly_at_and_one_over():
    at = canonicalize_target("/" + "a" * 4095, DEFAULT_BUDGETS)
    assert isinstance(at, CanonicalTarget) and len(at.path) == 4096
    over = canonicalize_target("/" + "a" * 4096, DEFAULT_BUDGETS)
    assert refusal_triple(over) == ("path", "path", 400)


def test_path_decodes_once_and_never_resolves():
    parsed = parse(request(b"GET /%75ser HTTP/1.1"))
    assert parsed.target.path == "/user"
    parsed = parse(request(b"GET /repos/o/r/compare/main...fork%3Afeature HTTP/1.1"))
    assert parsed.target.path == "/repos/o/r/compare/main...fork:feature"
    assert parse(request(b"GET / HTTP/1.1")).target.path == "/"


# ----------------------------------------------------------- query stage


def test_query_grammar_decodes_once_with_form_semantics_and_reencodes_canonically():
    parsed = parse(request(b"GET /search/issues?q=repo%3AOWNER%2FREPO+is%3Aopen HTTP/1.1"))
    assert parsed.target.query == (QueryPair("q", b"repo:OWNER/REPO is:open"),)
    assert canonical_query(parsed.target.query) == "q=repo%3AOWNER%2FREPO%20is%3Aopen"
    same = parse(request(b"GET /search/issues?q=repo:OWNER/REPO%20is:open HTTP/1.1"))
    assert same.target.query == parsed.target.query
    plus = parse(request(b"GET /x?q=a%2Bb HTTP/1.1")).target.query
    assert plus == (QueryPair("q", b"a+b"),) and canonical_query(plus) == "q=a%2Bb"
    double = parse(request(b"GET /x?q=%2541 HTTP/1.1")).target.query
    assert double == (QueryPair("q", b"%41"),) and canonical_query(double) == "q=%2541"
    repeated = parse(request(b"GET /x?state=open&state=closed&state=open HTTP/1.1")).target.query
    assert repeated == (
        QueryPair("state", b"open"),
        QueryPair("state", b"closed"),
        QueryPair("state", b"open"),
    )
    bare = parse(request(b"GET /x?flag&a=b=c&empty= HTTP/1.1")).target.query
    assert bare == (QueryPair("flag", None), QueryPair("a", b"b=c"), QueryPair("empty", b""))
    assert canonical_query(bare) == "flag&a=b%3Dc&empty="
    assert canonical_query(()) == ""
    name = parse(request(b"GET /x?%C3%A9=1 HTTP/1.1")).target.query
    assert name == (QueryPair("\xc3\xa9", b"1"),) and canonical_query(name) == "%C3%A9=1"


def test_query_pairs_exactly_at_and_one_over():
    at = "/x?" + "&".join(f"p{i}=v" for i in range(32))
    assert isinstance(canonicalize_target(at, DEFAULT_BUDGETS), CanonicalTarget)
    over = at + "&p32=v"
    assert refusal_triple(canonicalize_target(over, DEFAULT_BUDGETS)) == ("query", "query", 400)


def test_query_limit_exactly_at_and_one_over():
    at = canonicalize_target("/x?q=" + "a" * 4094, DEFAULT_BUDGETS)
    assert isinstance(at, CanonicalTarget)
    over = canonicalize_target("/x?q=" + "a" * 4095, DEFAULT_BUDGETS)
    assert refusal_triple(over) == ("query", "query", 400)


def test_no_query_ever_changes_a_classification_or_denylist_decision():
    hostile_queries = [
        b"",
        b"?hooks=1",
        b"?path=%2Frepos%2Fo%2Fr%2Fhooks",
        b"?q=%2e%2e%2Fhooks",
        b"?state=open&state=closed",
        b"?" + b"&".join(b"p%d=v" % i for i in range(32)),
    ]
    for query in hostile_queries:
        permitted = parse(request(b"GET /repos/o/r/issues" + query + b" HTTP/1.1"))
        assert isinstance(permitted, ParsedRequest), query
        assert classify_rest(permitted.method, permitted.target.path) is None
        denied = parse(request(b"GET /repos/o/r/hooks" + query + b" HTTP/1.1"))
        assert isinstance(denied, ParsedRequest), query
        assert classify_rest(denied.method, denied.target.path) is Reason.ADMIN_READ


def test_canonicalize_target_placeholder_method_fields():
    refusal = canonicalize_target("/x?q=%G1", DEFAULT_BUDGETS)
    assert refusal.method is MethodClass.other
    assert refusal.method_len is None and refusal.method_sha256 is None


# ---------------------------------------------------------------- headers


def test_header_count_exactly_at_and_one_over():
    fields = [b"X-%d: v" % i for i in range(64)]
    assert isinstance(parse(request(b"GET /user HTTP/1.1", *fields)), ParsedRequest)
    over = parse(request(b"GET /user HTTP/1.1", *fields, b"X-64: v"))
    assert refusal_triple(over) == (None, "headers", 400)
    assert over.target is not None and over.target.path == "/user"


def test_header_bytes_exactly_at_and_one_over():
    first = b"A: " + b"a" * (8190 - 3)  # a line of 8192 bytes once its CRLF is counted
    second = b"B: " + b"b" * (8190 - 3)
    assert len(first) + 2 == 8192 and len(second) + 2 == 8192
    assert isinstance(parse(request(b"GET /user HTTP/1.1", first, second)), ParsedRequest)
    over = parse(request(b"GET /user HTTP/1.1", first, second + b"b"))
    assert refusal_triple(over) == (None, "headers", 400)


def test_header_field_exactly_at_and_one_over():
    at = b"A: " + b"a" * (8192 - 3)
    assert isinstance(parse(request(b"GET /user HTTP/1.1", at)), ParsedRequest)
    assert refusal_triple(parse(request(b"GET /user HTTP/1.1", at + b"a"))) == (
        None,
        "headers",
        400,
    )


def test_timeouts_and_eof_name_head_or_body():
    head = SlowReader(b"GET /user HTTP/1.1\r\nAccept: a")
    result = read_request(head, DEFAULT_BUDGETS)
    assert refusal_triple(result) == (None, "headers", 400) and result.target.path == "/user"
    body = SlowReader(b"POST /graphql HTTP/1.1\r\nContent-Length: 5\r\n\r\n{}")
    assert refusal_triple(read_request(body, DEFAULT_BUDGETS)) == (None, "body", 400)
    chunked = SlowReader(b"POST /graphql HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{")
    assert refusal_triple(read_request(chunked, DEFAULT_BUDGETS)) == (None, "body", 400)
    eof_head = parse(b"GET /user HTTP/1.1\r\nAccept: a\r\n")
    assert refusal_triple(eof_head) == (None, "headers", 400)
    eof_body = parse(request(b"POST /graphql HTTP/1.1", b"Content-Length: 5", body=b"{}"))
    assert refusal_triple(eof_body) == (None, "body", 400)
    slow_ok = SlowReader(request(b"POST /graphql HTTP/1.1", b"Content-Length: 2", body=b"{}"))
    assert read_request(slow_ok, DEFAULT_BUDGETS).body == b"{}"


def test_header_values_are_trimmed_and_names_keep_their_spelling():
    parsed = parse(
        request(
            b"POST /graphql HTTP/1.1",
            b"Content-Length: 2",
            b"accept:\t application/json \t",
            b"Graphql-Features: merge_queue",
            body=b"{}",
        )
    )
    assert parsed.headers == (("accept", "application/json"), ("Graphql-Features", "merge_queue"))


# ---------------------------------------------------------------- framing


def test_smuggling_shapes_yield_exactly_one_refusal_and_read_nothing_past_the_head():
    tail = b"GET /user/keys HTTP/1.1\r\n\r\n"
    shapes = {
        "cl-te": request(
            b"POST /graphql HTTP/1.1",
            b"Content-Length: 6",
            b"Transfer-Encoding: chunked",
            body=b"0\r\n\r\n" + tail,
        ),
        "te-cl": request(
            b"POST /graphql HTTP/1.1",
            b"Transfer-Encoding: chunked",
            b"Content-Length: 4",
            body=b"2c\r\n" + tail + b"\r\n0\r\n\r\n",
        ),
        "te-te": request(
            b"POST /graphql HTTP/1.1",
            b"Transfer-Encoding: chunked",
            b"Transfer-Encoding: x",
            body=b"0\r\n\r\n" + tail,
        ),
    }
    for name, data in shapes.items():
        stream = reader(data)
        result = read_request(stream, DEFAULT_BUDGETS)
        assert refusal_triple(result) == (None, "framing", 400), name
        # One refusal; the body (and the smuggled line in it) is untouched.
        assert stream.read() == data.split(b"\r\n\r\n", 1)[1], name


def test_read_request_never_reads_past_the_declared_body_end():
    tail = b"GET /user/keys HTTP/1.1\r\n\r\n"
    fixed = reader(request(b"POST /graphql HTTP/1.1", b"Content-Length: 2", body=b"{}" + tail))
    assert read_request(fixed, DEFAULT_BUDGETS).body == b"{}"
    assert fixed.read() == tail
    chunked = reader(
        request(
            b"POST /graphql HTTP/1.1",
            b"Transfer-Encoding: chunked",
            body=b"2\r\n{}\r\n0\r\n\r\n" + tail,
        )
    )
    assert read_request(chunked, DEFAULT_BUDGETS).body == b"{}"
    assert chunked.read() == tail
    bodyless = reader(request(b"GET /user HTTP/1.1") + tail)
    assert isinstance(read_request(bodyless, DEFAULT_BUDGETS), ParsedRequest)
    assert bodyless.read() == tail


def test_body_framing_matrix():
    post = b"POST /graphql HTTP/1.1"
    ok = parse(request(post, b"Content-Length: 0"))
    assert isinstance(ok, ParsedRequest) and ok.body == b""
    none = parse(request(post))
    assert isinstance(none, ParsedRequest) and none.body == b""
    dechunked = parse(
        request(post, b"Transfer-Encoding: Chunked", body=b"3\r\nabc\r\n0002\r\nde\r\n0\r\n\r\n")
    )
    assert dechunked.body == b"abcde"
    get_zero = parse(request(b"GET /user HTTP/1.1", b"Content-Length: 0"))
    assert isinstance(get_zero, ParsedRequest)
    for headers in (
        (b"Content-Length: 2", b"Content-Length: 2"),
        (b"Content-Length: 2", b"Content-Length: 3"),
        (b"Content-Length: 2, 2",),
        (b"Content-Length: -2",),
        (b"Content-Length: 2", b"Transfer-Encoding: chunked"),
        (b"Transfer-Encoding: gzip",),
        (b"Transfer-Encoding: identity",),
        (b"Transfer-Encoding: chunked, gzip",),
        (b"Expect: 100-continue", b"Content-Length: 2"),
        (b"Upgrade: h2c", b"Content-Length: 2"),
        (b"Connection: Upgrade", b"Content-Length: 2"),
        (b"TE: trailers", b"Content-Length: 2"),
        (b"Trailer: X", b"Content-Length: 2"),
        (b"Content-Length: %d" % (DEFAULT_BUDGETS.request_body_limit + 1),),
    ):
        assert refusal_triple(parse(request(post, *headers, body=b"{}"))) == (
            None,
            "framing",
            400,
        ), headers
    assert refusal_triple(
        parse(request(b"GET /user HTTP/1.1", b"Content-Length: 2", body=b"{}"))
    ) == (
        None,
        "body",
        400,
    )
    limit = DEFAULT_BUDGETS.request_body_limit
    at = parse(request(post, b"Content-Length: %d" % limit, body=b"x" * limit))
    assert isinstance(at, ParsedRequest) and len(at.body) == limit
    chunked_at = request(
        post,
        b"Transfer-Encoding: chunked",
        body=b"%x\r\n" % limit + b"x" * limit + b"\r\n0\r\n\r\n",
    )
    assert len(parse(chunked_at).body) == limit
    over = b"%x\r\n" % (limit + 1) + b"x" * (limit + 1) + b"\r\n0\r\n\r\n"
    chunked_over = parse(request(post, b"Transfer-Encoding: chunked", body=over))
    assert refusal_triple(chunked_over) == (None, "framing", 400)
    two_chunks_over = b"%x\r\n" % limit + b"x" * limit + b"\r\n1\r\nx\r\n0\r\n\r\n"
    assert refusal_triple(
        parse(request(post, b"Transfer-Encoding: chunked", body=two_chunks_over))
    ) == (None, "framing", 400)


# ------------------------------------------------- upstream reconstruction


STRIPPED_CLIENT_HEADERS = [
    b"Host: evil.example",
    b"Authorization: token stolen",
    b"Proxy-Authorization: x",
    b"Proxy-Connection: keep-alive",
    b"Connection: keep-alive",
    b"Keep-Alive: timeout=5",
    b"Forwarded: for=1.2.3.4",
    b"X-Forwarded-For: 1.2.3.4",
    b"Via: 1.1 proxy",
    b"Cookie: a=b",
    b"Accept-Encoding: gzip",
    b"User-Agent: curl/8",
    b"Origin: http://x",
    b"Referer: http://x",
    b"Range: bytes=0-1",
    b"Time-Zone: Etc/UTC",
    b"X-Gh-Cache-Ttl: 24h0m0s",
    b"X-Custom: anything",
]


def test_upstream_request_is_rebuilt_from_validated_parts_only():
    parsed = parse(
        request(
            b"POST /graphql HTTP/1.1",
            *STRIPPED_CLIENT_HEADERS,
            b"Accept: application/vnd.github+json",
            b"Content-Type: application/json; charset=utf-8",
            b"X-GitHub-Api-Version: 2022-11-28",
            b"GraphQL-Features: merge_queue",
            b'If-None-Match: "etag"',
            b"If-Modified-Since: Sat, 29 Oct 1994 19:43:31 GMT",
            b"Content-Length: 11",
            body=b'{"query":1}',
        )
    )
    assert isinstance(parsed, ParsedRequest)
    assert [name for name, _ in parsed.headers] == list(CLIENT_HEADER_ALLOWLIST)
    upstream = build_upstream_request(parsed, authorization="Bearer x", user_agent="relay/1")
    assert upstream.headers == (
        ("Host", "api.github.com"),
        ("User-Agent", "relay/1"),
        ("Accept-Encoding", "identity"),
        ("Authorization", "Bearer x"),
        ("Accept", "application/vnd.github+json"),
        ("Content-Type", "application/json; charset=utf-8"),
        ("X-GitHub-Api-Version", "2022-11-28"),
        ("GraphQL-Features", "merge_queue"),
        ("If-None-Match", '"etag"'),
        ("If-Modified-Since", "Sat, 29 Oct 1994 19:43:31 GMT"),
        ("Content-Length", "11"),
    )
    assert upstream.body == b'{"query":1}' and upstream.method == "POST"
    rebuilt = {"host", "authorization", "accept-encoding", "user-agent"}
    sent = {name.lower() for name, _ in upstream.headers}
    for stripped in STRIPPED_CLIENT_HEADERS:
        name, _, value = stripped.decode().partition(": ")
        assert (name, value) not in upstream.headers, stripped  # never the client's value
        assert name.lower() in rebuilt or name.lower() not in sent, stripped


def test_forwarded_headers_go_once_and_within_the_limit_and_content_type_is_post_only():
    parsed = parse(
        request(
            b"GET /user HTTP/1.1",
            b"Accept: first",
            b"Accept: second",
            b"Content-Type: application/json",
            b"If-None-Match: " + b"x" * 1025,
        )
    )
    upstream = build_upstream_request(parsed, authorization=None, user_agent="relay/1")
    assert upstream.headers == (
        ("Host", "api.github.com"),
        ("User-Agent", "relay/1"),
        ("Accept-Encoding", "identity"),
        ("Accept", "first"),
    )
    assert upstream.method == "GET" and upstream.body == b""
    at_limit = parse(request(b"GET /user HTTP/1.1", b"If-None-Match: " + b"x" * 1024))
    assert ("If-None-Match", "x" * 1024) in build_upstream_request(
        at_limit, authorization=None, user_agent="u"
    ).headers
    for content_type in (b"text/plain", b"application/json; boundary=x", b"application/jsonx"):
        wrong = parse(
            request(
                b"POST /graphql HTTP/1.1",
                b"Content-Type: " + content_type,
                b"Content-Length: 2",
                body=b"{}",
            )
        )
        names = [
            name
            for name, _ in build_upstream_request(wrong, authorization=None, user_agent="u").headers
        ]
        assert "Content-Type" not in names, content_type
    plain = parse(
        request(
            b"POST /graphql HTTP/1.1",
            b"content-type: APPLICATION/JSON",
            b"Content-Length: 2",
            body=b"{}",
        )
    )
    assert ("content-type", "APPLICATION/JSON") in build_upstream_request(
        plain, authorization=None, user_agent="u"
    ).headers


def test_content_length_is_recomputed_from_the_buffered_body():
    chunked = parse(
        request(
            b"POST /graphql HTTP/1.1",
            b"Transfer-Encoding: chunked",
            body=b'4\r\n{"a"\r\n1\r\n}\r\n0\r\n\r\n',
        )
    )
    upstream = build_upstream_request(chunked, authorization=None, user_agent="u")
    assert upstream.body == b'{"a"}' and ("Content-Length", "5") in upstream.headers
    assert all(name.lower() != "transfer-encoding" for name, _ in upstream.headers)


def test_upstream_request_target_reproduces_from_validated_parts():
    parsed = parse(request(b"GET /repos/o/r/issues?state=open&labels=a%2Cb&page=2 HTTP/1.1"))
    upstream = build_upstream_request(parsed, authorization=None, user_agent="u")
    assert upstream.request_target == parsed.target.path + "?" + canonical_query(
        parsed.target.query
    )
    assert upstream.request_target == "/repos/o/r/issues?state=open&labels=a%2Cb&page=2"
    bare = build_upstream_request(
        parse(request(b"GET /user HTTP/1.1")), authorization=None, user_agent="u"
    )
    assert bare.request_target == "/user" and "?" not in bare.request_target


# ------------------------------------------------------- response headers


def test_response_header_allowlist():
    kept = [(name, "v") for name in RESPONSE_HEADER_ALLOWLIST] + [
        ("X-RateLimit-Limit", "60"),
        ("x-ratelimit-remaining", "59"),
        ("X-RateLimit-Reset", "1"),
        ("etag", "w"),
    ]
    stripped = [
        ("Location", "https://objects.githubusercontent.com/x"),
        ("Set-Cookie", "a=b"),
        ("Content-Encoding", "gzip"),
        ("Transfer-Encoding", "chunked"),
        ("Content-Length", "5"),
        ("Connection", "close"),
        ("Keep-Alive", "timeout=5"),
        ("Server", "GitHub.com"),
        ("Alt-Svc", "h3"),
        ("Strict-Transport-Security", "max-age=1"),
        ("Content-Security-Policy", "default-src 'none'"),
        ("X-OAuth-Scopes", "repo"),
        ("X-Accepted-OAuth-Scopes", "repo"),
        ("X-Frame-Options", "deny"),
        ("Access-Control-Allow-Origin", "*"),
    ]
    assert filter_response_headers(stripped + kept) == tuple(kept)
    assert filter_response_headers([]) == ()


def test_reader_protocol_is_satisfied_by_a_buffered_reader_and_the_stub():
    data = request(b"GET /user HTTP/1.1", b"Accept: a")
    assert read_request(reader(data), DEFAULT_BUDGETS) == read_request(
        SlowReader(data), DEFAULT_BUDGETS
    )
    assert refusal_triple(canonicalize_target("", DEFAULT_BUDGETS)) == (
        "target-form",
        "target-form",
        400,
    )
    assert Stage.REQUEST_LINE.value == "request-line"
