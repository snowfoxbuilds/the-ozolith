"""The GitHub Relay's write-ahead audit representation and sink (ADR-0057 item 8).

Every record kind has an explicit schema, serialized and measured before it
authorizes anything, redacted by construction: a target is recorded in one of
three tagged forms (full, digest, invalid), a method as a closed
classification, a redirect host as a validity status with a literal only when
valid, and never a credential, header, body, raw request-target, or byte the
client or upstream chose. The sink is append-only over one descriptor with
a single write plus ``fdatasync`` per record, room reserved before any record
is written, and one failure latching it unavailable for good.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from theozolith_worker.relay.classify import GraphQLClassification
from theozolith_worker.relay.ingress import (
    CanonicalTarget,
    canonical_query,
    percent_encode,
    sha256_hex,
)
from theozolith_worker.relay.reasons import (
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

RELAY_PARENT = ".relay"
SINK_NAME = "gh-audit.jsonl"
SUMMARY_NAME = "gh-audit.summary.json"
SPOOL_DIR = "spool"

FORM_FULL = "full"
FORM_DIGEST = "digest"
FORM_INVALID = "invalid"

STATE_OK = "ok"
STATE_BUDGET_EXHAUSTED = "budget-exhausted"
STATE_UNAVAILABLE = "unavailable"

TERMINAL_PRESENT = "present"
TERMINAL_MISSING = "missing"
TERMINAL_MALFORMED = "malformed"

# The routing parameters whose literal values a full-form record may carry,
# under a fixed printable-ASCII cap; every other value is length and digest.
LITERAL_QUERY_NAMES = frozenset({"page", "per_page", "state", "sort", "direction"})
LITERAL_VARIABLE_NAMES = frozenset({"owner", "name", "repo", "number", "first", "last", "states"})
LITERAL_VALUE_CAP = 256
OPERATION_NAME_CAP = 128

# Records reserved per request: one for a refusal; an intent, three
# redirect-intents, and a completion for an authorized request.
RESERVATION_RECORDS = {"refusal": 1, "authorized": 5}

_TERMINAL_PREFIX = b'{"kind":"terminal"'
MANDATORY_FIELDS = {
    Kind.INTENT.value: ("ts", "seq", "decision", "reason", "target"),
    Kind.REDIRECT_INTENT.value: ("ts", "seq", "hop", "decision", "target"),
    Kind.COMPLETION.value: (
        "ts",
        "seq",
        "outcome",
        "status",
        "request_bytes",
        "response_bytes",
        "redirects",
    ),
    Kind.TERMINAL.value: (
        "ts",
        "reason",
        "connection_budget_exhausted",
        "request_budget_exhausted",
        "audit_budget_exhausted",
        "accepted",
        "busy_refused",
        "no_request",
        "requests_seen",
        "requests_charged",
    ),
}


def format_ts(epoch: float) -> str:
    """RFC 3339 UTC with always six microsecond digits: 27 bytes."""
    seconds = int(epoch)
    micros = min(max(int((epoch - seconds) * 1_000_000), 0), 999_999)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{micros:06d}Z"


def _printable_ascii(data: bytes) -> bool:
    return all(0x20 <= byte <= 0x7E for byte in data)


def _query_entries(target: CanonicalTarget) -> list[dict]:
    entries = []
    for pair in target.query:
        value = b"" if pair.value is None else pair.value
        entry: dict = {
            "name": percent_encode(pair.name.encode("latin-1")),
            "len": len(value),
            "sha256": sha256_hex(value),
        }
        if (
            pair.value is not None
            and pair.name in LITERAL_QUERY_NAMES
            and len(pair.value) < LITERAL_VALUE_CAP
            and _printable_ascii(pair.value)
        ):
            entry["value"] = pair.value.decode("ascii")
        entries.append(entry)
    return entries


def _graphql_full(graphql: GraphQLClassification | None) -> dict | None:
    if graphql is None:
        return None
    if not graphql.parsed:
        return {"parsed": False}
    out: dict = {"parsed": True, "op_type": graphql.operation_type}
    if graphql.operation_name is None:
        out["op_name"] = None
    else:
        name = graphql.operation_name.encode("utf-8")
        if len(name) <= OPERATION_NAME_CAP:
            out["op_name"] = graphql.operation_name
        else:
            out["op_name_len"] = len(name)
            out["op_name_sha256"] = sha256_hex(name)
    variables = []
    for variable in graphql.variables:
        entry: dict = {
            "name": variable.name,
            "type": variable.json_type,
            "len": len(variable.canonical),
            "sha256": sha256_hex(variable.canonical),
        }
        if variable.name in LITERAL_VARIABLE_NAMES and len(variable.canonical) < LITERAL_VALUE_CAP:
            entry["value"] = json.loads(variable.canonical)
        variables.append(entry)
    out["variables"] = variables
    return out


def _graphql_digest(graphql: GraphQLClassification | None) -> dict | None:
    if graphql is None:
        return None
    if not graphql.parsed:
        return {"parsed": False}
    encoded = b"".join(variable.canonical for variable in graphql.variables)
    return {
        "parsed": True,
        "op_type": graphql.operation_type,
        "variables_count": len(graphql.variables),
        "variables_sha256": sha256_hex(encoded),
    }


@dataclass(frozen=True)
class Target:
    """The tagged target representation, total over every seen request line.
    Build one through ``full``, ``digest``, or ``invalid``; ``to_json``
    renders the form's exact key order."""

    form: str
    method: MethodClass
    method_len: int | None = None
    method_sha256: str | None = None
    canonical: CanonicalTarget | None = None
    graphql: GraphQLClassification | None = None
    stage: Stage | None = None
    raw_len: int | None = None
    raw_sha256: str | None = None

    @classmethod
    def full(
        cls,
        method: MethodClass,
        target: CanonicalTarget,
        graphql: GraphQLClassification | None,
        *,
        method_len: int | None = None,
        method_sha256: str | None = None,
    ) -> Target:
        return cls(FORM_FULL, method, method_len, method_sha256, target, graphql)

    @classmethod
    def digest(
        cls,
        method: MethodClass,
        target: CanonicalTarget,
        graphql: GraphQLClassification | None,
        *,
        method_len: int | None = None,
        method_sha256: str | None = None,
    ) -> Target:
        return cls(FORM_DIGEST, method, method_len, method_sha256, target, graphql)

    @classmethod
    def invalid(
        cls,
        method: MethodClass,
        method_len: int | None,
        method_sha256: str | None,
        stage: Stage,
        raw_len: int,
        raw_sha256: str,
    ) -> Target:
        return cls(
            FORM_INVALID,
            method,
            method_len,
            method_sha256,
            stage=stage,
            raw_len=raw_len,
            raw_sha256=raw_sha256,
        )

    def to_json(self) -> dict:
        out: dict = {"form": self.form, "method": self.method.value}
        if self.method is MethodClass.other:
            out["method_len"] = self.method_len
            out["method_sha256"] = self.method_sha256
        if self.form == FORM_FULL:
            assert self.canonical is not None
            out["path"] = self.canonical.path
            out["query"] = _query_entries(self.canonical)
            out["graphql"] = _graphql_full(self.graphql)
        elif self.form == FORM_DIGEST:
            assert self.canonical is not None
            path = self.canonical.path.encode("ascii")
            query = canonical_query(self.canonical.query).encode("ascii")
            out["path_len"] = len(path)
            out["path_sha256"] = sha256_hex(path)
            out["query_pairs"] = len(self.canonical.query)
            out["query_len"] = len(query)
            out["query_sha256"] = sha256_hex(query)
            out["graphql"] = _graphql_digest(self.graphql)
        else:
            assert self.stage is not None
            out["stage"] = self.stage.value
            out["target_len"] = self.raw_len
            out["target_sha256"] = self.raw_sha256
            out["graphql"] = None
        return out


