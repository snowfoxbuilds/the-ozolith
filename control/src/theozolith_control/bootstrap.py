"""The bootstrap listener: three inert public values over dedicated plaintext
HTTP (ADR-0023).

A closed route table by decision, not convention — GET/HEAD only, no auth,
no state, no cookies, and exactly three paths:

    /ca.pem        the CA certificate (PEM)
    /origin        the browser origin — since ADR-0034 the same IP-based
                   URL as /control-url (browsers and nodes dial one
                   address; the route stays for compatibility)
    /control-url   the IP-based control URL nodes dial (the node channel is
                   IP-only — ADR-0023 as amended 2026-07-28; it must agree
                   with the join exchange's answer)

Everything else answers 404; non-GET methods answer 405. It runs on its own
port (default in control.toml, ``bootstrap_port``), never mounted on the
HTTPS app, so ADR-0022's fail-closed origin posture is untouched. The
channel is safe not because it is trusted but because every byte on it is
public and the one value that matters — the CA certificate — is
integrity-checked against the join string's pinned fingerprint by
``theozolith-nodedaemon provision``. Code never rides it: the installer and
node distribution come over pre-trusted channels (GitHub release HTTPS, or
scp).
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CA_PATH = "/ca.pem"
ORIGIN_PATH = "/origin"
CONTROL_URL_PATH = "/control-url"

# The listener shares the Control Node process with the authenticated HTTPS
# app, so an unauthenticated flood must not exhaust it (OZ-04). A short read
# timeout kills slowloris-style partial requests, every response closes its
# connection (no keep-alive thread parking), a bounded pool caps concurrent
# handler threads, and the accept backlog is small.
BOOTSTRAP_READ_TIMEOUT = 10.0
BOOTSTRAP_MAX_WORKERS = 16
BOOTSTRAP_BACKLOG = 32


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard ceiling on concurrent handler threads:
    beyond BOOTSTRAP_MAX_WORKERS in flight, a new connection is dropped rather
    than allowed to spawn another thread and file descriptor."""

    daemon_threads = True
    request_queue_size = BOOTSTRAP_BACKLOG

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(BOOTSTRAP_MAX_WORKERS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # at capacity: drop, do not spawn
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def detect_host_ip() -> str:
    """The host's outbound IPv4 address — what init puts in the server-cert
    SAN and join-token creation embeds as the bootstrap address. A UDP
    connect never sends a packet; it only asks the kernel for the route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: never actually dialed
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class BootstrapServer:
    """The listener, owned by ``serve`` (same process, its own port and
    thread — one service to run, two sockets)."""

    def __init__(self, *, ca_pem: bytes, origin: str, control_url: str, port: int, host: str = ""):
        routes = {
            CA_PATH: (ca_pem, "application/x-pem-file"),
            ORIGIN_PATH: (origin.encode() + b"\n", "text/plain; charset=utf-8"),
            CONTROL_URL_PATH: (control_url.encode() + b"\n", "text/plain; charset=utf-8"),
        }

        class Handler(BaseHTTPRequestHandler):
            server_version = "theozolith-bootstrap"
            protocol_version = "HTTP/1.1"
            # A partial/slow request holds a pool slot and an fd; time it out.
            timeout = BOOTSTRAP_READ_TIMEOUT

            def _emit(self, status: int, body: bytes, content_type: str, send_body: bool) -> None:
                # Every response closes its connection: no keep-alive means no
                # idle client can park a handler thread (OZ-04).
                self.close_connection = True
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if send_body:
                    self.wfile.write(body)

            def _answer(self, *, send_body: bool) -> None:
                entry = routes.get(self.path)
                if entry is None:
                    self._emit(404, b"not found\n", "text/plain; charset=utf-8", send_body)
                    return
                body, content_type = entry
                self._emit(200, body, content_type, send_body)

            def do_GET(self):
                self._answer(send_body=True)

            def do_HEAD(self):
                self._answer(send_body=False)

            def send_error(self, code, message=None, explain=None):
                # Unknown methods land here via handle_one_request: keep the
                # closed-table posture (405, no HTML error page).
                if code == 501:
                    code = 405
                body = b"method not allowed\n" if code == 405 else b"not found\n"
                self._emit(code, body, "text/plain; charset=utf-8", send_body=True)

            def log_message(self, format, *args):
                pass  # three public values need no access log

        self._server = _BoundedThreadingHTTPServer((host, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="bootstrap-listener"
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
