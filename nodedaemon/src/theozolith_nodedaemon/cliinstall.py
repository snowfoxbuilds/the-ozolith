"""Fail-closed CLI Pin installation (ADR-0055 point 4).

The Node Daemon converges each pinned agent CLI version into
``<state-dir>/cli/<tool>/<version>/claude`` through ORDERED GATES, each
failing before the next runs: platform-tuple selection from the pinned map
(before any download), a bounded staged download, SRI integrity over the
COMPLETE tarball before any extraction, archive validation of EVERY member
before any is extracted, manual member-by-member extraction (no
``extractall``, no archive-controlled destination ever), normalized ownership
and modes (archive metadata is never trusted), and one same-filesystem atomic
rename into the published version directory. Nothing partial is ever visible
at a non-dot path; a failure at any gate raises a typed ``CliInstallError``
subclass, cleans its staging, and retains every previously verified version.

The node makes NO registry-metadata trust decisions: the tarball URL is
derived from the pinned ``{package}`` + version by npm's URL convention, and
verification uses only the pinned integrity — every network-derived trust
decision happened at ingest (ADR-0048/0055).

Stdlib-only (ADR-0010: the daemon has zero runtime dependencies). The
supported-tuple/package table lives worker-side (``ClaudeAdapter``); this
module only detects its OWN tuple and looks it up in the wire-delivered map —
a dev-only contract test pins the key spelling between the two sides.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import os
import platform
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CLI_DOWNLOAD_TIMEOUT_SECONDS = 600  # wall-clock deadline for the whole download
CLI_DOWNLOAD_MAX_BYTES = 512 * 1024**2  # the linux binary is ~215 MB; tarball well under this
CLI_DOWNLOAD_CHUNK_BYTES = 1024**2
CLI_ARCHIVE_MAX_ENTRIES = 256
CLI_ARCHIVE_MAX_EXPANDED_BYTES = 1024**3
CLI_BINARY_NAME = "claude"
CLI_PACKAGE_PREFIX = "package/"  # npm tarball layout
# Verified against the real platform tarball (2.1.258, 2026-09-02): the
# package ships package/claude, package/package.json, LICENSE.md, README.md.
CLI_BINARY_MEMBER = "package/claude"

# The platform components this daemon can ever detect; their product is the
# complete set of tuple keys the pinned map may be asked for (the worker-side
# ClaudeAdapter.CLI_PLATFORM_PACKAGES table must spell its keys from exactly
# this set — contract-tested).
SUPPORTED_OSES = ("linux",)
SUPPORTED_ARCHES = ("x64", "arm64")
SUPPORTED_LIBCS = ("glibc", "musl")


def supported_tuple_keys() -> tuple[str, ...]:
    """Every tuple key ``platform_tuple_key`` can possibly emit."""
    return tuple(
        f"{os_name}-{arch}-{libc}"
        for os_name in SUPPORTED_OSES
        for arch in SUPPORTED_ARCHES
        for libc in SUPPORTED_LIBCS
    )


class CliInstallError(RuntimeError):
    """A CLI Pin install failed. Subclass names are the stable, redacted
    heartbeat/event classes; messages carry names, versions, tuple keys,
    stages, and byte counts only — never request URLs, credentials, or
    archive member contents (ADR-0055 point 7)."""


class CliPlatformUnsupported(CliInstallError):
    """This node's platform tuple is unsupported or absent from the pinned map."""


class CliDownloadFailed(CliInstallError):
    """The tarball download errored or exceeded its wall-clock deadline."""


class CliDownloadTooLarge(CliInstallError):
    """The download exceeded the byte cap before completing."""


class CliIntegrityMismatch(CliInstallError):
    """The complete tarball does not match the pinned SRI integrity."""


class CliArchiveInvalid(CliInstallError):
    """The verified tarball's archive semantics are outside the allowlist."""


class CliBinaryInvalid(CliInstallError):
    """The extracted binary is not a regular file."""


class CliPublishFailed(CliInstallError):
    """A staging- or publish-side filesystem operation failed: store/staging
    setup, the staged-tarball read at the integrity check, extraction writes,
    binary assembly, export mode normalization, or the atomic publication
    rename."""