@dataclass(frozen=True)
class HostRepr:
    """A redirect ``Location``'s host: a literal only when valid, a length
    and digest when oversized or invalid, nothing when absent. No port,
    user-info, path, query, or fragment field exists by construction."""

    status: HostStatus
    value: str | None = None
    length: int | None = None
    sha256: str | None = None

    def to_json(self) -> dict:
        out: dict = {"status": self.status.value}
        if self.status is HostStatus.VALID:
            out["value"] = self.value
        elif self.status in (HostStatus.OVERSIZED, HostStatus.INVALID):
            out["len"] = self.length
            out["sha256"] = self.sha256
        return out


@dataclass(frozen=True)
class RedirectEntry:
    hop: int
    status: int
    decision: RedirectDecision
    reason: Reason | None
    scheme: Scheme
    host: HostRepr

    def to_json(self) -> dict:
        return {
            "hop": self.hop,
            "status": self.status,
            "decision": self.decision.value,
            "reason": None if self.reason is None else self.reason.value,
            "scheme": self.scheme.value,
            "host": self.host.to_json(),
        }


@dataclass(frozen=True)
class ReservedBudgets:
    request_bytes: int
    response_bytes: int
    audit_bytes: int

    def to_json(self) -> dict:
        return {
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "audit_bytes": self.audit_bytes,
        }


