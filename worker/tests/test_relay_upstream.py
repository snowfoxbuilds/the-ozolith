"""The upstream client (ADR-0057 items 5 and 6): the credential attached at
the wire only, the pre-delivery response gate into memory or an unlinked
spool, the aggregate reservation, the content-encoding and header rules,
and the per-hop redirect policy — every check against a local
``http.server`` reached through ``connection_factory``."""

from __future__ import annotations

import errno
import http.client
import os
import threading
import time
from pathlib import Path

import pytest
from relayrig import CREDENTIAL, FakeUpstream, Response, Seen
from theozolith_worker.relay.ingress import (
    ParsedRequest,
    UpstreamRequest,
    build_upstream_request,
    canonicalize_target,
)
from theozolith_worker.relay.reasons import (
    Budgets,
    HostStatus,
    MethodClass,
    Outcome,
    Reason,
    RedirectDecision,
    Scheme,
)
from theozolith_worker.relay.upstream import (
    HOST,
    ORIGIN,
    USER_AGENT,
    AggregateBudget,
    Live,
    NoUpstream,
    SpoolHandle,
    UpstreamClient,
    UpstreamResult,
    hop_request,
    request_size,
)

LIMIT = 4096
SMALL = Budgets(response_body_limit=LIMIT, upstream_timeout=5.0)
AUTHORIZATION = f"Bearer {CREDENTIAL}"


@pytest.fixture
def upstream():
    rig = FakeUpstream().start()
    yield rig
    rig.stop()


@pytest.fixture
def spool_dir(tmp_path: Path) -> Path:
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    return spool


def request_for(
    target: str,
    method: MethodClass = MethodClass.GET,
    headers: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
) -> tuple[UpstreamRequest, ParsedRequest]:
    canonical = canonicalize_target(target, SMALL)
    parsed = ParsedRequest(method, canonical, headers, body)
    return build_upstream_request(parsed, authorization=None, user_agent=USER_AGENT), parsed


def send(
    client: UpstreamClient,
    target: str,
    *,
    method: MethodClass = MethodClass.GET,
    headers: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
    budgets: Budgets = SMALL,
    aggregate: AggregateBudget | None = None,
    authorize_hop=None,
) -> UpstreamResult:
    request, parsed = request_for(target, method, headers, body)
    return client.send(
        request,
        method_class=method,
        canonical_target=parsed.target,
        authorize_hop=authorize_hop or (lambda hop, hop_target: None),
        aggregate=aggregate or AggregateBudget(budgets),
    )


def body_bytes(result: UpstreamResult) -> bytes:
    assert result.body is not None
    if isinstance(result.body, SpoolHandle):
        data = result.body.read()
        result.body.close()
        return data
    return result.body


def graphql_body() -> bytes:
    return b'{"query":"query { viewer { login } }"}'


def to(path: str) -> str:
    return ORIGIN + path


# -- surface -----------------------------------------------------------------


def test_live_takes_exactly_one_credential_source(tmp_path: Path):
    Live(credential="x")
    Live(credential_file=tmp_path / "token")
    with pytest.raises(ValueError):
        Live()
    with pytest.raises(ValueError):
        Live(credential="x", credential_file=tmp_path / "token")
    assert NoUpstream() == NoUpstream()


def test_request_size_is_the_reconstructed_request_without_authorization():
    request, _ = request_for(
        "/repos/o/r/issues?state=open", MethodClass.POST, (("Accept", "*/*"),), b"{}"
    )
    wire = f"POST {request.request_target} HTTP/1.1\r\n".encode()
    wire += "".join(f"{k}: {v}\r\n" for k, v in request.headers).encode() + b"\r\n" + b"{}"
    assert request_size(request) == len(wire)
    assert b"Authorization" not in wire


def test_send_without_a_credential_is_a_programming_error(spool_dir: Path, upstream):
    client = upstream.client(SMALL, spool_dir, credential=None)
    with pytest.raises(RuntimeError):
        send(client, "/zen")


def test_spool_handle_reads_whole_and_closes_idempotently(spool_dir: Path):
    fd = os.open(spool_dir / "s", os.O_CREAT | os.O_RDWR, 0o600)
    os.write(fd, b"abc" * 100)
    handle = SpoolHandle(fd, 300)
    assert handle.read() == b"abc" * 100
    assert handle.read() == b"abc" * 100
    handle.close()
    handle.close()
    with pytest.raises(ValueError):
        handle.read()


# -- the credential and the wire request ------------------------------------


def test_credential_rides_only_the_wire_authorization_header(spool_dir: Path, upstream):
    upstream.route("/zen", Response(200, [("Content-Type", "text/plain")], b"keep calm"))
    client = upstream.client(SMALL, spool_dir)
    result = send(
        client, "/zen", headers=(("Accept", "application/json"), ("Authorization", "Bearer bad"))
    )
    assert result.outcome is Outcome.DELIVERED
    assert body_bytes(result) == b"keep calm"
    seen = upstream.requests[0]
    assert seen.header("Authorization") == AUTHORIZATION
    assert seen.header("Host") == HOST
    assert seen.header("Accept-Encoding") == "identity"
    assert seen.header("User-Agent") == USER_AGENT
    assert seen.header("Accept") == "application/json"
    assert result.request_bytes == request_size(
        request_for("/zen", headers=(("Accept", "application/json"),))[0]
    )
    assert result.response_bytes == 9


