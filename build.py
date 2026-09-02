#!/usr/bin/env python3
"""Bootstrap build for a bare checkout (ADR-0023; environment-managed, ADR-0041).

``theozolith build`` cannot be the first command — it presupposes an
installed CLI. This shim is the one sanctioned exception to "never a script
run out of the repo directory": it exists to end that state. It wraps the
SAME build implementation ``theozolith build`` wraps
(``theozolith_control.product.build_distribution`` — one implementation,
two entry paths; they cannot drift).

The operator never manages an environment (ADR-0041): run as root, the shim
creates (or reuses) the ``/opt/theozolith`` venv, re-executes itself with
that interpreter, builds every component wheel from the CLEAN checkout into
``dist/``, installs them there, and publishes the human entry points into
``/usr/local/bin`` (validated first, linked atomically, foreign paths
refused — never overwritten) — so ``sudo python3 build.py`` is the whole
managed bootstrap. It then chains into the fleet publish (ADR-0051): the
just-installed venv CLI runs ``theozolith build --dist dist/
--if-initialized`` as a subprocess, reusing this run's wheels — a box with
no Control Node yet skips with a notice (the bootstrap-first-boot case),
an initialized box that fails to publish fails THIS command, and
``--no-publish`` opts out. ``--venv PATH`` is the unmanaged escape hatch
(dev checkouts, tests): same build, install, and publish attempt, no root,
no links.

This is the FIRST-DAY command; every day after, ``sudo theozolith build``
does everything this does — build, install locally, serve, and pin (ADR-0051
amendment) — so the chained publish passes ``--no-install`` (this shim
already installed) and the operator never runs both.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import secrets  # noqa: F401 — re-exported so tests can monkeypatch shim.secrets.token_hex
import shutil
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 — the guard exists FOR older interpreters
    sys.exit(
        "error: theozolith needs python3 >= 3.11"
        f" (this is {sys.version_info[0]}.{sys.version_info[1]})"
    )

REPO_ROOT = Path(__file__).resolve().parent

# The shared implementation, imported straight from the checkout: the product
# module is deliberately stdlib-only at import time (component separability),
# so a bare interpreter can run the build. Since ADR-0051's amendment the
# install machinery (managed venv, entry-point links, the install helper) lives
# there too — one implementation, so `theozolith build` and this shim cannot
# drift — and is re-imported here by name.
sys.path.insert(0, str(REPO_ROOT / "control" / "src"))

from theozolith_control.product import (  # noqa: E402
    LINK_DIR,
    LINKED_ENTRY_POINTS,  # noqa: F401 — re-exported for test_build_shim
    MANAGED_VENV,
    ProductError,
    build_distribution,
    install_distribution,
    link_entry_points,  # noqa: F401 — re-exported for test_build_shim
)

# Belt over braces for the re-exec: a venv interpreter that runs but does
# not identify as the venv would exec-loop; the marker turns pass two into
# a hard error. One broken-venv shape only — a missing, non-executable, or
# unrunnable interpreter is caught by its own check before the exec.
REEXEC_MARKER = "THEOZOLITH_BOOTSTRAP_REEXEC"


def inside(venv: Path) -> bool:
    """True when this interpreter IS the target venv's."""
    return Path(sys.prefix).resolve() == venv.resolve()


def require_root(geteuid=None) -> None:
    """The managed bootstrap writes system paths — refused without root on
    BOTH passes (the second pass is reachable directly: the documented
    `sudo /opt/theozolith/bin/python build.py` spelling short-circuits)."""
    geteuid = os.geteuid if geteuid is None else geteuid
    if geteuid() != 0:
        raise SystemExit(
            f"error: the managed bootstrap writes {MANAGED_VENV} and {LINK_DIR}"
            " — run with sudo (or pass --venv PATH for an unmanaged dev install)"
        )


def ensure_environment(
    venv: Path,
    argv: list[str],
    *,
    managed: bool,
    runner=None,
    geteuid=None,
    execv=None,
    environ=None,
    find_spec=None,
) -> None:
    """Create-or-reuse the target venv, health-check its interpreter, and
    re-execute this shim with it (ADR-0041) — after which the build and
    install run inside it unchanged. Every malformed venv shape is refused
    with remediation (repair or delete), and the shim never package-manages
    on its own (the ADR-0037 posture); pip health is pre-flighted by
    check_pip on the in-venv pass, which every entry shape funnels through.
    Does not return except under test."""
    runner = subprocess.run if runner is None else runner
    geteuid = os.geteuid if geteuid is None else geteuid
    execv = os.execv if execv is None else execv
    environ = os.environ if environ is None else environ
    find_spec = importlib.util.find_spec if find_spec is None else find_spec
    if environ.get(REEXEC_MARKER):
        raise SystemExit(
            f"error: re-executed with {sys.executable} but it does not identify"
            f" as {venv} — the venv is broken; delete it and re-run"
        )
    if managed:
        require_root(geteuid)
    if venv.exists() and not (venv / "pyvenv.cfg").is_file():
        raise SystemExit(
            f"error: {venv} exists but is not a virtual environment — refusing"
            " to touch it; move it aside and re-run"
        )
    if not (venv / "pyvenv.cfg").is_file():
        if find_spec("ensurepip") is None:
            raise SystemExit(
                "error: this interpreter cannot create virtual environments"
                " (no ensurepip) — install your distro's python3-venv package"
                " (Debian/Ubuntu: apt install python3-venv) and re-run"
            )
        proc = runner([sys.executable, "-m", "venv", str(venv)], check=False)
        if proc.returncode != 0:
            # The target did not exist before this run's create (the non-venv
            # guard above), so the partial tree is this run's alone — removing
            # it keeps a re-run starting from scratch, not from debris.
            shutil.rmtree(venv, ignore_errors=True)
            raise SystemExit(
                f"error: venv creation failed with exit status {proc.returncode}"
                f" ('{sys.executable} -m venv {venv}' — its output above has the"
                " cause); nothing was kept — fix that and re-run"
            )
    python = venv / "bin" / "python"
    if not python.is_file():
        raise SystemExit(
            f"error: {venv} has no bin/python — the venv is broken; delete it and re-run"
        )
    if not os.access(python, os.X_OK):
        raise SystemExit(
            f"error: {python} is not executable — the venv is broken; restore its"
            " execute permission to repair it, or delete the venv and re-run"
        )
    environ[REEXEC_MARKER] = "1"
    try:
        execv(str(python), [str(python), str(REPO_ROOT / "build.py"), *argv])
    except OSError as exc:
        raise SystemExit(
            f"error: could not execute {python} ({exc.strerror or exc}) — the"
            " venv is broken; repair its interpreter or delete the venv and re-run"
        ) from None


