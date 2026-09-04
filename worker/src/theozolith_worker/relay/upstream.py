"""The GitHub Relay's upstream client (ADR-0057 items 5 and 6): credential
injection, the pre-delivery response gate, and the redirect follower.

The credential lives here and nowhere else in the relay — the server hands
over a request built with ``authorization=None`` and this module attaches
``Authorization`` at the wire, after the server's write-ahead record is
durable. No upstream byte is returned until the whole response has been
read within the per-request limit, into memory or an unlinked spool file,
and every redirect answer is a fresh policy decision against the origin pin
made before the hop's request exists.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from theozolith_worker import __version__
from theozolith_worker.relay import uri
from theozolith_worker.relay.audit import HostRepr, RedirectEntry
from theozolith_worker.relay.classify import GRAPHQL_PATH, classify_rest
from theozolith_worker.relay.ingress import (
    CanonicalTarget,
    IngressRefusal,
    UpstreamRequest,
    canonical_query,
    canonicalize_target,
    filter_response_headers,
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

ORIGIN = "https://api.github.com"
HOST = "api.github.com"
PORT = 443
USER_AGENT = f"theozolith-relay/{__version__}"
MEMORY_SPOOL_THRESHOLD = 1024 * 1024
_READ_CHUNK = 64 * 1024
_METHOD_PRESERVING = frozenset({301, 302, 307, 308})
_MIN_SOCKET_TIMEOUT = 0.001
# A followed hop carries the relay's own headers only — never a client one.
_HOP_HEADERS = (
    ("Host", HOST),
    ("User-Agent", USER_AGENT),
    ("Accept-Encoding", "identity"),
)

ConnectionFactory = Callable[[str, int, float], http.client.HTTPConnection]
# Called at the crash-injection points inside a send: ``during_upstream``
# once the original request is on the wire, ``before_redirect_send`` and
# ``after_redirect_send`` around each followed hop's request.
Observer = Callable[[str, int], None]


@dataclass(frozen=True)
class Live:
    """A GitHub.com upstream. Exactly one credential source: the production
    driver passes ``credential`` from memory so no token touches disk; the
    bench passes ``credential_file`` (``_FILE`` style, never argv)."""

    credential_file: Path | None = None
    credential: str | None = None

    def __post_init__(self) -> None:
        if (self.credential_file is None) == (self.credential is None):
            raise ValueError("Live takes exactly one of credential_file or credential")


@dataclass(frozen=True)
class NoUpstream:
    """The bench's ``none`` mode: every validated request is refused."""


Upstream = Live | NoUpstream


class AggregateBudget:
    """The per-Run aggregate byte budgets, atomic under their own lock so
    concurrent requests near a limit cannot overshoot it."""

    def __init__(self, budgets: Budgets):
        self._lock = threading.Lock()
        self._request_remaining = budgets.aggregate_request_bytes
        self._response_remaining = budgets.aggregate_response_bytes

    @property
    def request_remaining(self) -> int:
        with self._lock:
            return self._request_remaining

    @property
    def response_remaining(self) -> int:
        with self._lock:
            return self._response_remaining

    def charge_request(self, n: int) -> bool:
        with self._lock:
            if n > self._request_remaining:
                return False
            self._request_remaining -= n
            return True

    def reserve_response(self, n: int) -> bool:
        with self._lock:
            if n > self._response_remaining:
                return False
            self._response_remaining -= n
            return True

    def release_response(self, n: int) -> None:
        with self._lock:
            self._response_remaining += n


