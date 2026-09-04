"""The GitHub Relay's hostile-ingress socket server (ADR-0057 items 6, 8, 10).

Any process in the run container can connect to the socket, so every byte
read from it is untrusted and every guarantee here holds without assuming
the client is ``gh``: one request per connection, bounded head and body read
times, a fixed pool of workers behind a bounded open-connection cap, and a
finite connection budget charged at accept. Per request the order is fixed —
parse, classify, reserve budgets and audit capacity, append and sync the
intent record — and only after that write returns does anything reach the
credential-holding upstream client. Acceptance ends exactly once, on agent
exit or connection-budget exhaustion — the first of the two to claim it
under the state lock, agent exit waking the acceptor through a pipe rather
than a poll — and one shutdown sequence follows.
"""

from __future__ import annotations

import contextlib
import http
import json
import os
import selectors
import signal
import socket
import threading
import time
from collections.abc import Callable
from typing import Protocol

from theozolith_worker.relay.audit import (
    STATE_UNAVAILABLE,
    AuditSink,
    AuditUnavailable,
    CompletionRecord,
    IntentRecord,
    RedirectIntentRecord,
    Reservation,
    ReservedBudgets,
    Target,
    TerminalRecord,
    fits,
    serialize,
)
from theozolith_worker.relay.classify import GRAPHQL_PATH, classify_graphql, classify_rest
from theozolith_worker.relay.ingress import (
    CanonicalTarget,
    IngressRefusal,
    NoRequestLine,
    ParsedRequest,
    build_upstream_request,
    read_request,
)
from theozolith_worker.relay.reasons import Budgets, Decision, MethodClass, Outcome, Reason
from theozolith_worker.relay.upstream import (
    USER_AGENT,
    AggregateBudget,
    SpoolHandle,
    UpstreamClient,
    UpstreamResult,
    discard_spool,
    request_size,
    response_reservation,
)

REASON_AGENT_EXIT = "agent-exit"
REASON_CONNECTION_BUDGET = "connection-budget-exhausted"

ACCEPT_POLL_SECONDS = 0.1
DRAIN_POLL_SECONDS = 0.05
BUSY_SEND_SECONDS = 1.0
_RECV_CHUNK = 64 * 1024
_HEAD_END = b"\r\n\r\n"

MUTATION_MESSAGE = (
    "Workers never write to GitHub. Everything you want published goes through"
    " your Output Proposal — run `format-output status`."
)
BUDGET_MESSAGE = (
    "GitHub Relay: {budget} exhausted for this Run; further `gh` calls are refused."
    " Your prompt carries the task; `format-output status` shows your proposal."
)
NO_UPSTREAM_MESSAGE = (
    "This benchmark run has no GitHub upstream; the task is fully described in your prompt"
)

