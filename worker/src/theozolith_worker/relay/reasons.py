"""The GitHub Relay's closed vocabularies and per-Run budgets (ADR-0057).

Every value here is a contract shared by the relay's policy core, its
transport, and the audit record schema: a refusal reason, a stage, a record
kind, an outcome, a redirect classification, or a budget constant. The audit
serializer emits these strings verbatim, so each stays at most 32 bytes (the
serialized-bound class of ADR-0057 item 8) and the sets never grow silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Reason(StrEnum):
    """Every stable refusal reason code the relay can record or answer with.

    The whole closed set lives here so the audit serializer has one source of
    truth; the redirect, gate, upstream, and concurrency codes exist for the
    transport's consumption and are never produced by the policy core.
    """

    AUDIT_UNREPRESENTABLE = "audit-unrepresentable"
    AUDIT_UNAVAILABLE = "audit-unavailable"
    AUDIT_BUDGET = "audit-budget"
    NO_UPSTREAM = "no-upstream"
    MUTATION = "mutation"
    ADMIN_READ = "admin-read"
    GRAPHQL_UNPARSEABLE = "graphql-unparseable"
    GRAPHQL_MULTI_OPERATION = "graphql-multi-operation"
    GRAPHQL_NON_QUERY = "graphql-non-query"
    REQUEST_LINE = "request-line"
    VERSION = "version"
    TARGET_FORM = "target-form"
    METHOD = "method"
    PATH = "path"
    QUERY = "query"
    HEADERS = "headers"
    FRAMING = "framing"
    BODY = "body"
    BUDGET_REQUESTS = "budget-requests"
    BUDGET_REQUEST_BYTES = "budget-request-bytes"
    BUDGET_RESPONSE_BYTES = "budget-response-bytes"
    BUDGET_CONCURRENCY = "budget-concurrency"
    REDIRECT_GRAPHQL = "redirect-graphql"
    REDIRECT_METHOD = "redirect-method"
    REDIRECT_ORIGIN = "redirect-origin"
    REDIRECT_DENYLIST = "redirect-denylist"
    REDIRECT_LOCATION = "redirect-location"
    REDIRECT_LOOP = "redirect-loop"
    REDIRECT_HOPS = "redirect-hops"
    REDIRECT_BUDGET = "redirect-budget"
    GATE_RESPONSE_BYTES = "gate-response-bytes"
    GATE_AGGREGATE = "gate-aggregate"
    CONTENT_ENCODING = "content-encoding"
    UPSTREAM_TIMEOUT = "upstream-timeout"
    UPSTREAM_ERROR = "upstream-error"
    ABORTED = "aborted"


class MethodClass(StrEnum):
    """The closed method classification every record carries: a known token
    by name, anything else as ``other`` with its length and digest recorded
    beside it, so no record ever holds an unbounded method string."""

    GET = "GET"
    HEAD = "HEAD"
    POST = "POST"
    CONNECT = "CONNECT"
    OPTIONS = "OPTIONS"
    TRACE = "TRACE"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    other = "other"


class Stage(StrEnum):
    """The request-line validation stages, in the fixed order the ingress
    parser applies them; an invalid-form record names the one that refused."""

    REQUEST_LINE = "request-line"
    VERSION = "version"
    TARGET_FORM = "target-form"
    METHOD = "method"
    PATH = "path"
    QUERY = "query"


class Kind(StrEnum):
    INTENT = "intent"
    REDIRECT_INTENT = "redirect-intent"
    COMPLETION = "completion"
    TERMINAL = "terminal"


class Outcome(StrEnum):
    DELIVERED = "delivered"
    REFUSED_GATE = "refused-gate"
    REFUSED_REDIRECT = "refused-redirect"
    TIMEOUT = "timeout"
    UPSTREAM_ERROR = "upstream-error"
    ABORTED = "aborted"


class Scheme(StrEnum):
    """A redirect ``Location``'s scheme as a closed classification."""

    HTTPS = "https"
    HTTP = "http"
    OTHER = "other"
    INVALID = "invalid"
    ABSENT = "absent"


class HostStatus(StrEnum):
    """A redirect ``Location``'s host validity: a literal is recorded only
    for ``valid``; the other statuses carry a length and digest at most."""

    VALID = "valid"
    OVERSIZED = "oversized"
    INVALID = "invalid"
    ABSENT = "absent"


class Decision(StrEnum):
    AUTHORIZED = "authorized"
    REFUSED = "refused"


class RedirectDecision(StrEnum):
    FOLLOWED = "followed"
    REFUSED = "refused"


@dataclass(frozen=True)
class Budgets:
    """The relay's per-Run limits (ADR-0057 items 6 and 8), byte values as
    ints. The ingress limits bound the parser; ``record_cap`` and
    ``file_cap`` bound the audit sink; they are deliberately independent."""

    connection_budget: int = 4000
    request_budget: int = 2000
    concurrency: int = 4
    open_connections: int = 8
    request_body_limit: int = 1 * 1024 * 1024
    response_body_limit: int = 16 * 1024 * 1024
    aggregate_request_bytes: int = 32 * 1024 * 1024
    aggregate_response_bytes: int = 256 * 1024 * 1024
    upstream_timeout: float = 30.0
    redirect_hops: int = 3
    head_read_seconds: float = 10.0
    body_read_seconds: float = 30.0
    record_cap: int = 4096
    file_cap: int = 16 * 1024 * 1024
    query_pairs: int = 32
    request_line_limit: int = 8 * 1024
    path_limit: int = 4 * 1024
    query_limit: int = 4 * 1024
    header_count: int = 64
    headers_total: int = 16 * 1024
    header_field: int = 8 * 1024


DEFAULT_BUDGETS = Budgets()