class SpoolHandle:
    """A gated response body that outgrew memory: an already-unlinked
    temporary file, readable once through its descriptor and gone with it."""

    def __init__(self, fd: int, size: int):
        self._fd: int | None = fd
        self.size = size

    def read(self) -> bytes:
        return b"".join(self.chunks())

    def chunks(self, size: int = _READ_CHUNK):
        if self._fd is None:
            raise ValueError("spool already closed")
        os.lseek(self._fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(self._fd, size)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


@dataclass(frozen=True)
class UpstreamResult:
    outcome: Outcome
    status: int | None
    headers: tuple[tuple[str, str], ...]
    body: bytes | SpoolHandle | None
    request_bytes: int
    response_bytes: int
    redirects: tuple[RedirectEntry, ...]
    reason: Reason | None


class _Refused(Exception):
    """Ends a send with a refusal outcome; the chain so far rides along."""

    def __init__(self, outcome: Outcome, reason: Reason):
        super().__init__(reason.value)
        self.outcome = outcome
        self.reason = reason


class _Body:
    """The gate's accumulator: memory up to the spool threshold, then an
    unlinked spool file under the spool directory, counting actual bytes."""

    def __init__(self, spool_dir: Path, threshold: int, spool_write):
        self._spool_dir = spool_dir
        self._threshold = threshold
        self._spool_write = spool_write
        self._buffer = bytearray()
        self._fd: int | None = None
        self.size = 0

    def append(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self._fd is None and self.size <= self._threshold:
            self._buffer += chunk
            return
        if self._fd is None:
            fd, name = tempfile.mkstemp(dir=str(self._spool_dir), prefix="response-")
            os.unlink(name)
            self._fd = fd
            prefix, self._buffer = bytes(self._buffer), bytearray()
            self._write_all(prefix)
        self._write_all(chunk)

    def _write_all(self, data: bytes) -> None:
        assert self._fd is not None
        view = memoryview(data)
        while view:
            written = self._spool_write(self._fd, view)
            view = view[written:]

    def finish(self) -> bytes | SpoolHandle:
        if self._fd is None:
            return bytes(self._buffer)
        handle = SpoolHandle(self._fd, self.size)
        self._fd = None
        return handle

    def discard(self) -> None:
        self._buffer = bytearray()
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def request_size(request: UpstreamRequest) -> int:
    """The reconstructed request's byte length as the relay charges it: the
    request line, the headers it was built with, and the body — never the
    ``Authorization`` this module adds."""
    line = f"{request.method} {request.request_target} HTTP/1.1\r\n"
    headers = "".join(f"{name}: {value}\r\n" for name, value in request.headers)
    return len(line.encode("ascii")) + len(headers.encode("latin-1")) + 2 + len(request.body)


def discard_spool(spool_dir: Path) -> None:
    """Delete whatever the spool directory holds; a crash between a spool
    file's creation and its unlink is the only way it holds anything."""
    try:
        entries = list(spool_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            entry.unlink()


def _redirect_entry(
    hop: int,
    status: int,
    decision: RedirectDecision,
    reason: Reason | None,
    locations: list[str],
) -> RedirectEntry:
    """The completion record's bounded view of a redirect answer, taken from
    the raw ``Location`` value(s): a missing header is ``absent``, a
    duplicated one ``invalid`` with the host of the first as delimited."""
    if not locations:
        absent = HostRepr(HostStatus.ABSENT)
        return RedirectEntry(hop, status, decision, reason, Scheme.ABSENT, absent)
    parts = uri.split_reference(locations[0])
    if parts is None:
        return RedirectEntry(
            hop, status, decision, reason, Scheme.INVALID, HostRepr(HostStatus.ABSENT)
        )
    scheme = Scheme.INVALID if len(locations) > 1 else uri.classify_scheme(parts)
    return RedirectEntry(hop, status, decision, reason, scheme, uri.classify_host(parts))


def _pinned_origin(parts: uri.UriParts) -> bool:
    """The origin pin: exactly ``https``, the host byte-equal to
    ``api.github.com`` after lowercasing with no trailing dot, user-info,
    or percent-encoding, and the default port."""
    if uri.classify_scheme(parts) is not Scheme.HTTPS or parts.authority is None:
        return False
    if "%" in parts.authority:
        return False
    authority = uri.parse_authority(parts.authority)
    if authority is None or authority.userinfo is not None:
        return False
    if authority.host.lower() != HOST:
        return False
    return authority.port is None or authority.port == str(PORT)


class UpstreamClient:
    """Sends validated requests to the pinned origin with the credential
    attached, gates every response before a byte of it is handed back, and
    follows a redirect only per hop through ``authorize_hop``."""

    def __init__(
        self,
        credential: str | None,
        budgets: Budgets,
        spool_dir: Path,
        *,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        spool_threshold: int = MEMORY_SPOOL_THRESHOLD,
        _spool_write=os.write,
    ):
        self._credential = credential
        self._budgets = budgets
        self.spool_dir = spool_dir
        self._factory = connection_factory or self._default_factory
        self._clock = clock
        self._spool_threshold = spool_threshold
        self._spool_write = _spool_write
        self._lock = threading.Lock()
        # Each in-flight connection's socket, held here because getresponse()
        # forgets it once the response owns it — and abort() must reach it.
        self._in_flight: dict[http.client.HTTPConnection, socket.socket | None] = {}
        self._aborted = False

    @staticmethod
    def _default_factory(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPSConnection(host, port, timeout=timeout)

    @property
    def aborted(self) -> bool:
        return self._aborted

    def abort(self) -> None:
        """Agent exit: every in-flight upstream connection is shut down so
        its worker's read returns, and every send from now on — in flight
        or not yet started — ends ``aborted`` without touching the wire."""
        with self._lock:
            self._aborted = True
            sockets = list(self._in_flight.values())
        for sock in sockets:
            _shutdown_socket(sock)

    def _open(self, deadline: float) -> http.client.HTTPConnection:
        with self._lock:
            if self._aborted:
                raise _Refused(Outcome.ABORTED, Reason.ABORTED)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise _Refused(Outcome.TIMEOUT, Reason.UPSTREAM_TIMEOUT)
            connection = self._factory(HOST, PORT, remaining)
            self._in_flight[connection] = None
        return connection

    def _connected(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            if self._aborted:
                raise _Refused(Outcome.ABORTED, Reason.ABORTED)
            self._in_flight[connection] = connection.sock

    def _close(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            sock = self._in_flight.pop(connection, None)
        connection.close()
        if sock is not None:
            sock.close()

    def _arm(self, connection: http.client.HTTPConnection, deadline: float) -> None:
        """The wall-clock deadline governs every socket operation."""
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _Refused(Outcome.TIMEOUT, Reason.UPSTREAM_TIMEOUT)
        connection.timeout = remaining
        if connection.sock is not None:
            connection.sock.settimeout(max(remaining, _MIN_SOCKET_TIMEOUT))

    def _put(
        self,
        connection: http.client.HTTPConnection,
        method: str,
        target: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        deadline: float,
    ) -> http.client.HTTPResponse:
        assert self._credential is not None
        self._arm(connection, deadline)
        connection.connect()
        self._connected(connection)
        self._arm(connection, deadline)
        connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            connection.putheader(name, value)
        connection.putheader("Authorization", f"Bearer {self._credential}")
        connection.endheaders(body if body else None)
        self._arm(connection, deadline)
        return connection.getresponse()

    def _gate(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
        deadline: float,
        already_read: int,
    ) -> _Body:
        """Read the whole body counting actual bytes; one byte past the
        per-request limit (cumulative over the chain) refuses."""
        limit = self._budgets.response_body_limit
        body = _Body(self.spool_dir, self._spool_threshold, self._spool_write)
        try:
            while True:
                remaining = limit - already_read - body.size
                self._arm(connection, deadline)
                chunk = response.read(min(_READ_CHUNK, remaining + 1))
                if not chunk:
                    # http.client hands back an empty read, not IncompleteRead,
                    # when a Content-Length body ends early; a short body is
                    # never delivered as a whole one.
                    if response.length:
                        raise http.client.IncompleteRead(b"")
                    return body
                if len(chunk) > remaining:
                    raise _Refused(Outcome.REFUSED_GATE, Reason.GATE_RESPONSE_BYTES)
                body.append(chunk)
        except BaseException:
            body.discard()
            raise

    def send(
        self,
        request: UpstreamRequest,
        *,
        method_class: MethodClass,
        canonical_target: CanonicalTarget,
        authorize_hop: Callable[[int, CanonicalTarget], Reason | None],
        aggregate: AggregateBudget,
        observe: Observer | None = None,
    ) -> UpstreamResult:
        """One credentialed exchange, redirects included. ``authorize_hop``
        is the server's write-ahead step for a candidate hop: ``None``
        authorizes it, a ``Reason`` refuses it and is recorded as that hop's
        entry. The request bytes are already charged by the caller; the
        response allowance is reserved here, atomically, before any
        connection is opened, and the unused remainder returned after."""
        if self._credential is None:
            raise RuntimeError("UpstreamClient.send needs a credential")
        budgets = self._budgets
        request_bytes = request_size(request)
        if not aggregate.reserve_response(budgets.response_body_limit):
            return UpstreamResult(
                Outcome.REFUSED_GATE, None, (), None, request_bytes, 0, (), Reason.GATE_AGGREGATE
            )
        chain = _Chain(
            self,
            request,
            method_class=method_class,
            canonical_target=canonical_target,
            authorize_hop=authorize_hop,
            observe=observe,
        )
        try:
            return chain.run(request_bytes)
        finally:
            aggregate.release_response(budgets.response_body_limit - chain.response_bytes)


def _shutdown_socket(sock: socket.socket | None) -> None:
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)  # the only wake-up a blocked recv honors


class _Chain:
    """One send's state: the current hop, the bytes so far, and the
    redirect entries the completion record will carry."""

    def __init__(
        self,
        client: UpstreamClient,
        request: UpstreamRequest,
        *,
        method_class: MethodClass,
        canonical_target: CanonicalTarget,
        authorize_hop: Callable[[int, CanonicalTarget], Reason | None],
        observe: Observer | None,
    ):
        self._client = client
        self._budgets = client._budgets
        self._request = request
        self._method_class = method_class
        self._graphql = method_class is MethodClass.POST and canonical_target.path == GRAPHQL_PATH
        self._authorize_hop = authorize_hop
        self._observe = observe or (lambda point, hop: None)
        self._visited = {_target_key(canonical_target)}
        self.response_bytes = 0
        self.entries: list[RedirectEntry] = []
        self._hop = 0
        self._status: int | None = None

    def run(self, request_bytes: int) -> UpstreamResult:
        client = self._client
        deadline = client._clock() + self._budgets.upstream_timeout
        method = self._request.method
        target = self._request.request_target
        headers = self._request.headers
        body = self._request.body
        try:
            while True:
                status, response_headers, gated = self._exchange(
                    method, target, headers, body, deadline
                )
                if not _is_redirect(status):
                    return UpstreamResult(
                        Outcome.DELIVERED,
                        status,
                        filter_response_headers(response_headers),
                        gated,
                        request_bytes,
                        self.response_bytes,
                        tuple(self.entries),
                        None,
                    )
                if isinstance(gated, SpoolHandle):
                    gated.close()
                locations = [v for name, v in response_headers if name.lower() == "location"]
                target = self._decide(status, locations, target)
                headers = _HOP_HEADERS
                body = b""
        except _Refused as refused:
            return UpstreamResult(
                refused.outcome,
                self._status,
                (),
                None,
                request_bytes,
                self.response_bytes,
                tuple(self.entries),
                refused.reason,
            )

    def _exchange(
        self,
        method: str,
        target: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        deadline: float,
    ) -> tuple[int, list[tuple[str, str]], bytes | SpoolHandle]:
        client = self._client
        connection = client._open(deadline)
        try:
            if self._hop:
                self._observe("before_redirect_send", self._hop)
            response = client._put(connection, method, target, headers, body, deadline)
            self._status = response.status
            self._observe("after_redirect_send" if self._hop else "during_upstream", self._hop)
            response_headers = list(response.getheaders())
            encoding = response.getheader("Content-Encoding")
            if encoding is not None and encoding.strip().lower() != "identity":
                raise _Refused(Outcome.REFUSED_GATE, Reason.CONTENT_ENCODING)
            gated = client._gate(connection, response, deadline, self.response_bytes)
            self.response_bytes += gated.size
            return response.status, response_headers, gated.finish()
        except _Refused:
            raise
        except (OSError, http.client.HTTPException) as exc:
            if client.aborted:
                raise _Refused(Outcome.ABORTED, Reason.ABORTED) from exc
            timed_out = isinstance(exc, TimeoutError) or client._clock() >= deadline
            if timed_out:
                raise _Refused(Outcome.TIMEOUT, Reason.UPSTREAM_TIMEOUT) from exc
            raise _Refused(Outcome.UPSTREAM_ERROR, Reason.UPSTREAM_ERROR) from exc
        finally:
            client._close(connection)

    def _refuse(self, status: int, reason: Reason, locations: list[str]) -> _Refused:
        self.entries.append(
            _redirect_entry(self._hop, status, RedirectDecision.REFUSED, reason, locations)
        )
        return _Refused(Outcome.REFUSED_REDIRECT, reason)

    def _decide(self, status: int, locations: list[str], base_target: str) -> str:
        """Item 5 per hop: every check re-run against the resolved
        ``Location``; the returned canonical request-target is the next
        hop's, sent only after ``authorize_hop`` made its record durable."""
        self._hop += 1
        if self._graphql:
            raise self._refuse(status, Reason.REDIRECT_GRAPHQL, locations)
        if self._hop > self._budgets.redirect_hops:
            raise self._refuse(status, Reason.REDIRECT_HOPS, locations)
        if status not in _METHOD_PRESERVING:
            raise self._refuse(status, Reason.REDIRECT_METHOD, locations)
        if len(locations) != 1:
            raise self._refuse(status, Reason.REDIRECT_LOCATION, locations)
        raw = uri.split_reference(locations[0])
        resolved = uri.resolve_location(ORIGIN + base_target, locations[0])
        if raw is None or raw.scheme is None or resolved is None:
            raise self._refuse(status, Reason.REDIRECT_LOCATION, locations)
        parts = uri.split_reference(resolved)
        if parts is None or not _pinned_origin(parts):
            raise self._refuse(status, Reason.REDIRECT_ORIGIN, locations)
        if parts.fragment is not None:
            raise self._refuse(status, Reason.REDIRECT_LOCATION, locations)
        request_target = parts.path + ("" if parts.query is None else "?" + parts.query)
        canonical = canonicalize_target(request_target, self._budgets)
        if isinstance(canonical, IngressRefusal):
            raise self._refuse(status, Reason.REDIRECT_LOCATION, locations)
        policy = classify_rest(self._method_class, canonical.path)
        if policy is Reason.ADMIN_READ:
            raise self._refuse(status, Reason.REDIRECT_DENYLIST, locations)
        if policy is not None:
            raise self._refuse(status, Reason.REDIRECT_METHOD, locations)
        key = _target_key(canonical)
        if key in self._visited:
            raise self._refuse(status, Reason.REDIRECT_LOOP, locations)
        refusal = self._authorize_hop(self._hop, canonical)
        if refusal is not None:
            raise self._refuse(status, refusal, locations)
        self._visited.add(key)
        self.entries.append(
            _redirect_entry(self._hop, status, RedirectDecision.FOLLOWED, None, locations)
        )
        return key


def _target_key(target: CanonicalTarget) -> str:
    query = canonical_query(target.query)
    return target.path + (f"?{query}" if query else "")


def _is_redirect(status: int) -> bool:
    """Every 3xx but ``304 Not Modified``, which carries no ``Location``
    semantics and is a conditional GET's ordinary answer."""
    return 300 <= status < 400 and status != 304