@dataclass(frozen=True)
class IntentRecord:
    seq: int
    ts: str
    decision: Decision
    reason: Reason | None
    target: Target
    budgets: ReservedBudgets | None

    kind = Kind.INTENT

    def to_json(self) -> dict:
        out = {
            "kind": self.kind.value,
            "ts": self.ts,
            "seq": self.seq,
            "decision": self.decision.value,
            "reason": None if self.reason is None else self.reason.value,
            "target": self.target.to_json(),
        }
        if self.decision is Decision.AUTHORIZED:
            out["budgets"] = None if self.budgets is None else self.budgets.to_json()
        return out


@dataclass(frozen=True)
class RedirectIntentRecord:
    seq: int
    ts: str
    hop: int
    target: Target

    kind = Kind.REDIRECT_INTENT

    def to_json(self) -> dict:
        return {
            "kind": self.kind.value,
            "ts": self.ts,
            "seq": self.seq,
            "hop": self.hop,
            "decision": Decision.AUTHORIZED.value,
            "target": self.target.to_json(),
        }


@dataclass(frozen=True)
class CompletionRecord:
    seq: int
    ts: str
    outcome: Outcome
    status: int | None
    request_bytes: int
    response_bytes: int
    redirects: tuple[RedirectEntry, ...]

    kind = Kind.COMPLETION

    def to_json(self) -> dict:
        return {
            "kind": self.kind.value,
            "ts": self.ts,
            "seq": self.seq,
            "outcome": self.outcome.value,
            "status": self.status,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "redirects": [entry.to_json() for entry in self.redirects],
        }


@dataclass(frozen=True)
class TerminalRecord:
    ts: str
    reason: str
    connection_budget_exhausted: bool
    request_budget_exhausted: bool
    audit_budget_exhausted: bool
    accepted: int
    busy_refused: int
    no_request: int
    requests_seen: int
    requests_charged: int

    kind = Kind.TERMINAL

    def to_json(self) -> dict:
        return {
            "kind": self.kind.value,
            "ts": self.ts,
            "reason": self.reason,
            "connection_budget_exhausted": self.connection_budget_exhausted,
            "request_budget_exhausted": self.request_budget_exhausted,
            "audit_budget_exhausted": self.audit_budget_exhausted,
            "accepted": self.accepted,
            "busy_refused": self.busy_refused,
            "no_request": self.no_request,
            "requests_seen": self.requests_seen,
            "requests_charged": self.requests_charged,
        }


Record = IntentRecord | RedirectIntentRecord | CompletionRecord | TerminalRecord


def serialize(record: Record) -> bytes:
    """One ASCII-only line: invalid UTF-8 and control bytes are escaped by
    ``ensure_ascii``, and nothing is ever truncated."""
    line = json.dumps(record.to_json(), separators=(",", ":"), ensure_ascii=True)
    return line.encode("ascii") + b"\n"


def fits(line: bytes, budgets: Budgets) -> bool:
    return len(line) <= budgets.record_cap