# Every reason's stable, credential-silent message. None of them says what
# the credential can or cannot do: the relay's policy is the only subject.
REFUSAL_MESSAGES: dict[Reason, str] = {
    Reason.MUTATION: MUTATION_MESSAGE,
    Reason.BUDGET_REQUESTS: BUDGET_MESSAGE.format(budget="request budget"),
    Reason.BUDGET_REQUEST_BYTES: BUDGET_MESSAGE.format(budget="request-byte budget"),
    Reason.BUDGET_RESPONSE_BYTES: BUDGET_MESSAGE.format(budget="response-byte budget"),
    Reason.AUDIT_BUDGET: BUDGET_MESSAGE.format(budget="audit budget"),
    Reason.NO_UPSTREAM: NO_UPSTREAM_MESSAGE,
    Reason.BUDGET_CONCURRENCY: (
        "GitHub Relay: too many open connections for this Run; retry once one has finished."
    ),
    Reason.ADMIN_READ: (
        "GitHub Relay: this endpoint is an administrative read and is not relayed."
    ),
    Reason.GRAPHQL_UNPARSEABLE: (
        "GitHub Relay: the GraphQL document could not be classified and was not relayed."
    ),
    Reason.GRAPHQL_MULTI_OPERATION: (
        "GitHub Relay: a GraphQL document with more than one operation is not relayed."
    ),
    Reason.GRAPHQL_NON_QUERY: "GitHub Relay: only GraphQL query operations are relayed.",
    Reason.REQUEST_LINE: "GitHub Relay: malformed request line.",
    Reason.VERSION: "GitHub Relay: only HTTP/1.1 is accepted.",
    Reason.TARGET_FORM: "GitHub Relay: the request-target must be origin-form.",
    Reason.METHOD: "GitHub Relay: only GET, HEAD, and POST are accepted.",
    Reason.PATH: "GitHub Relay: the request path is not in canonical form.",
    Reason.QUERY: "GitHub Relay: the query string is not in canonical form.",
    Reason.HEADERS: "GitHub Relay: malformed or over-limit request headers.",
    Reason.FRAMING: "GitHub Relay: the request framing is not accepted.",
    Reason.BODY: "GitHub Relay: the request body is not accepted.",
    Reason.REDIRECT_GRAPHQL: "GitHub Relay: a GraphQL redirect is never followed.",
    Reason.REDIRECT_METHOD: "GitHub Relay: the upstream redirect would change the method.",
    Reason.REDIRECT_ORIGIN: "GitHub Relay: the upstream redirect leaves api.github.com.",
    Reason.REDIRECT_DENYLIST: (
        "GitHub Relay: the upstream redirect targets an administrative read."
    ),
    Reason.REDIRECT_LOCATION: "GitHub Relay: the upstream redirect location is not accepted.",
    Reason.REDIRECT_LOOP: "GitHub Relay: the upstream redirect loops.",
    Reason.REDIRECT_HOPS: "GitHub Relay: the upstream redirect chain is too long.",
    Reason.REDIRECT_BUDGET: "GitHub Relay: a budget was exhausted during a redirect.",
    Reason.GATE_RESPONSE_BYTES: (
        "GitHub Relay: the upstream response exceeded the per-request response limit"
        " and was discarded."
    ),
    Reason.GATE_AGGREGATE: (
        "GitHub Relay: the response-byte budget for this Run cannot cover this response;"
        " it was discarded."
    ),
    Reason.CONTENT_ENCODING: (
        "GitHub Relay: the upstream response used a content encoding the relay does not deliver."
    ),
    Reason.UPSTREAM_TIMEOUT: "GitHub Relay: the upstream request timed out.",
    Reason.UPSTREAM_ERROR: "GitHub Relay: the upstream request failed.",
    Reason.ABORTED: "GitHub Relay: the request was aborted at agent exit.",
    Reason.AUDIT_UNREPRESENTABLE: (
        "GitHub Relay: this request cannot be recorded within the audit record limit"
        " and was refused."
    ),
    Reason.AUDIT_UNAVAILABLE: (
        "GitHub Relay: the audit sink is unavailable; every request is refused for the"
        " rest of this Run."
    ),
}

# The status class of every refusal the server decides itself; an ingress
# refusal answers with the status the parser assigned.
REFUSAL_STATUS: dict[Reason, int] = {
    Reason.REQUEST_LINE: 400,
    Reason.TARGET_FORM: 400,
    Reason.PATH: 400,
    Reason.QUERY: 400,
    Reason.HEADERS: 400,
    Reason.FRAMING: 400,
    Reason.BODY: 400,
    Reason.METHOD: 405,
    Reason.VERSION: 505,
    Reason.MUTATION: 403,
    Reason.ADMIN_READ: 403,
    Reason.GRAPHQL_UNPARSEABLE: 403,
    Reason.GRAPHQL_MULTI_OPERATION: 403,
    Reason.GRAPHQL_NON_QUERY: 403,
    Reason.NO_UPSTREAM: 403,
    Reason.BUDGET_REQUESTS: 429,
    Reason.BUDGET_REQUEST_BYTES: 429,
    Reason.BUDGET_RESPONSE_BYTES: 429,
    Reason.BUDGET_CONCURRENCY: 429,
    Reason.REDIRECT_GRAPHQL: 502,
    Reason.REDIRECT_METHOD: 502,
    Reason.REDIRECT_ORIGIN: 502,
    Reason.REDIRECT_DENYLIST: 502,
    Reason.REDIRECT_LOCATION: 502,
    Reason.REDIRECT_LOOP: 502,
    Reason.REDIRECT_HOPS: 502,
    Reason.REDIRECT_BUDGET: 502,
    Reason.GATE_RESPONSE_BYTES: 502,
    Reason.GATE_AGGREGATE: 502,
    Reason.CONTENT_ENCODING: 502,
    Reason.UPSTREAM_TIMEOUT: 502,
    Reason.UPSTREAM_ERROR: 502,
    Reason.ABORTED: 502,
    Reason.AUDIT_UNREPRESENTABLE: 503,
    Reason.AUDIT_UNAVAILABLE: 503,
    Reason.AUDIT_BUDGET: 503,
}


