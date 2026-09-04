"""The write-ahead audit representation and sink (ADR-0057 item 8): record
schemas and serialized bounds, the three tagged target forms, redaction, the
reservation and failure semantics of the sink, and the summary parser."""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import stat
from pathlib import Path

import pytest
from theozolith_worker.relay.audit import (
    RESERVATION_RECORDS,
    SINK_NAME,
    AuditFailure,
    AuditSink,
    AuditUnavailable,
    CompletionRecord,
    HostRepr,
    IntentRecord,
    RedirectEntry,
    RedirectIntentRecord,
    ReservedBudgets,
    SinkExistsError,
    Target,
    TerminalRecord,
    create_relay_dir,
    fits,
    format_ts,
    open_sink,
    parse_records,
    relay_dir,
    relay_root,
    serialize,
)
from theozolith_worker.relay.classify import classify_graphql
from theozolith_worker.relay.ingress import (
    CanonicalTarget,
    IngressRefusal,
    ParsedRequest,
    QueryPair,
    canonicalize_target,
    read_request,
    sha256_hex,
)
from theozolith_worker.relay.reasons import (
    DEFAULT_BUDGETS,
    Budgets,
    Decision,
    HostStatus,
    Kind,
    MethodClass,
    Outcome,
    Reason,
    RedirectDecision,
    Scheme,
    Stage,
)

FIXTURES = Path(__file__).parent / "relay_fixtures"
TS = format_ts(1_700_000_000.123456)
BIG = 10**20 - 1  # the widest integer of the serialized-bound classes
SHA = "f" * 64
SEEN_REQUEST_LINE_LIMIT = DEFAULT_BUDGETS.request_line_limit


def parse(data: bytes, budgets: Budgets = DEFAULT_BUDGETS):
    return read_request(io.BufferedReader(io.BytesIO(data)), budgets)


def request(line: bytes, *headers: bytes, body: bytes = b"") -> bytes:
    return line + b"\r\n" + b"".join(header + b"\r\n" for header in headers) + b"\r\n" + body


def target_of(raw_target: str) -> CanonicalTarget:
    target = canonicalize_target(raw_target, DEFAULT_BUDGETS)
    assert isinstance(target, CanonicalTarget)
    return target


def intent_for_refusal(seq: int, refusal: IngressRefusal) -> IntentRecord:
    """The intent record the transport writes for a parser refusal: the
    invalid form when the target never validated, the full form otherwise."""
    if refusal.target is None:
        assert refusal.stage is not None
        target = Target.invalid(
            refusal.method,
            refusal.method_len,
            refusal.method_sha256,
            refusal.stage,
            refusal.raw_target_len,
            refusal.raw_target_sha256,
        )
    else:
        target = Target.full(refusal.method, refusal.target, None)
    return IntentRecord(seq, TS, Decision.REFUSED, refusal.reason, target, None)


def authorized(seq: int, target: Target) -> IntentRecord:
    return IntentRecord(seq, TS, Decision.AUTHORIZED, None, target, ReservedBudgets(1, 2, 3))


class FakeFd:
    """The injectable syscall seams: records every write, optionally short,
    optionally failing, with a separately failing fdatasync."""

    def __init__(self, *, short: bool = False, fail_write: bool = False, fail_sync: bool = False):
        self.writes: list[bytes] = []
        self.short = short
        self.fail_write = fail_write
        self.fail_sync = fail_sync
        self.syncs = 0

    def write(self, fd: int, data: bytes) -> int:
        if self.fail_write:
            raise OSError(28, "No space left on device")
        self.writes.append(data)
        return len(data) - 1 if self.short else len(data)

    def fdatasync(self, fd: int) -> None:
        self.syncs += 1
        if self.fail_sync:
            raise OSError(5, "Input/output error")

    def sink(self, budgets: Budgets = DEFAULT_BUDGETS) -> AuditSink:
        return AuditSink(
            -1, budgets, clock=lambda: 1_700_000_000.0, _write=self.write, _fdatasync=self.fdatasync
        )


# ----------------------------------------------------------- timestamps


def test_format_ts_is_always_27_bytes_rfc3339_utc_with_six_microsecond_digits():
    for epoch in (0.0, 1.0, 1_700_000_000.0, 1_700_000_000.5, 1_700_000_000.999999, 4102444800.0):
        stamp = format_ts(epoch)
        assert len(stamp.encode("ascii")) == 27, epoch
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", stamp), epoch
    assert format_ts(0.0) == "1970-01-01T00:00:00.000000Z"
    assert format_ts(1_700_000_000.0) == "2023-11-14T22:13:20.000000Z"
    assert format_ts(1_700_000_000.5) == "2023-11-14T22:13:20.500000Z"


# --------------------------------------------------------- record shapes


