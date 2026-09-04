"""The relay's closed vocabularies and budget constants (ADR-0057)."""

from __future__ import annotations

from enum import Enum

from theozolith_worker.relay import reasons
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

ALL_ENUMS = (
    Reason,
    MethodClass,
    Stage,
    Kind,
    Outcome,
    Scheme,
    HostStatus,
    Decision,
    RedirectDecision,
)

# The closed reason set of ADR-0057, one code per line of the contract.
REASON_CODES = {
    "audit-unrepresentable",
    "audit-unavailable",
    "audit-budget",
    "no-upstream",
    "mutation",
    "admin-read",
    "graphql-unparseable",
    "graphql-multi-operation",
    "graphql-non-query",
    "request-line",
    "version",
    "target-form",
    "method",
    "path",
    "query",
    "headers",
    "framing",
    "body",
    "budget-requests",
    "budget-request-bytes",
    "budget-response-bytes",
    "budget-concurrency",
    "redirect-graphql",
    "redirect-method",
    "redirect-origin",
    "redirect-denylist",
    "redirect-location",
    "redirect-loop",
    "redirect-hops",
    "redirect-budget",
    "gate-response-bytes",
    "gate-aggregate",
    "content-encoding",
    "upstream-timeout",
    "upstream-error",
    "aborted",
}


def test_every_member_is_a_str_enum_value_at_most_32_bytes():
    # ADR-0057 item 8: a closed-enum value is one of the fixed-width classes
    # the serialized bound is derived from.
    for enum in ALL_ENUMS:
        assert issubclass(enum, str) and issubclass(enum, Enum)
        for member in enum:
            assert isinstance(member.value, str)
            assert len(member.value.encode("ascii")) <= 32, (enum, member)


def test_reason_is_exactly_the_closed_set():
    assert {member.value for member in Reason} == REASON_CODES
    assert len(Reason) == len(REASON_CODES)
    # Member identifiers use underscores; values carry the hyphenated code.
    assert Reason.GRAPHQL_UNPARSEABLE.value == "graphql-unparseable"
    assert Reason.AUDIT_UNREPRESENTABLE == "audit-unrepresentable"
    assert Reason("request-line") is Reason.REQUEST_LINE


def test_method_classification_is_closed_with_other_last():
    assert [member.value for member in MethodClass] == [
        "GET",
        "HEAD",
        "POST",
        "CONNECT",
        "OPTIONS",
        "TRACE",
        "PUT",
        "PATCH",
        "DELETE",
        "other",
    ]
    assert MethodClass.other.value == "other"


def test_stage_order_is_the_validation_order():
    assert [member.value for member in Stage] == [
        "request-line",
        "version",
        "target-form",
        "method",
        "path",
        "query",
    ]


def test_record_kinds_outcomes_and_decisions():
    assert [k.value for k in Kind] == ["intent", "redirect-intent", "completion", "terminal"]
    assert [o.value for o in Outcome] == [
        "delivered",
        "refused-gate",
        "refused-redirect",
        "timeout",
        "upstream-error",
        "aborted",
    ]
    assert [d.value for d in Decision] == ["authorized", "refused"]
    assert [d.value for d in RedirectDecision] == ["followed", "refused"]


def test_redirect_classifications():
    assert [s.value for s in Scheme] == ["https", "http", "other", "invalid", "absent"]
    assert [h.value for h in HostStatus] == ["valid", "oversized", "invalid", "absent"]


def test_budget_defaults_are_the_relay_constants():
    assert Budgets() == DEFAULT_BUDGETS
    assert (
        Budgets(
            connection_budget=4000,
            request_budget=2000,
            concurrency=4,
            open_connections=8,
            request_body_limit=1 * 1024 * 1024,
            response_body_limit=16 * 1024 * 1024,
            aggregate_request_bytes=32 * 1024 * 1024,
            aggregate_response_bytes=256 * 1024 * 1024,
            upstream_timeout=30.0,
            redirect_hops=3,
            head_read_seconds=10.0,
            body_read_seconds=30.0,
            record_cap=4096,
            file_cap=16 * 1024 * 1024,
            query_pairs=32,
            request_line_limit=8 * 1024,
            path_limit=4 * 1024,
            query_limit=4 * 1024,
            header_count=64,
            headers_total=16 * 1024,
            header_field=8 * 1024,
        )
        == DEFAULT_BUDGETS
    )
    assert DEFAULT_BUDGETS.request_body_limit == 1048576
    assert DEFAULT_BUDGETS.file_cap == 16777216


def test_budgets_are_frozen_and_the_module_exports_every_name():
    try:
        DEFAULT_BUDGETS.record_cap = 1  # type: ignore[misc]
        raise AssertionError("Budgets must be frozen")
    except AttributeError:
        pass
    for name in (
        "Reason",
        "MethodClass",
        "Stage",
        "Kind",
        "Outcome",
        "Scheme",
        "HostStatus",
        "Decision",
        "RedirectDecision",
        "Budgets",
        "DEFAULT_BUDGETS",
    ):
        assert hasattr(reasons, name)
