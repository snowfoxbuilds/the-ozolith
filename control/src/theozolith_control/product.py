"""Two update paths, one machinery (ADR-0015 amendment 2026-07-22).

``theozolith update`` (user path) resolves the latest published release —
or an explicit ``--version`` — and pins it. ``theozolith build`` (developer
path) builds the distribution from the local source checkout, pins the
checkout's git SHA, and uploads the built wheels for the Control Node to
serve to node pulls. A dirty tree is REFUSED (revision ruling amending
ADR-0015): every pin names a committed SHA — a dirty pin is a moving
target where two different trees share one pin and re-uploads silently
overwrite. Local iteration relies on ``theozolith test``, never on
deploying uncommitted state. Both paths converge on
``POST /api/v1/product/update``: the pin bump is committed to product.toml
in the Config Repo, and nodes CONVERGE on the pin — every heartbeat
compares the running version against it — with the fanned-out update
command only an immediate nudge, never the mechanism of record.

Nodes never pull source and never build the product. "Never deploy :latest"
is restated as never deploy an unrecorded version: a fresh install with no
product.toml pin resolves the latest release and writes the pin
(``ensure_pin``, run at serve startup), so a running fleet always has a
recorded version.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

# The components one product version covers (ADR-0013 §8: one versioned
# distribution). Order matters only cosmetically.
COMPONENTS = ("knowledge", "worker", "control", "nodedaemon")

# Where the user path resolves "the latest published release". The anchor
# package is the one every node installs; THEOZOLITH_RELEASE_INDEX_URL
# points air-gapped deployments at their own index.
RELEASE_INDEX_URL = "https://pypi.org/pypi/theozolith-nodedaemon/json"

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class ProductError(RuntimeError):
    """An update-path step could not complete."""


def _env_value(environ, name: str) -> str | None:
    """A local VAR/VAR_FILE reader: this module imports nothing from
    theozolith_worker at import time (component separability)."""
    file_path = environ.get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    value = environ.get(name)
    return value if value else None


def safe_segment(value: str) -> bool:
    """True for version/filename path segments that cannot traverse."""
    return bool(_SAFE_SEGMENT.match(value)) and ".." not in value


def _log(message: str) -> None:
    print(message, flush=True)


# -- release resolution (user path) --------------------------------------------


def resolve_latest_release(http_get=None, environ=None) -> str:
    """The latest published release version, from the release index."""
    environ = os.environ if environ is None else environ
    url = _env_value(environ, "THEOZOLITH_RELEASE_INDEX_URL") or RELEASE_INDEX_URL

    def _default_get(target: str) -> bytes:
        with urllib.request.urlopen(target, timeout=30) as resp:
            return resp.read()

    get = http_get or _default_get
    try:
        payload = json.loads(get(url))
    except Exception as exc:
        raise ProductError(f"cannot resolve the latest release from {url}: {exc}") from exc
    version = payload.get("info", {}).get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version:
        raise ProductError(f"release index {url} answered no version")
    return version


# -- the pin (product.toml in the Config Repo) ----------------------------------


def read_pin(config_repo: Path) -> str:
    try:
        data = tomllib.loads((config_repo / "product.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    version = data.get("product", {}).get("version", "")
    return version if isinstance(version, str) else ""


def write_pin(config_repo: Path, version: str, *, runner=subprocess.run, log=_log) -> None:
    """Write the pin and commit it when the Config Repo is git-backed —
    the recorded version IS the deployment decision (ADR-0006/0015)."""
    if not version:
        raise ProductError("refusing to pin an empty version (never deploy an unrecorded version)")
    config_repo.mkdir(parents=True, exist_ok=True)
    target = config_repo / "product.toml"
    target.write_text(
        "# The deployed product version (ADR-0015, amended 2026-07-22):\n"
        "# written by `theozolith update` / `theozolith build`. Rollback is\n"
        "# re-pinning a previous version with the same command.\n"
        f'[product]\nversion = "{version}"\n',
        encoding="utf-8",
    )
    if not (config_repo / ".git").exists():
        return  # folder mode: the file itself is the record
    status = runner(
        ["git", "status", "--porcelain", "product.toml"],
        cwd=str(config_repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode == 0 and not (status.stdout or "").strip():
        return  # re-pinning the already-recorded version: nothing to commit
    for args in (
        ["git", "add", "product.toml"],
        [
            "git",
            "-c",
            "user.name=theozolith",
            "-c",
            "user.email=theozolith@invalid",
            "commit",
            "-m",
            f"theozolith: pin product version {version}",
        ],
    ):
        proc = runner(args, cwd=str(config_repo), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ProductError(
                f"could not commit the pin bump: {' '.join(args[:2])} failed:"
                f" {(proc.stderr or proc.stdout or '').strip()[:300]}"
            )
    log(f"pinned product version {version} (committed to the Config Repo)")


def ensure_pin(config_repo: Path, *, http_get=None, runner=subprocess.run, log=_log) -> str:
    """A running fleet always has a recorded version (2026-07-22): when
    product.toml lacks a pin, resolve the latest release and write it."""
    version = read_pin(config_repo)
    if version:
        return version
    version = resolve_latest_release(http_get)
    write_pin(config_repo, version, runner=runner, log=log)
    return version


# -- the source build (developer path) -------------------------------------------

# Working-tree noise never worth copying into the build sandbox.
_BUILD_IGNORES = shutil.ignore_patterns(
    "__pycache__", "*.egg-info", ".pytest_cache", ".ruff_cache", "dist", "build", "node_modules"
)


def source_version(source: Path, *, runner=subprocess.run) -> str:
    """The checkout's pin: ``<base>+g<sha12>`` (a PEP 440 local version, so
    wheels carry it and nodes report it back in heartbeats). A dirty tree is
    refused outright — every pin names a committed SHA, or two different
    trees could share one pin and re-uploads would silently overwrite."""
    try:
        rev = runner(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(source),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ProductError(f"{source} is not a git checkout: {exc}") from exc
    if rev.returncode != 0:
        raise ProductError(f"{source} is not a git checkout: {(rev.stderr or '').strip()[:200]}")
    sha = (rev.stdout or "").strip()
    status = runner(
        ["git", "status", "--porcelain"],
        cwd=str(source),
        capture_output=True,
        text=True,
        check=False,
    )
    if (status.stdout or "").strip():
        raise ProductError(
            "refusing to build from a dirty tree: the pin must name a committed"
            " SHA (never deploy an unrecorded version). Commit or stash your"
            " changes; for local iteration run `theozolith test` instead."
        )
    try:
        base = tomllib.loads((source / "nodedaemon" / "pyproject.toml").read_text())["project"][
            "version"
        ]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise ProductError(f"cannot read the base version from {source}: {exc}") from exc
    return f"{base}+g{sha}"


def _stamp_version(component_dir: Path, version: str) -> None:
    pyproject = component_dir / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    stamped = re.sub(r'(?m)^version = ".*"$', f'version = "{version}"', text, count=1)
    pyproject.write_text(stamped, encoding="utf-8")
    for init in (component_dir / "src").glob("*/__init__.py"):
        text = init.read_text(encoding="utf-8")
        init.write_text(
            re.sub(r'(?m)^__version__ = ".*"$', f'__version__ = "{version}"', text, count=1),
            encoding="utf-8",
        )


def _pip_wheel(component_dir: Path, out_dir: Path, runner) -> None:
    proc = runner(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(out_dir),
            str(component_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ProductError(
            f"pip wheel failed for {component_dir.name}:"
            f" {(proc.stderr or proc.stdout or '').strip()[-400:]}"
        )


def build_distribution(
    source: Path, out_dir: Path, *, runner=subprocess.run, log=_log
) -> tuple[str, list[str]]:
    """Build the whole distribution from a CLEAN checkout into ``out_dir``
    (``source_version`` refuses a dirty tree). Returns (version, wheel
    filenames)."""
    version = source_version(source, runner=runner)
    out_dir.mkdir(parents=True, exist_ok=True)
    # A reused out_dir (build.py's ./dist — the CLI path passes a fresh
    # tempdir) may hold wheels from an earlier version; the collection glob
    # below would sweep them into the result and pip would face two
    # versions of every package (ResolutionImpossible on the Pi, 2026-08-01).
    stale = [p for p in out_dir.iterdir() if p.name.endswith(".whl")]
    for wheel in stale:
        wheel.unlink()
    if stale:
        log(f"removed {len(stale)} stale wheel(s) from {out_dir}")
    with tempfile.TemporaryDirectory(prefix="theozolith-build-") as sandbox:
        for component in COMPONENTS:
            staged = Path(sandbox) / component
            shutil.copytree(source / component, staged, ignore=_BUILD_IGNORES)
            _stamp_version(staged, version)
            _pip_wheel(staged, out_dir, runner)
    wheels = sorted(p.name for p in out_dir.iterdir() if p.name.endswith(".whl"))
    if not wheels:
        raise ProductError("the build produced no wheels")
    log(f"built {len(wheels)} wheel(s) at version {version}")
    return version, wheels


def prune_artifacts(artifacts_dir: Path, keep: set[str]) -> list[str]:
    """Cache, not archive: the artifact store holds at most the pinned and
    the previous version sets. Returns the pruned version names."""
    if not artifacts_dir.is_dir():
        return []
    pruned = []
    for entry in artifacts_dir.iterdir():
        if entry.is_dir() and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)
            pruned.append(entry.name)
    return sorted(pruned)


# -- the `theozolith` CLI ---------------------------------------------------------


def _update_via_api(args, version: str) -> int:
    from theozolith_control.cli import _admin_env, _call

    url, token, ca = _admin_env(args)
    answer = _call(
        url, "/api/v1/product/update", token=token, method="POST", body={"version": version}, ca=ca
    )
    queued = answer.get("queued", [])
    _log(f"pinned {answer.get('version', version)}; update queued for: {', '.join(queued) or '-'}")
    _log("nodes apply on their next heartbeat (drain-aware queue-behind); the")
    _log("Control Node's own host was queued last")
    return 0


def _upload_artifact(url: str, token: str, ca: str | None, version: str, path: Path) -> None:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/v1/product/artifacts/{version}/{path.name}",
        data=path.read_bytes(),
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "User-Agent": "theozolith-cli",
        },
    )
    context = None
    if url.startswith("https"):
        context = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=120, context=context) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise SystemExit(
            f"error: upload of {path.name} refused (HTTP {exc.code}): {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: cannot reach {url}: {exc.reason}") from exc


def _cmd_update(args) -> int:
    version = args.version or resolve_latest_release()
    if not args.version:
        _log(f"latest published release: {version}")
    return _update_via_api(args, version)


def _cmd_test(args) -> int:
    """The local-development signal (revision ruling amending ADR-0015):
    iterate with `theozolith test` against the working tree — deploying
    uncommitted state is not a testing path."""
    source = Path(args.source).resolve()
    checks = [
        [sys.executable, "-m", "pytest"],
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
    ]
    for argv in checks:
        _log(f"$ {' '.join(argv[2:] if argv[1] == '-m' else argv)}")
        try:
            proc = subprocess.run(argv, cwd=str(source), check=False)
        except OSError as exc:
            print(f"error: cannot run {argv[2]}: {exc}", file=sys.stderr)
            return 1
        if proc.returncode != 0:
            return proc.returncode
    _log("all checks passed")
    return 0


def _cmd_build(args) -> int:
    from theozolith_control.cli import _admin_env, _call

    source = Path(args.source).resolve()
    url, token, ca = _admin_env(args)
    # Pre-flight: building four wheels takes minutes, and every one of them
    # is wasted if the Control Node is not up. Fail here, before the work.
    _call(url, "/api/v1/healthz", token=token, ca=ca)
    with tempfile.TemporaryDirectory(prefix="theozolith-wheels-") as staging:
        out_dir = Path(staging)
        version, wheels = build_distribution(source, out_dir)
        for name in wheels:
            _upload_artifact(url, token, ca, version, out_dir / name)
        _log(f"uploaded {len(wheels)} wheel(s); the Control Node serves them for node pulls")
    return _update_via_api(args, version)


def _cmd_join_token(args) -> int:
    """Node provisioning tokens (ADR-0023): `create` prints the bare join
    string AND the complete ready-to-paste command — the operator never
    composes the line; `revoke` is the oops backstop."""
    from theozolith_control.cli import _admin_env, _call

    url, token, ca = _admin_env(args)
    if args.join_cmd == "create":
        body: dict = {"ttl_seconds": args.ttl, "uses": args.uses}
        if args.addr:
            body["addr"] = args.addr
        answer = _call(url, "/api/v1/join-tokens", token=token, method="POST", body=body, ca=ca)
        _log(f"join token {answer.get('id')} minted ({args.uses} use(s), {args.ttl:.0f}s TTL)")
        _log("")
        _log(f"join string:      {answer.get('join_string')}")
        _log("")
        _log("node already installed — paste on the box:")
        _log(f"  {answer.get('provision_command')}")
        _log("fresh box — installer over GitHub HTTPS, then provision:")
        _log(f"  {answer.get('install_command')}")
        return 0
    if args.join_cmd == "revoke":
        answer = _call(url, f"/api/v1/join-tokens/{args.id}", token=token, method="DELETE", ca=ca)
        _log("revoked" if answer.get("revoked") else "no such outstanding token")
        return 0
    for entry in _call(url, "/api/v1/join-tokens", token=token, ca=ca).get("tokens", []):
        _log(f"{entry['id']}  uses_left={entry['uses_left']}  expires_at={entry['expires_at']:.0f}")
    return 0


def register(commands) -> None:
    """Register the fleet-operator subcommands on the single ``theozolith``
    parser (ADR-0032). This module stays stdlib-only at import (ADR-0030),
    so the merged parser lives in ``cli`` — which already imports it — and
    calls back in here."""
    update = commands.add_parser(
        "update",
        help="pin the latest published release (or --version) and fan the update out",
    )
    update.add_argument("--version", help="an explicit release to pin (rollback = re-pin)")
    update.set_defaults(func=_cmd_update)

    build = commands.add_parser(
        "build",
        help=(
            "build the distribution from a CLEAN source checkout, pin its git"
            " SHA, serve the wheels, and fan the update out (a dirty tree is"
            " refused — iterate with `theozolith test`)"
        ),
    )
    build.add_argument("--source", default=".", help="source checkout (default: current directory)")
    build.set_defaults(func=_cmd_build)

    test = commands.add_parser(
        "test",
        help="the local-development signal: run the checkout's test and lint suite",
    )
    test.add_argument("--source", default=".", help="source checkout (default: current directory)")
    test.set_defaults(func=_cmd_test)

    join = commands.add_parser(
        "join-token",
        help="mint or revoke node-provisioning join tokens (create prints the paste)",
    )
    join_sub = join.add_subparsers(dest="join_cmd", required=True)
    join_create = join_sub.add_parser(
        "create", help="mint one (default: 1h TTL, single use) and print the paste"
    )
    join_create.add_argument(
        "--ttl", type=float, default=3600.0, help="seconds until expiry (default 3600)"
    )
    join_create.add_argument(
        "--uses", type=int, default=1, help="redemptions allowed (default 1; batches widen it)"
    )
    join_create.add_argument(
        "--addr",
        help="bootstrap address nodes dial, host[:port] (default: this box's IP + the"
        " bootstrap port)",
    )
    join_create.set_defaults(func=_cmd_join_token)
    join_revoke = join_sub.add_parser("revoke", help="revoke an outstanding token by id")
    join_revoke.add_argument("id")
    join_revoke.set_defaults(func=_cmd_join_token)
    join_sub.add_parser("list", help="outstanding tokens (ids and windows only)").set_defaults(
        func=_cmd_join_token
    )


def main(argv: list[str] | None = None) -> int:
    # Lazy: this module must import stdlib-only for the build.py bootstrap
    # (ADR-0030); the merged parser and its dependencies load only when run.
    from theozolith_control.cli import main as merged_main

    return merged_main(argv)


if __name__ == "__main__":
    sys.exit(main())
