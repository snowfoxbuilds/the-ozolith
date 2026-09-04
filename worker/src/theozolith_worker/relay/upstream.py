"""The GitHub Relay's upstream client (ADR-0057 items 5 and 6): credential
injection, the pre-delivery response gate, and the redirect follower.

The credential lives here and nowhere else in the relay — the server hands
over a request built with ``authorization=None`` and this module attaches
``Authorization`` at the wire, after the server's write-ahead record is
durable. No upstream byte is returned until the whole response has been
read within the per-request limit, into memory or an unlinked spool file,
and every redirect answer is a fresh policy decision against the origin pin
made before the hop's request exists.

One absolute deadline per send covers name resolution, the connect, the
request, the response head, every body read, and every followed hop; every
byte the gate reads is counted against the aggregate whether the send ends
delivered, refused, timed out, truncated, or aborted; and ``abort`` reaches
a name lookup and a connect in progress as well as a read.
"""

from __future__ import annotations

import contextlib
import errno
import http.client
import os
import selectors
import socket
import ssl
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
    concurrent requests near a limit cannot overshoot it. Response bytes
    are reserved per send at the per-request limit and settled to the
    actual count read; request bytes are charged per request sent, the
    original by the server and every followed hop by the chain."""

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

    def refund_request(self, n: int) -> None:
        """A charged hop whose request never came to exist."""
        with self._lock:
            self._request_remaining += n

    def reserve_response(self, n: int) -> bool:
        with self._lock:
            if n > self._response_remaining:
                return False
            self._response_remaining -= n
            return True

    def settle_response(self, reserved: int, consumed: int) -> None:
        """Keep what was actually read and return the rest of the
        reservation. The reservation already covers the one byte the gate
        reads past the per-request limit to see an overflow, so ``consumed``
        never exceeds ``reserved``; the clamp makes that an invariant the
        aggregate keeps whatever a caller does — remaining never rises above
        what was reserved here nor falls below zero."""
        with self._lock:
            charged = min(max(consumed, 0), reserved)
            self._response_remaining += reserved - charged


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
    """``request_bytes`` is every request this send charged — the original
    and each followed hop; ``response_bytes`` is every body byte the gate
    read, the same count the aggregate keeps, whatever the outcome."""

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
    unlinked spool file under the spool directory."""

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


def hop_request(method: str, request_target: str) -> UpstreamRequest:
    """The request a followed hop sends — the method preserved, the relay's
    fixed headers, no body — built as one value so the bytes charged for
    the hop are the bytes put on the wire."""
    return UpstreamRequest(method, request_target, _HOP_HEADERS, b"")