_ARCH_NORMALIZE = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def platform_tuple_key() -> str:
    """This node's ``<os>-<arch>-<libc>`` tuple key — DETERMINISTIC detection,
    spelled exactly as the pinned map's keys (ADR-0055): os from
    ``platform.system()``, arch normalized from ``platform.machine()``, libc
    ``glibc`` when the interpreter links it, ``musl`` when the musl loader is
    present. Anything else raises ``CliPlatformUnsupported``."""
    system = platform.system().lower()
    if system not in SUPPORTED_OSES:
        raise CliPlatformUnsupported(
            f"unsupported platform OS {system!r} (supported: {', '.join(SUPPORTED_OSES)})"
        )
    machine = platform.machine().lower()
    arch = _ARCH_NORMALIZE.get(machine)
    if arch is None:
        raise CliPlatformUnsupported(f"unsupported platform architecture {machine!r}")
    if platform.libc_ver()[0] == "glibc":
        libc = "glibc"
    elif glob.glob("/lib/ld-musl-*.so*"):
        libc = "musl"
    else:
        raise CliPlatformUnsupported("cannot determine the C library (neither glibc nor musl)")
    return f"{system}-{arch}-{libc}"


def _default_fetch(url: str):
    return urllib.request.urlopen(url, timeout=60)  # https by construction


def _normalize_export_modes(
    cli_root: Path, tool_root: Path, version_dir: Path, binary: Path, tool: str, version: str
) -> None:
    """Deck-visible modes are code-owned, never umask residue: the tree is
    mounted read-only into Flight Deck containers running an arbitrary
    non-root UID, so the launch path must be world-traversable and the binary
    world-readable+executable. Doubles as the REPAIR path for an install
    published by an older daemon amendment or under a restrictive service
    umask."""
    try:
        for directory in (cli_root, tool_root, version_dir):
            os.chmod(directory, 0o755)
        os.chmod(binary, 0o755)
    except OSError as exc:
        raise CliPublishFailed(
            f"{tool} {version}: export mode normalization failed: {type(exc).__name__}"
        ) from exc


def ensure_cli_version(
    cli_root: Path,
    tool: str,
    version: str,
    platforms: dict,
    *,
    fetch=None,
    log=lambda message: None,
) -> Path:
    """Ensure ``<cli_root>/<tool>/<version>/claude`` is installed and return
    its path — the ADR-0055 point 4 gates (a)-(g), in order. An already
    published version returns without touching the network, re-normalizing
    its modes (the repair path for entries created by an older daemon
    amendment or under a restrictive service umask); a broken version
    directory (present, but the binary missing or irregular — or the version
    directory itself a SYMLINK, which can point outside the mounted cli tree
    and resolve differently or dangle inside the deck container, so it is
    never served) is renamed aside dot-prefixed and reinstalled. Every
    failure raises a typed ``CliInstallError`` with staging cleaned and
    nothing partial published."""
    fetch = fetch or _default_fetch
    cli_root = Path(cli_root)
    tool_root = cli_root / tool
    version_dir = tool_root / version
    published = version_dir / CLI_BINARY_NAME

    # (a) Tuple selection BEFORE any download; fast-path an installed version.
    key = platform_tuple_key()
    entry = platforms.get(key)
    if not isinstance(entry, dict):
        raise CliPlatformUnsupported(
            f"platform {key} is absent from the pinned map for {tool} {version}"
            f" (pinned: {', '.join(sorted(platforms)) or 'none'})"
        )
    package = str(entry.get("package", ""))
    integrity = str(entry.get("integrity", ""))
    if published.is_file() and not published.is_symlink() and not version_dir.is_symlink():
        _normalize_export_modes(cli_root, tool_root, version_dir, published, tool, version)
        return published
    if version_dir.exists() or version_dir.is_symlink():
        # Present but not a healthy install: retire it aside (dot-prefixed —
        # invisible to the export contract) and reinstall from scratch.
        broken = tool_root / f".broken-{version}-{os.getpid()}"
        try:
            os.replace(version_dir, broken)
        except OSError as exc:
            raise CliPublishFailed(
                f"{tool} {version}: cannot retire a broken version directory: {type(exc).__name__}"
            ) from exc
        log(f"cli {tool} {version}: broken version directory retired for reinstall")

    try:
        # Deck-visible directory modes are code-owned, never umask residue:
        # the tree mounts read-only into containers running an arbitrary
        # non-root UID.
        os.makedirs(tool_root, exist_ok=True)
        os.chmod(cli_root, 0o755)
        os.chmod(tool_root, 0o755)
    except OSError as exc:
        raise CliPublishFailed(
            f"{tool} {version}: cli store setup failed: {type(exc).__name__}"
        ) from exc
    staging = tool_root / f".staging-{version}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    try:
        try:
            staging.mkdir()
        except OSError as exc:
            raise CliPublishFailed(
                f"{tool} {version}: staging setup failed: {type(exc).__name__}"
            ) from exc
        tarball = staging / "package.tgz"
        _download(package, version, tarball, fetch)  # (b)
        _verify_integrity(tarball, integrity, key, package, version)  # (c)
        _extract_validated(tarball, staging / "extract", package)  # (d)+(e)+(f)
        # (g) Assemble the publish directory holding exactly the binary at its
        # root — the deck-facing contract is <tool>/<version>/claude, decoupled
        # from npm packaging — then publish with ONE same-filesystem rename.
        publish = staging / "publish"
        try:
            publish.mkdir()
            # The published dir's explicit mode, set BEFORE it becomes visible.
            os.chmod(publish, 0o755)
        except OSError as exc:
            raise CliPublishFailed(
                f"{tool} {version}: publish-directory setup failed: {type(exc).__name__}"
            ) from exc
        try:
            os.replace(staging / "extract" / CLI_BINARY_MEMBER, publish / CLI_BINARY_NAME)
        except OSError as exc:
            raise CliPublishFailed(
                f"{tool} {version}: binary assembly rename failed: {type(exc).__name__}"
            ) from exc
        try:
            os.replace(publish, version_dir)
        except OSError as exc:
            raise CliPublishFailed(
                f"{tool} {version}: publish rename failed: {type(exc).__name__}"
            ) from exc
    finally:
        # Every exit path — success included — reclaims the staging remnants;
        # nothing partial is ever visible at a published (non-dot) path.
        shutil.rmtree(staging, ignore_errors=True)
    log(f"cli {tool} {version} installed ({key})")
    return published