# -- the response gate -------------------------------------------------------


def test_gate_delivers_exactly_at_the_limit_and_refuses_one_byte_over(spool_dir: Path, upstream):
    upstream.route("/at", Response(200, [], b"a" * LIMIT))
    upstream.route("/over", Response(200, [], b"b" * (LIMIT + 1)))
    client = upstream.client(SMALL, spool_dir, spool_threshold=64)
    at = send(client, "/at")
    assert at.outcome is Outcome.DELIVERED
    assert at.response_bytes == LIMIT
    assert body_bytes(at) == b"a" * LIMIT
    over = send(client, "/over")
    assert (over.outcome, over.reason) == (Outcome.REFUSED_GATE, Reason.GATE_RESPONSE_BYTES)
    assert over.body is None
    assert over.status == 200
    assert over.response_bytes == LIMIT + 1  # the one overflow byte was read, so it is counted
    assert list(spool_dir.iterdir()) == []


def test_gate_counts_cumulatively_across_a_redirect_chain(spool_dir: Path, upstream):
    half = LIMIT // 2 + 1
    upstream.route("/start", Response(302, [("Location", to("/end"))], b"x" * half))
    upstream.route("/end", Response(200, [], b"y" * half))
    client = upstream.client(SMALL, spool_dir)
    result = send(client, "/start")
    assert (result.outcome, result.reason) == (Outcome.REFUSED_GATE, Reason.GATE_RESPONSE_BYTES)
    assert len(result.redirects) == 1 and result.redirects[0].decision is RedirectDecision.FOLLOWED


def test_aggregate_reservation_is_exhausted_across_requests(spool_dir: Path, upstream):
    budgets = Budgets(response_body_limit=100, aggregate_response_bytes=250, upstream_timeout=5.0)
    upstream.route("/full", Response(200, [], b"z" * 100))
    client = upstream.client(budgets, spool_dir)
    aggregate = AggregateBudget(budgets)
    first = send(client, "/full", budgets=budgets, aggregate=aggregate)
    second = send(client, "/full", budgets=budgets, aggregate=aggregate)
    assert first.outcome is second.outcome is Outcome.DELIVERED
    assert aggregate.response_remaining == 50
    third = send(client, "/full", budgets=budgets, aggregate=aggregate)
    assert (third.outcome, third.reason) == (Outcome.REFUSED_GATE, Reason.GATE_AGGREGATE)
    assert third.body is None and third.status is None
    assert aggregate.response_remaining == 50
    assert len(upstream.requests) == 2


def test_unused_response_allowance_returns_to_the_aggregate(spool_dir: Path, upstream):
    budgets = Budgets(response_body_limit=100, aggregate_response_bytes=250, upstream_timeout=5.0)
    upstream.route("/small", Response(200, [], b"z" * 10))
    client = upstream.client(budgets, spool_dir)
    aggregate = AggregateBudget(budgets)
    for _ in range(5):
        assert (
            send(client, "/small", budgets=budgets, aggregate=aggregate).outcome
            is Outcome.DELIVERED
        )
    assert aggregate.response_remaining == 200


def test_concurrent_requests_near_the_aggregate_never_overshoot(spool_dir: Path, upstream):
    budgets = Budgets(response_body_limit=100, aggregate_response_bytes=250, upstream_timeout=5.0)
    upstream.route("/slow", Response(200, [], b"z" * 100, stall_after=50, stall_seconds=0.4))
    client = upstream.client(budgets, spool_dir)
    aggregate = AggregateBudget(budgets)
    barrier = threading.Barrier(5)
    results: list[UpstreamResult] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        result = send(client, "/slow", budgets=budgets, aggregate=aggregate)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    outcomes = sorted(result.outcome.value for result in results)
    assert outcomes == ["delivered", "delivered", "refused-gate", "refused-gate", "refused-gate"]
    assert {r.reason for r in results if r.outcome is Outcome.REFUSED_GATE} == {
        Reason.GATE_AGGREGATE
    }
    assert aggregate.response_remaining == 50
    assert len(upstream.requests) == 2


def test_upstream_timeout_mid_body(spool_dir: Path, upstream):
    budgets = Budgets(response_body_limit=LIMIT, upstream_timeout=0.4)
    upstream.route("/stall", Response(200, [], b"q" * 1000, stall_after=100, stall_seconds=1.5))
    client = upstream.client(budgets, spool_dir, spool_threshold=16)
    started = time.monotonic()
    result = send(client, "/stall", budgets=budgets)
    assert (result.outcome, result.reason) == (Outcome.TIMEOUT, Reason.UPSTREAM_TIMEOUT)
    assert result.body is None
    assert time.monotonic() - started < 1.4
    assert list(spool_dir.iterdir()) == []


def test_upstream_timeout_on_a_stalled_head(spool_dir: Path, upstream):
    budgets = Budgets(response_body_limit=LIMIT, upstream_timeout=0.3)

    def stall(seen: Seen) -> Response:
        time.sleep(1.0)
        return Response(200, [], b"late")

    upstream.route("/head-stall", stall)
    result = send(upstream.client(budgets, spool_dir), "/head-stall", budgets=budgets)
    assert (result.outcome, result.reason, result.status) == (
        Outcome.TIMEOUT,
        Reason.UPSTREAM_TIMEOUT,
        None,
    )