@dataclass(frozen=True)
class AuditFailure:
    """The audit-failure report shape the transport emits to the driver."""

    kind: Kind
    seq: int | None
    hop: int | None

    def to_json(self) -> dict:
        return {"event": "audit-failure", "kind": self.kind.value, "seq": self.seq, "hop": self.hop}


class AuditUnavailable(Exception):
    """A failed sink write: the record kind, request sequence, and hop that
    failed — never a path, query, or byte of the record."""

    def __init__(self, kind: Kind, seq: int | None, hop: int | None):
        super().__init__(f"audit write failed: kind={kind.value} seq={seq} hop={hop}")
        self.kind = kind
        self.seq = seq
        self.hop = hop

    def failure(self) -> AuditFailure:
        return AuditFailure(self.kind, self.seq, self.hop)


class SinkExistsError(OSError):
    """An entry already exists at the sink path (a symlink included)."""


@dataclass
class Reservation:
    """Room held against the file cap for one request's records; the unused
    remainder returns to the pool only through ``AuditSink.release``."""

    kind: str
    size: int
    used: int = 0
    released: bool = False


class AuditSink:
    """Append-only writer over one descriptor; the pathname is never
    reopened. The terminal record's room is reserved at construction and
    never released. State: ``ok`` → ``budget-exhausted`` when a reservation
    would cross the file cap, ``unavailable`` after any failed write (no
    further write of any kind)."""

    def __init__(
        self,
        fd: int,
        budgets: Budgets,
        *,
        clock=time.time,
        _write=os.write,
        _fdatasync=os.fdatasync,
    ):
        self.fd = fd
        self.budgets = budgets
        self.clock = clock
        self._write = _write
        self._fdatasync = _fdatasync
        self.state = STATE_OK
        self._seq = 0
        self._terminal_room = budgets.record_cap
        self._committed = self._terminal_room
        self._written = 0
        self._terminal_written = False

    @property
    def bytes_written(self) -> int:
        return self._written

    @property
    def bytes_committed(self) -> int:
        """Terminal room plus every live reservation plus what released
        reservations actually wrote: the file can never grow past this."""
        return self._committed

    def now(self) -> str:
        return format_ts(self.clock())

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def reserve(self, kind: str) -> Reservation | None:
        size = RESERVATION_RECORDS[kind] * self.budgets.record_cap
        if self._committed + size > self.budgets.file_cap:
            self.state = STATE_BUDGET_EXHAUSTED
            return None
        self._committed += size
        return Reservation(kind, size)

    def release(self, reservation: Reservation) -> None:
        if reservation.released:
            return
        reservation.released = True
        self._committed -= reservation.size - reservation.used

    def intent_for(self, full: IntentRecord) -> IntentRecord:
        """``full`` when it fits the record cap; otherwise the digest-form
        record — an ``audit-unrepresentable`` refusal when the input was
        authorized, the same refusal under its own reason otherwise."""
        if full.target.form != FORM_FULL or fits(serialize(full), self.budgets):
            return full
        assert full.target.canonical is not None
        digest = Target.digest(
            full.target.method,
            full.target.canonical,
            full.target.graphql,
            method_len=full.target.method_len,
            method_sha256=full.target.method_sha256,
        )
        if full.decision is Decision.AUTHORIZED:
            return IntentRecord(
                full.seq, full.ts, Decision.REFUSED, Reason.AUDIT_UNREPRESENTABLE, digest, None
            )
        return IntentRecord(full.seq, full.ts, full.decision, full.reason, digest, None)

    def _append(
        self,
        kind: Kind,
        line: bytes,
        seq: int | None,
        hop: int | None,
        reservation: Reservation | None,
    ) -> None:
        if self.state == STATE_UNAVAILABLE:
            raise AuditUnavailable(kind, seq, hop)
        if not fits(line, self.budgets):
            raise ValueError(f"{kind.value} record exceeds the record cap and must not be written")
        if reservation is not None:
            if reservation.released:
                raise ValueError("reservation already released")
            if reservation.used + len(line) > reservation.size:
                raise ValueError(f"{kind.value} record exceeds its reservation")
        try:
            written = self._write(self.fd, line)
            if written != len(line):
                raise OSError(f"short write: {written} of {len(line)} bytes")
            self._fdatasync(self.fd)
        except OSError as exc:
            self.state = STATE_UNAVAILABLE
            raise AuditUnavailable(kind, seq, hop) from exc
        self._written += len(line)
        if reservation is not None:
            reservation.used += len(line)

    def write_intent(self, record: IntentRecord, reservation: Reservation) -> None:
        if record.decision is Decision.AUTHORIZED and record.target.form != FORM_FULL:
            raise ValueError("only a full-form target can authorize")
        self._append(Kind.INTENT, serialize(record), record.seq, None, reservation)

    def write_redirect_intent(self, record: RedirectIntentRecord, reservation: Reservation) -> None:
        if record.target.form != FORM_FULL:
            raise ValueError("a redirect-intent record carries the full form only")
        self._append(Kind.REDIRECT_INTENT, serialize(record), record.seq, record.hop, reservation)

    def write_completion(self, record: CompletionRecord, reservation: Reservation) -> None:
        self._append(Kind.COMPLETION, serialize(record), record.seq, None, reservation)

    def write_terminal(self, record: TerminalRecord) -> None:
        if self._terminal_written:
            raise ValueError("the terminal record is written at most once")
        self._append(Kind.TERMINAL, serialize(record), None, None, None)
        self._terminal_written = True