def test_every_record_kind_serializes_to_one_ascii_line_in_its_field_order():
    target = Target.full(MethodClass.GET, target_of("/repos/o/r/issues?page=2&q=x"), None)
    intent = authorized(1, target)
    line = serialize(intent)
    assert line.endswith(b"\n") and line.count(b"\n") == 1 and line.isascii()
    record = json.loads(line)
    assert list(record) == ["kind", "ts", "seq", "decision", "reason", "target", "budgets"]
    assert record["reason"] is None and record["budgets"] == {
        "request_bytes": 1,
        "response_bytes": 2,
        "audit_bytes": 3,
    }
    refused = IntentRecord(2, TS, Decision.REFUSED, Reason.ADMIN_READ, target, None)
    assert list(json.loads(serialize(refused))) == [
        "kind",
        "ts",
        "seq",
        "decision",
        "reason",
        "target",
    ]
    assert json.loads(serialize(refused))["reason"] == "admin-read"
    redirect = RedirectIntentRecord(1, TS, 2, target)
    assert json.loads(serialize(redirect)) == {
        "kind": "redirect-intent",
        "ts": TS,
        "seq": 1,
        "hop": 2,
        "decision": "authorized",
        "target": target.to_json(),
    }
    entry = RedirectEntry(
        1,
        302,
        RedirectDecision.FOLLOWED,
        None,
        Scheme.HTTPS,
        HostRepr(HostStatus.VALID, "api.github.com"),
    )
    completion = CompletionRecord(1, TS, Outcome.DELIVERED, 200, 10, 20, (entry,))
    assert json.loads(serialize(completion)) == {
        "kind": "completion",
        "ts": TS,
        "seq": 1,
        "outcome": "delivered",
        "status": 200,
        "request_bytes": 10,
        "response_bytes": 20,
        "redirects": [
            {
                "hop": 1,
                "status": 302,
                "decision": "followed",
                "reason": None,
                "scheme": "https",
                "host": {"status": "valid", "value": "api.github.com"},
            }
        ],
    }
    terminal = TerminalRecord(TS, "agent-exit", False, True, False, 5, 1, 2, 4, 4)
    assert json.loads(serialize(terminal)) == {
        "kind": "terminal",
        "ts": TS,
        "reason": "agent-exit",
        "connection_budget_exhausted": False,
        "request_budget_exhausted": True,
        "audit_budget_exhausted": False,
        "accepted": 5,
        "busy_refused": 1,
        "no_request": 2,
        "requests_seen": 4,
        "requests_charged": 4,
    }
    assert "seq" not in json.loads(serialize(terminal))


def test_full_form_target_reduces_the_query_and_graphql_to_names_lengths_and_digests():
    target = target_of(
        "/repos/o/r/issues?page=2&per_page=30&q=repo%3Ao%2Fr+is%3Aopen&flag&sort=%C3%A9"
    )
    full = Target.full(MethodClass.GET, target, None).to_json()
    assert list(full) == ["form", "method", "path", "query", "graphql"]
    assert full["form"] == "full" and full["method"] == "GET" and full["graphql"] is None
    assert full["path"] == "/repos/o/r/issues"
    assert full["query"] == [
        {"name": "page", "len": 1, "sha256": sha256_hex(b"2"), "value": "2"},
        {"name": "per_page", "len": 2, "sha256": sha256_hex(b"30"), "value": "30"},
        {"name": "q", "len": 16, "sha256": sha256_hex(b"repo:o/r is:open")},
        {"name": "flag", "len": 0, "sha256": sha256_hex(b"")},
        {"name": "sort", "len": 2, "sha256": sha256_hex(b"\xc3\xa9")},
    ]
    long_state = CanonicalTarget(
        "/x", (QueryPair("state", b"o" * 256), QueryPair("state", b"o" * 255)), 1, SHA
    )
    entries = Target.full(MethodClass.GET, long_state, None).to_json()["query"]
    assert "value" not in entries[0] and entries[1]["value"] == "o" * 255
    encoded_name = CanonicalTarget("/x", (QueryPair("\xc3\xa9 a", b"1"),), 1, SHA)
    assert (
        Target.full(MethodClass.GET, encoded_name, None).to_json()["query"][0]["name"]
        == "%C3%A9%20a"
    )

    body = json.dumps(
        {
            "query": "query "
            + "N" * 129
            + "($owner: String!) { repository(owner: $owner) { id } }",
            "variables": {
                "owner": "o",
                "repo": "r",
                "number": 7,
                "states": ["OPEN"],
                "secret": "s" * 10,
                "first": "f" * 256,
            },
        }
    ).encode()
    graphql = classify_graphql(body)
    rendered = Target.full(MethodClass.POST, target_of("/graphql"), graphql).to_json()["graphql"]
    assert list(rendered) == ["parsed", "op_type", "op_name_len", "op_name_sha256", "variables"]
    assert rendered["op_name_len"] == 129 and rendered["op_name_sha256"] == sha256_hex(b"N" * 129)
    by_name = {entry["name"]: entry for entry in rendered["variables"]}
    assert by_name["owner"] == {
        "name": "owner",
        "type": "string",
        "len": 3,
        "sha256": sha256_hex(b'"o"'),
        "value": "o",
    }
    assert by_name["number"] == {
        "name": "number",
        "type": "number",
        "len": 1,
        "sha256": sha256_hex(b"7"),
        "value": 7,
    }
    assert by_name["states"]["value"] == ["OPEN"] and by_name["states"]["type"] == "array"
    assert "value" not in by_name["secret"] and by_name["secret"]["len"] == 12
    assert "value" not in by_name["first"]  # over the literal cap: length and digest stay
    short_name = Target.full(
        MethodClass.POST, target_of("/graphql"), classify_graphql(b'{"query":"query Q { a }"}')
    )
    assert short_name.to_json()["graphql"]["op_name"] == "Q"
    anonymous = Target.full(
        MethodClass.POST, target_of("/graphql"), classify_graphql(b'{"query":"{ a }"}')
    )
    assert anonymous.to_json()["graphql"]["op_name"] is None
    unparsed = Target.full(MethodClass.POST, target_of("/graphql"), classify_graphql(b"nope"))
    assert unparsed.to_json()["graphql"] == {"parsed": False}


