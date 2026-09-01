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

import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from theozolith_control import bearerhttp, repolock
from theozolith_control.controltoml import COMMIT_AUTHOR_EMAIL, COMMIT_AUTHOR_NAME

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
    the recorded version IS the deployment decision (ADR-0006/0015). The
    write-and-commit holds the shared pinned-build write lock (ADR-0048
    amendment): a pin bump can never land inside an ingest transaction's
    window and be overwritten or orphaned by its ref move — the contending
    writer fails cleanly and retries instead."""
    if not version:
        raise ProductError("refusing to pin an empty version (never deploy an unrecorded version)")
    try:
        with repolock.pinned_write_lock(config_repo, writer="product-pin write"):
            _write_pin_locked(config_repo, version, runner, log)
    except repolock.RepoLockError as exc:
        raise ProductError(str(exc)) from exc


def _write_pin_locked(config_repo: Path, version: str, runner, log) -> None:
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
            f"user.name={COMMIT_AUTHOR_NAME}",
            "-c",
            f"user.email={COMMIT_AUTHOR_EMAIL}",
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


def _pip_wheel(component_dir: Path, out_dir: Path, runner) -> str:
    """Build one component wheel and return its filename. ``--no-deps`` makes
    pip produce exactly one wheel; it is built into a private staging dir
    under ``out_dir`` and moved into place, so ``build_distribution`` can name
    exactly the wheels THIS run produced even when ``out_dir`` is a persistent
    ``dist/`` still holding a previous build's wheels (the bootstrap shim's
    case — installing that stale set alongside the fresh one is the two-version
    conflict pip refuses)."""
    with tempfile.TemporaryDirectory(prefix=".wheel-", dir=out_dir) as stage:
        proc = runner(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(stage),
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
        built = [p for p in Path(stage).iterdir() if p.name.endswith(".whl")]
        if len(built) != 1:
            raise ProductError(
                f"expected exactly one wheel for {component_dir.name}, pip produced {len(built)}"
            )
        wheel = built[0]
        # stage is under out_dir, so this is a same-filesystem atomic rename.
        os.replace(wheel, out_dir / wheel.name)
        return wheel.name


def build_distribution(
    source: Path, out_dir: Path, *, runner=subprocess.run, log=_log
) -> tuple[str, list[str]]:
    """Build the whole distribution from a CLEAN checkout into ``out_dir``
    (``source_version`` refuses a dirty tree). Returns (version, wheel
    filenames)."""
    version = source_version(source, runner=runner)
    out_dir.mkdir(parents=True, exist_ok=True)
    wheels: list[str] = []
    with tempfile.TemporaryDirectory(prefix="theozolith-build-") as sandbox:
        for component in COMPONENTS:
            staged = Path(sandbox) / component
            shutil.copytree(source / component, staged, ignore=_BUILD_IGNORES)
            _stamp_version(staged, version)
            # Only this run's wheels — never whatever else out_dir already holds:
            # a persistent dist/ (the bootstrap shim) accumulates prior SHAs'
            # wheels, and installing two versions of a package is what pip refuses.
            wheels.append(_pip_wheel(staged, out_dir, runner))
    wheels = sorted(wheels)
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


# -- the managed venv install (shared by `theozolith build` and build.py) --------

# The managed environment (ADR-0041): a system venv the service user can reach
# (a home venv is refused by init's exec policy — ADR-0034), the same layout
# `install-nodedaemon.sh` builds on node-shaped boxes. `_cmd_build` compares
# this against sys.prefix to decide whether a build also installs locally.
MANAGED_VENV = Path("/opt/theozolith")

# The service user the Node Daemon runs as (ADR-0037): it owns the managed
# venv so it can self-update into it (ADR-0015).
SERVICE_USER = "ozolith"

# The entry points reachable without the venv on PATH: the human CLI and its
# deprecated alias (ADR-0032), plus the daemon CLI that `theozolith init
# --with-local-node` resolves from PATH (ADR-0037). The remaining console
# scripts are machine-run by absolute path — the Node Daemon resolves
# theozolith-driver itself (ADR-0020, stacks.resolve_launcher).
LINKED_ENTRY_POINTS = (
    "theozolith",
    "theozolith-control",  # the deprecated alias, linked for its one release
    "theozolith-nodedaemon",
)
LINK_DIR = Path("/usr/local/bin")


def hand_venv_to_service_user(
    venv: Path, *, user: str = SERVICE_USER, runner=None, log=_log
) -> bool:
    """Give the managed venv to the node service user so the Node Daemon can
    self-update into it (ADR-0015). A no-op returning False on a box with no
    such user (a control-only host) or when not running as root — the two
    shapes where there is nothing to hand over — so every local install can
    call it unconditionally and a rebuild on the Control Node host never hands
    the venv back to root. Raises ProductError if the chown itself fails."""
    import pwd

    runner = subprocess.run if runner is None else runner
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False  # no node service user on this box: nothing to hand over
    if os.geteuid() != 0:
        return False  # an unprivileged install cannot chown; skip quietly
    proc = runner(
        ["chown", "-R", f"{user}:{user}", str(venv)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ProductError(
            f"could not hand {venv} to the {user} service user:"
            f" {(proc.stderr or proc.stdout or '').strip()[:300]}"
        )
    log(f"handed {venv} to the {user} service user")
    return True


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


def install_distribution(venv: Path, wheels: list[Path], *, runner=None, log=_log) -> None:
    """Install the built wheels into ``venv`` and make it usable: pip-install
    (``venv/bin/python -m pip install --upgrade <wheels>``, the built wheels so
    the two entry paths stay byte-identical), and — ONLY when ``venv`` is the
    managed venv (ADR-0041) — link the managed entry points and hand the venv
    to the service user (ADR-0015). Both the links and the handover share that
    one gate: an unmanaged ``--venv`` target is never linked and never chowned
    to the service user (the documented escape hatch stays hands-off). Raises
    ProductError with pip's tail on a failed install; the entry-point
    publication raises SystemExit on a malformed install. One implementation
    for both ``theozolith build`` and the bootstrap shim."""
    runner = subprocess.run if runner is None else runner
    python = venv / "bin" / "python"
    proc = runner(
        [str(python), "-m", "pip", "install", "--upgrade", *(str(w) for w in wheels)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ProductError(
            f"pip install of the built wheels into {venv} failed:"
            f" {(proc.stderr or proc.stdout or '').strip()[-400:]}"
        )
    if venv.resolve() == MANAGED_VENV.resolve():
        link_entry_points(venv)
        # Hand the venv over only for the managed venv — the same gate as the
        # links (ADR-0041). `sudo python3 build.py --venv <dev-venv>` on a box
        # that happens to have an ozolith user must never chown that arbitrary
        # dev venv to the service user: --venv is the unmanaged escape hatch
        # (no root assumed, no links, and now no ownership handover).
        hand_venv_to_service_user(venv, runner=runner, log=log)


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
    try:
        bearerhttp.open_bearer(request, ca=ca, timeout=120)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise SystemExit(
            f"error: upload of {path.name} refused (HTTP {exc.code}): {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"error: cannot reach {url}: {exc.reason}") from exc
    except bearerhttp.BearerTransportError as exc:
        raise SystemExit(f"error: {exc}") from exc


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


def _wheels_from_dist(dist_dir: Path, version: str) -> list[Path]:
    """The component wheels at EXACTLY the checkout's version: a persistent
    ``dist/`` (the bootstrap shim's) accumulates prior SHAs' wheels, so
    selection is by version — stale wheels simply never match (the ``+`` in
    the PEP 440 local version is glob-inert) — and a missing or ambiguous
    component wheel refuses loudly."""
    wheels: list[Path] = []
    for component in COMPONENTS:
        matches = sorted(dist_dir.glob(f"theozolith_{component}-{version}-*.whl"))
        if len(matches) != 1:
            raise ProductError(
                f"--dist {dist_dir}: expected exactly one theozolith_{component}"
                f" wheel at version {version}, found {len(matches)} — the dist"
                " dir does not match the checkout; re-run the build (or drop"
                " --dist)"
            )
        wheels.append(matches[0])
    return wheels


def _local_install_skip_reason(args) -> str | None:
    """Why ``theozolith build`` will NOT also install into this box's venv, or
    None when it will. Eligible only from the managed venv, as root, without
    --no-install: the everyday command completes the edit-to-fleet step on the
    Control Node host (ADR-0051 amendment), while a dev-box publish (the
    unmanaged venv, non-root) is unchanged."""
    if args.no_install:
        return "--no-install"
    if Path(sys.prefix).resolve() != MANAGED_VENV.resolve():
        return f"not the managed venv ({sys.prefix})"
    if os.geteuid() != 0:
        return "not root"
    return None


@contextlib.contextmanager
def _staged_wheels(args, source: Path):
    """(version, [wheel Path]) for one build: the pre-built ``--dist`` set
    (the shim's wheels, no second build) or a fresh build into a temp dir. A
    fresh build's temp dir lives for the whole ``with`` body so install and
    upload both see the files; ``--dist`` reuses a persistent dir untouched."""
    if args.dist:
        # source_version still runs first — the clean-tree refusal and the
        # version the wheels must carry come from the same place.
        version = source_version(source)
        yield version, _wheels_from_dist(Path(args.dist).resolve(), version)
    else:
        with tempfile.TemporaryDirectory(prefix="theozolith-wheels-") as staging:
            out_dir = Path(staging)
            version, names = build_distribution(source, out_dir)
            yield version, [out_dir / name for name in names]


def _cmd_build(args) -> int:
    # Lazy imports: this module stays stdlib-only at import time (ADR-0030).
    from theozolith_control import statuscli
    from theozolith_control.cli import _call

    source = Path(args.source).resolve()
    # Decide the local install up front (ADR-0051 amendment): `sudo theozolith
    # build` from the managed venv installs the built wheels into THIS box too,
    # so the everyday command brings this box and the fleet to the new SHA in
    # one step. A non-root or unmanaged invocation, or --no-install (the shim's
    # chained publish), skips it and just publishes.
    skip_reason = _local_install_skip_reason(args)
    local_install = skip_reason is None

    # Target resolution has ONE implementation (statuscli.resolve_target,
    # ADR-0039); --if-initialized converts exactly its refusal — the two
    # uninitialized-box shapes, no URL / no admin token — into a skip, so the
    # bootstrap shim's chained publish (ADR-0051) never needs to probe init
    # state itself. Every failure AFTER resolution stays loud.
    publish = True
    skip_exc: statuscli.TargetError | None = None
    try:
        url, token, ca = statuscli.resolve_target(args.url, args.ca)
    except statuscli.TargetError as exc:
        if not args.if_initialized:
            raise SystemExit(f"error: {exc}") from exc
        publish = False
        skip_exc = exc

    if publish:
        # Pre-flight: building four wheels takes minutes, and every one is
        # wasted if the Control Node is not up. Fail here, before the work.
        _call(url, "/api/v1/healthz", token=token, ca=ca)
    elif not local_install:
        # No Control Node target and nothing to install locally: nothing to do
        # but the bootstrap-first-boot notice (the build would be wasted).
        _skip_publish_notice(skip_exc)
        return 0

    with _staged_wheels(args, source) as (version, wheels):
        if local_install:
            # A failed local install stops BEFORE any upload — the fleet is
            # never carried ahead of the Control Node host (ADR-0051 amendment).
            install_distribution(MANAGED_VENV, wheels)
            _log(
                f"installed {version} into {MANAGED_VENV} on this box — restart"
                " theozolith-control.service to run it; the local Node Daemon"
                " converges on its next heartbeat"
            )
        else:
            _log(
                f"local install skipped ({skip_reason}); this box converges through its Node Daemon"
            )
        if not publish:
            _skip_publish_notice(skip_exc)
            return 0
        for path in wheels:
            _upload_artifact(url, token, ca, version, path)
        _log(f"uploaded {len(wheels)} wheel(s); the Control Node serves them for node pulls")
    return _update_via_api(args, version)


def _skip_publish_notice(exc) -> None:
    _log(f"publish skipped: {exc}")
    _log(
        "(no Control Node on this box yet — after 'sudo theozolith init',"
        " 'sudo theozolith build' serves the wheels and pins the version)"
    )


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
        digest = str(answer.get("ca_sha256") or "")
        if digest:
            colon_fp = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()
            _log("")
            _log(f"CA SHA-256:        {colon_fp}")
            _log("  (the fingerprint this deployment's nodes pin — use it to verify a")
            _log("  browser ca.pem before trusting it: openssl x509 -in ca.pem -noout")
            _log("  -fingerprint -sha256)")
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
    build.add_argument(
        "--dist",
        help="upload the pre-built wheels in this directory (they must match"
        " the checkout's clean-tree version) instead of rebuilding — the"
        " bootstrap shim's one-step publish path (ADR-0051)",
    )
    build.add_argument(
        "--if-initialized",
        action="store_true",
        help="skip with a notice (exit 0) when this box has no Control Node"
        " target yet (bootstrap first boot) instead of failing",
    )
    build.add_argument(
        "--no-install",
        action="store_true",
        help="publish only: skip installing the built wheels into this box's"
        " managed venv (the bootstrap shim passes this after its own install)",
    )
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