def response_reservation(budgets: Budgets) -> int:
    """What a send reserves against the aggregate before it reads a byte:
    the per-request response limit plus the one byte the gate reads past it
    to detect an overflow. Reserving the whole of what a single response can
    receive is what keeps the aggregate strictly bounded — every admitted
    send holds room for its worst case, so settling can never drive the
    remaining capacity negative, and the server's admission peek uses the
    same figure so it never authorizes a request the gate would then refuse."""
    return budgets.response_body_limit + 1


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
    follows a redirect only per hop through ``authorize_hop``.

    The client opens every socket itself so that the send's deadline and
    ``abort`` govern name resolution and the connect too: the blocking
    lookup runs in a throwaway thread waited on alongside the wake pipe, then
    a non-blocking connect awaited on a selector together with that same wake
    pipe ``abort`` writes to, then the TLS handshake on the wrapped socket,
    all armed with what remains of the deadline. The socket is kept here for
    the life of the exchange because ``http.client`` forgets it once a
    ``Connection: close`` response owns it, and every later read must still
    be armed and abortable."""

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
        _socket=socket.socket,
        _getaddrinfo=socket.getaddrinfo,
    ):
        self._credential = credential
        self._budgets = budgets
        self.spool_dir = spool_dir
        self._factory = connection_factory or self._default_factory
        self._clock = clock
        self._spool_threshold = spool_threshold
        self._spool_write = _spool_write
        self._socket = _socket
        self._getaddrinfo = _getaddrinfo
        self._lock = threading.Lock()
        self._tls: ssl.SSLContext | None = None
        self._in_flight: dict[http.client.HTTPConnection, socket.socket | None] = {}
        self._aborted = False
        self._wake_r, self._wake_w = os.pipe()
        self._closed = False

    def _default_factory(self, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._tls_context())

    def _tls_context(self) -> ssl.SSLContext:
        with self._lock:
            if self._tls is None:
                self._tls = ssl.create_default_context()
            return self._tls

    @property
    def aborted(self) -> bool:
        return self._aborted

    def abort(self) -> None:
        """Agent exit: every send from now on — in flight or not yet
        started — ends ``aborted`` without touching the wire. A connect in
        progress wakes through the pipe; a blocked handshake or read wakes
        because its socket is shut down."""
        with self._lock:
            if self._aborted:
                return
            self._aborted = True
            sockets = list(self._in_flight.values())
            wake = not self._closed
        if wake:
            os.write(self._wake_w, b"x")
        for sock in sockets:
            _shutdown_socket(sock)

    def close(self) -> None:
        """Release the wake pipe once no send can start again."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        os.close(self._wake_r)
        os.close(self._wake_w)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _Refused(Outcome.TIMEOUT, Reason.UPSTREAM_TIMEOUT)
        return remaining

    def _arm(self, sock: socket.socket, deadline: float) -> None:
        """The absolute deadline governs the next socket operation."""
        sock.settimeout(max(self._remaining(deadline), _MIN_SOCKET_TIMEOUT))

    def _track(self, connection: http.client.HTTPConnection, sock: socket.socket | None) -> None:
        with self._lock:
            if self._aborted:
                raise _Refused(Outcome.ABORTED, Reason.ABORTED)
            self._in_flight[connection] = sock

    def _open(self, deadline: float) -> http.client.HTTPConnection:
        connection = self._factory(HOST, PORT, self._remaining(deadline))
        self._track(connection, None)
        return connection

    def _close(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            sock = self._in_flight.pop(connection, None)
        connection.close()
        if sock is not None:
            sock.close()

    def _await_connect(self, sock: socket.socket, deadline: float) -> None:
        remaining = self._remaining(deadline)
        with selectors.DefaultSelector() as selector:
            selector.register(self._wake_r, selectors.EVENT_READ)
            selector.register(sock, selectors.EVENT_WRITE)
            ready = {key.fileobj for key, _ in selector.select(remaining)}
        if self._wake_r in ready or self._aborted:
            raise _Refused(Outcome.ABORTED, Reason.ABORTED)
        if sock not in ready:
            raise _Refused(Outcome.TIMEOUT, Reason.UPSTREAM_TIMEOUT)

    def _resolve(self, host: str, port: int, deadline: float) -> list:
        """Name resolution under the send's one deadline and abort. The
        blocking ``getaddrinfo`` cannot be interrupted, so it runs in a
        throwaway thread and this waits on the abort pipe and the thread's
        completion together, bounded by what is left of the deadline. An
        abort returns ``aborted`` at once and the deadline a timeout, each
        without waiting for the lookup; a result that lands after either is
        left in the daemon thread and never read, so it opens no socket. The
        same ``deadline`` then governs every address the lookup returns."""
        done_r, done_w = os.pipe()
        holder: dict[str, object] = {}

        def resolve() -> None:
            try:
                holder["addrinfos"] = self._getaddrinfo(
                    host, port, socket.AF_UNSPEC, socket.SOCK_STREAM
                )
            except BaseException as exc:  # re-raised on the caller's thread
                holder["error"] = exc
            finally:
                with contextlib.suppress(OSError):
                    os.write(done_w, b"x")
                os.close(done_w)

        threading.Thread(target=resolve, daemon=True, name="relay-resolve").start()
        try:
            remaining = self._remaining(deadline)
            with selectors.DefaultSelector() as selector:
                selector.register(self._wake_r, selectors.EVENT_READ)
                selector.register(done_r, selectors.EVENT_READ)
                ready = {key.fileobj for key, _ in selector.select(remaining)}
        finally:
            os.close(done_r)
        if self._wake_r in ready or self._aborted:
            raise _Refused(Outcome.ABORTED, Reason.ABORTED)
        if done_r not in ready:
            raise _Refused(Outcome.TIMEOUT, Reason.UPSTREAM_TIMEOUT)
        error = holder.get("error")
        if error is not None:
            raise error
        return holder["addrinfos"]

    def _open_socket(
        self, connection: http.client.HTTPConnection, deadline: float
    ) -> socket.socket:
        failure: OSError | None = None
        for family, kind, proto, _, address in self._resolve(
            connection.host, connection.port, deadline
        ):
            sock = self._socket(family, kind, proto)
            try:
                self._track(connection, sock)
                sock.setblocking(False)
                err = sock.connect_ex(address)
                if err == errno.EINPROGRESS:
                    self._await_connect(sock, deadline)
                    err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err:
                    raise OSError(err, os.strerror(err))
                sock.setblocking(True)
                return sock
            except OSError as exc:
                sock.close()
                failure = exc
            except BaseException:
                sock.close()
                raise
        if failure is None:
            failure = OSError(errno.EHOSTUNREACH, "no address for the origin")
        raise failure

    def _secure(
        self, connection: http.client.HTTPConnection, sock: socket.socket, deadline: float
    ) -> socket.socket:
        tls = self._tls_context().wrap_socket(
            sock, server_hostname=connection.host, do_handshake_on_connect=False
        )
        try:
            self._track(connection, tls)
            self._arm(tls, deadline)
            tls.do_handshake()
        except BaseException:
            tls.close()
            raise
        return tls

    def _connect(self, connection: http.client.HTTPConnection, deadline: float) -> socket.socket:
        sock = self._open_socket(connection, deadline)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            sock.close()
            raise
        if isinstance(connection, http.client.HTTPSConnection):
            sock = self._secure(connection, sock, deadline)
        connection.sock = sock
        return sock

    def _put(
        self,
        connection: http.client.HTTPConnection,
        method: str,
        target: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes,
        deadline: float,
    ) -> tuple[http.client.HTTPResponse, socket.socket]:
        assert self._credential is not None
        sock = self._connect(connection, deadline)
        self._arm(sock, deadline)
        connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            connection.putheader(name, value)
        connection.putheader("Authorization", f"Bearer {self._credential}")
        connection.endheaders(body if body else None)
        self._arm(sock, deadline)
        return connection.getresponse(), sock

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
        entry. The original request's bytes are already charged by the
        caller; each followed hop's are charged here before its record is
        written. The response allowance is reserved here, atomically, before
        any connection is opened, and settled to the bytes actually read
        once the send is over, however it ended."""
        if self._credential is None:
            raise RuntimeError("UpstreamClient.send needs a credential")
        request_bytes = request_size(request)
        reservation = response_reservation(self._budgets)
        if not aggregate.reserve_response(reservation):
            return UpstreamResult(
                Outcome.REFUSED_GATE, None, (), None, request_bytes, 0, (), Reason.GATE_AGGREGATE
            )
        chain = _Chain(
            self,
            request,
            method_class=method_class,
            canonical_target=canonical_target,
            authorize_hop=authorize_hop,
            aggregate=aggregate,
            observe=observe,
        )
        try:
            return chain.run(request_bytes)
        finally:
            aggregate.settle_response(reservation, chain.received)


def _shutdown_socket(sock: socket.socket | None) -> None:
    """The one wake-up a blocked recv or handshake honors. The base method
    is called on purpose: ``SSLSocket.shutdown`` would also drop the SSL
    object under the thread still reading through it."""
    if sock is not None:
        with contextlib.suppress(OSError):
            socket.socket.shutdown(sock, socket.SHUT_RDWR)


class _Chain:
    """One send's state: the current hop, the bytes charged and read so
    far, and the redirect entries the completion record will carry."""

    def __init__(
        self,
        client: UpstreamClient,
        request: UpstreamRequest,
        *,
        method_class: MethodClass,
        canonical_target: CanonicalTarget,
        authorize_hop: Callable[[int, CanonicalTarget], Reason | None],
        aggregate: AggregateBudget,
        observe: Observer | None,
    ):
        self._client = client
        self._budgets = client._budgets
        self._request = request
        self._method_class = method_class
        self._graphql = method_class is MethodClass.POST and canonical_target.path == GRAPHQL_PATH
        self._authorize_hop = authorize_hop
        self._aggregate = aggregate
        self._observe = observe or (lambda point, hop: None)
        self._visited = {_target_key(canonical_target)}
        self.received = 0
        self.request_bytes = 0
        self.entries: list[RedirectEntry] = []
        self._hop = 0
        self._status: int | None = None

    def run(self, request_bytes: int) -> UpstreamResult:
        self.request_bytes = request_bytes
        deadline = self._client._clock() + self._budgets.upstream_timeout
        current = self._request
        try:
            while True:
                status, response_headers, gated = self._exchange(current, deadline)
                if not _is_redirect(status):
                    return UpstreamResult(
                        Outcome.DELIVERED,
                        status,
                        filter_response_headers(response_headers),
                        gated,
                        self.request_bytes,
                        self.received,
                        tuple(self.entries),
                        None,
                    )
                if isinstance(gated, SpoolHandle):
                    gated.close()
                locations = [v for name, v in response_headers if name.lower() == "location"]
                current = self._decide(status, locations, current.request_target)
        except _Refused as refused:
            return UpstreamResult(
                refused.outcome,
                self._status,
                (),
                None,
                self.request_bytes,
                self.received,
                tuple(self.entries),
                refused.reason,
            )

    def _exchange(
        self, request: UpstreamRequest, deadline: float
    ) -> tuple[int, list[tuple[str, str]], bytes | SpoolHandle]:
        client = self._client
        connection = client._open(deadline)
        try:
            if self._hop:
                self._observe("before_redirect_send", self._hop)
            response, sock = client._put(
                connection,
                request.method,
                request.request_target,
                request.headers,
                request.body,
                deadline,
            )
            self._status = response.status
            self._observe("after_redirect_send" if self._hop else "during_upstream", self._hop)
            response_headers = list(response.getheaders())
            encoding = response.getheader("Content-Encoding")
            if encoding is not None and encoding.strip().lower() != "identity":
                raise _Refused(Outcome.REFUSED_GATE, Reason.CONTENT_ENCODING)
            gated = self._gate(response, sock, deadline)
            return response.status, response_headers, gated.finish()
        except _Refused:
            raise
        except Exception as exc:
            # Aborted wins over every other reading of a failure: the shut
            # down socket surfaces as EOF, a reset, or an SSL error, none of
            # which is the origin's doing.
            if client.aborted:
                raise _Refused(Outcome.ABORTED, Reason.ABORTED) from exc
            if not isinstance(exc, OSError | http.client.HTTPException):
                raise
            if isinstance(exc, TimeoutError) or client._clock() >= deadline:
                raise _Refused(Outcome.TIMEOUT, Reason.UPSTREAM_TIMEOUT) from exc
            raise _Refused(Outcome.UPSTREAM_ERROR, Reason.UPSTREAM_ERROR) from exc
        finally:
            client._close(connection)

    def _gate(
        self, response: http.client.HTTPResponse, sock: socket.socket, deadline: float
    ) -> _Body:
        """Read the whole body, counting every byte handed up before
        anything is decided about it: one byte past the per-request limit
        (cumulative over the chain) refuses, and that byte is counted too.
        ``read1`` makes one receive per call, so a timeout never loses
        bytes already received to the count."""
        client = self._client
        limit = self._budgets.response_body_limit
        body = _Body(client.spool_dir, client._spool_threshold, client._spool_write)
        try:
            while True:
                remaining = limit - self.received
                if response.isclosed():
                    # A Content-Length body read to its end released the
                    # socket already; there is nothing left to arm.
                    chunk = b""
                else:
                    client._arm(sock, deadline)
                    chunk = response.read1(min(_READ_CHUNK, remaining + 1))
                if not chunk:
                    if response.length:
                        # http.client hands back an empty read, not
                        # IncompleteRead, when a Content-Length body ends
                        # early; a short body is never delivered as a whole.
                        raise http.client.IncompleteRead(b"")
                    if client.aborted:
                        # A close-delimited body's EOF may be the abort's.
                        raise _Refused(Outcome.ABORTED, Reason.ABORTED)
                    return body
                self.received += len(chunk)
                if len(chunk) > remaining:
                    raise _Refused(Outcome.REFUSED_GATE, Reason.GATE_RESPONSE_BYTES)
                body.append(chunk)
        except BaseException:
            body.discard()
            raise

    def _refuse(self, status: int, reason: Reason, locations: list[str]) -> _Refused:
        self.entries.append(
            _redirect_entry(self._hop, status, RedirectDecision.REFUSED, reason, locations)
        )
        return _Refused(Outcome.REFUSED_REDIRECT, reason)

    def _decide(self, status: int, locations: list[str], base_target: str) -> UpstreamRequest:
        """Item 5 per hop: every check re-run against the resolved
        ``Location``; the returned request is the next hop's, its bytes
        charged and its record durable before it is sent."""
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
        # The hop's exact bytes are charged before its record is written and
        # before any connection for it exists; a hop the audit step then
        # refuses never comes to exist, so its charge goes back.
        following = hop_request(self._request.method, key)
        hop_bytes = request_size(following)
        if not self._aggregate.charge_request(hop_bytes):
            raise self._refuse(status, Reason.REDIRECT_BUDGET, locations)
        refusal = self._authorize_hop(self._hop, canonical)
        if refusal is not None:
            self._aggregate.refund_request(hop_bytes)
            raise self._refuse(status, refusal, locations)
        self.request_bytes += hop_bytes
        self._visited.add(key)
        self.entries.append(
            _redirect_entry(self._hop, status, RedirectDecision.FOLLOWED, None, locations)
        )
        return following


def _target_key(target: CanonicalTarget) -> str:
    query = canonical_query(target.query)
    return target.path + (f"?{query}" if query else "")


def _is_redirect(status: int) -> bool:
    """Every 3xx but ``304 Not Modified``, which carries no ``Location``
    semantics and is a conditional GET's ordinary answer."""
    return 300 <= status < 400 and status != 304