def test_digest_and_invalid_forms():
    target = target_of("/repos/o/r/issues?state=open&labels=a%2Cb")
    graphql = classify_graphql(b'{"query":"query Q($a: Int) { x }","variables":{"a":1,"b":"c"}}')
    digest = Target.digest(MethodClass.POST, target, graphql).to_json()
    assert list(digest) == [
        "form",
        "method",
        "path_len",
        "path_sha256",
        "query_pairs",
        "query_len",
        "query_sha256",
        "graphql",
    ]
    assert digest["path_len"] == 17 and digest["path_sha256"] == sha256_hex(b"/repos/o/r/issues")
    assert digest["query_pairs"] == 2 and digest["query_len"] == len("state=open&labels=a%2Cb")
    assert digest["query_sha256"] == sha256_hex(b"state=open&labels=a%2Cb")
    assert digest["graphql"] == {
        "parsed": True,
        "op_type": "query",
        "variables_count": 2,
        "variables_sha256": sha256_hex(b'1"c"'),
    }
    assert Target.digest(MethodClass.GET, target, None).to_json()["graphql"] is None
    assert Target.digest(MethodClass.POST, target, classify_graphql(b"x")).to_json()["graphql"] == {
        "parsed": False
    }
    invalid = Target.invalid(
        MethodClass.other, 5000, SHA, Stage.METHOD, 6, sha256_hex(b"/hooks")
    ).to_json()
    assert invalid == {
        "form": "invalid",
        "method": "other",
        "method_len": 5000,
        "method_sha256": SHA,
        "stage": "method",
        "target_len": 6,
        "target_sha256": sha256_hex(b"/hooks"),
        "graphql": None,
    }
    known = Target.invalid(MethodClass.PUT, None, None, Stage.METHOD, 1, sha256_hex(b"/")).to_json()
    assert list(known) == ["form", "method", "stage", "target_len", "target_sha256", "graphql"]


# ------------------------------------------------------ representability


def test_oversize_admitted_targets_yield_digest_form_refusals():
    sink = FakeFd().sink()
    # A 4,096-byte path, and 32 pairs whose names alone fill the query limit:
    # both admitted by the parser, neither describable within the record cap.
    for raw in (
        "/" + "a" * 4095,
        "/x?" + "&".join("n" * 123 + f"{i:02d}=v" for i in range(32)),
    ):
        target = target_of(raw)
        full = authorized(1, Target.full(MethodClass.GET, target, None))
        assert not fits(serialize(full), DEFAULT_BUDGETS), raw[:20]
        record = sink.intent_for(full)
        assert record.decision is Decision.REFUSED and record.reason is Reason.AUDIT_UNREPRESENTABLE
        assert record.target.form == "digest" and record.budgets is None
        line = serialize(record)
        assert fits(line, DEFAULT_BUDGETS)
        parsed = json.loads(line)
        assert list(parsed) == ["kind", "ts", "seq", "decision", "reason", "target"]
        assert parsed["target"]["path_sha256"] == sha256_hex(target.path.encode())
    variables = {"v" * 40 + str(i): i for i in range(100)}
    body = json.dumps({"query": "query Q { a }", "variables": variables}).encode()
    graphql = classify_graphql(body)
    assert graphql.refusal is None
    full = authorized(2, Target.full(MethodClass.POST, target_of("/graphql"), graphql))
    assert not fits(serialize(full), DEFAULT_BUDGETS)
    record = sink.intent_for(full)
    assert record.reason is Reason.AUDIT_UNREPRESENTABLE
    assert json.loads(serialize(record))["target"]["graphql"]["variables_count"] == 100
    # An oversize refusal keeps its own reason in the digest form.
    denied = target_of("/orgs/" + "o" * 4000 + "/members")
    refusal = IntentRecord(
        3, TS, Decision.REFUSED, Reason.ADMIN_READ, Target.full(MethodClass.GET, denied, None), None
    )
    reduced = sink.intent_for(refusal)
    assert reduced.reason is Reason.ADMIN_READ and reduced.target.form == "digest"
    assert reduced.decision is Decision.REFUSED