def test_a_truncated_body_is_an_upstream_error_with_nothing_delivered(spool_dir: Path, upstream):
    upstream.route("/cut", Response(200, [], b"q" * 1000, close_after=200))
    result = send(upstream.client(SMALL, spool_dir, spool_threshold=16), "/cut")
    assert (result.outcome, result.reason) == (Outcome.UPSTREAM_ERROR, Reason.UPSTREAM_ERROR)
    assert result.body is None
    assert len(upstream.requests) == 1
    assert list(spool_dir.iterdir()) == []


def test_a_malformed_status_line_is_an_upstream_error(spool_dir: Path, upstream):
    upstream.route("/garbage", Response(raw=b"NOT HTTP\r\n\r\n"))
    result = send(upstream.client(SMALL, spool_dir), "/garbage")
    assert (result.outcome, result.reason, result.status) == (
        Outcome.UPSTREAM_ERROR,
        Reason.UPSTREAM_ERROR,
        None,
    )


def test_nothing_is_returned_before_the_whole_body_was_read(spool_dir: Path, upstream):
    upstream.route("/paced", Response(200, [], b"p" * 300, stall_after=100, stall_seconds=0.4))
    client = upstream.client(SMALL, spool_dir, spool_threshold=16)
    started = time.monotonic()
    result = send(client, "/paced")
    elapsed = time.monotonic() - started
    assert elapsed >= 0.4
    assert result.outcome is Outcome.DELIVERED
    assert isinstance(result.body, SpoolHandle)
    assert result.body.size == 300
    assert body_bytes(result) == b"p" * 300
    assert list(spool_dir.iterdir()) == []


@pytest.mark.parametrize("failure", ["interrupted", "enospc"])
def test_a_failed_spool_write_is_an_upstream_error_and_leaves_no_spool(
    spool_dir, upstream, failure
):
    # 100 KiB crosses the client's 64 KiB read boundary: two spool writes.
    upstream.route("/big", Response(200, [], b"s" * 100_000))
    budgets = Budgets(response_body_limit=1024 * 1024, upstream_timeout=5.0)
    calls = {"n": 0}

    def spool_write(fd: int, data) -> int:
        calls["n"] += 1
        if failure == "enospc":
            raise OSError(errno.ENOSPC, "No space left on device")
        if calls["n"] == 2:
            raise OSError(errno.EIO, "Input/output error")
        return os.write(fd, data)

    client = upstream.client(budgets, spool_dir, spool_threshold=64, _spool_write=spool_write)
    result = send(client, "/big", budgets=budgets)
    assert calls["n"] == (1 if failure == "enospc" else 2)
    assert (result.outcome, result.reason) == (Outcome.UPSTREAM_ERROR, Reason.UPSTREAM_ERROR)
    assert result.body is None
    assert list(spool_dir.iterdir()) == []


def test_a_missing_spool_directory_is_an_upstream_error(tmp_path: Path, upstream):
    upstream.route("/big", Response(200, [], b"s" * 2000))
    client = upstream.client(SMALL, tmp_path / "absent", spool_threshold=64)
    result = send(client, "/big")
    assert (result.outcome, result.reason) == (Outcome.UPSTREAM_ERROR, Reason.UPSTREAM_ERROR)


def test_spools_are_unlinked_on_open_and_gone_after_close(spool_dir: Path, upstream):
    upstream.route("/big", Response(200, [], b"s" * 5000, chunked=True))
    client = upstream.client(Budgets(response_body_limit=8192), spool_dir, spool_threshold=64)
    result = send(client, "/big", budgets=Budgets(response_body_limit=8192))
    assert isinstance(result.body, SpoolHandle)
    assert list(spool_dir.iterdir()) == []
    assert body_bytes(result) == b"s" * 5000
    assert list(spool_dir.iterdir()) == []


# -- content encoding and headers -------------------------------------------


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "x-unknown", "identity, gzip"])
def test_non_identity_content_encoding_is_refused_undecoded(spool_dir: Path, upstream, encoding):
    upstream.route("/enc", Response(200, [("Content-Encoding", encoding)], b"\x1f\x8b\x08garbage"))
    result = send(upstream.client(SMALL, spool_dir), "/enc")
    assert (result.outcome, result.reason) == (Outcome.REFUSED_GATE, Reason.CONTENT_ENCODING)
    assert result.body is None
    assert result.headers == ()
    assert len(upstream.requests) == 1


def test_identity_content_encoding_is_delivered(spool_dir: Path, upstream):
    upstream.route("/id", Response(200, [("Content-Encoding", "identity")], b"plain"))
    result = send(upstream.client(SMALL, spool_dir), "/id")
    assert result.outcome is Outcome.DELIVERED
    assert body_bytes(result) == b"plain"
    assert all(name.lower() != "content-encoding" for name, _ in result.headers)