def _phrase(status: int) -> str:
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return ""


def refusal_response(status: int, reason: Reason) -> bytes:
    body = json.dumps(
        {"message": REFUSAL_MESSAGES[reason], "reason": reason.value},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    head = (
        f"HTTP/1.1 {status} {_phrase(status)}\r\n"
        "Connection: close\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    )
    return head.encode("ascii") + body


class _HeadBodyReader:
    """The ingress parser's reader over one connection, holding the 10 s
    head and 30 s body limits as deadlines: every receive is armed with what
    remains, and the body clock starts when the end-of-head sentinel passes
    through the byte stream. A timeout surfaces as ``TimeoutError``."""

    def __init__(self, conn: socket.socket, budgets: Budgets, clock=time.monotonic):
        self._conn = conn
        self._clock = clock
        self._body_seconds = budgets.body_read_seconds
        self._deadline = clock() + budgets.head_read_seconds
        self._in_head = True
        self._tail = b""
        self._buffer = bytearray()
        self._eof = False

    def _fill(self) -> bool:
        if self._eof:
            return False
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError
        self._conn.settimeout(remaining)
        try:
            chunk = self._conn.recv(_RECV_CHUNK)
        except TimeoutError:
            raise
        except OSError:
            chunk = b""  # a reset peer reads as a closed one
        if not chunk:
            self._eof = True
            return False
        if self._in_head:
            window = self._tail + chunk
            if _HEAD_END in window:
                self._in_head = False
                self._deadline = self._clock() + self._body_seconds
            else:
                self._tail = window[-(len(_HEAD_END) - 1) :]
        self._buffer += chunk
        return True

    def readline(self, size: int = -1, /) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            if newline != -1 and (size < 0 or newline < size):
                end = newline + 1
                break
            if size >= 0 and len(self._buffer) >= size:
                end = size
                break
            if not self._fill():
                end = len(self._buffer)
                break
        line = bytes(self._buffer[:end])
        del self._buffer[:end]
        return line

    def read(self, size: int = -1, /) -> bytes:
        if size < 0:
            while self._fill():
                pass
            size = len(self._buffer)
        elif not self._buffer and not self._fill():
            return b""
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


class Hooks(Protocol):
    """Crash-injection points at the write-ahead boundaries; every method is
    optional, and one that raises ends its request with nothing further
    written — the in-process stand-in for a relay crash at that point."""

    def after_intent_fdatasync(self, seq: int) -> None: ...

    def after_redirect_intent_fdatasync(self, seq: int, hop: int) -> None: ...

    def before_redirect_send(self, seq: int, hop: int) -> None: ...

    def after_redirect_send(self, seq: int, hop: int) -> None: ...

    def during_upstream(self, seq: int) -> None: ...

    def after_upstream_complete(self, seq: int) -> None: ...

    def listener_ready(self) -> None: ...


def _refusal_target(refusal: IngressRefusal) -> Target:
    if refusal.target is None:
        assert refusal.stage is not None
        return Target.invalid(
            refusal.method,
            refusal.method_len,
            refusal.method_sha256,
            refusal.stage,
            refusal.raw_target_len,
            refusal.raw_target_sha256,
        )
    return Target.full(refusal.method, refusal.target, None)


class _Relay:
    def __init__(
        self,
        listen_fd: int,
        sink_fd: int,
        *,
        upstream: UpstreamClient | None,
        budgets: Budgets,
        report,
        run_id: str,
        log: Callable[[str], None],
        sink: AuditSink | None,
        hooks: Hooks | None,
        agent_exit: threading.Event | None,
    ):
        self.listener = socket.socket(fileno=listen_fd)
        self.upstream = upstream
        self.budgets = budgets
        self.report = report
        self.run_id = run_id
        self.log = log
        self.sink = sink if sink is not None else AuditSink(sink_fd, budgets)
        self.hooks = hooks
        self.agent_exit = agent_exit if agent_exit is not None else threading.Event()
        self.aggregate = AggregateBudget(budgets)

        self.state_lock = threading.Lock()
        self.accepted = 0
        self.busy_refused = 0
        self.no_request = 0
        self.requests_seen = 0
        self.requests_charged = 0
        self.connection_budget_exhausted = False
        self.request_budget_exhausted = False
        self.audit_budget_exhausted = False
        self.audit_failure_reported = False

        self.open_slots = threading.BoundedSemaphore(budgets.open_connections)
        self.work = threading.Condition()
        self.queue: list[socket.socket] = []
        self.outstanding = 0
        self.stopping = False
        # Set the instant agent exit claims acceptance, read without the lock
        # by a worker about to serve a connection it popped just then.
        self.exiting = threading.Event()
        self.acceptance_over = threading.Event()
        # The arbitration variable: the first claim under state_lock wins.
        self.accept_reason: str | None = None
        self._wake_r, self._wake_w = os.pipe()
        self.report_lock = threading.Lock()

    # -- reporting -------------------------------------------------------

    def _report(self, event: dict) -> None:
        with self.report_lock:
            self.report.write(json.dumps(event, separators=(",", ":")) + "\n")
            self.report.flush()

    def _report_audit_failure(self, exc: AuditUnavailable) -> None:
        with self.report_lock:
            if self.audit_failure_reported:
                return
            self.audit_failure_reported = True
        self._report(exc.failure().to_json())

    def _hook(self, name: str, *args) -> None:
        fn = getattr(self.hooks, name, None) if self.hooks is not None else None
        if fn is not None:
            fn(*args)

    # -- acceptance ------------------------------------------------------

    def _claim_locked(self, reason: str) -> None:
        """Under state_lock: end acceptance for ``reason`` unless the other
        reason already did. An agent-exit claim also stops every worker
        from starting on a connection it has not read yet."""
        if self.accept_reason is None:
            self.accept_reason = reason
            if reason == REASON_AGENT_EXIT:
                self.exiting.set()

    def _watch_agent_exit(self) -> None:
        """Claim agent exit the moment the event is set — before the
        acceptor could accept another connection — abort every upstream
        exchange, then wake the acceptor out of its select."""
        self.agent_exit.wait()
        with self.state_lock:
            self._claim_locked(REASON_AGENT_EXIT)
        if self.upstream is not None:
            self.upstream.abort()
        os.write(self._wake_w, b"x")

    def _accept_loop(self) -> None:
        listener = self.listener
        listener.setblocking(False)
        selector = selectors.DefaultSelector()
        selector.register(listener, selectors.EVENT_READ)
        selector.register(self._wake_r, selectors.EVENT_READ)
        while True:
            ready = selector.select(timeout=ACCEPT_POLL_SECONDS)  # the poll is a backstop
            listener_ready = any(key.fileobj is listener for key, _ in ready)
            if listener_ready:
                self._hook("listener_ready")
            reason = self._accept_one(listener, listener_ready)
            if reason is not None:
                break
        selector.close()
        self._end_acceptance(reason)

    def _accept_one(self, listener: socket.socket, listener_ready: bool) -> str | None:
        """One arbitration step: agent exit, once set, wins before any
        accept; otherwise a ready listener's connection is accepted and
        charged, and the one that spends the last unit claims exhaustion —
        it was accepted, so it is served. The accept itself is outside the
        state lock so a worker is never blocked from freeing its slot while
        the acceptor holds it."""
        with self.state_lock:
            if self.agent_exit.is_set():
                self._claim_locked(REASON_AGENT_EXIT)
            reason = self.accept_reason
        if reason is not None:
            return reason
        if not listener_ready:
            return None
        try:
            conn, _ = listener.accept()
        except OSError:
            return None
        with self.state_lock:
            if self.accept_reason == REASON_AGENT_EXIT:
                # Agent exit claimed acceptance between the check and the
                # accept: this connection is abandoned unread, never counted
                # or served, like one still in the backlog at listener close.
                with contextlib.suppress(OSError):
                    conn.close()
                return self.accept_reason
            self.accepted += 1
            if self.accepted >= self.budgets.connection_budget:
                self.connection_budget_exhausted = True
                self._claim_locked(REASON_CONNECTION_BUDGET)
            reason = self.accept_reason
        if self.open_slots.acquire(blocking=False):
            with self.work:
                self.queue.append(conn)
                self.outstanding += 1
                self.work.notify()
        else:
            self._refuse_busy(conn)
        return reason

    def _refuse_busy(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(BUSY_SEND_SECONDS)
            conn.sendall(refusal_response(429, Reason.BUDGET_CONCURRENCY))
        except OSError:
            pass
        finally:
            conn.close()
        with self.state_lock:
            self.busy_refused += 1

    def _end_acceptance(self, reason: str) -> None:
        """Close the listener and unlink its path, so a later connect fails
        in the kernel and a connection still queued in the backlog is reset —
        no accept-and-refuse loop exists after this point."""
        try:
            path = self.listener.getsockname()
        except OSError:
            path = ""
        self.listener.close()
        if isinstance(path, str) and path:
            with contextlib.suppress(OSError):
                os.unlink(path)
        assert self.accept_reason == reason
        self.acceptance_over.set()

    # -- workers ---------------------------------------------------------

    def _next_connection(self) -> socket.socket | None:
        with self.work:
            while not self.queue:
                if self.stopping:
                    return None
                self.work.wait()
            return self.queue.pop(0)

    def _worker(self) -> None:
        while True:
            conn = self._next_connection()
            if conn is None:
                return
            try:
                self._handle(conn)
            except Exception as exc:  # one request's crash never takes the relay
                self.log(f"relay {self.run_id}: request handler failed: {type(exc).__name__}")
            finally:
                self._finish_connection(conn)

    def _finish_connection(self, conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            conn.close()
        self.open_slots.release()
        with self.work:
            self.outstanding -= 1
            self.work.notify_all()

    def _handle(self, conn: socket.socket) -> None:
        if self.exiting.is_set():
            # Popped after agent exit claimed acceptance: closed unread, as
            # if it had still been in the queue.
            with self.state_lock:
                self.no_request += 1
            return
        reader = _HeadBodyReader(conn, self.budgets)
        seen = read_request(reader, self.budgets)
        if isinstance(seen, NoRequestLine):
            with self.state_lock:
                self.no_request += 1
            return
        with self.state_lock:
            self.requests_seen += 1
            seq = self.sink.next_seq()
        self._serve_request(conn, seen, seq)

    # -- one request -----------------------------------------------------

    def _serve_request(self, conn: socket.socket, seen: ParsedRequest | IngressRefusal, seq: int):
        budgets = self.budgets
        parsed: ParsedRequest | None = None
        upstream_request = None
        if isinstance(seen, IngressRefusal):
            decision, reason = Decision.REFUSED, seen.reason
            target = _refusal_target(seen)
        else:
            parsed = seen
            graphql = None
            if parsed.method is MethodClass.POST and parsed.target.path == GRAPHQL_PATH:
                graphql = classify_graphql(parsed.body)
                reason = graphql.refusal
            else:
                reason = classify_rest(parsed.method, parsed.target.path)
            if reason is None and self.upstream is None:
                reason = Reason.NO_UPSTREAM
            decision = Decision.REFUSED if reason is not None else Decision.AUTHORIZED
            target = Target.full(parsed.method, parsed.target, graphql)

        with self.state_lock:
            if self.requests_charged < budgets.request_budget:
                self.requests_charged += 1
            else:
                self.request_budget_exhausted = True
                if decision is Decision.AUTHORIZED:
                    decision, reason = Decision.REFUSED, Reason.BUDGET_REQUESTS
            if decision is Decision.AUTHORIZED:
                assert parsed is not None
                upstream_request = build_upstream_request(
                    parsed, authorization=None, user_agent=USER_AGENT
                )
                if not self.aggregate.charge_request(request_size(upstream_request)):
                    decision, reason = Decision.REFUSED, Reason.BUDGET_REQUEST_BYTES
                elif self.aggregate.response_remaining < response_reservation(budgets):
                    decision, reason = Decision.REFUSED, Reason.BUDGET_RESPONSE_BYTES
            early = self._reserve_and_record(seq, decision, reason, target)
        if isinstance(early, Reason):
            self._answer(conn, 503, early)
            return
        record, reservation = early
        self._hook("after_intent_fdatasync", seq)

        if record.decision is Decision.REFUSED:
            assert record.reason is not None
            if isinstance(seen, IngressRefusal) and record.reason is seen.reason:
                status = seen.status
            else:
                status = REFUSAL_STATUS[record.reason]
            with self.state_lock:
                self.sink.release(reservation)
            self._answer(conn, status, record.reason)
            return

        assert parsed is not None and upstream_request is not None and self.upstream is not None
        method = parsed.method

        def authorize_hop(hop: int, hop_target: CanonicalTarget) -> Reason | None:
            return self._authorize_hop(seq, reservation, method, hop, hop_target)

        def observe(point: str, hop: int) -> None:
            if point == "during_upstream":
                self._hook(point, seq)
            else:
                self._hook(point, seq, hop)

        result = self.upstream.send(
            upstream_request,
            method_class=method,
            canonical_target=parsed.target,
            authorize_hop=authorize_hop,
            aggregate=self.aggregate,
            observe=observe,
        )
        try:
            self._hook("after_upstream_complete", seq)
            completion = CompletionRecord(
                seq,
                self.sink.now(),
                result.outcome,
                result.status,
                result.request_bytes,
                result.response_bytes,
                result.redirects,
            )
            with self.state_lock:
                try:
                    self.sink.write_completion(completion, reservation)
                    completed = True
                except AuditUnavailable as exc:
                    self._report_audit_failure(exc)
                    completed = False
                finally:
                    self.sink.release(reservation)
            if not completed:
                self._answer(conn, 503, Reason.AUDIT_UNAVAILABLE)
                return
            self._deliver(conn, method, result)
        finally:
            _discard(result.body)

    def _reserve_and_record(
        self, seq: int, decision: Decision, reason: Reason | None, target: Target
    ) -> tuple[IntentRecord, Reservation] | Reason:
        """Under the state lock: audit capacity, then the intent record —
        appended and synced before anything else happens for this request.
        A ``Reason`` is the 503 the request gets instead, with no record."""
        budgets = self.budgets
        if self.sink.state == STATE_UNAVAILABLE:
            return Reason.AUDIT_UNAVAILABLE
        reservation = self.sink.reserve(
            "authorized" if decision is Decision.AUTHORIZED else "refusal"
        )
        if reservation is None:
            self.audit_budget_exhausted = True
            return Reason.AUDIT_BUDGET
        reserved = None
        if decision is Decision.AUTHORIZED:
            reserved = ReservedBudgets(
                budgets.request_body_limit, budgets.response_body_limit, reservation.size
            )
        record = self.sink.intent_for(
            IntentRecord(seq, self.sink.now(), decision, reason, target, reserved)
        )
        try:
            self.sink.write_intent(record, reservation)
        except AuditUnavailable as exc:
            self._report_audit_failure(exc)
            self.sink.release(reservation)
            return Reason.AUDIT_UNAVAILABLE
        return record, reservation

    def _authorize_hop(
        self,
        seq: int,
        reservation: Reservation,
        method: MethodClass,
        hop: int,
        target: CanonicalTarget,
    ) -> Reason | None:
        """The write-ahead step for one candidate hop: its redirect-intent
        record is measured, then appended and synced under the request's
        reservation, and only then may the credential be attached to it."""
        record = RedirectIntentRecord(seq, self.sink.now(), hop, Target.full(method, target, None))
        if not fits(serialize(record), self.budgets):
            return Reason.AUDIT_UNREPRESENTABLE
        with self.state_lock:
            try:
                self.sink.write_redirect_intent(record, reservation)
            except AuditUnavailable as exc:
                self._report_audit_failure(exc)
                return Reason.AUDIT_UNAVAILABLE
        self._hook("after_redirect_intent_fdatasync", seq, hop)
        return None

    # -- answering -------------------------------------------------------

    def _send(self, conn: socket.socket, data: bytes) -> bool:
        try:
            conn.settimeout(self.budgets.body_read_seconds)
            conn.sendall(data)
            return True
        except OSError:
            return False

    def _answer(self, conn: socket.socket, status: int, reason: Reason) -> None:
        self._send(conn, refusal_response(status, reason))

    def _deliver(self, conn: socket.socket, method: MethodClass, result: UpstreamResult) -> None:
        if result.outcome is Outcome.ABORTED:
            _discard(result.body)
            return
        if result.outcome is not Outcome.DELIVERED:
            _discard(result.body)
            assert result.reason is not None
            self._answer(conn, 502, result.reason)
            return
        assert result.status is not None and result.body is not None
        body = result.body
        size = body.size if isinstance(body, SpoolHandle) else len(body)
        lines = [f"HTTP/1.1 {result.status} {_phrase(result.status)}"]
        lines += [f"{name}: {value}" for name, value in result.headers]
        lines.append("Connection: close")
        if method is not MethodClass.HEAD:
            lines.append(f"Content-Length: {size}")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        try:
            if not self._send(conn, head) or method is MethodClass.HEAD:
                return
            if isinstance(body, SpoolHandle):
                for chunk in body.chunks():
                    if not self._send(conn, chunk):
                        return
            else:
                self._send(conn, body)
        finally:
            _discard(body)

    # -- shutdown --------------------------------------------------------

    def _abort_for_agent_exit(self) -> None:
        """Agent exit: connections still queued are closed unread (no
        request line was seen), and every in-flight upstream request is
        aborted so its worker's completion can say so."""
        with self.work:
            pending, self.queue = self.queue, []
        for conn in pending:
            with self.state_lock:
                self.no_request += 1
            self._finish_connection(conn)
        if self.upstream is not None:
            self.upstream.abort()

    def _drain(self, reason: str) -> None:
        aborted = reason == REASON_AGENT_EXIT
        if aborted:
            self._abort_for_agent_exit()
        with self.work:
            self.stopping = True
            self.work.notify_all()
        while True:
            with self.work:
                if self.outstanding == 0:
                    return
                self.work.wait(DRAIN_POLL_SECONDS)
            if not aborted and self.agent_exit.is_set():
                aborted = True
                self._abort_for_agent_exit()

    def _write_terminal(self, reason: str) -> None:
        if self.sink.state == STATE_UNAVAILABLE:
            return
        with self.state_lock:
            record = TerminalRecord(
                self.sink.now(),
                reason,
                self.connection_budget_exhausted,
                self.request_budget_exhausted,
                self.audit_budget_exhausted,
                self.accepted,
                self.busy_refused,
                self.no_request,
                self.requests_seen,
                self.requests_charged,
            )
            try:
                self.sink.write_terminal(record)
            except AuditUnavailable as exc:
                self._report_audit_failure(exc)

    def _install_sigterm(self) -> None:
        # Outside the main thread signal() raises: an in-process caller
        # drives agent_exit itself.
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGTERM, lambda signum, frame: self.agent_exit.set())

    def run(self) -> int:
        self._install_sigterm()
        watcher = threading.Thread(
            target=self._watch_agent_exit, daemon=True, name="relay-agent-exit"
        )
        watcher.start()
        workers = [
            threading.Thread(target=self._worker, daemon=True, name=f"relay-worker-{index}")
            for index in range(self.budgets.concurrency)
        ]
        for worker in workers:
            worker.start()
        acceptor = threading.Thread(target=self._accept_loop, daemon=True, name="relay-accept")
        acceptor.start()
        self._report({"event": "ready"})
        self.acceptance_over.wait()
        acceptor.join()
        reason = self.accept_reason
        assert reason is not None
        self._drain(reason)
        for worker in workers:
            worker.join()
        self._write_terminal(reason)
        if self.upstream is not None:
            discard_spool(self.upstream.spool_dir)
            self.upstream.close()
        self._report({"event": "exit", "reason": reason, "audit": self.sink.state})
        # The run is over whichever way acceptance ended; the watcher is
        # released before its pipe goes, so its wake byte never lands on a
        # reused descriptor.
        self.agent_exit.set()
        watcher.join()
        os.close(self._wake_r)
        os.close(self._wake_w)
        return 0


def _discard(body: bytes | SpoolHandle | None) -> None:
    if isinstance(body, SpoolHandle):
        body.close()


def serve(
    listen_fd: int,
    sink_fd: int,
    *,
    upstream: UpstreamClient | None,
    budgets: Budgets,
    report,
    run_id: str,
    log: Callable[[str], None],
    sink: AuditSink | None = None,
    hooks: Hooks | None = None,
    agent_exit: threading.Event | None = None,
) -> int:
    """Serve the relay socket until acceptance ends, run the one shutdown
    sequence, and return 0. ``upstream=None`` is none mode. The keyword
    seams exist for in-process tests: a scripted ``sink``, crash ``hooks``,
    and an ``agent_exit`` event in place of SIGTERM."""
    relay = _Relay(
        listen_fd,
        sink_fd,
        upstream=upstream,
        budgets=budgets,
        report=report,
        run_id=run_id,
        log=log,
        sink=sink,
        hooks=hooks,
        agent_exit=agent_exit,
    )
    return relay.run()
