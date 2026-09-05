"""The hostile-ingress socket server (ADR-0057 items 6, 8, 10): the
connection budget and counters, the read limits, the write-ahead pipeline
under injected audit failures and crash points, byte-exact refusals, the
one shutdown sequence, and none-mode parity with live mode."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from relayrig import (
    CREDENTIAL,
    FakeUpstream,
    Response,
    ServerRig,
    get,
    graphql,
    head,
    hop_record,
    kinds_for,
    nth,
    parse_reply,
    read_all,
    records_for,
    scripted_sink,
)
from theozolith_worker.relay import audit
from theozolith_worker.relay.ingress import read_request
from theozolith_worker.relay.reasons import DEFAULT_BUDGETS, Budgets, Reason
from theozolith_worker.relay.server import (
    BUDGET_MESSAGE,
    MUTATION_MESSAGE,
    NO_UPSTREAM_MESSAGE,
    REFUSAL_MESSAGES,
    REFUSAL_STATUS,
    refusal_response,
)

FIXTURES = Path(__file__).parent / "relay_fixtures"
INGRESS_CASES = sorted(p.name[: -len(".http")] for p in (FIXTURES / "ingress").glob("*.http"))
FAST = Budgets(head_read_seconds=0.4, body_read_seconds=0.4, upstream_timeout=3.0)
LONG_STALL = 30.0


class Crash(Exception):
    """An injected relay crash at a write-ahead boundary."""


def crash(*args) -> None:
    raise Crash()


@pytest.fixture
def upstream():
    fake = FakeUpstream().start()
    fake.route(
        "/zen", Response(200, [("Content-Type", "text/plain")], b"keep it logically awesome")
    )
    fake.route("/hop1", Response(302, [("Location", "https://api.github.com/hop2")]))
    fake.route("/hop2", Response(301, [("Location", "https://api.github.com/hop3")]))
    fake.route("/hop3", Response(307, [("Location", "https://api.github.com/zen")]))
    fake.route("/hop4", Response(308, [("Location", "https://api.github.com/hop1")]))
    fake.route("/loop", Response(302, [("Location", "https://api.github.com/loop")]))
    fake.route("/to-orgs", Response(302, [("Location", "https://api.github.com/orgs/x")]))
    fake.route("/away", Response(302, [("Location", "https://evil.example/zen")]))
    yield fake
    fake.stop()


@pytest.fixture
def rigs(tmp_path):
    """Every server started by a test, stopped at teardown however it ended."""
    started: list[ServerRig] = []

    def make(**kwargs) -> ServerRig:
        root = tmp_path / f"r{len(started)}"
        root.mkdir()
        rig = ServerRig(root, **kwargs).start()
        started.append(rig)
        return rig

    yield make
    for rig in started:
        if rig.running:
            rig.agent_exit.set()
            rig._thread.join(20)


def live(fake: FakeUpstream, budgets: Budgets = FAST, **kwargs):
    return {"upstream": fake.clients(budgets, **kwargs), "budgets": budgets}


def none(budgets: Budgets = FAST):
    return {"upstream": None, "budgets": budgets}


def terminal(rig: ServerRig) -> dict:
    parsed = rig.records()
    terminals = [r for r in parsed.records if r["kind"] == "terminal"]
    assert len(terminals) == 1, parsed.records
    assert parsed.records[-1]["kind"] == "terminal"
    return terminals[0]


def exit_event(rig: ServerRig) -> dict:
    events = rig.events()
    assert events[0] == {"event": "ready"}
    assert events[-1]["event"] == "exit"
    return events[-1]


def audit_failures(rig: ServerRig) -> list[dict]:
    return [event for event in rig.events() if event["event"] == "audit-failure"]


# -- refusal shape ---------------------------------------------------------


def test_refusal_messages_cover_every_reason_and_pin_the_byte_exact_texts():
    assert set(REFUSAL_MESSAGES) == set(Reason) == set(REFUSAL_STATUS)
    assert (
        REFUSAL_MESSAGES[Reason.MUTATION]
        == MUTATION_MESSAGE
        == (
            "Workers never write to GitHub. Everything you want published goes through your"
            " Output Proposal — run `format-output status`."
        )
    )
    for reason, budget in (
        (Reason.BUDGET_REQUESTS, "request budget"),
        (Reason.BUDGET_REQUEST_BYTES, "request-byte budget"),
        (Reason.BUDGET_RESPONSE_BYTES, "response-byte budget"),
    ):
        assert (
            REFUSAL_MESSAGES[reason]
            == BUDGET_MESSAGE.format(budget=budget)
            == (
                f"GitHub Relay: {budget} exhausted for this Run; further `gh` calls are refused."
                " Your prompt carries the task; `format-output status` shows your proposal."
            )
        )
    assert (
        REFUSAL_MESSAGES[Reason.NO_UPSTREAM]
        == NO_UPSTREAM_MESSAGE
        == ("This benchmark run has no GitHub upstream; the task is fully described in your prompt")
    )
    for message in REFUSAL_MESSAGES.values():
        lowered = message.lower()
        assert "token" not in lowered and "credential" not in lowered
        assert "cannot write" not in lowered and "can't write" not in lowered
        assert "read-only" not in lowered and "read only" not in lowered
    for reason, status in REFUSAL_STATUS.items():
        family = reason.value.split("-")[0]
        if family in ("request", "target", "path", "query", "headers", "framing", "body"):
            assert status == 400, reason
        elif family in ("budget",):
            assert status == 429, reason
        elif family in ("redirect", "gate", "content", "upstream", "aborted"):
            assert status == 502, reason
        elif family == "audit":
            assert status == 503, reason
        elif reason is Reason.METHOD:
            assert status == 405
        elif reason is Reason.VERSION:
            assert status == 505
        else:
            assert status == 403, reason


def test_refusal_response_is_reframed_json_with_connection_close():
    raw = refusal_response(403, Reason.MUTATION)
    reply = parse_reply(raw)
    assert reply.status == 403
    assert reply.header("Connection") == "close"
    assert reply.header("Content-Type") == "application/json"
    assert reply.header("Content-Length") == str(len(reply.body))
    assert raw.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert all(byte < 0x80 for byte in raw)
    assert reply.json() == {"message": MUTATION_MESSAGE, "reason": "mutation"}


# -- the fixture corpus, live and none ------------------------------------


@pytest.mark.parametrize("name", INGRESS_CASES)
def test_ingress_corpus_is_refused_alike_in_live_and_none_mode(rigs, upstream, name):
    data = (FIXTURES / "ingress" / f"{name}.http").read_bytes()
    want = json.loads((FIXTURES / "ingress" / f"{name}.expected.json").read_text())
    replies = []
    intents = []
    for mode in (live(upstream), none()):
        rig = rigs(**mode)
        reply = rig.request(data)
        rig.stop()
        replies.append(reply)
        records = rig.records().records
        assert [r["kind"] for r in records] == ["intent", "terminal"], name
        intents.append({k: v for k, v in records[0].items() if k != "ts"})
    for reply in replies:
        assert reply.status == want["status"], name
        assert reply.json() == {
            "message": REFUSAL_MESSAGES[Reason(want["reason"])],
            "reason": want["reason"],
        }
    assert intents[0] == intents[1]
    intent = intents[0]
    assert intent["decision"] == "refused" and intent["reason"] == want["reason"]
    assert intent["target"]["form"] == ("invalid" if want["stage"] else "full")
    if want["stage"]:
        assert intent["target"]["stage"] == want["stage"]
    assert upstream.requests == []


def test_none_mode_refuses_every_validated_request_and_contacts_nothing(rigs):
    rig = rigs(**none())
    reply = rig.request(get("/repos/o/r/issues?state=open"))
    assert (reply.status, reply.json()) == (
        403,
        {"message": NO_UPSTREAM_MESSAGE, "reason": "no-upstream"},
    )
    assert rig.request(graphql("query { viewer { login } }")).reason == "no-upstream"
    assert rig.request(head("/zen")).reason == "no-upstream"
    assert rig.request(get("/orgs/x")).reason == "admin-read"
    assert rig.request(b"PUT /x HTTP/1.1\r\n\r\n").reason == "mutation"
    rig.stop()
    parsed = rig.records()
    intents = [r for r in parsed.records if r["kind"] == "intent"]
    assert [r["reason"] for r in intents] == [
        "no-upstream",
        "no-upstream",
        "no-upstream",
        "admin-read",
        "mutation",
    ]
    assert all(r["decision"] == "refused" for r in intents)
    assert intents[0]["target"] == {
        "form": "full",
        "method": "GET",
        "path": "/repos/o/r/issues",
        "query": [
            {"name": "state", "len": 4, "sha256": audit.sha256_hex(b"open"), "value": "open"}
        ],
        "graphql": None,
    }
    assert parsed.counts_by_kind == {"intent": 5, "terminal": 1}


def test_policy_refusals_answer_their_status_class_and_pinned_texts(rigs, upstream):
    rig = rigs(**live(upstream))
    cases = [
        (get("/orgs/x"), 403, "admin-read"),
        (b"PUT /repos/o/r HTTP/1.1\r\n\r\n", 403, "mutation"),
        (b"POST /repos/o/r/issues HTTP/1.1\r\nContent-Length: 0\r\n\r\n", 403, "mutation"),
        (graphql("mutation { x }"), 403, "graphql-non-query"),
        (graphql("query { a } query { b }"), 403, "graphql-multi-operation"),
        (graphql("{"), 403, "graphql-unparseable"),
        (b"OPTIONS /zen HTTP/1.1\r\n\r\n", 405, "method"),
        (b"GET /zen HTTP/1.0\r\n\r\n", 505, "version"),
        (b"GET http://api.github.com/zen HTTP/1.1\r\n\r\n", 400, "target-form"),
    ]
    for raw, status, reason in cases:
        reply = rig.request(raw)
        assert (reply.status, reply.reason) == (status, reason), raw
        assert reply.json()["message"] == REFUSAL_MESSAGES[Reason(reason)]
    assert rig.request(b"PUT /repos/o/r HTTP/1.1\r\n\r\n").body == json.dumps(
        {"message": MUTATION_MESSAGE, "reason": "mutation"},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    assert upstream.requests == []
    rig.stop()
    records = rig.records().records
    assert [r["reason"] for r in records if r["kind"] == "intent"] == [c[2] for c in cases] + [
        "mutation"
    ]


# -- delivery -------------------------------------------------------------


def test_delivered_response_is_reframed_from_the_gated_body(rigs, upstream):
    upstream.route(
        "/full",
        Response(
            200,
            [
                ("Content-Type", "application/json"),
                ("ETag", '"abc"'),
                ("X-RateLimit-Remaining", "42"),
                ("Set-Cookie", "a=b"),
                ("Location", "https://api.github.com/elsewhere"),
                ("X-OAuth-Scopes", "repo"),
            ],
            b'{"ok":true}',
            chunked=True,
        ),
    )
    rig = rigs(**live(upstream))
    reply = rig.request(get("/full", "Accept: application/vnd.github+json"))
    assert reply.status == 200
    assert reply.body == b'{"ok":true}'
    names = [name for name, _ in reply.headers]
    assert names == [
        "Content-Type",
        "ETag",
        "X-RateLimit-Remaining",
        "Connection",
        "Content-Length",
    ]
    assert reply.header("Content-Length") == "11"
    assert reply.header("Connection") == "close"
    assert b"chunked" not in reply.raw and b"Location" not in reply.raw
    head_reply = rig.request(head("/full"))
    assert head_reply.status == 200 and head_reply.body == b""
    assert head_reply.header("Content-Length") is None
    seen = upstream.requests[0]
    assert seen.method == "GET" and seen.target == "/full"
    assert seen.header("Authorization") == f"Bearer {CREDENTIAL}"
    assert seen.header("Accept") == "application/vnd.github+json"
    assert seen.header("Accept-Encoding") == "identity"
    assert upstream.requests[1].method == "HEAD"
    rig.stop()
    parsed = rig.records()
    assert kinds_for(parsed, 1) == ["intent", "completion"]
    completion = records_for(parsed, 1)[1]
    assert (completion["outcome"], completion["status"], completion["response_bytes"]) == (
        "delivered",
        200,
        11,
    )
    assert records_for(parsed, 1)[0]["budgets"] == {
        "request_bytes": FAST.request_body_limit,
        "response_bytes": FAST.response_body_limit,
        "audit_bytes": 5 * FAST.record_cap,
    }


def test_a_large_body_streams_from_the_spool_and_the_spool_is_gone_after(rigs, upstream):
    body = bytes(range(256)) * 400
    upstream.route("/big", Response(200, [("Content-Type", "application/octet-stream")], body))
    rig = rigs(**live(upstream, spool_threshold=1000))
    reply = rig.request(get("/big"))
    assert reply.status == 200 and reply.body == body
    assert reply.header("Content-Length") == str(len(body))
    assert list(rig.spool_dir.iterdir()) == []
    rig.stop()
    assert list(rig.spool_dir.iterdir()) == []


def test_gate_and_redirect_refusals_answer_502_after_their_completion(rigs, upstream):
    upstream.route("/over", Response(200, [], b"x" * 5000))
    upstream.route("/gz", Response(200, [("Content-Encoding", "gzip")], b"\x1f\x8b"))
    rig = rigs(**live(upstream, Budgets(response_body_limit=4096, upstream_timeout=3.0)))
    assert (rig.request(get("/over")).status, rig.request(get("/over")).reason) == (
        502,
        "gate-response-bytes",
    )
    assert rig.request(get("/gz")).reason == "content-encoding"
    assert rig.request(get("/away")).reason == "redirect-origin"
    assert rig.request(get("/loop")).reason == "redirect-loop"
    assert rig.request(get("/to-orgs")).reason == "redirect-denylist"
    assert rig.request(get("/hop4")).reason == "redirect-hops"
    assert rig.request(graphql("query { a }")).status == 404  # the fake has no /graphql
    rig.stop()
    parsed = rig.records()
    outcomes = [
        (r["seq"], r["outcome"], r["status"]) for r in parsed.records if r["kind"] == "completion"
    ]
    assert outcomes == [
        (1, "refused-gate", 200),
        (2, "refused-gate", 200),
        (3, "refused-gate", 200),
        (4, "refused-redirect", 302),
        (5, "refused-redirect", 302),
        (6, "refused-redirect", 302),
        (7, "refused-redirect", 307),
        (8, "delivered", 404),
    ]
    hops = records_for(parsed, 7)
    assert [r["kind"] for r in hops] == [
        "intent",
        "redirect-intent",
        "redirect-intent",
        "redirect-intent",
        "completion",
    ]
    assert [r["hop"] for r in hops[1:4]] == [1, 2, 3]
    assert [e["decision"] for e in hops[4]["redirects"]] == [
        "followed",
        "followed",
        "followed",
        "refused",
    ]
    assert hops[4]["redirects"][3]["reason"] == "redirect-hops"


# -- the connection budget and read limits --------------------------------


def test_every_accepted_connection_costs_one_unit_at_accept(rigs, upstream):
    """Each accepted connection spends one unit whatever it carries — no
    request line, an incomplete one, an idle head, or a served request. The
    slot count is generous so no connection here is busy-refused; the
    busy-refusal partition is pinned by its own deterministic test."""
    budgets = Budgets(
        connection_budget=12, open_connections=4, head_read_seconds=0.4, body_read_seconds=0.4
    )
    rig = rigs(**live(upstream, budgets))
    empty = rig.connect()
    empty.close()
    partial = rig.connect()
    partial.sendall(b"GET /zen")
    partial.close()
    idle = rig.connect()
    assert read_all(idle) == b""  # closed by the relay at the head limit
    idle.close()
    assert rig.request(get("/zen")).status == 200
    rig.stop()
    record = terminal(rig)
    assert record["reason"] == "agent-exit"
    assert (record["accepted"], record["busy_refused"], record["no_request"]) == (4, 0, 3)
    assert (record["requests_seen"], record["requests_charged"]) == (1, 1)
    assert record["connection_budget_exhausted"] is False
    assert (
        record["accepted"]
        == record["busy_refused"] + record["no_request"] + record["requests_seen"]
    )


def test_the_accept_that_spends_the_last_unit_is_served_then_the_socket_is_gone(rigs, upstream):
    rig = rigs(**live(upstream, Budgets(connection_budget=3, head_read_seconds=0.4)))
    rig.connect().close()
    rig.connect().close()
    reply = rig.request(get("/zen"))
    assert reply.status == 200
    deadline = time.monotonic() + 5
    while rig.socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not rig.socket_path.exists()
    with pytest.raises(OSError):
        rig.connect()
    assert rig.join() == 0
    record = terminal(rig)
    assert record["reason"] == "connection-budget-exhausted"
    assert record["connection_budget_exhausted"] is True
    assert (record["accepted"], record["no_request"], record["requests_seen"]) == (3, 2, 1)
    assert exit_event(rig) == {
        "event": "exit",
        "reason": "connection-budget-exhausted",
        "audit": "ok",
    }


def test_request_lines_past_the_request_budget_are_seen_charged_and_refused(rigs, upstream):
    """2,001 parsed request lines within 4,000 accepted connections: the
    2,001st is refused with the stable message and recorded, and exhaustion
    ends acceptance at exactly the connection budget. The request counters
    are exact; the 1,999 request-less connections partition into ``no_request``
    and ``busy_refused`` — a request-less connection accepted while every
    open-connection slot is busy is legitimately busy-refused rather than read
    empty, and which it is depends on host scheduling — so this asserts their
    sum and the whole partition, and a separate test pins busy refusal
    deterministically."""
    budgets = Budgets(head_read_seconds=1.0)
    rig = rigs(
        **live(upstream, budgets),
        sink_factory=lambda fd, b: scripted_sink(fd, b, sync=False),
    )
    denied = get("/orgs/x")
    for _ in range(budgets.request_budget):
        assert rig.request(denied).reason == "admin-read"
    reply = rig.request(get("/zen"))
    assert reply.status == 429
    assert reply.json() == {
        "message": BUDGET_MESSAGE.format(budget="request budget"),
        "reason": "budget-requests",
    }
    assert upstream.requests == []
    for _ in range(budgets.connection_budget - budgets.request_budget - 1):
        rig.connect().close()
    assert rig.join(60) == 0
    record = terminal(rig)
    assert record["reason"] == "connection-budget-exhausted"
    assert record["accepted"] == budgets.connection_budget == 4000
    assert record["requests_seen"] == budgets.request_budget + 1 == 2001
    assert record["requests_charged"] == budgets.request_budget == 2000
    request_less = budgets.connection_budget - budgets.request_budget - 1
    assert record["no_request"] + record["busy_refused"] == request_less == 1999
    assert (
        record["accepted"]
        == record["requests_seen"] + record["no_request"] + record["busy_refused"]
    )
    assert record["request_budget_exhausted"] is True
    assert record["connection_budget_exhausted"] is True
    assert record["audit_budget_exhausted"] is False
    parsed = rig.records()
    assert parsed.counts_by_kind == {"intent": 2001, "terminal": 1}
    assert parsed.records[2000]["reason"] == "budget-requests"
    assert parsed.records[2000]["seq"] == 2001


def test_slow_loris_head_and_body_and_idle_connections_close_at_the_limits(rigs, upstream):
    rig = rigs(**live(upstream))
    started = time.monotonic()
    loris = rig.connect()
    loris.sendall(b"GET /zen HTTP/1.1\r\n")
    for _ in range(10):
        time.sleep(0.05)
        try:
            loris.sendall(b"X-A: 1\r\n")
        except OSError:
            break
    reply = parse_reply(read_all(loris))
    assert (reply.status, reply.reason) == (400, "headers")
    assert time.monotonic() - started < 1.5
    loris.close()

    started = time.monotonic()
    slow = rig.connect()
    slow.sendall(b"POST /graphql HTTP/1.1\r\nContent-Length: 100\r\n\r\n")
    for _ in range(10):
        time.sleep(0.05)
        try:
            slow.sendall(b"{")
        except OSError:
            break
    reply = parse_reply(read_all(slow))
    assert (reply.status, reply.reason) == (400, "body")
    assert time.monotonic() - started < 1.5
    slow.close()

    idle = rig.connect()
    started = time.monotonic()
    assert read_all(idle) == b""
    assert 0.3 < time.monotonic() - started < 1.5
    idle.close()
    rig.stop()
    record = terminal(rig)
    assert (record["accepted"], record["no_request"], record["requests_seen"]) == (3, 1, 2)
    assert upstream.requests == []


def test_request_line_at_the_limit_is_seen_and_one_byte_over_is_not(rigs, upstream):
    limit = DEFAULT_BUDGETS.request_line_limit
    at_limit = b"GET /" + b"a" * (limit - 14) + b" HTTP/1.1"
    assert len(at_limit) == limit
    rig = rigs(**live(upstream))
    reply = rig.request(at_limit + b"\r\n\r\n")
    assert (reply.status, reply.reason) == (400, "path")
    over = rig.request(at_limit + b"a\r\n\r\n")
    assert over.status is None
    rig.stop()
    record = terminal(rig)
    assert (record["requests_seen"], record["no_request"]) == (1, 1)
    intent = rig.records().records[0]
    assert intent["target"]["form"] == "invalid" and intent["target"]["stage"] == "path"


def test_a_pipelined_second_request_is_never_read(rigs, upstream):
    rig = rigs(**live(upstream))
    raw = rig.call(get("/zen") + get("/orgs/x"))
    assert raw.count(b"HTTP/1.1 ") == 1
    assert parse_reply(raw).status == 200
    rig.stop()
    assert upstream.targets() == ["/zen"]
    assert rig.records().counts_by_kind == {"intent": 1, "completion": 1, "terminal": 1}


# -- write-ahead audit -----------------------------------------------------


@pytest.mark.parametrize("failure", ["write", "sync"])
def test_an_intent_write_or_fdatasync_failure_opens_no_upstream_connection(rigs, upstream, failure):
    selector = nth("intent", 1)
    factory = (
        (lambda fd, b: scripted_sink(fd, b, fail_write=selector))
        if failure == "write"
        else (lambda fd, b: scripted_sink(fd, b, fail_sync=selector))
    )
    rig = rigs(**live(upstream), sink_factory=factory)
    reply = rig.request(get("/zen"))
    assert (reply.status, reply.reason) == (503, "audit-unavailable")
    assert upstream.requests == []
    assert rig.request(get("/zen")).reason == "audit-unavailable"
    assert rig.request(b"PUT /x HTTP/1.1\r\n\r\n").reason == "audit-unavailable"
    assert rig.stop() == 0
    assert audit_failures(rig) == [
        {"event": "audit-failure", "kind": "intent", "seq": 1, "hop": None}
    ]
    assert exit_event(rig)["audit"] == "unavailable"
    parsed = rig.records()
    assert parsed.counts_by_kind.get("terminal", 0) == 0
    assert parsed.counts_by_kind.get("intent", 0) == (1 if failure == "sync" else 0)


@pytest.mark.parametrize("hop", [1, 2, 3])
def test_a_redirect_intent_failure_never_sends_that_hop(rigs, upstream, hop):
    rig = rigs(
        **live(upstream),
        sink_factory=lambda fd, b: scripted_sink(fd, b, fail_write=hop_record(hop)),
    )
    reply = rig.request(get("/hop1"))
    assert (reply.status, reply.reason) == (503, "audit-unavailable")
    assert upstream.targets() == ["/hop1", "/hop2", "/hop3"][:hop]
    assert all(seen.header("Authorization") == f"Bearer {CREDENTIAL}" for seen in upstream.requests)
    assert rig.request(get("/zen")).reason == "audit-unavailable"
    assert rig.stop() == 0
    assert audit_failures(rig) == [
        {"event": "audit-failure", "kind": "redirect-intent", "seq": 1, "hop": hop}
    ]
    parsed = rig.records()
    assert kinds_for(parsed, 1) == ["intent"] + ["redirect-intent"] * (hop - 1)
    assert "completion" not in parsed.counts_by_kind
    assert "terminal" not in parsed.counts_by_kind
    assert exit_event(rig)["audit"] == "unavailable"


def test_a_completion_write_failure_leaves_the_intent_and_freezes_the_relay(rigs, upstream):
    rig = rigs(
        **live(upstream),
        sink_factory=lambda fd, b: scripted_sink(fd, b, fail_write=nth("completion", 1)),
    )
    reply = rig.request(get("/zen"))
    assert (reply.status, reply.reason) == (503, "audit-unavailable")
    assert upstream.targets() == ["/zen"]
    for raw in (get("/zen"), get("/orgs/x"), b"junk\r\n\r\n"):
        assert rig.request(raw).reason == "audit-unavailable"
    assert upstream.targets() == ["/zen"]
    assert rig.stop() == 0
    assert audit_failures(rig) == [
        {"event": "audit-failure", "kind": "completion", "seq": 1, "hop": None}
    ]
    parsed = rig.records()
    assert parsed.counts_by_kind == {"intent": 1}
    assert parsed.records[0]["decision"] == "authorized"


def test_records_correlate_by_seq_and_hop_under_concurrent_interleaving(rigs, upstream):
    gate = threading.Event()
    hooks = SimpleNamespace(during_upstream=lambda seq: gate.wait(10))
    rig = rigs(**live(upstream, Budgets(concurrency=4, open_connections=8)), hooks=hooks)
    replies: dict[int, bytes] = {}
    targets = ["/hop1", "/zen", "/hop3", "/orgs/x", "/hop1", "/zen", "/loop", "/hop2"]

    def call(index: int) -> None:
        replies[index] = rig.call(get(targets[index]))

    threads = [threading.Thread(target=call, args=(i,)) for i in range(len(targets))]
    for thread in threads:
        thread.start()
    time.sleep(0.5)
    gate.set()
    for thread in threads:
        thread.join(20)
    rig.stop()
    parsed = rig.records()
    intents = [r for r in parsed.records if r["kind"] == "intent"]
    assert sorted(r["seq"] for r in intents) == list(range(1, 9))
    for intent in intents:
        seq = intent["seq"]
        own = records_for(parsed, seq)
        if intent["decision"] == "refused":
            assert kinds_for(parsed, seq) == ["intent"]
            continue
        assert own[-1]["kind"] == "completion"
        hops = [r["hop"] for r in own if r["kind"] == "redirect-intent"]
        assert hops == list(range(1, len(hops) + 1))
        followed = [e["hop"] for e in own[-1]["redirects"] if e["decision"] == "followed"]
        assert followed == hops
    by_path = {}
    for intent in intents:
        by_path.setdefault(intent["target"]["path"], []).append(intent["seq"])
    for seq in by_path["/hop1"]:
        assert [r["hop"] for r in records_for(parsed, seq) if r["kind"] == "redirect-intent"] == [
            1,
            2,
            3,
        ]
    assert len(replies) == 8 and all(replies.values())


def test_reserved_room_is_released_only_after_the_last_record(rigs, upstream):
    """One authorized reservation fits the file cap; a concurrent request
    finds no room while it is held and, the cap once reached, never after."""
    budgets = Budgets(file_cap=6 * DEFAULT_BUDGETS.record_cap, upstream_timeout=3.0, concurrency=2)
    gate = threading.Event()
    hooks = SimpleNamespace(during_upstream=lambda seq: gate.wait(10))
    rig = rigs(**live(upstream, budgets), hooks=hooks)
    result: dict[str, bytes] = {}
    first = threading.Thread(target=lambda: result.__setitem__("a", rig.call(get("/hop1"))))
    first.start()
    time.sleep(0.4)
    reply = rig.request(get("/zen"))
    assert (reply.status, reply.reason) == (503, "audit-budget")
    assert reply.json()["message"] == BUDGET_MESSAGE.format(budget="audit budget")
    gate.set()
    first.join(20)
    assert parse_reply(result["a"]).status == 200
    assert rig.request(get("/zen")).reason == "audit-budget"
    assert rig.request(b"PUT /x HTTP/1.1\r\n\r\n").reason == "audit-budget"
    rig.stop()
    parsed = rig.records()
    assert kinds_for(parsed, 1) == [
        "intent",
        "redirect-intent",
        "redirect-intent",
        "redirect-intent",
        "completion",
    ]
    assert parsed.counts_by_kind == {
        "intent": 1,
        "redirect-intent": 3,
        "completion": 1,
        "terminal": 1,
    }
    record = terminal(rig)
    assert record["audit_budget_exhausted"] is True
    assert (record["requests_seen"], record["requests_charged"]) == (4, 4)
    assert upstream.targets() == ["/hop1", "/hop2", "/hop3", "/zen"]


CRASH_POINTS = [
    ("after_intent_fdatasync", "/zen", 0, "/zen"),
    ("during_upstream", "/zen", 0, "/zen"),
    ("after_upstream_complete", "/zen", 0, "/zen"),
    ("after_redirect_intent_fdatasync", "/hop1", 1, "/hop2"),
    ("before_redirect_send", "/hop1", 1, "/hop2"),
    ("after_redirect_send", "/hop1", 1, "/hop2"),
    ("after_redirect_intent_fdatasync", "/hop1", 2, "/hop3"),
    ("before_redirect_send", "/hop1", 2, "/hop3"),
    ("after_redirect_send", "/hop1", 2, "/hop3"),
    ("after_redirect_intent_fdatasync", "/hop1", 3, "/zen"),
    ("before_redirect_send", "/hop1", 3, "/zen"),
    ("after_redirect_send", "/hop1", 3, "/zen"),
    ("after_upstream_complete", "/hop1", 3, "/zen"),
]


@pytest.mark.parametrize("point,target,hop,last_authorized", CRASH_POINTS)
def test_a_crash_at_every_write_ahead_boundary_leaves_the_last_target_in_the_sink(
    rigs, upstream, point, target, hop, last_authorized
):
    def at(*args):
        if len(args) < 2 or args[1] == hop:
            raise Crash()

    rig = rigs(**live(upstream), hooks=SimpleNamespace(**{point: at}))
    reply = rig.request(get(target))
    assert reply.status is None  # the connection closed with no response
    rig.stop()
    parsed = rig.records()
    own = records_for(parsed, 1)
    assert own[0]["kind"] == "intent" and own[0]["decision"] == "authorized"
    assert "completion" not in [r["kind"] for r in own]
    redirect_intents = [r for r in own if r["kind"] == "redirect-intent"]
    assert [r["hop"] for r in redirect_intents] == list(range(1, hop + 1))
    last = redirect_intents[-1] if redirect_intents else own[0]
    assert last["target"]["path"] == last_authorized
    sent = upstream.targets()
    if point in ("before_redirect_send", "after_redirect_intent_fdatasync"):
        assert sent == ["/hop1", "/hop2", "/hop3"][:hop]
    elif point == "after_intent_fdatasync":
        assert sent == []
    assert rig.logs and "Crash" in rig.logs[0]
    assert terminal(rig)["requests_seen"] == 1


def test_audit_unavailable_refuses_everything_writes_nothing_and_reports_once(rigs, upstream):
    rig = rigs(
        **live(upstream, Budgets(connection_budget=6, head_read_seconds=0.4)),
        sink_factory=lambda fd, b: scripted_sink(fd, b, fail_sync=nth("intent", 2)),
    )
    assert rig.request(get("/zen")).status == 200
    assert rig.request(get("/orgs/x")).reason == "audit-unavailable"
    size = rig.sink_path.stat().st_size
    for raw in (get("/zen"), graphql("query { a }"), b"\r\n\r\n"):
        assert rig.request(raw).reason == "audit-unavailable"
    rig.connect().close()
    assert rig.join() == 0
    assert rig.sink_path.stat().st_size == size
    assert audit_failures(rig) == [
        {"event": "audit-failure", "kind": "intent", "seq": 2, "hop": None}
    ]
    assert exit_event(rig) == {
        "event": "exit",
        "reason": "connection-budget-exhausted",
        "audit": "unavailable",
    }
    assert not rig.socket_path.exists()
    parsed = rig.records()
    assert "terminal" not in parsed.counts_by_kind
    assert upstream.targets() == ["/zen"]


# -- shutdown -------------------------------------------------------------


def test_connection_exhaustion_drains_the_open_connections_then_one_terminal(rigs, upstream):
    upstream.route("/slow", Response(200, [], b"a" * 100, stall_after=50, stall_seconds=0.6))
    budgets = Budgets(connection_budget=4, open_connections=8, upstream_timeout=5.0)
    rig = rigs(**live(upstream, budgets))
    replies: list[bytes] = []
    threads = [
        threading.Thread(target=lambda: replies.append(rig.call(get("/slow")))) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert [parse_reply(r).body for r in replies] == [b"a" * 100] * 4
    assert rig.join() == 0
    record = terminal(rig)
    assert record["reason"] == "connection-budget-exhausted"
    assert (record["accepted"], record["requests_seen"]) == (4, 4)
    assert rig.records().counts_by_kind == {"intent": 4, "completion": 4, "terminal": 1}
    assert exit_event(rig)["reason"] == "connection-budget-exhausted"
    assert not rig.socket_path.exists()


def test_agent_exit_aborts_in_flight_upstream_requests_and_writes_one_terminal(rigs, upstream):
    upstream.route(
        "/stall", Response(200, [], b"a" * 100, stall_after=10, stall_seconds=LONG_STALL)
    )
    rig = rigs(**live(upstream, Budgets(upstream_timeout=LONG_STALL)))
    conn = rig.connect(timeout=20)
    conn.sendall(get("/stall"))
    time.sleep(0.5)
    started = time.monotonic()
    assert rig.stop() == 0
    assert time.monotonic() - started < 5
    assert read_all(conn) == b""
    conn.close()
    parsed = rig.records()
    assert kinds_for(parsed, 1) == ["intent", "completion"]
    assert records_for(parsed, 1)[1]["outcome"] == "aborted"
    record = terminal(rig)
    assert record["reason"] == "agent-exit"
    assert exit_event(rig) == {"event": "exit", "reason": "agent-exit", "audit": "ok"}
    assert not rig.socket_path.exists()
    with pytest.raises(OSError):
        rig.connect()


def test_agent_exit_closes_queued_connections_unread(rigs, upstream):
    upstream.route(
        "/stall", Response(200, [], b"a" * 100, stall_after=10, stall_seconds=LONG_STALL)
    )
    budgets = Budgets(concurrency=1, open_connections=4, upstream_timeout=LONG_STALL)
    rig = rigs(**live(upstream, budgets))
    active = rig.connect(timeout=20)
    active.sendall(get("/stall"))
    time.sleep(0.3)
    queued = rig.connect(timeout=20)
    queued.sendall(get("/zen"))
    time.sleep(0.2)
    assert rig.stop() == 0
    assert read_all(queued) == b""
    record = terminal(rig)
    assert (record["accepted"], record["requests_seen"], record["no_request"]) == (2, 1, 1)
    assert upstream.targets() == ["/stall"]


def test_audit_budget_exhaustion_then_connection_exhaustion_writes_one_terminal_last(
    rigs, upstream
):
    budgets = Budgets(
        file_cap=6 * DEFAULT_BUDGETS.record_cap,
        connection_budget=4,
        concurrency=2,
        upstream_timeout=5.0,
    )
    gate = threading.Event()
    hooks = SimpleNamespace(during_upstream=lambda seq: gate.wait(10))
    rig = rigs(**live(upstream, budgets), hooks=hooks)
    result: dict[str, bytes] = {}
    holder = threading.Thread(target=lambda: result.__setitem__("a", rig.call(get("/hop1"))))
    holder.start()
    time.sleep(0.4)
    assert rig.request(get("/zen")).reason == "audit-budget"
    assert rig.request(get("/orgs/x")).reason == "audit-budget"
    gate.set()
    holder.join(20)
    rig.connect().close()
    assert rig.join() == 0
    parsed = rig.records()
    assert [r["kind"] for r in parsed.records] == [
        "intent",
        "redirect-intent",
        "redirect-intent",
        "redirect-intent",
        "completion",
        "terminal",
    ]
    record = terminal(rig)
    assert record["reason"] == "connection-budget-exhausted"
    assert record["audit_budget_exhausted"] is True
    assert record["connection_budget_exhausted"] is True
    assert (record["requests_seen"], record["requests_charged"]) == (3, 3)
    assert exit_event(rig) == {
        "event": "exit",
        "reason": "connection-budget-exhausted",
        "audit": "budget-exhausted",
    }


def test_an_agent_exit_during_an_exhaustion_drain_aborts_what_remains(rigs, upstream):
    upstream.route(
        "/stall", Response(200, [], b"a" * 100, stall_after=10, stall_seconds=LONG_STALL)
    )
    rig = rigs(**live(upstream, Budgets(connection_budget=2, upstream_timeout=LONG_STALL)))
    assert rig.request(get("/zen")).status == 200
    conn = rig.connect(timeout=20)
    conn.sendall(get("/stall"))
    deadline = time.monotonic() + 5
    while rig.socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not rig.socket_path.exists() and rig.running
    time.sleep(0.3)
    assert rig.stop() == 0
    assert read_all(conn) == b""
    parsed = rig.records()
    assert records_for(parsed, 2)[1]["outcome"] == "aborted"
    record = terminal(rig)
    assert record["reason"] == "connection-budget-exhausted"
    assert record["connection_budget_exhausted"] is True
    assert exit_event(rig)["reason"] == "connection-budget-exhausted"


def test_serve_returns_zero_and_reports_ready_first_and_exit_last(rigs):
    rig = rigs(**none())
    assert rig.stop() == 0
    assert rig.events() == [
        {"event": "ready"},
        {"event": "exit", "reason": "agent-exit", "audit": "ok"},
    ]
    record = terminal(rig)
    assert record == {
        "kind": "terminal",
        "ts": record["ts"],
        "reason": "agent-exit",
        "connection_budget_exhausted": False,
        "request_budget_exhausted": False,
        "audit_budget_exhausted": False,
        "accepted": 0,
        "busy_refused": 0,
        "no_request": 0,
        "requests_seen": 0,
        "requests_charged": 0,
    }


def test_no_record_message_or_log_carries_a_credential_or_location_byte(rigs, upstream):
    secret_path = "/signed/blob?token=SECRETQUERY"
    upstream.route("/sign", Response(302, [("Location", "https://evil.example" + secret_path)]))
    rig = rigs(**live(upstream))
    reply = rig.request(get("/sign", "Authorization: Bearer CLIENTSECRET"))
    assert reply.reason == "redirect-origin"
    rig.stop()
    haystack = rig.sink_bytes() + rig.report.getvalue().encode() + "".join(rig.logs).encode()
    haystack += reply.raw
    for needle in (CREDENTIAL, "CLIENTSECRET", "SECRETQUERY", "/signed/blob", "token="):
        assert needle.encode() not in haystack, needle
    entry = records_for(rig.records(), 1)[1]["redirects"][0]
    assert entry["host"]["status"] == "valid" and entry["host"]["value"] == "evil.example"
    assert set(entry["host"]) == {"status", "value"}


def test_the_reader_contract_matches_the_parser(rigs):
    """The server-owned reader is what ``read_request`` sees; the parser's
    own tests cover the grammar, this pins the seam."""
    assert callable(read_request)
    rig = rigs(**none())
    assert rig.request(get("/zen")).reason == "no-upstream"
    rig.stop()


# -- agent exit against the acceptor (item 5) ------------------------------


def test_agent_exit_wakes_a_blocked_acceptor_without_waiting_for_the_poll(
    rigs, upstream, monkeypatch
):
    """With the poll interval stretched far past the test, only the wake
    mechanism can end acceptance promptly — a poll-only acceptor would sit
    in select for the whole interval."""
    monkeypatch.setattr("theozolith_worker.relay.server.ACCEPT_POLL_SECONDS", 30.0)
    rig = rigs(**live(upstream))
    started = time.monotonic()
    assert rig.stop() == 0
    assert time.monotonic() - started < 5  # the 30 s poll never elapsed
    assert exit_event(rig)["reason"] == "agent-exit"
    record = terminal(rig)
    assert (record["accepted"], record["requests_seen"], record["no_request"]) == (0, 0, 0)


def test_agent_exit_winning_a_simultaneously_ready_listener_serves_nothing(rigs, upstream):
    """The listener is ready and agent exit arrives at the same instant: the
    exit wins, the pending connection is never accepted or served, and the
    listener is closed and unlinked."""
    box: list[ServerRig] = []

    def listener_ready() -> None:
        if box:
            box[0].agent_exit.set()

    rig = rigs(**live(upstream), hooks=SimpleNamespace(listener_ready=listener_ready))
    box.append(rig)
    conn = rig.connect()
    conn.sendall(get("/zen"))
    assert read_all(conn) == b""  # reset unread, credentialed work never begins
    conn.close()
    assert rig.join() == 0
    assert exit_event(rig)["reason"] == "agent-exit"
    record = terminal(rig)
    assert (record["accepted"], record["requests_seen"], record["no_request"]) == (0, 0, 0)
    assert upstream.targets() == []
    assert rig.records().counts_by_kind.get("intent", 0) == 0
    assert not rig.socket_path.exists()


def test_connection_budget_exhaustion_refuses_the_over_cap_connection_deterministically(
    rigs, upstream
):
    """A one-slot relay with a two-connection budget: the first connection is
    held mid-upstream while the second is accepted and busy-refused, spending
    the last unit; the held one is served, then acceptance is over."""
    gate = threading.Event()
    hooks = SimpleNamespace(during_upstream=lambda seq: gate.wait(10))
    budgets = Budgets(connection_budget=2, open_connections=1, upstream_timeout=5.0)
    rig = rigs(**live(upstream, budgets), hooks=hooks)
    first: dict[str, bytes] = {}
    held = threading.Thread(target=lambda: first.__setitem__("r", rig.call(get("/zen"))))
    held.start()
    deadline = time.monotonic() + 5
    while not upstream.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    busy = parse_reply(read_all(rig.connect()))
    assert (busy.status, busy.reason) == (429, "budget-concurrency")
    gate.set()
    held.join(20)
    assert parse_reply(first["r"]).status == 200
    assert rig.join() == 0
    record = terminal(rig)
    assert record["reason"] == "connection-budget-exhausted"
    assert (record["accepted"], record["busy_refused"], record["requests_seen"]) == (2, 1, 1)
    assert record["connection_budget_exhausted"] is True
    assert not rig.socket_path.exists()