def test_response_headers_are_allowlisted_and_framing_is_stripped(spool_dir: Path, upstream):
    headers = [
        ("Content-Type", "application/json"),
        ("ETag", '"abc"'),
        ("Link", '<https://api.github.com/x?page=2>; rel="next"'),
        ("X-RateLimit-Remaining", "42"),
        ("X-GitHub-Request-Id", "AB:CD"),
        ("Set-Cookie", "logged_in=no"),
        ("Location", "https://api.github.com/elsewhere"),
        ("X-OAuth-Scopes", "repo"),
        ("Strict-Transport-Security", "max-age=1"),
    ]
    upstream.route("/hdr", Response(200, headers, b"{}", chunked=True))
    result = send(upstream.client(SMALL, spool_dir), "/hdr")
    assert result.outcome is Outcome.DELIVERED
    names = {name.lower() for name, _ in result.headers}
    assert {"content-type", "etag", "link", "x-ratelimit-remaining", "x-github-request-id"} <= names
    for stripped in ("set-cookie", "location", "x-oauth-scopes", "strict-transport-security"):
        assert stripped not in names
    for framing in ("transfer-encoding", "content-length", "connection", "content-encoding"):
        assert framing not in names
    assert body_bytes(result) == b"{}"


def test_head_delivers_no_body_and_counts_no_bytes(spool_dir: Path, upstream):
    upstream.route("/zen", Response(200, [("Content-Type", "text/plain")], b"twelve bytes"))
    result = send(upstream.client(SMALL, spool_dir), "/zen", method=MethodClass.HEAD)
    assert result.outcome is Outcome.DELIVERED
    assert result.body == b""
    assert result.response_bytes == 0
    assert upstream.requests[0].method == "HEAD"


def test_304_is_delivered_not_treated_as_a_redirect(spool_dir: Path, upstream):
    upstream.route("/etag", Response(304, [("ETag", '"abc"')]))
    result = send(upstream.client(SMALL, spool_dir), "/etag", headers=(("If-None-Match", '"abc"'),))
    assert (result.outcome, result.status) == (Outcome.DELIVERED, 304)
    assert result.redirects == ()


# -- redirects: GraphQL ------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_graphql_redirect_is_refused_outright(spool_dir: Path, upstream, status):
    upstream.route("/graphql", Response(status, [("Location", to("/graphql"))]))
    result = send(
        upstream.client(SMALL, spool_dir),
        "/graphql",
        method=MethodClass.POST,
        headers=(("Content-Type", "application/json"),),
        body=graphql_body(),
    )
    assert (result.outcome, result.reason, result.status) == (
        Outcome.REFUSED_REDIRECT,
        Reason.REDIRECT_GRAPHQL,
        status,
    )
    assert result.body is None
    assert len(result.redirects) == 1
    entry = result.redirects[0]
    assert (entry.hop, entry.status, entry.decision, entry.reason) == (
        1,
        status,
        RedirectDecision.REFUSED,
        Reason.REDIRECT_GRAPHQL,
    )
    assert len(upstream.requests) == 1
    assert upstream.requests[0].method == "POST"
    assert upstream.requests[0].body == graphql_body()


# -- redirects: REST ---------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("method", [MethodClass.GET, MethodClass.HEAD])
def test_rest_redirects_are_followed_method_preserving_without_a_body(
    spool_dir: Path, upstream, status, method
):
    upstream.route("/start", Response(status, [("Location", to("/zen"))], b"moved"))
    upstream.route("/zen", Response(200, [("Content-Type", "text/plain")], b"arrived"))
    client = upstream.client(SMALL, spool_dir)
    result = send(
        client,
        "/start",
        method=method,
        headers=(("Accept", "application/json"), ("X-GitHub-Api-Version", "2022-11-28")),
    )
    assert (result.outcome, result.status) == (Outcome.DELIVERED, 200)
    assert body_bytes(result) == (b"" if method is MethodClass.HEAD else b"arrived")
    assert [seen.method for seen in upstream.requests] == [method.value, method.value]
    assert upstream.targets() == ["/start", "/zen"]
    hop = upstream.requests[1]
    assert hop.header("Authorization") == AUTHORIZATION
    assert hop.header("Accept") is None
    assert hop.header("X-GitHub-Api-Version") is None
    assert hop.header("Content-Length") in (None, "0")
    assert hop.body == b""
    assert hop.header("Accept-Encoding") == "identity"
    assert len(result.redirects) == 1
    entry = result.redirects[0]
    assert (entry.hop, entry.status, entry.decision, entry.reason) == (
        1,
        status,
        RedirectDecision.FOLLOWED,
        None,
    )
    assert (entry.scheme, entry.host.status, entry.host.value) == (
        Scheme.HTTPS,
        HostStatus.VALID,
        "api.github.com",
    )


@pytest.mark.parametrize("status", [303, 300, 305, 306])
def test_non_method_preserving_statuses_are_refused(spool_dir: Path, upstream, status):
    upstream.route("/start", Response(status, [("Location", to("/zen"))]))
    upstream.route("/zen", Response(200, [], b"never"))
    result = send(upstream.client(SMALL, spool_dir), "/start")
    assert (result.outcome, result.reason, result.status) == (
        Outcome.REFUSED_REDIRECT,
        Reason.REDIRECT_METHOD,
        status,
    )
    assert result.redirects[0].reason is Reason.REDIRECT_METHOD
    assert upstream.targets() == ["/start"]


