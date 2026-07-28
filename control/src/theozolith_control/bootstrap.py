"""The bootstrap listener: three inert public values over dedicated plaintext
HTTP (ADR-0023).

A closed route table by decision, not convention — GET/HEAD only, no auth,
no state, no cookies, and exactly three paths:

    /ca.pem        the CA certificate (PEM)
    /origin        the public origin
    /control-url   the canonical control URL

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

            def _answer(self, *, send_body: bool) -> None:
                entry = routes.get(self.path)
                if entry is None:
                    body = b"not found\n"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if send_body:
                        self.wfile.write(body)
                    return
                body, content_type = entry
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if send_body:
                    self.wfile.write(body)

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
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass  # three public values need no access log

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
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
