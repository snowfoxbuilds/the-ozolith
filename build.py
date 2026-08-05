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
``dist/``, installs them there, and links the human entry points into
``/usr/local/bin`` — so ``sudo python3 build.py`` is the whole bootstrap.
From then on, source-based updates are ``theozolith build``. ``--venv
PATH`` is the unmanaged escape hatch (dev checkouts, tests): same build and
install, no root, no links.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 11):  # noqa: UP036 — the guard exists FOR older interpreters
    sys.exit(
        "error: theozolith needs python3 >= 3.11"
        f" (this is {sys.version_info[0]}.{sys.version_info[1]})"
    )

REPO_ROOT = Path(__file__).resolve().parent

# The shared implementation, imported straight from the checkout: the
# product module is deliberately stdlib-only at import time (component
# separability), so a bare interpreter can run the build.
sys.path.insert(0, str(REPO_ROOT / "control" / "src"))

from theozolith_control.product import ProductError, build_distribution  # noqa: E402

# The managed environment (ADR-0041): a system path the service user can
# reach (a home venv is refused by init's exec policy — ADR-0034), the same
# venv layout `install-nodedaemon.sh` builds on node-shaped boxes.
MANAGED_VENV = Path("/opt/theozolith")

# The entry points reachable without the venv on PATH: the human CLI and
# its deprecated alias (ADR-0032), plus the daemon CLI that `theozolith
# init --with-local-node` resolves from PATH (ADR-0037). The remaining
# console scripts are machine-run by absolute path.
LINKED_ENTRY_POINTS = (
    "theozolith",
    "theozolith-control",  # the deprecated alias, linked for its one release
    "theozolith-nodedaemon",
)
LINK_DIR = Path("/usr/local/bin")

# Belt over braces for the re-exec: if the venv interpreter does not
# identify as the venv, fail loudly instead of exec-looping.
REEXEC_MARKER = "THEOZOLITH_BOOTSTRAP_REEXEC"


def inside(venv: Path) -> bool:
    """True when this interpreter IS the target venv's."""
    return Path(sys.prefix).resolve() == venv.resolve()


def ensure_environment(
    venv: Path,
    argv: list[str],
    *,
    managed: bool,
    runner=subprocess.run,
    geteuid=os.geteuid,
    execv=os.execv,
    environ=os.environ,
    find_spec=importlib.util.find_spec,
) -> None:
    """Create-or-reuse the target venv and re-execute this shim with its
    interpreter (ADR-0041) — after which the build and install run inside
    it unchanged. Refuses with remediation and never package-manages on
    its own (the ADR-0037 posture). Does not return except under test."""
    if environ.get(REEXEC_MARKER):
        raise SystemExit(
            f"error: re-executed with {sys.executable} but it does not identify"
            f" as {venv} — the venv looks broken; delete it and re-run"
        )
    if managed and geteuid() != 0:
        raise SystemExit(
            f"error: the managed bootstrap writes {venv} and {LINK_DIR} — run"
            " with sudo (or pass --venv PATH for an unmanaged dev install)"
        )
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
            raise SystemExit(f"error: could not create the venv at {venv}")
    python = venv / "bin" / "python"
    if not python.is_file():
        raise SystemExit(f"error: {venv} has no bin/python — delete the venv and re-run")
    environ[REEXEC_MARKER] = "1"
    execv(str(python), [str(python), str(REPO_ROOT / "build.py"), *argv])


def link_entry_points(venv: Path, *, link_dir: Path = LINK_DIR) -> None:
    """The human-reachable entry points on PATH without the venv on it —
    idempotent, last install wins."""
    link_dir.mkdir(parents=True, exist_ok=True)
    for name in LINKED_ENTRY_POINTS:
        target = venv / "bin" / name
        if not target.is_file():
            raise SystemExit(
                f"error: the install produced no {target} — the wheel set looks incomplete"
            )
        link = link_dir / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)


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
    args = parser.parse_args(argv)
    managed = args.venv is None
    venv = MANAGED_VENV if managed else Path(args.venv).resolve()
    if not inside(venv):
        ensure_environment(venv, argv, managed=managed)
        return 0  # unreachable outside tests: ensure_environment re-execs or raises
    out_dir = REPO_ROOT / "dist"
    try:
        version, wheels = build_distribution(REPO_ROOT, out_dir)
    except ProductError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Installing the built wheels (not the source trees) keeps the two entry
    # paths byte-identical: what this box runs is what nodes will pull.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            *(str(out_dir / name) for name in wheels),
        ],
        check=False,
    )
    if proc.returncode != 0:
        print("error: pip install of the built wheels failed", file=sys.stderr)
        return proc.returncode
    if managed:
        link_entry_points(venv)
    print(f"built and installed {len(wheels)} wheel(s) at version {version} into {venv}")
    print("next: 'sudo theozolith init' on the Control Node box, then 'theozolith build'")
    print("for future source updates (this shim was only the bootstrap).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
