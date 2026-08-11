"""The single generic driver entry point: ``theozolith-driver <ref> [--once]``.

One console script launches every worker type. Built-in types are invoked as
``theozolith-driver builtin:implementer``; custom types (ADR-0042) as
``theozolith-driver drivers/<name>``. Control resolves a worker type's driver
ref to exactly this command (ADR-0044/ADR-0020), so one place names the
launcher for both. ``--once`` is the daemon-less dev path: a single
dispatch-run pass, then exit.

Custom drivers (ADR-0042). A ``drivers/<name>`` ref is code delivered to the
node as a hash-pinned, daemon-verified config distribution; ``name`` must be a
Python identifier (``^[a-z_][a-z0-9_]*$`` — dashes are unimportable). The
Node Daemon injects ``THEOZOLITH_DRIVERS_DIR`` = the absolute path of the
unpacked distribution root (``.../config-dist/<hash>``, holding ``drivers/``);
this launcher appends it to ``sys.path`` — never prepends, so a driver can
never shadow stdlib or the product ``theozolith_worker`` packages — imports
``drivers.<name>``, and runs its exported ``Driver`` class (a
``theozolith_worker.api.Worker`` subclass) through the shared loop. Everything
outside ``theozolith_worker.api`` is internal and carries no stability promise;
the config distribution's ``built_against`` stamp is compared to the running
worker version and any skew is logged as a single advisory line, never a
failure. A start-time crash (bad ref, missing/incomplete distribution, import
failure, wrong export, stale api) writes a traceback to stderr, emits a
best-effort ``theozolith.error`` (component ``driver-host``), and exits 1; the
supervisor's bounded relaunch owns the retry (ADR-0016).
"""

from __future__ import annotations

import argparse
import importlib
import itertools
import json
import os
import re
import sys
import traceback
from pathlib import Path

from theozolith_worker import events
from theozolith_worker.base import Worker
from theozolith_worker.config import ConfigError, env_value
from theozolith_worker.implementer import Implementer
from theozolith_worker.reviewer import Reviewer

# The built-in worker types this launcher knows, keyed by the driver ref a
# worker type declares. Control's resolution map (configrepo.BUILTIN_DRIVERS)
# must carry the same keys — the contract test pins the two together.
BUILTIN_WORKERS: dict[str, type[Worker]] = {
    "builtin:implementer": Implementer,
    "builtin:reviewer": Reviewer,
}

# A custom-driver ref (ADR-0042): ``drivers/<name>``. The name must be a valid
# Python identifier — the module is imported as ``drivers.<name>`` — so dashes
# are rejected here (runner-side) and control-side (the resolver), one shape.
CUSTOM_DRIVER_PREFIX = "drivers/"
CUSTOM_DRIVER_NAME = re.compile(r"[a-z_][a-z0-9_]*")

# The dashboard-facing component for a custom-driver start crash: the failure
# happens before a worker type (and thus a role) is known, so the driver host
# itself is named rather than ``<role>-driver`` (ADR-0042; events.py).
DRIVER_HOST_COMPONENT = "driver-host"


def _log(message: str) -> None:
    print(message, flush=True)  # journal-visible under the supervisor


class DriverStartError(RuntimeError):
    """A custom driver cannot be resolved or imported (ADR-0042). Raised for
    the validation failures the launcher detects itself (bad ref, missing
    distribution, wrong export); import-time failures surface as their own
    exception types and are handled the same way."""


def run_driver(driver_cls: type[Worker], *, once: bool = False) -> int:
    """Construct one worker type from the environment and run its loop.

    The stable seam (``theozolith_worker.api.run_driver``, ADR-0042) that the
    custom-driver launcher and the builtins share: it owns loop construction —
    config load, GitHub client, dispatch client, event sink, and session
    factory all come from the worker type's ``load`` + ``__init__`` defaults.
    Returns the number of items executed (meaningful under ``once``).
    """
    return driver_cls.load().run(once=once)


# -- custom drivers (ADR-0042) --------------------------------------------------