def test_exact_cap_full_form_is_written_whole_and_one_byte_over_becomes_the_digest_form():
    def full_intent(path_len: int) -> IntentRecord:
        return authorized(
            7, Target.full(MethodClass.GET, target_of("/" + "a" * (path_len - 1)), None)
        )

    base = len(serialize(full_intent(10)))
    exact = full_intent(10 + DEFAULT_BUDGETS.record_cap - base)
    assert len(serialize(exact)) == DEFAULT_BUDGETS.record_cap
    fake = FakeFd()
    sink = fake.sink()
    kept = sink.intent_for(exact)
    assert kept is exact
    reservation = sink.reserve("authorized")
    sink.write_intent(kept, reservation)
    assert fake.writes == [serialize(exact)]
    over = full_intent(10 + DEFAULT_BUDGETS.record_cap - base + 1)
    assert len(serialize(over)) == DEFAULT_BUDGETS.record_cap + 1
    reduced = sink.intent_for(over)
    assert reduced.target.form == "digest" and reduced.reason is Reason.AUDIT_UNREPRESENTABLE
    reservation = sink.reserve("refusal")
    sink.write_intent(reduced, reservation)
    record = json.loads(fake.writes[-1])
    assert set(record) == {"kind", "ts", "seq", "decision", "reason", "target"}
    assert set(record["target"]) == {
        "form",
        "method",
        "path_len",
        "path_sha256",
        "query_pairs",
        "query_len",
        "query_sha256",
        "graphql",
    }
    with pytest.raises(ValueError):
        sink.write_intent(over, sink.reserve("authorized"))


# ------------------------------------------------------- refusal totality


REFUSAL_TOTALITY = {
    "bad-hex-path": (request(b"GET /%G1 HTTP/1.1"), "path", "path", "GET"),
    "bad-query-escape": (request(b"GET /x?q=%G1 HTTP/1.1"), "query", "query", "GET"),
    "absolute-form": (
        request(b"GET http://api.github.com/user HTTP/1.1"),
        "target-form",
        "target-form",
        "GET",
    ),
    "authority-form": (
        request(b"GET api.github.com:443 HTTP/1.1"),
        "target-form",
        "target-form",
        "GET",
    ),
    "asterisk-form": (request(b"GET * HTTP/1.1"), "target-form", "target-form", "GET"),
    "fragment": (request(b"GET /user#x HTTP/1.1"), "target-form", "target-form", "GET"),
    "http-1-0": (request(b"GET /user HTTP/1.0"), "version", "version", "GET"),
    "long-method": (request(b"M" * 5000 + b" /user HTTP/1.1"), "method", "method", "other"),
    "empty-line": (b"\r\n\r\n", "request-line", "request-line", "other"),
}


@pytest.mark.parametrize("name", sorted(REFUSAL_TOTALITY))
def test_every_rejected_request_line_has_an_invalid_form_record_under_its_own_reason(name):
    data, stage, reason, method = REFUSAL_TOTALITY[name]
    refusal = parse(data)
    assert isinstance(refusal, IngressRefusal) and refusal.target is None
    line = serialize(intent_for_refusal(1, refusal))
    assert fits(line, DEFAULT_BUDGETS) and line.isascii()
    record = json.loads(line)
    assert record["decision"] == "refused" and record["reason"] == reason
    assert record["reason"] != "audit-unrepresentable"
    target = record["target"]
    assert target["form"] == "invalid" and target["stage"] == stage and target["method"] == method
    assert "path" not in target and "query" not in target and target["graphql"] is None
    raw_target = (
        data.split(b"\r\n", 1)[0].split(b" ")[1] if b" " in data.split(b"\r\n", 1)[0] else b""
    )
    assert target["target_len"] == len(raw_target)
    assert target["target_sha256"] == sha256_hex(raw_target)
    if method == "other":
        token = data.split(b" ", 1)[0] if name == "long-method" else b""
        assert target["method_len"] == len(token) and target["method_sha256"] == sha256_hex(token)
        assert b"MMMM" not in line
    else:
        assert "method_len" not in target
    for raw_byte in (b"%G1", b"api.github.com", b"#x", b"HTTP/1.0"):
        assert raw_byte not in line


def test_unclassifiable_graphql_bodies_get_full_form_records_with_parsed_false():
    for body, reason in (
        (b"{not json", Reason.GRAPHQL_UNPARSEABLE),
        (b'{"query":"query A { a } query B { b }"}', Reason.GRAPHQL_MULTI_OPERATION),
        (b'{"query":"fragment F on T { a }"}', Reason.GRAPHQL_UNPARSEABLE),
    ):
        parsed = parse(
            request(b"POST /graphql HTTP/1.1", b"Content-Length: %d" % len(body), body=body)
        )
        assert isinstance(parsed, ParsedRequest)
        graphql = classify_graphql(parsed.body)
        assert graphql.refusal is reason
        record = IntentRecord(
            1,
            TS,
            Decision.REFUSED,
            graphql.refusal,
            Target.full(parsed.method, parsed.target, graphql),
            None,
        )
        rendered = json.loads(serialize(record))
        assert rendered["reason"] == reason.value
        assert rendered["target"]["form"] == "full" and rendered["target"]["path"] == "/graphql"
        assert rendered["target"]["graphql"] == {"parsed": False}


def test_digest_form_is_the_shape_for_a_validated_target_refused_by_policy_that_does_not_fit():
    denied = parse(request(b"GET /orgs/" + b"o" * 4080 + b"/members HTTP/1.1"))
    assert isinstance(denied, ParsedRequest)
    record = IntentRecord(
        1,
        TS,
        Decision.REFUSED,
        Reason.ADMIN_READ,
        Target.full(MethodClass.GET, denied.target, None),
        None,
    )
    reduced = FakeFd().sink().intent_for(record)
    assert reduced.target.form == "digest" and reduced.reason is Reason.ADMIN_READ
    assert set(json.loads(serialize(reduced))) == {
        "kind",
        "ts",
        "seq",
        "decision",
        "reason",
        "target",
    }


