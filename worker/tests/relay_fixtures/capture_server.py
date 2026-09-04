"""Throwaway Unix-socket capture server for regenerating this corpus.

Records every request ``gh`` puts on the wire, byte-exact, as ``NN.http``
under the output directory, and answers just enough JSON for ``gh`` to keep
going. Never used by the tests; see README.md and regenerate.sh.

Usage: capture_server.py <socket-path> <out-dir>
"""

from __future__ import annotations

import json
import os
import socket
import sys


def read_request(conn: socket.socket) -> bytes:
    conn.settimeout(10)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(rest) < length:
        chunk = conn.recv(65536)
        if not chunk:
            break
        rest += chunk
    return head + b"\r\n\r\n" + rest


def respond(conn: socket.socket, request: bytes) -> None:
    line = request.split(b"\r\n", 1)[0].decode("latin-1")
    _, target, _ = line.split(" ", 2)
    headers = [("Content-Type", "application/json; charset=utf-8"), ("Connection", "close")]
    if target.startswith("/graphql"):
        body = json.dumps({"data": {}}).encode()
    elif target.startswith("/search/"):
        body = json.dumps({"total_count": 0, "incomplete_results": False, "items": []}).encode()
        link = os.environ.get("CAPTURE_LINK")
        if link and "&page=" not in target:
            headers.append(("Link", link))
    else:
        body = b"[]"
    headers.append(("Content-Length", str(len(body))))
    head = "HTTP/1.1 200 OK\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n"
    conn.sendall(head.encode() + body)


def main(argv: list[str]) -> None:
    sock_path, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(16)
    counter = 0
    while True:
        conn, _ = server.accept()
        try:
            request = read_request(conn)
            if not request:
                continue
            counter += 1
            with open(os.path.join(out_dir, f"{counter:02d}.http"), "wb") as fh:
                fh.write(request)
            respond(conn, request)
        finally:
            conn.close()


if __name__ == "__main__":
    main(sys.argv)