def _worker_version() -> str:
    """The running ``theozolith-worker`` version, from installed distribution
    metadata (a source build's ``+g<sha>`` local version survives), falling
    back to the package constant when the distribution is not installed."""
    import importlib.metadata

    try:
        return importlib.metadata.version("theozolith-worker")
    except importlib.metadata.PackageNotFoundError:
        from theozolith_worker import __version__

        return __version__


def _advisory_built_against(metadata: dict) -> str:
    """The advisory ``built_against`` stamp from a config-distribution envelope:
    the string value when present, ``""`` otherwise. Reimplements
    ``nodedaemon.configdist.advisory_built_against`` — the worker package cannot
    import nodedaemon (stdlib-only, ADR-0010) — so an absent or non-string stamp
    is the empty advisory state, never a failure (ADR-0042)."""
    built = metadata.get("built_against")
    return built if isinstance(built, str) else ""


def _advisory_stamp_check(dist_root: Path, *, log) -> None:
    """Compare the distribution's ``built_against`` stamp to the running worker
    version and log ONE advisory line on a mismatch (ADR-0042). Advisory only:
    the daemon already verified the tree before pointing at it, so an unreadable
    or malformed ``config-dist.json`` — or an absent/non-string stamp — is
    skipped silently rather than failing the driver. The runner's job is to run
    the driver, not re-police the distribution."""
    try:
        metadata = json.loads((dist_root / "config-dist.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(metadata, dict):
        return
    built_against = _advisory_built_against(metadata)
    running = _worker_version()
    if built_against and running and built_against != running:
        log(
            f"config distribution built against {built_against}, running"
            f" {running} — advisory, ADR-0042"
        )


def _log_fork_header(module, *, ref: str, log) -> None:
    """Log the one leading fork-header comment if present (ADR-0042 fork
    protocol v0): ``# forked-from: <ancestor> @ <version>`` in the module's
    first few lines. Documentation plus a log line so a human autopsying a
    crash knows the ancestry — no enforcement, no tooling. Fresh-authored
    drivers carry no header and log nothing."""
    path = getattr(module, "__file__", None)
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as handle:
            head = list(itertools.islice(handle, 10))
    except OSError:
        return
    for line in head:
        stripped = line.strip()
        if stripped.startswith("#") and "forked-from:" in stripped:
            log(f"{ref}: {stripped.lstrip('#').strip()}")
            return


def _load_custom_driver(name: str, environ, *, log) -> type[Worker]:
    """Resolve, import, and validate a ``drivers/<name>`` custom driver from the
    daemon-injected distribution (steps 1-4 of ADR-0042's runner contract).
    Returns the ``Driver`` class; raises on any failure so the caller emits the
    start-crash error and exits 1."""
    dist_root = env_value(environ, "THEOZOLITH_DRIVERS_DIR")
    if not dist_root:
        raise DriverStartError(
            "THEOZOLITH_DRIVERS_DIR is unset — the Node Daemon injects the unpacked"
            " config-distribution root for a drivers/<name> Stack (ADR-0042); a"
            " custom driver cannot run without it (for daemon-less dev, point it at"
            " a distribution root holding drivers/)"
        )
    root = Path(dist_root)
    if not (root / "drivers").is_dir():
        raise DriverStartError(
            f"THEOZOLITH_DRIVERS_DIR {dist_root!r} has no drivers/ directory — it is"
            " not an unpacked config distribution (ADR-0042)"
        )
    _advisory_stamp_check(root, log=log)
    # APPEND, never prepend: drivers.* is a unique namespace, so append costs
    # nothing, while a driver tree named like a stdlib or product package
    # (e.g. drivers/theozolith_worker/) can never shadow the real one —
    # theozolith_worker.api resolves from the product venv, drivers.<name> from
    # the distribution, one interpreter (ADR-0042).
    if dist_root not in sys.path:
        sys.path.append(dist_root)
    module = importlib.import_module(f"{CUSTOM_DRIVER_PREFIX.rstrip('/')}.{name}")
    driver_cls = getattr(module, "Driver", None)
    if not isinstance(driver_cls, type) or not issubclass(driver_cls, Worker):
        found = "nothing" if driver_cls is None else type(driver_cls).__name__
        raise DriverStartError(
            f"drivers/{name} must export a top-level 'Driver' subclassing"
            f" theozolith_worker.api.Worker (ADR-0042); found {found}"
        )
    _log_fork_header(module, ref=f"{CUSTOM_DRIVER_PREFIX}{name}", log=log)
    return driver_cls


def _startup_sink(environ) -> events.EventSink | None:
    """A ``ControlNodeSink`` from the driver channel env, or ``None`` when no
    Control Node URL is configured. Never raises — start-crash reporting is
    best-effort and must not itself crash the launcher (ADR-0042)."""
    try:
        url = env_value(environ, "CONTROL_NODE_URL")
        if not url:
            return None
        return events.ControlNodeSink(
            url,
            token=env_value(environ, "THEOZOLITH_NODE_TOKEN") or "",
            ca=env_value(environ, "THEOZOLITH_TLS_CA"),
        )
    except Exception:
        return None


def _emit_startup_error(
    environ, *, error_class: str, message: str, context: str, sink=None
) -> None:
    """Best-effort ``theozolith.error`` for a custom-driver start crash
    (ADR-0042). Never raises: a failed error event must not mask the real
    failure or change the exit code. The component is fixed ``driver-host`` — no
    worker type (hence no role) is known this early — and the node/stack come
    straight from the daemon-injected env."""
    try:
        sink = sink if sink is not None else _startup_sink(environ)
        if sink is None:
            return
        sink.emit(
            {
                "type": events.ERROR_EVENT,
                "node": env_value(environ, "THEOZOLITH_NODE_NAME") or "",
                "component": DRIVER_HOST_COMPONENT,
                "stack": env_value(environ, "THEOZOLITH_STACK") or "",
                "error_class": error_class,
                "message": str(message)[: events.ERROR_MESSAGE_CHARS],
                "context": str(context)[-events.ERROR_CONTEXT_CHARS :],
            }
        )
    except Exception:
        pass


def _run_custom_driver(ref: str, *, once: bool, environ=None, sink=None, log=_log) -> int:
    """Run a ``drivers/<name>`` custom driver. A start-time crash writes a
    traceback to stderr, emits a best-effort ``theozolith.error``, and exits 1;
    the dispatch loop itself surfaces its own per-pass errors (base.Worker)."""
    environ = os.environ if environ is None else environ
    name = ref[len(CUSTOM_DRIVER_PREFIX) :]
    try:
        if not CUSTOM_DRIVER_NAME.fullmatch(name):
            raise DriverStartError(
                f"invalid custom driver ref {ref!r} — the name after 'drivers/' must"
                " be a valid Python identifier (^[a-z_][a-z0-9_]*$); dashes are"
                " unimportable (ADR-0042)"
            )
        driver_cls = _load_custom_driver(name, environ, log=log)
    except Exception as exc:  # any start crash: import failures included
        context = traceback.format_exc()
        print(context, file=sys.stderr, flush=True)
        _emit_startup_error(
            environ,
            error_class=type(exc).__name__,
            message=f"custom driver {ref!r} failed at start: {exc}",
            context=context,
            sink=sink,
        )
        return 1
    try:
        run_driver(driver_cls, once=once)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"{driver_cls.role} driver stopped", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-driver",
        description=(
            "TheOzolith driver launcher: run a worker type's dispatch-run loop."
            " Built-in refs: " + ", ".join(sorted(BUILTIN_WORKERS)) + "."
            " Custom drivers: drivers/<name> (ADR-0042)."
        ),
    )
    parser.add_argument(
        "ref",
        help="worker type ref: builtin:implementer, builtin:reviewer, or drivers/<name>",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="One dispatch-run pass (at most one claim), then exit. Requires a"
        " reachable Control Node (ADR-0017).",
    )
    args = parser.parse_args(argv)
    if args.ref.startswith(CUSTOM_DRIVER_PREFIX):
        return _run_custom_driver(args.ref, once=args.once)
    driver_cls = BUILTIN_WORKERS.get(args.ref)
    if driver_cls is None:
        known = ", ".join(sorted(BUILTIN_WORKERS))
        print(
            f"error: unknown driver ref {args.ref!r} (known: {known}; or drivers/<name>)",
            file=sys.stderr,
        )
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