def check_pip(python: Path, *, runner=None) -> None:
    """Pre-flight the exact operation the install step runs — ``python -m
    pip`` (a ``bin/pip`` wrapper on disk proves nothing about the module) —
    before any build work starts. Called on the in-venv pass, which every
    entry shape funnels through: create + re-exec, reuse + re-exec, and the
    direct ``sudo /opt/theozolith/bin/python build.py`` spelling."""
    runner = subprocess.run if runner is None else runner
    try:
        proc = runner(
            [str(python), "-m", "pip", "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(
            f"error: could not run '{python} -m pip' ({exc.strerror or exc}) —"
            " the venv cannot install the built wheels; repair its pip module"
            " or delete the venv and re-run"
        ) from None
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        cause = f" ({stderr[-1]})" if stderr else ""
        raise SystemExit(
            f"error: '{python} -m pip --version' failed with exit status"
            f" {proc.returncode}{cause} — the venv cannot install the built"
            " wheels; repair its pip module or delete the venv and re-run"
        )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="bootstrap a bare checkout: build every component wheel and"
        " install the theozolith CLI (ADR-0023/0041)"
    )
    parser.add_argument(
        "--venv",
        help="unmanaged escape hatch: build and install into this venv instead"
        f" of the managed {MANAGED_VENV} (no root, no {LINK_DIR} links)",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="install only: skip the chained 'theozolith build' publish to"
        " the Control Node (ADR-0051)",
    )
    args = parser.parse_args(argv)
    managed = args.venv is None
    venv = MANAGED_VENV if managed else Path(args.venv).resolve()
    if not inside(venv):
        ensure_environment(venv, argv, managed=managed)
        return 0  # unreachable outside tests: ensure_environment re-execs or raises
    if managed:
        require_root()
    check_pip(Path(sys.executable))
    out_dir = REPO_ROOT / "dist"
    try:
        version, wheels = build_distribution(REPO_ROOT, out_dir)
        # Installing the built wheels (not the source trees) keeps the two
        # entry paths byte-identical: what this box runs is what nodes pull.
        # The shared helper pip-installs, links the managed entry points (when
        # this IS the managed venv), and hands the venv to the service user —
        # ONE implementation, so `theozolith build` and this shim can't drift.
        install_distribution(venv, [out_dir / name for name in wheels])
    except ProductError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"built and installed {len(wheels)} wheel(s) at version {version} into {venv}")
    if args.no_publish:
        print("publish skipped (--no-publish); run 'theozolith build' to serve")
        print("the wheels and pin the version when you are ready.")
        return 0
    # The chained publish (ADR-0051) runs the just-installed venv CLI as a
    # SUBPROCESS — never an in-process import: the checkout's modules (this
    # shim's sys.path) and the installed ones must not mix — reusing the
    # wheels this run built (--dist skips the second multi-minute build).
    # --if-initialized keeps the ADR-0030 bootstrap case: a box with no
    # Control Node target yet skips with a notice inside the subprocess; an
    # initialized box that fails to publish fails THIS command. --no-install:
    # this shim already installed locally, so the subprocess publishes only
    # (its own local-install eligibility would double the work — ADR-0051).
    cli = venv / "bin" / "theozolith"
    publish_argv = [
        str(cli),
        "build",
        "--source",
        str(REPO_ROOT),
        "--dist",
        str(out_dir),
        "--if-initialized",
        "--no-install",
    ]
    try:
        proc = subprocess.run(publish_argv, check=False)
    except OSError as exc:
        print(
            f"error: could not run {cli} ({exc.strerror or exc}) — the install"
            " looks incomplete; re-run the bootstrap",
            file=sys.stderr,
        )
        return 1
    if proc.returncode != 0:
        print(
            "error: the wheels installed on this box but the publish failed —"
            " the fleet was NOT updated; fix the cause above and run"
            " 'sudo theozolith build' (or re-run with --no-publish to defer)",
            file=sys.stderr,
        )
        return proc.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