@dataclass(frozen=True)
class ParseResult:
    records: list[dict]
    counts_by_kind: dict[str, int]
    unparseable_offset: int | None
    unparseable_length: int | None
    terminal: str


def _parse_line(line: bytes) -> dict | None:
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    mandatory = MANDATORY_FIELDS.get(record.get("kind"))
    if mandatory is None or any(field not in record for field in mandatory):
        return None
    return record


def parse_records(data: bytes) -> ParseResult:
    """Line by line, never raising: the first line without its newline, or
    that is not a complete JSON object of a known kind with its mandatory
    fields, is the unparseable tail and nothing after it is parsed."""
    records: list[dict] = []
    counts: dict[str, int] = {}
    terminal = TERMINAL_MISSING
    offset = 0
    tail_offset: int | None = None
    while offset < len(data):
        newline = data.find(b"\n", offset)
        record = None if newline == -1 else _parse_line(data[offset:newline])
        if record is None:
            tail_offset = offset
            break
        records.append(record)
        counts[record["kind"]] = counts.get(record["kind"], 0) + 1
        if record["kind"] == Kind.TERMINAL.value:
            terminal = TERMINAL_PRESENT
        offset = newline + 1
    if tail_offset is None:
        return ParseResult(records, counts, None, None, terminal)
    if data[tail_offset:].startswith(_TERMINAL_PREFIX):
        terminal = TERMINAL_MALFORMED
    return ParseResult(records, counts, tail_offset, len(data) - tail_offset, terminal)


def relay_root(jobs_dir: str | Path) -> Path:
    return Path(jobs_dir) / RELAY_PARENT


def relay_dir(jobs_dir: str | Path, run_id: str) -> Path:
    if not run_id or "/" in run_id or run_id in (".", ".."):
        raise ValueError(f"run_id is not a single path segment: {run_id!r}")
    return relay_root(jobs_dir) / run_id


def create_relay_dir(jobs_dir: str | Path, run_id: str) -> Path:
    """The per-Run driver-owned directory, mode 0700, refusing any existing
    entry (``FileExistsError`` propagates; nothing is ever unlinked)."""
    path = relay_dir(jobs_dir, run_id)
    relay_root(jobs_dir).mkdir(mode=0o700, exist_ok=True)
    os.mkdir(path, 0o700)
    return path


def open_sink(relay_dir: str | Path) -> int:
    """Open the sink once, exclusively, never through a symlink; the returned
    descriptor is the only route every later write takes."""
    flags = os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_APPEND | os.O_WRONLY
    path = str(Path(relay_dir) / SINK_NAME)
    try:
        return os.open(path, flags, 0o600)
    except FileExistsError as exc:
        message = "an entry already exists at the audit sink path"
        raise SinkExistsError(exc.errno, message, path) from exc