# ------------------------------------------------------ serialized bounds


def widest_entry(host: HostRepr) -> RedirectEntry:
    return RedirectEntry(
        BIG, BIG, RedirectDecision.FOLLOWED, Reason.GRAPHQL_MULTI_OPERATION, Scheme.INVALID, host
    )


def test_serialized_bounds_of_the_fixed_forms():
    literal = HostRepr(HostStatus.VALID, "h" * 253)
    completion = CompletionRecord(
        BIG, TS, Outcome.REFUSED_REDIRECT, BIG, BIG, BIG, (widest_entry(literal),) * 4
    )
    line = serialize(completion)
    assert len(line) < 3400 and fits(line, DEFAULT_BUDGETS)
    digest_host = HostRepr(HostStatus.OVERSIZED, None, BIG, SHA)
    digest_line = serialize(
        CompletionRecord(
            BIG, TS, Outcome.REFUSED_REDIRECT, BIG, BIG, BIG, (widest_entry(digest_host),) * 4
        )
    )
    assert len(digest_line) < 2900
    invalid = IntentRecord(
        BIG,
        TS,
        Decision.REFUSED,
        Reason.GRAPHQL_MULTI_OPERATION,
        Target.invalid(MethodClass.other, BIG, SHA, Stage.REQUEST_LINE, BIG, SHA),
        None,
    )
    assert len(serialize(invalid)) < 1400
    # The digest form renders from a real canonical target, so its integers
    # are the admission maxima; widening each to 20 digits stays under the bound.
    target = target_of("/" + "a" * 4095)
    graphql = classify_graphql(b'{"query":"subscription { a }","variables":{"a":1}}')
    digest = IntentRecord(
        BIG,
        TS,
        Decision.REFUSED,
        Reason.GRAPHQL_MULTI_OPERATION,
        Target.digest(MethodClass.other, target, graphql, method_len=BIG, method_sha256=SHA),
        None,
    )
    digest_line = serialize(digest)
    rendered = json.loads(digest_line)["target"]
    narrow_integers = (
        rendered["path_len"],
        rendered["query_pairs"],
        rendered["query_len"],
        rendered["graphql"]["variables_count"],
    )
    slack = sum(20 - len(str(value)) for value in narrow_integers)
    assert len(digest_line) + slack < 1400
    terminal = TerminalRecord(
        TS, "connection-budget-exhausted", True, True, True, BIG, BIG, BIG, BIG, BIG
    )
    assert len(serialize(terminal)) < 1400


# ------------------------------------------------- redirect metadata


def test_host_representation_records_a_literal_only_when_valid():
    literal = HostRepr(HostStatus.VALID, "h" * 253).to_json()
    assert literal == {"status": "valid", "value": "h" * 253}
    oversized_host = "h" * 254
    oversized = HostRepr(
        HostStatus.OVERSIZED, None, 254, sha256_hex(oversized_host.encode())
    ).to_json()
    assert oversized == {
        "status": "oversized",
        "len": 254,
        "sha256": sha256_hex(oversized_host.encode()),
    }
    assert "hhhh" not in json.dumps(oversized)
    hostile = b'user@ho"st\\\x01\xc3\xa9'
    invalid = HostRepr(HostStatus.INVALID, None, len(hostile), sha256_hex(hostile)).to_json()
    assert invalid == {"status": "invalid", "len": len(hostile), "sha256": sha256_hex(hostile)}
    assert "\\" not in json.dumps(invalid) and "user" not in json.dumps(invalid)
    assert HostRepr(HostStatus.ABSENT).to_json() == {"status": "absent"}
    entry = RedirectEntry(
        1,
        301,
        RedirectDecision.REFUSED,
        Reason.REDIRECT_ORIGIN,
        Scheme.HTTP,
        HostRepr(HostStatus.ABSENT),
    )
    assert set(entry.to_json()) == {"hop", "status", "decision", "reason", "scheme", "host"}
    for forbidden in ("port", "userinfo", "user_info", "path", "query", "fragment"):
        assert forbidden not in RedirectEntry.__dataclass_fields__
        assert forbidden not in HostRepr.__dataclass_fields__


# ---------------------------------------------------------- redaction


FORBIDDEN_SUBSTRINGS = (
    b"sentinel",
    b"Authorization",
    b"User-Agent",
    b"GitHub CLI",
    b"Time-Zone",
    b"Accept-Encoding",
    b"application/vnd",
    b"repository(",
    b"__type(",
    b"HTTP/1",
)


def corpus_records() -> list[bytes]:
    lines = []
    for path in sorted((FIXTURES / "rest").glob("*.http")) + sorted(
        (FIXTURES / "ingress").glob("*.http")
    ):
        result = parse(path.read_bytes())
        if isinstance(result, ParsedRequest):
            graphql = classify_graphql(result.body) if result.target.path == "/graphql" else None
            target = Target.full(result.method, result.target, graphql)
            lines.append(serialize(authorized(1, target)))
            lines.append(
                serialize(
                    IntentRecord(
                        1,
                        TS,
                        Decision.REFUSED,
                        Reason.ADMIN_READ,
                        Target.digest(result.method, result.target, graphql),
                        None,
                    )
                )
            )
        elif isinstance(result, IngressRefusal):
            lines.append(serialize(intent_for_refusal(1, result)))
    for path in sorted((FIXTURES / "graphql").glob("*.json")):
        if ".expected" in path.name:
            continue
        graphql = classify_graphql(path.read_bytes())
        lines.append(
            serialize(authorized(1, Target.full(MethodClass.POST, target_of("/graphql"), graphql)))
        )
    return lines


