"""The GitHub Relay child process: ``python -m theozolith_worker.relay``.

The driver's supervisor spawns this with the listening socket, the audit
sink, and — in live mode — a pipe carrying the credential as inherited
descriptors named by number on the command line. No credential rides argv
or the environment; the spool directory is a driver-owned, non-secret path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from theozolith_worker.relay.reasons import DEFAULT_BUDGETS, Budgets
from theozolith_worker.relay.server import serve
from theozolith_worker.relay.upstream import UpstreamClient


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m theozolith_worker.relay")
    parser.add_argument("--listen-fd", type=int, required=True)
    parser.add_argument("--sink-fd", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--credential-fd", type=int)
    mode.add_argument("--no-upstream", action="store_true")
    parser.add_argument("--spool-dir", required=True)
    parser.add_argument("--budgets", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    budgets = DEFAULT_BUDGETS if args.budgets is None else Budgets(**json.loads(args.budgets))
    upstream = None
    if args.credential_fd is not None:
        credential = _read_all(args.credential_fd).decode("utf-8").strip()
        os.close(args.credential_fd)
        upstream = UpstreamClient(credential, budgets, Path(args.spool_dir))
    return serve(
        args.listen_fd,
        args.sink_fd,
        upstream=upstream,
        budgets=budgets,
        report=sys.stdout,
        run_id=args.run_id,
        log=lambda message: print(message, file=sys.stderr, flush=True),
    )


if __name__ == "__main__":
    sys.exit(main())