@pytest.mark.parametrize(
    "location, reason",
    [
        ("https://evil.example/zen", Reason.REDIRECT_ORIGIN),
        ("https://api.github.com.evil.example/zen", Reason.REDIRECT_ORIGIN),
        ("https://api.github.com./zen", Reason.REDIRECT_ORIGIN),
        ("http://api.github.com/zen", Reason.REDIRECT_ORIGIN),
        ("https://api.github.com:8443/zen", Reason.REDIRECT_ORIGIN),
        ("https://api.github.com:/zen", Reason.REDIRECT_ORIGIN),
        ("https://user@api.github.com/zen", Reason.REDIRECT_ORIGIN),
        ("https://api%2Egithub.com/zen", Reason.REDIRECT_ORIGIN),
        ("ftp://api.github.com/zen", Reason.REDIRECT_ORIGIN),
        ("/zen", Reason.REDIRECT_LOCATION),
        ("//api.github.com/zen", Reason.REDIRECT_LOCATION),
        ("zen", Reason.REDIRECT_LOCATION),
        ("https://api.github.com/zen two", Reason.REDIRECT_LOCATION),
        ("https://api.github.com/zen#frag", Reason.REDIRECT_LOCATION),
        ("https://api.github.com", Reason.REDIRECT_LOCATION),
        ("https://api.github.com/repos/o/r/..%2Fx", Reason.REDIRECT_LOCATION),
        ("https://api.github.com//zen", Reason.REDIRECT_LOCATION),
        ("https://api.github.com/zen/", Reason.REDIRECT_LOCATION),
        ("https://api.github.com/zen/../orgs/x", Reason.REDIRECT_LOCATION),
        ("https://api.github.com/zen?%G1", Reason.REDIRECT_LOCATION),
        ("https://api.github.com/orgs/x", Reason.REDIRECT_DENYLIST),
        ("https://api.github.com/repos/o/r/hooks", Reason.REDIRECT_DENYLIST),
        ("https://api.github.com/start", Reason.REDIRECT_LOOP),
    ],
)
def test_every_hop_is_revalidated_and_the_refused_target_never_contacted(
    spool_dir: Path, upstream, location, reason
):
    upstream.route("/start", Response(302, [("Location", location)]))
    upstream.route("/zen", Response(200, [], b"never"))
    upstream.route("/orgs/x", Response(200, [], b"never"))
    upstream.route("/repos/o/r/hooks", Response(200, [], b"never"))
    result = send(upstream.client(SMALL, spool_dir), "/start")
    assert (result.outcome, result.reason, result.status) == (Outcome.REFUSED_REDIRECT, reason, 302)
    assert result.body is None
    assert len(result.redirects) == 1
    entry = result.redirects[0]
    assert (entry.hop, entry.decision, entry.reason) == (1, RedirectDecision.REFUSED, reason)
    assert upstream.targets() == ["/start"]
    assert all(seen.header("Authorization") == AUTHORIZATION for seen in upstream.requests)


@pytest.mark.parametrize(
    "locations", [[], ["https://api.github.com/a", "https://api.github.com/b"]]
)
def test_a_missing_or_duplicated_location_refuses(spool_dir: Path, upstream, locations):
    upstream.route("/start", Response(302, [("Location", value) for value in locations]))
    result = send(upstream.client(SMALL, spool_dir), "/start")
    assert (result.outcome, result.reason) == (Outcome.REFUSED_REDIRECT, Reason.REDIRECT_LOCATION)
    entry = result.redirects[0]
    assert entry.scheme is (Scheme.ABSENT if not locations else Scheme.INVALID)
    assert upstream.targets() == ["/start"]


def test_a_loop_across_two_hops_is_refused_at_the_repeat(spool_dir: Path, upstream):
    upstream.route("/a", Response(302, [("Location", to("/b"))]))
    upstream.route("/b", Response(302, [("Location", to("/a"))]))
    result = send(upstream.client(SMALL, spool_dir), "/a")
    assert (result.outcome, result.reason) == (Outcome.REFUSED_REDIRECT, Reason.REDIRECT_LOOP)
    assert [(e.hop, e.decision) for e in result.redirects] == [
        (1, RedirectDecision.FOLLOWED),
        (2, RedirectDecision.REFUSED),
    ]
    assert upstream.targets() == ["/a", "/b"]


def chain(upstream, length: int, final: Response) -> None:
    for index in range(length):
        upstream.route(f"/c{index}", Response(301, [("Location", to(f"/c{index + 1}"))]))
    upstream.route(f"/c{length}", final)


def test_three_hops_follow_and_the_fourth_redirect_is_refused_at_the_limit(spool_dir, upstream):
    chain(upstream, 4, Response(200, [], b"unreached"))
    result = send(upstream.client(SMALL, spool_dir), "/c0")
    assert (result.outcome, result.reason, result.status) == (
        Outcome.REFUSED_REDIRECT,
        Reason.REDIRECT_HOPS,
        301,
    )
    assert [(e.hop, e.decision, e.reason) for e in result.redirects] == [
        (1, RedirectDecision.FOLLOWED, None),
        (2, RedirectDecision.FOLLOWED, None),
        (3, RedirectDecision.FOLLOWED, None),
        (4, RedirectDecision.REFUSED, Reason.REDIRECT_HOPS),
    ]
    assert upstream.targets() == ["/c0", "/c1", "/c2", "/c3"]