def test_no_credential_header_body_or_raw_byte_appears_in_any_record_across_the_corpus():
    lines = corpus_records()
    assert len(lines) > 150
    for line in lines:
        assert line.isascii() and line.endswith(b"\n")
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in line, (forbidden, line)
    # The raw bytes of every hostile target and unknown method token are
    # absent: the invalid form carries lengths and digests only.
    hostile = b"".join(lines)
    for raw in (b"%G1", b"%2Fhooks", b"BREW", b"/user/keys", b"%252F", b"h2c", b"100-continue"):
        assert raw not in hostile


def test_invalid_utf8_and_control_bytes_are_escaped_never_raw():
    body = b'{"query":"query Q { a }","variables":{"a\\u0001b":1,"caf\xc3\xa9":2,"\\ud800":3}}'
    graphql = classify_graphql(body)
    assert graphql.refusal is None
    line = serialize(authorized(1, Target.full(MethodClass.POST, target_of("/graphql"), graphql)))
    assert line.isascii()
    assert b"\\u0001" in line and b"\\u00e9" in line and b"\\ud800" in line
    assert not any(byte < 0x20 for byte in line[:-1])


# ---------------------------------------------------------- the sink


def test_relay_dir_is_0700_beside_the_trusted_input_and_refuses_an_existing_entry(tmp_path):
    assert relay_root(tmp_path) == tmp_path / ".relay"
    assert relay_dir(tmp_path, "run-1") == tmp_path / ".relay" / "run-1"
    created = create_relay_dir(tmp_path, "run-1")
    assert created == tmp_path / ".relay" / "run-1"
    assert stat.S_IMODE(created.stat().st_mode) == 0o700
    assert stat.S_IMODE(relay_root(tmp_path).stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        create_relay_dir(tmp_path, "run-1")
    assert created.is_dir()  # nothing was unlinked
    for bad in ("", ".", "..", "a/b"):
        with pytest.raises(ValueError):
            relay_dir(tmp_path, bad)


def test_open_sink_is_exclusive_0600_append_only_and_never_follows_a_symlink(tmp_path):
    relay = create_relay_dir(tmp_path, "run-1")
    fd = open_sink(relay)
    try:
        path = relay / SINK_NAME
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert os.fstat(fd).st_ino == path.stat().st_ino
        assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND
        with pytest.raises(SinkExistsError):
            open_sink(relay)
    finally:
        os.close(fd)
    linked = create_relay_dir(tmp_path, "run-2")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("forged")
    os.symlink(elsewhere, linked / SINK_NAME)
    with pytest.raises(SinkExistsError):
        open_sink(linked)
    assert elsewhere.read_text() == "forged"
    dangling = create_relay_dir(tmp_path, "run-3")
    os.symlink(tmp_path / "missing", dangling / SINK_NAME)
    with pytest.raises(SinkExistsError):
        open_sink(dangling)
    assert not (tmp_path / "missing").exists()
    assert issubclass(SinkExistsError, OSError)


def test_real_sink_roundtrip_through_parse_records(tmp_path):
    relay = create_relay_dir(tmp_path, "run-1")
    fd = open_sink(relay)
    sink = AuditSink(fd, DEFAULT_BUDGETS)
    try:
        target = Target.full(MethodClass.GET, target_of("/repos/o/r/issues?page=1"), None)
        seq = sink.next_seq()
        assert seq == 1
        reservation = sink.reserve("authorized")
        intent = sink.intent_for(authorized(seq, target))
        sink.write_intent(intent, reservation)
        sink.write_redirect_intent(RedirectIntentRecord(seq, sink.now(), 1, target), reservation)
        sink.write_completion(
            CompletionRecord(seq, sink.now(), Outcome.DELIVERED, 200, 1, 2, ()), reservation
        )
        sink.release(reservation)
        refused_seq = sink.next_seq()
        refusal = sink.reserve("refusal")
        sink.write_intent(
            intent_for_refusal(refused_seq, parse(request(b"PUT /x HTTP/1.1"))), refusal
        )
        sink.release(refusal)
        sink.write_terminal(
            TerminalRecord(sink.now(), "agent-exit", False, False, False, 2, 0, 0, 2, 2)
        )
        assert sink.state == "ok"
    finally:
        os.close(fd)
    data = (relay / SINK_NAME).read_bytes()
    result = parse_records(data)
    assert result.counts_by_kind == {
        "intent": 2,
        "redirect-intent": 1,
        "completion": 1,
        "terminal": 1,
    }
    assert [record["kind"] for record in result.records] == [
        "intent",
        "redirect-intent",
        "completion",
        "intent",
        "terminal",
    ]
    assert result.unparseable_offset is None and result.unparseable_length is None
    assert result.terminal == "present"
    assert sink.bytes_written == len(data)
    assert [record.get("seq") for record in result.records] == [1, 1, 1, 2, None]


def test_terminal_room_is_reserved_at_construction_and_reservations_latch_budget_exhausted():
    budgets = Budgets(record_cap=100, file_cap=600)
    sink = FakeFd().sink(budgets)
    assert sink.bytes_committed == 100 and sink.state == "ok"
    assert RESERVATION_RECORDS == {"refusal": 1, "authorized": 5}
    held = sink.reserve("authorized")
    assert held is not None and held.size == 500 and sink.bytes_committed == 600
    assert sink.reserve("refusal") is None
    assert sink.state == "budget-exhausted"
    # A reservation already held still writes, and the terminal still has its room.
    fake = FakeFd()
    sink = fake.sink(Budgets(record_cap=4096, file_cap=4096 * 6))
    held = sink.reserve("authorized")
    assert sink.reserve("refusal") is None and sink.state == "budget-exhausted"
    target = Target.full(MethodClass.GET, target_of("/user"), None)
    sink.write_intent(authorized(1, target), held)
    sink.write_completion(CompletionRecord(1, TS, Outcome.DELIVERED, 200, 1, 1, ()), held)
    sink.release(held)
    sink.write_terminal(TerminalRecord(TS, "agent-exit", False, False, True, 1, 0, 0, 1, 1))
    assert len(fake.writes) == 3 and fake.syncs == 3
    with pytest.raises(ValueError):
        sink.write_terminal(TerminalRecord(TS, "agent-exit", False, False, True, 1, 0, 0, 1, 1))


def test_budget_exhaustion_never_recovers_when_a_held_reservation_is_released():
    # Terminal room plus one authorized reservation is exactly the cap.
    fake = FakeFd()
    sink = fake.sink(Budgets(record_cap=4096, file_cap=4096 * 6))
    held = sink.reserve("authorized")
    assert held is not None
    assert sink.reserve("refusal") is None and sink.state == "budget-exhausted"
    target = Target.full(MethodClass.GET, target_of("/user"), None)
    sink.write_intent(authorized(1, target), held)
    sink.write_completion(CompletionRecord(1, TS, Outcome.DELIVERED, 200, 1, 1, ()), held)
    sink.release(held)
    # The release returned the unused room, but the state is a latch: no
    # refusal or authorized reservation is handed out again, ever.
    committed = sink.bytes_committed
    assert committed == 4096 + held.used < 4096 * 6
    for _ in range(3):
        assert sink.reserve("refusal") is None
        assert sink.reserve("authorized") is None
        assert sink.state == "budget-exhausted"
        assert sink.bytes_committed == committed
    # The terminal record still has the room reserved for it at construction.
    sink.write_terminal(TerminalRecord(TS, "agent-exit", False, False, True, 1, 0, 0, 1, 1))
    assert len(fake.writes) == 3 and sink.state == "budget-exhausted"


def test_unavailable_takes_precedence_over_budget_exhaustion():
    fake = FakeFd(fail_sync=True)
    sink = fake.sink(Budgets(record_cap=4096, file_cap=4096 * 6))
    held = sink.reserve("authorized")
    target = Target.full(MethodClass.GET, target_of("/user"), None)
    with pytest.raises(AuditUnavailable):
        sink.write_intent(authorized(1, target), held)
    assert sink.state == "unavailable"
    # A reservation that would have crossed the cap never relabels the state.
    assert sink.reserve("refusal") is None and sink.state == "unavailable"
    sink.release(held)
    assert sink.reserve("refusal") is None and sink.state == "unavailable"


def test_unused_reservation_bytes_are_released_only_after_the_last_record():
    budgets = Budgets(record_cap=1000, file_cap=100_000)
    fake = FakeFd()
    sink = fake.sink(budgets)
    reservation = sink.reserve("authorized")
    assert sink.bytes_committed == 1000 + 5000
    target = Target.full(MethodClass.GET, target_of("/user"), None)
    sink.write_intent(authorized(1, target), reservation)
    written = len(fake.writes[0])
    assert reservation.used == written and sink.bytes_committed == 6000
    concurrent = sink.reserve("refusal")
    assert sink.bytes_committed == 7000
    sink.write_completion(CompletionRecord(1, TS, Outcome.DELIVERED, 200, 1, 1, ()), reservation)
    written += len(fake.writes[1])
    sink.release(reservation)
    assert sink.bytes_committed == 7000 - (5000 - written)
    sink.release(reservation)  # idempotent
    assert sink.bytes_committed == 7000 - (5000 - written)
    with pytest.raises(ValueError):
        sink.write_completion(
            CompletionRecord(1, TS, Outcome.DELIVERED, 200, 1, 1, ()), reservation
        )
    assert concurrent is not None and not concurrent.released
    sink.write_intent(
        IntentRecord(2, TS, Decision.REFUSED, Reason.ADMIN_READ, target, None), concurrent
    )
    sink.release(concurrent)
    assert sink.bytes_committed == 7000 - (5000 - written) - (1000 - len(fake.writes[-1]))


def test_a_record_never_exceeds_the_room_its_reservation_holds():
    # A refusal reserves one record's room: its intent fits, but a second
    # record under the same reservation would claim room reserved for nobody.
    fake = FakeFd()
    sink = fake.sink(Budgets(record_cap=250, file_cap=100_000))
    reservation = sink.reserve("refusal")
    target = Target.full(MethodClass.GET, target_of("/user"), None)
    sink.write_intent(
        IntentRecord(2, TS, Decision.REFUSED, Reason.ADMIN_READ, target, None), reservation
    )
    with pytest.raises(ValueError):
        sink.write_completion(
            CompletionRecord(2, TS, Outcome.DELIVERED, 200, 1, 1, ()), reservation
        )
    assert len(fake.writes) == 1 and sink.state == "ok"


@pytest.mark.parametrize("failure", ["short", "fail_write", "fail_sync"])
def test_any_failed_write_raises_audit_unavailable_and_latches_with_no_further_write(failure):
    fake = FakeFd(**{failure: True})
    sink = fake.sink()
    reservation = sink.reserve("authorized")
    target = Target.full(MethodClass.GET, target_of("/user"), None)
    with pytest.raises(AuditUnavailable) as excinfo:
        sink.write_intent(authorized(3, target), reservation)
    assert (excinfo.value.kind, excinfo.value.seq, excinfo.value.hop) == (Kind.INTENT, 3, None)
    assert excinfo.value.failure() == AuditFailure(Kind.INTENT, 3, None)
    assert excinfo.value.failure().to_json() == {
        "event": "audit-failure",
        "kind": "intent",
        "seq": 3,
        "hop": None,
    }
    assert sink.state == "unavailable"
    writes_before = len(fake.writes)
    with pytest.raises(AuditUnavailable) as again:
        sink.write_terminal(TerminalRecord(TS, "agent-exit", False, False, False, 1, 0, 0, 1, 1))
    assert again.value.kind is Kind.TERMINAL and again.value.seq is None
    with pytest.raises(AuditUnavailable):
        sink.write_completion(CompletionRecord(3, TS, Outcome.ABORTED, None, 0, 0, ()), reservation)
    assert len(fake.writes) == writes_before
    assert sink.state == "unavailable"


def test_redirect_intent_failure_names_the_hop():
    fake = FakeFd(fail_sync=True)
    sink = fake.sink()
    reservation = sink.reserve("authorized")
    target = Target.full(MethodClass.GET, target_of("/user"), None)
    with pytest.raises(AuditUnavailable) as excinfo:
        sink.write_redirect_intent(RedirectIntentRecord(9, TS, 2, target), reservation)
    assert (excinfo.value.kind, excinfo.value.seq, excinfo.value.hop) == (
        Kind.REDIRECT_INTENT,
        9,
        2,
    )


def test_only_the_full_form_authorizes():
    sink = FakeFd().sink()
    target = target_of("/user")
    digest = Target.digest(MethodClass.GET, target, None)
    with pytest.raises(ValueError):
        sink.write_intent(authorized(1, digest), sink.reserve("authorized"))
    with pytest.raises(ValueError):
        sink.write_redirect_intent(
            RedirectIntentRecord(1, TS, 1, digest), sink.reserve("authorized")
        )
    invalid = Target.invalid(MethodClass.GET, None, None, Stage.PATH, 1, SHA)
    with pytest.raises(ValueError):
        sink.write_intent(authorized(1, invalid), sink.reserve("authorized"))
    assert sink.state == "ok"


def test_sequence_numbers_are_dense_and_one_based():
    sink = FakeFd().sink()
    assert [sink.next_seq() for _ in range(5)] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------- the parser


def test_parse_records_reports_a_torn_tail_without_raising():
    good = serialize(authorized(1, Target.full(MethodClass.GET, target_of("/user"), None)))
    completion = serialize(CompletionRecord(1, TS, Outcome.DELIVERED, 200, 1, 1, ()))
    torn = completion[:-10]
    result = parse_records(good + torn)
    assert result.counts_by_kind == {"intent": 1}
    assert (result.unparseable_offset, result.unparseable_length) == (len(good), len(torn))
    assert result.terminal == "missing"
    garbage = parse_records(good + b"not json\n" + completion)
    assert garbage.counts_by_kind == {"intent": 1}
    assert (garbage.unparseable_offset, garbage.unparseable_length) == (
        len(good),
        len(b"not json\n" + completion),
    )
    unknown = parse_records(good + b'{"kind":"other"}\n')
    assert unknown.unparseable_offset == len(good)
    missing_field = parse_records(b'{"kind":"intent","ts":"x"}\n')
    assert missing_field.records == [] and missing_field.unparseable_offset == 0
    assert parse_records(b"") == parse_records(b"")
    assert parse_records(b"").terminal == "missing" and parse_records(b"").records == []
    assert parse_records(b"\xff\xfe\n").unparseable_offset == 0


def test_parse_records_classifies_the_terminal_record():
    terminal = serialize(TerminalRecord(TS, "agent-exit", False, False, False, 1, 0, 0, 1, 1))
    present = parse_records(terminal)
    assert present.terminal == "present" and present.counts_by_kind == {"terminal": 1}
    malformed = parse_records(terminal[:-5])
    assert malformed.terminal == "malformed"
    assert (malformed.unparseable_offset, malformed.unparseable_length) == (0, len(terminal) - 5)
    intent = serialize(authorized(1, Target.full(MethodClass.GET, target_of("/user"), None)))
    assert parse_records(intent + terminal[:-1]).terminal == "malformed"
    assert parse_records(intent).terminal == "missing"
    assert parse_records(intent + terminal).terminal == "present"
