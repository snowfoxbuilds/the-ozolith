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
managed bootstrap. From then on, source-based updates are ``theozolith
build``. ``--venv PATH`` is the unmanaged escape hatch (dev checkouts,
tests): same build and install, no root, no links.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import secrets
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


def _own_temp_symlink(target: Path, link_dir: Path, name: str) -> Path:
    """Create a temporary symlink to ``target`` under a name this invocation
    provably owns: collision-resistant randomness plus exclusive creation
    (symlink(2) fails EEXIST on any existing path, dangling links included).
    An occupied candidate name — whatever occupies it — is left alone and
    another name chosen; no path is ever "ours by name"."""
    for _ in range(32):
        tmp = link_dir / f".{name}.{secrets.token_hex(8)}.tmp"
        try:
            os.symlink(target, tmp)
        except FileExistsError:
            continue
        return tmp
    raise SystemExit(
        f"error: could not create a temporary link in {link_dir} — 32 randomly"
        f" named .{name}.*.tmp candidates were already taken; clean the"
        " directory up and re-run"
    )


def link_entry_points(venv: Path, *, link_dir: Path | None = None) -> None:
    """Publish the human-reachable entry points into ``link_dir``. Every
    source (a non-symlink regular executable file) and every destination
    (absent, or already this installation's symlink) is validated before
    the directory is created or anything else is touched; any other
    occupant is refused by name, never unlinked. Each destination then
    lands atomically — an exclusively created temp symlink renamed over it
    in the same directory — but the set of three is sequential, not
    transactional: an interruption can leave a valid subset published, and
    a re-run converges on the full set. Only temp links this invocation
    itself created are ever removed."""
    link_dir = LINK_DIR if link_dir is None else link_dir
    publishable: list[tuple[Path, Path]] = []
    for name in LINKED_ENTRY_POINTS:
        target = venv / "bin" / name
        if target.is_symlink() or not target.is_file() or not os.access(target, os.X_OK):
            raise SystemExit(
                f"error: the install produced no executable regular file at"
                f" {target} (missing, non-executable, or a symlink) — the wheel"
                " set looks incomplete; nothing was linked"
            )
        link = link_dir / name
        if link.is_symlink():
            existing = os.readlink(link)
            if existing != str(target):
                raise SystemExit(
                    f"error: {link} is a symlink to {existing}, not to this"
                    f" installation's {target} — refusing to replace it; remove"
                    " it yourself and re-run; nothing was linked"
                )
        elif link.exists():
            kind = "directory" if link.is_dir() else "regular file"
            raise SystemExit(
                f"error: {link} exists and is a {kind} — refusing to overwrite"
                " it; move it aside and re-run; nothing was linked"
            )
        publishable.append((target, link))
    link_dir.mkdir(parents=True, exist_ok=True)
    for target, link in publishable:
        tmp = None
        try:
            tmp = _own_temp_symlink(target, link_dir, link.name)
            os.replace(tmp, link)
        except OSError as exc:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    tmp.unlink()
            raise SystemExit(
                f"error: could not publish {link}: {exc} — links published"
                " before this one are valid; re-run to converge on the full set"
            ) from None


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
    if managed:
        require_root()
    check_pip(Path(sys.executable))
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