@pytest.mark.parametrize("hops", [0, 1, 3])
def test_completion_entry_counts_for_followed_chains(spool_dir: Path, upstream, hops):
    chain(upstream, hops, Response(200, [("Content-Type", "text/plain")], b"done"))
    result = send(upstream.client(SMALL, spool_dir), "/c0")
    assert (result.outcome, result.status) == (Outcome.DELIVERED, 200)
    assert body_bytes(result) == b"done"
    assert len(result.redirects) == hops
    assert all(e.decision is RedirectDecision.FOLLOWED for e in result.redirects)
    assert [e.hop for e in result.redirects] == list(range(1, hops + 1))
    assert upstream.targets() == [f"/c{i}" for i in range(hops + 1)]


def test_a_hop_the_server_does_not_authorize_is_never_sent(spool_dir: Path, upstream):
    chain(upstream, 3, Response(200, [], b"unreached"))
    asked: list[tuple[int, str]] = []

    def authorize(hop: int, target) -> Reason | None:
        asked.append((hop, target.path))
        return Reason.AUDIT_UNREPRESENTABLE if hop == 2 else None

    result = send(upstream.client(SMALL, spool_dir), "/c0", authorize_hop=authorize)
    assert (result.outcome, result.reason) == (
        Outcome.REFUSED_REDIRECT,
        Reason.AUDIT_UNREPRESENTABLE,
    )
    assert asked == [(1, "/c1"), (2, "/c2")]
    assert [(e.hop, e.decision, e.reason) for e in result.redirects] == [
        (1, RedirectDecision.FOLLOWED, None),
        (2, RedirectDecision.REFUSED, Reason.AUDIT_UNREPRESENTABLE),
    ]
    assert upstream.targets() == ["/c0", "/c1"]


def test_a_followed_hop_keeps_its_canonical_query(spool_dir: Path, upstream):
    upstream.route("/start", Response(302, [("Location", to("/search/issues?q=a+b&page=2"))]))
    upstream.route("/search/issues", Response(200, [], b"[]"))
    result = send(upstream.client(SMALL, spool_dir), "/start")
    assert result.outcome is Outcome.DELIVERED
    assert upstream.targets() == ["/start", "/search/issues?q=a%20b&page=2"]


# -- abort -------------------------------------------------------------------


def test_abort_mid_read_yields_aborted_and_later_sends_never_connect(spool_dir, upstream):
    upstream.route("/slow", Response(200, [], b"s" * 500, stall_after=100, stall_seconds=2.0))
    client = upstream.client(SMALL, spool_dir)
    results: list[UpstreamResult] = []
    thread = threading.Thread(target=lambda: results.append(send(client, "/slow")))
    thread.start()
    deadline = time.monotonic() + 3
    while not upstream.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    client.abort()
    thread.join(5)
    assert not thread.is_alive()
    assert (results[0].outcome, results[0].reason) == (Outcome.ABORTED, Reason.ABORTED)
    assert results[0].body is None
    later = send(client, "/slow")
    assert (later.outcome, later.reason) == (Outcome.ABORTED, Reason.ABORTED)
    assert len(upstream.requests) == 1
    assert client.aborted


# -- aggregate response accounting on every path -----------------------------


def _consume(result: UpstreamResult) -> None:
    if isinstance(result.body, SpoolHandle):
        result.body.close()


