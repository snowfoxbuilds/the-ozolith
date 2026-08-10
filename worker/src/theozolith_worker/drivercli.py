"""The single generic driver entry point: ``theozolith-driver <ref> [--once]``.

One console script launches every built-in worker type, invoked as
``theozolith-driver builtin:implementer``. Control resolves a worker type's
driver ref to exactly this command (ADR-0044/ADR-0020), so one place names the
launcher; the same vector will execute ``drivers/<name>`` custom drivers when
that sub-issue lands (ADR-0042). ``--once`` is the daemon-less dev path: a
single dispatch-run pass, then exit.
"""

from __future__ import annotations

import argparse
import sys

from theozolith_worker.base import Worker
from theozolith_worker.config import ConfigError
from theozolith_worker.implementer import Implementer
from theozolith_worker.reviewer import Reviewer

# The built-in worker types this launcher knows, keyed by the driver ref a
# worker type declares. Control's resolution map (configrepo.BUILTIN_DRIVERS)
# must carry the same keys — the contract test pins the two together.
BUILTIN_WORKERS: dict[str, type[Worker]] = {
    "builtin:implementer": Implementer,
    "builtin:reviewer": Reviewer,
}


def run_driver(driver_cls: type[Worker], *, once: bool = False) -> int:
    """Construct one worker type from the environment and run its loop.

    The stable seam (``theozolith_worker.api.run_driver``, ADR-0042) that the
    custom-driver launcher and the builtins share: it owns loop construction —
    config load, GitHub client, dispatch client, event sink, and session
    factory all come from the worker type's ``load`` + ``__init__`` defaults.
    Returns the number of items executed (meaningful under ``once``).
    """
    return driver_cls.load().run(once=once)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-driver",
        description=(
            "TheOzolith driver launcher: run a built-in worker type's dispatch-run loop."
            " Refs: " + ", ".join(sorted(BUILTIN_WORKERS)) + "."
        ),
    )
    parser.add_argument(
        "ref",
        help="worker type ref, e.g. builtin:implementer or builtin:reviewer",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="One dispatch-run pass (at most one claim), then exit. Requires a"
        " reachable Control Node (ADR-0017).",
    )
    args = parser.parse_args(argv)
    driver_cls = BUILTIN_WORKERS.get(args.ref)
    if driver_cls is None:
        known = ", ".join(sorted(BUILTIN_WORKERS))
        print(f"error: unknown driver ref {args.ref!r} (known: {known})", file=sys.stderr)
        return 2
    try:
        run_driver(driver_cls, once=args.once)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"{driver_cls.role} driver stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