def _download(package: str, version: str, dest: Path, fetch) -> None:
    """(b): stream the tarball into staging under a byte cap and a wall-clock
    deadline. The URL is npm's deterministic convention over pinned data —
    ``<registry>/<package>/-/<basename>-<version>.tgz`` — never fetched
    metadata."""
    basename = package.rpartition("/")[2]
    url = f"https://registry.npmjs.org/{package}/-/{basename}-{version}.tgz"
    deadline = time.monotonic() + CLI_DOWNLOAD_TIMEOUT_SECONDS
    received = 0
    try:
        with fetch(url) as stream, dest.open("wb") as out:
            while True:
                chunk = stream.read(CLI_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    return
                received += len(chunk)
                if received > CLI_DOWNLOAD_MAX_BYTES:
                    raise CliDownloadTooLarge(
                        f"{package} {version}: download exceeded {CLI_DOWNLOAD_MAX_BYTES} bytes"
                    )
                if time.monotonic() > deadline:
                    raise CliDownloadFailed(
                        f"{package} {version}: download exceeded the"
                        f" {CLI_DOWNLOAD_TIMEOUT_SECONDS}s deadline"
                        f" ({received} bytes received)"
                    )
                out.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        raise CliDownloadFailed(
            f"{package} {version}: download failed after {received} bytes: {type(exc).__name__}"
        ) from exc


def _verify_integrity(tarball: Path, integrity: str, key: str, package: str, version: str) -> None:
    """(c): the COMPLETE tarball file against the pinned SRI string, before
    any extraction. A malformed SRI is also a mismatch — the pin is
    machine-written, so any deviation is corruption. npm's integrity
    authenticates bytes only; it never substitutes for archive validation."""
    algorithm, _, encoded = integrity.partition("-")
    try:
        expected = base64.b64decode(encoded, validate=True) if encoded else b""
    except ValueError:
        expected = b""
    if algorithm != "sha512" or len(expected) != hashlib.sha512().digest_size:
        raise CliIntegrityMismatch(
            f"platform {key} package {package} {version}: pinned integrity is"
            " not a well-formed sha512 SRI string"
        )
    digest = hashlib.sha512()
    try:
        with tarball.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CLI_DOWNLOAD_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CliPublishFailed(
            f"platform {key} package {package} {version}: staged tarball"
            f" unreadable at the integrity check: {type(exc).__name__}"
        ) from exc
    if digest.digest() != expected:
        raise CliIntegrityMismatch(f"platform {key} package {package} {version}: sha512 mismatch")


def _validated_member_path(member: tarfile.TarInfo) -> str:
    """One member's normalized path, refused unless it is a plain relative
    path under the npm ``package/`` prefix (path only in messages, never
    content)."""
    name = member.name
    if name.startswith("/") or "\\" in name:
        raise CliArchiveInvalid(f"archive member {name!r}: absolute or non-POSIX path")
    parts = name.split("/")
    if parts and parts[-1] == "":  # a trailing slash on a directory entry
        parts = parts[:-1]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise CliArchiveInvalid(f"archive member {name!r}: empty, '.' or '..' path component")
    if parts[0] != CLI_PACKAGE_PREFIX.rstrip("/"):
        raise CliArchiveInvalid(f"archive member {name!r}: outside the {CLI_PACKAGE_PREFIX} layout")
    return "/".join(parts)


def _validate_archive(tar: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    """(d): EVERY member validated before any is extracted. Rejects absolute
    paths, traversal, links of every kind, devices/FIFOs/sockets, duplicate
    or file-vs-directory-conflicting paths, oversized archives (entry count
    and expanded bytes), a missing package.json or binary member, and any
    executable-mode member other than the binary."""
    try:
        members = tar.getmembers()
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        raise CliArchiveInvalid(f"archive index unreadable: {type(exc).__name__}") from exc
    if len(members) > CLI_ARCHIVE_MAX_ENTRIES:
        raise CliArchiveInvalid(
            f"archive holds {len(members)} entries (cap {CLI_ARCHIVE_MAX_ENTRIES})"
        )
    seen: dict[str, str] = {}
    validated: dict[str, tarfile.TarInfo] = {}
    expanded = 0
    for member in members:
        path = _validated_member_path(member)
        if member.isdir():
            kind = "dir"
        elif member.isreg():
            kind = "file"
        else:
            for probe, label in (
                (member.issym(), "symlink"),
                (member.islnk(), "hardlink"),
                (member.ischr(), "character device"),
                (member.isblk(), "block device"),
                (member.isfifo(), "FIFO"),
            ):
                if probe:
                    raise CliArchiveInvalid(f"archive member {path!r}: {label} refused")
            raise CliArchiveInvalid(f"archive member {path!r}: unsupported member type")
        if path in seen:
            raise CliArchiveInvalid(f"archive member {path!r}: duplicate path")
        seen[path] = kind
        validated[path] = member
        if kind == "file":
            if member.size < 0:
                raise CliArchiveInvalid(f"archive member {path!r}: negative size")
            expanded += member.size
            if expanded > CLI_ARCHIVE_MAX_EXPANDED_BYTES:
                raise CliArchiveInvalid(
                    f"archive expands past {CLI_ARCHIVE_MAX_EXPANDED_BYTES} bytes"
                )
            if member.mode & 0o111 and path != CLI_BINARY_MEMBER:
                raise CliArchiveInvalid(f"archive member {path!r}: unexpected executable payload")
    for path in seen:
        ancestor = path
        while "/" in ancestor:
            ancestor = ancestor.rsplit("/", 1)[0]
            if seen.get(ancestor) == "file":
                raise CliArchiveInvalid(
                    f"archive member {path!r}: conflicts with file {ancestor!r}"
                )
    if seen.get(f"{CLI_PACKAGE_PREFIX}package.json") != "file":
        raise CliArchiveInvalid(f"archive has no {CLI_PACKAGE_PREFIX}package.json")
    if seen.get(CLI_BINARY_MEMBER) != "file":
        raise CliArchiveInvalid(f"archive has no binary at {CLI_BINARY_MEMBER!r}")
    return validated


def _open_tarball(tarball: Path, package: str) -> tarfile.TarFile:
    try:
        return tarfile.open(tarball, mode="r:gz")
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        raise CliArchiveInvalid(
            f"{package}: not a readable gzip tarball: {type(exc).__name__}"
        ) from exc


def _extract_validated(tarball: Path, extract_dir: Path, package: str) -> None:
    """(d)+(e)+(f): validate every member, then extract member by member to
    paths computed from the VALIDATED names — no ``extract``/``extractall``
    call ever touches an archive-controlled name, so no link is ever followed
    — with modes normalized by this code (binary 0o755, files 0o644, dirs
    0o755; archive metadata is never trusted, and ownership is the service
    account by construction)."""
    with _open_tarball(tarball, package) as tar:
        validated = _validate_archive(tar)
        try:
            extract_dir.mkdir(parents=True)
            for path, member in validated.items():
                target = extract_dir / path
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise CliArchiveInvalid(f"archive member {path!r}: unreadable")
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, CLI_DOWNLOAD_CHUNK_BYTES)
                target.chmod(0o755 if path == CLI_BINARY_MEMBER else 0o644)
        except CliInstallError:
            raise
        except (tarfile.TarError, EOFError, ValueError) as exc:
            raise CliArchiveInvalid(
                f"{package}: archive read failed mid-extract: {type(exc).__name__}"
            ) from exc
        except OSError as exc:
            raise CliPublishFailed(
                f"{package}: staging write failed mid-extract: {type(exc).__name__}"
            ) from exc
    binary = extract_dir / CLI_BINARY_MEMBER
    if not binary.is_file() or binary.is_symlink():
        raise CliBinaryInvalid(f"{package}: extracted {CLI_BINARY_MEMBER!r} is not a regular file")