def test_the_aggregate_decreases_by_actual_response_bytes_on_every_path(spool_dir, upstream):
    """Every send settles the aggregate to the bytes the gate actually read,
    never the reservation and never zero — delivered, exactly at the cap,
    one over (the overflow byte counted too), truncated, timed out, and
    spool-write-failed. Repeated over-limit responses each charge only their
    own bytes."""
    budgets = Budgets(
        response_body_limit=LIMIT, aggregate_response_bytes=10 * LIMIT, upstream_timeout=0.5
    )
    upstream.route("/ok", Response(200, [], b"a" * 10))
    upstream.route("/cap", Response(200, [], b"b" * LIMIT))
    upstream.route("/over", Response(200, [], b"c" * (LIMIT + 1)))
    upstream.route("/cut", Response(200, [], b"d" * 1000, close_after=200))
    upstream.route("/stall", Response(200, [], b"e" * 1000, stall_after=100, stall_seconds=1.5))

    def spool_write(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    aggregate = AggregateBudget(budgets)
    expected = {
        "/ok": (Outcome.DELIVERED, 10),
        "/cap": (Outcome.DELIVERED, LIMIT),
        "/over": (Outcome.REFUSED_GATE, LIMIT + 1),
        "/over ": (Outcome.REFUSED_GATE, LIMIT + 1),  # repeated over-limit
        "/cut": (Outcome.UPSTREAM_ERROR, 200),
        "/stall": (Outcome.TIMEOUT, 100),
    }
    spent = 0
    start = aggregate.response_remaining
    for target, (outcome, received) in expected.items():
        client = upstream.client(budgets, spool_dir, spool_threshold=16, _spool_write=os.write)
        result = send(client, target.strip(), budgets=budgets, aggregate=aggregate)
        assert result.outcome is outcome, target
        assert result.response_bytes == received, target
        spent += received
        assert aggregate.response_remaining == start - spent, target
        assert list(spool_dir.iterdir()) == [], target
        _consume(result)

    # A spool-write failure still charges the bytes that reached the gate.
    upstream.route("/spool", Response(200, [], b"f" * 100_000))
    big = Budgets(
        response_body_limit=1024 * 1024,
        aggregate_response_bytes=8 * 1024 * 1024,
        upstream_timeout=5.0,
    )
    ag2 = AggregateBudget(big)
    client = upstream.client(big, spool_dir, spool_threshold=64, _spool_write=spool_write)
    result = send(client, "/spool", budgets=big, aggregate=ag2)
    assert result.outcome is Outcome.UPSTREAM_ERROR
    assert result.response_bytes > 0
    assert ag2.response_remaining == big.aggregate_response_bytes - result.response_bytes
    assert list(spool_dir.iterdir()) == []


def test_the_aggregate_decreases_by_actual_bytes_when_a_read_is_aborted(spool_dir, upstream):
    budgets = Budgets(
        response_body_limit=LIMIT, aggregate_response_bytes=10 * LIMIT, upstream_timeout=30.0
    )
    upstream.route("/slow", Response(200, [], b"g" * 800, stall_after=200, stall_seconds=3.0))
    aggregate = AggregateBudget(budgets)
    client = upstream.client(budgets, spool_dir)
    results: list[UpstreamResult] = []
    thread = threading.Thread(
        target=lambda: results.append(send(client, "/slow", budgets=budgets, aggregate=aggregate))
    )
    thread.start()
    deadline = time.monotonic() + 3
    while not upstream.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    client.abort()
    thread.join(5)
    assert not thread.is_alive()
    result = results[0]
    assert (result.outcome, result.reason) == (Outcome.ABORTED, Reason.ABORTED)
    assert result.response_bytes >= 200
    assert aggregate.response_remaining == budgets.aggregate_response_bytes - result.response_bytes
    assert list(spool_dir.iterdir()) == []


# -- the single absolute deadline --------------------------------------------


def test_a_head_then_body_delay_each_under_the_timeout_but_over_the_total(spool_dir, upstream):
    """One absolute deadline spans the whole exchange: a head delay and a
    body stall each shorter than the timeout, but longer than it together,
    time the send out — a per-phase timeout would let both through."""
    budgets = Budgets(response_body_limit=LIMIT, upstream_timeout=0.6)
    upstream.route(
        "/split",
        Response(200, [], b"h" * 400, head_delay=0.4, stall_after=100, stall_seconds=0.5),
    )
    client = upstream.client(budgets, spool_dir, spool_threshold=16)
    started = time.monotonic()
    result = send(client, "/split", budgets=budgets)
    elapsed = time.monotonic() - started
    assert (result.outcome, result.reason) == (Outcome.TIMEOUT, Reason.UPSTREAM_TIMEOUT)
    assert result.body is None
    assert 0.6 <= elapsed < 1.4
    assert list(spool_dir.iterdir()) == []


# -- abort during connect ----------------------------------------------------


def test_abort_during_connect_yields_aborted_without_waiting_for_the_timeout(spool_dir):
    """A connect to an unreachable origin is interrupted by abort at once,
    not left to the upstream timeout — the wake reaches the pending
    connect, not only an established read."""

    def blackhole(host, port, timeout):
        # 192.0.2.0/24 is TEST-NET-1 (RFC 5737): routable nowhere, so the
        # non-blocking connect stays in progress until it is woken.
        return http.client.HTTPConnection("192.0.2.1", 80, timeout=timeout)

    budgets = Budgets(response_body_limit=LIMIT, upstream_timeout=30.0)
    client = UpstreamClient(CREDENTIAL, budgets, spool_dir, connection_factory=blackhole)
    results: list[UpstreamResult] = []
    thread = threading.Thread(target=lambda: results.append(send(client, "/zen", budgets=budgets)))
    started = time.monotonic()
    thread.start()
    time.sleep(0.3)
    client.abort()
    thread.join(5)
    assert not thread.is_alive()
    elapsed = time.monotonic() - started
    assert (results[0].outcome, results[0].reason) == (Outcome.ABORTED, Reason.ABORTED)
    assert elapsed < 5
    assert send(client, "/zen", budgets=budgets).outcome is Outcome.ABORTED
    client.close()


def test_abort_during_a_detached_close_delimited_body_read(spool_dir, upstream):
    """A ``Connection: close`` response hands http.client's socket to the
    response object, clearing ``HTTPConnection.sock``; abort still reaches
    the read because the client kept the socket itself."""
    # The fake always sends ``Connection: close``, so http.client detaches
    # the socket into the response object and clears ``HTTPConnection.sock``;
    # the body then stalls, and abort must still reach the detached read.
    upstream.route(
        "/detached",
        Response(200, [], b"z" * 800, stall_after=200, stall_seconds=3.0),
    )
    budgets = Budgets(response_body_limit=1024 * 1024, upstream_timeout=30.0)
    client = upstream.client(budgets, spool_dir)
    results: list[UpstreamResult] = []
    thread = threading.Thread(
        target=lambda: results.append(send(client, "/detached", budgets=budgets))
    )
    thread.start()
    deadline = time.monotonic() + 3
    while not upstream.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    client.abort()
    thread.join(5)
    assert not thread.is_alive()
    assert results[0].outcome is Outcome.ABORTED


# -- per-hop request-byte budgets --------------------------------------------


def _hop_size(target: str) -> int:
    return request_size(hop_request("GET", target))


def test_a_redirect_with_no_request_byte_budget_is_refused_before_the_hop(spool_dir, upstream):
    upstream.route("/start", Response(302, [("Location", to("/zen"))]))
    upstream.route("/zen", Response(200, [], b"unreached"))
    budgets = Budgets(response_body_limit=LIMIT, aggregate_request_bytes=0, upstream_timeout=5.0)
    aggregate = AggregateBudget(budgets)
    result = send(
        upstream.client(budgets, spool_dir), "/start", budgets=budgets, aggregate=aggregate
    )
    assert (result.outcome, result.reason, result.status) == (
        Outcome.REFUSED_REDIRECT,
        Reason.REDIRECT_BUDGET,
        302,
    )
    assert result.body is None
    entry = result.redirects[0]
    assert (entry.hop, entry.decision, entry.reason) == (
        1,
        RedirectDecision.REFUSED,
        Reason.REDIRECT_BUDGET,
    )
    assert upstream.targets() == ["/start"]
    assert aggregate.request_remaining == 0


def test_a_redirect_with_exactly_the_hop_budget_is_followed(spool_dir, upstream):
    upstream.route("/start", Response(302, [("Location", to("/zen"))]))
    upstream.route("/zen", Response(200, [], b"ok"))
    hop = _hop_size("/zen")
    budgets = Budgets(response_body_limit=LIMIT, aggregate_request_bytes=hop, upstream_timeout=5.0)
    aggregate = AggregateBudget(budgets)
    result = send(
        upstream.client(budgets, spool_dir), "/start", budgets=budgets, aggregate=aggregate
    )
    assert result.outcome is Outcome.DELIVERED
    assert aggregate.request_remaining == 0
    assert len(result.redirects) == 1 and result.redirects[0].decision is RedirectDecision.FOLLOWED
    _consume(result)


def test_every_followed_hop_is_charged_exactly_once(spool_dir, upstream):
    chain(upstream, 3, Response(200, [], b"done"))
    hops = _hop_size("/c1") + _hop_size("/c2") + _hop_size("/c3")
    budgets = Budgets(response_body_limit=LIMIT, aggregate_request_bytes=hops, upstream_timeout=5.0)
    aggregate = AggregateBudget(budgets)
    result = send(upstream.client(budgets, spool_dir), "/c0", budgets=budgets, aggregate=aggregate)
    assert result.outcome is Outcome.DELIVERED
    assert len(result.redirects) == 3
    assert aggregate.request_remaining == 0
    _consume(result)


def test_a_hop_refused_by_the_audit_step_refunds_its_request_charge(spool_dir, upstream):
    chain(upstream, 3, Response(200, [], b"unreached"))

    def authorize(hop, target):
        return Reason.AUDIT_UNREPRESENTABLE if hop == 2 else None

    budgets = Budgets(
        response_body_limit=LIMIT,
        aggregate_request_bytes=_hop_size("/c1") + _hop_size("/c2") + _hop_size("/c3"),
        upstream_timeout=5.0,
    )
    aggregate = AggregateBudget(budgets)
    result = send(
        upstream.client(budgets, spool_dir),
        "/c0",
        budgets=budgets,
        aggregate=aggregate,
        authorize_hop=authorize,
    )
    assert (result.outcome, result.reason) == (
        Outcome.REFUSED_REDIRECT,
        Reason.AUDIT_UNREPRESENTABLE,
    )
    # hop 1 charged and followed; hop 2 charged then refunded when refused.
    assert aggregate.request_remaining == budgets.aggregate_request_bytes - _hop_size("/c1")


def test_concurrent_redirect_chains_charge_their_hops_atomically(spool_dir, upstream):
    """Conservation under contention: whatever the interleaving, the bytes
    charged (one per followed hop) plus what remains equal the budget, so no
    hop is charged twice and none is lost."""
    chain(upstream, 3, Response(200, [], b"x" * 10, stall_after=5, stall_seconds=0.3))
    per_hop = _hop_size("/c1")
    assert per_hop == _hop_size("/c2") == _hop_size("/c3")
    budget = 7 * per_hop  # four 3-hop chains want twelve; only seven fit
    budgets = Budgets(
        response_body_limit=LIMIT, aggregate_request_bytes=budget, upstream_timeout=5.0
    )
    aggregate = AggregateBudget(budgets)
    client = upstream.client(budgets, spool_dir)
    results: list[UpstreamResult] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        result = send(client, "/c0", budgets=budgets, aggregate=aggregate)
        with lock:
            results.append(result)
        _consume(result)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    followed = sum(
        1
        for result in results
        for entry in result.redirects
        if entry.decision is RedirectDecision.FOLLOWED
    )
    assert aggregate.request_remaining >= 0
    assert followed * per_hop + aggregate.request_remaining == budget
    assert aggregate.request_remaining < per_hop
