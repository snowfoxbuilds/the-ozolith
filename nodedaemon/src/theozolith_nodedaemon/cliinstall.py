"""Fail-closed CLI Pin installation (ADR-0055 point 4).

The Node Daemon converges each pinned agent CLI version into
``<state-dir>/cli/<tool>/<version>/`` through ORDERED GATES, each failing
before the next runs: platform-tuple selection from the pinned map (before
any download), a bounded staged download at the coordinate the contract
derives, SRI integrity over the COMPLETE tarball before any extraction,
archive validation of EVERY member against the tool's contract row before
any is extracted — a BOUNDED forward-only parse whose every byte, header
or body, is budgeted before it is read — manual member-by-member
extraction in a second bounded pass over the same verified file (no
``extractall``, no archive-controlled destination ever), normalized
ownership and modes (archive metadata is never trusted), and one
same-filesystem atomic rename into the published version directory.
Nothing partial is ever visible at a non-dot path; a failure at any gate
raises a typed ``CliInstallError`` subclass, cleans its staging, and
retains every previously verified version.

What an archive is ALLOWED to contain is a product decision, never read from
the archive or the registry: the CLI ARCHIVE CONTRACT below is a closed
table keyed by tool slug, one row per registered adapter, fixing the archive
root, the per-tuple payload prefix, the closed allowed-member set (each
member tagged executable or not), the exact executable member, the closed
published set, and the layout marker's closed schema. The registry supplies
bytes and their integrity only — never a path, a mode, or an executable
name. The node makes NO registry-metadata trust decisions: the tarball URL is
derived from the pinned ``{package}`` + version by npm's URL convention plus
the row's tarball-version rule, and verification uses only the pinned
integrity — every network-derived trust decision happened at ingest
(ADR-0048/0055).

Stdlib-only (ADR-0010: the daemon has zero runtime dependencies). The
resolution half of the same contract — wrapper, per-tuple package,
tarball-version rule — is declared worker-side as adapter constants, and a
dev-only contract test holds the two halves equal for every tool, the way
the tuple-key spelling is already held.
"""

from __future__ import annotations

import base64
import contextlib
import enum
import glob
import gzip
import hashlib
import json
import os
import platform
import shutil
import stat
import tarfile
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

CLI_DOWNLOAD_TIMEOUT_SECONDS = 600  # wall-clock deadline for the whole download
CLI_DOWNLOAD_MAX_BYTES = 512 * 1024**2  # claude ~215 MB unpacked, codex ~130 MB packed
CLI_DOWNLOAD_CHUNK_BYTES = 1024**2
CLI_ARCHIVE_MAX_ENTRIES = 256
CLI_ARCHIVE_MAX_EXPANDED_BYTES = 1024**3  # the sum of declared regular-file sizes
# Tar bytes OUTSIDE validated member bodies — headers, extension records,
# padding, end-of-archive blocks — for the whole parse (see _BoundedReader).
CLI_ARCHIVE_MAX_OVERHEAD_BYTES = 1024**2
CLI_MARKER_MAX_BYTES = 64 * 1024  # a layout marker read from the archive stream
CLI_PACKAGE_PREFIX = "package/"  # npm's fixed archive root, shared by every row

# The platform components this daemon can ever detect; their product is the
# complete set of tuple keys the pinned map may be asked for (each adapter's
# CLI platform table must spell its keys from exactly this set —
# contract-tested).
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


class CliFact(enum.Enum):
    """A marker field bound to the resolved coordinate instead of a row
    constant — the only values a marker may carry that are not spelled in the
    contract row itself, and every one of them is product-derived."""

    PACKAGE = "package"  # the tuple's platform package
    WRAPPER = "wrapper"  # the row's wrapper package
    VERSION = "version"  # the pinned base version
    PLATFORM_VERSION = "platform-version"  # base version + the tuple's tarball suffix
    TRIPLE = "triple"  # the tuple's target triple
    ENTRYPOINT = "entrypoint"  # the executable member's prefix-relative path


@dataclass(frozen=True)
class CliTuple:
    """One supported tuple key's resolution facts."""

    package: str  # claude: the platform package; codex: the wrapper itself
    version_suffix: str  # tarball basename suffix after the version: "" | "-linux-x64" | ...
    triple: str  # "" for claude; the vendored payload's target triple for codex


@dataclass(frozen=True)
class CliMember:
    """One allowed archive member, relative to the payload prefix (payload
    members) or the archive root (root members), with the executable tag the
    archive's mode bit must agree with in both directions."""

    path: str
    executable: bool


@dataclass(frozen=True)
class CliMarkerSpec:
    """A layout marker's closed schema: WHICH member, and every field bound
    to a row constant or a ``CliFact``. ``closed`` refuses fields outside the
    schema (the codex layout marker); an npm ``package.json`` binds its two
    named fields and ignores the rest of the vendor document."""

    member: str
    fields: tuple[tuple[str, CliFact | int | str], ...]
    closed: bool

    def expected(self, row: CliArchiveRow, resolved: CliTuple, version: str) -> dict:
        facts = {
            CliFact.PACKAGE: resolved.package,
            CliFact.WRAPPER: row.wrapper,
            CliFact.VERSION: version,
            CliFact.PLATFORM_VERSION: version + resolved.version_suffix,
            CliFact.TRIPLE: resolved.triple,
            CliFact.ENTRYPOINT: row.executable_member,
        }
        return {
            name: facts[binding] if isinstance(binding, CliFact) else binding
            for name, binding in self.fields
        }


@dataclass(frozen=True)
class CliArchiveRow:
    """One tool's CLI archive contract (ADR-0055 Decision 4)."""

    wrapper: str
    archive_root: str  # npm's "package/"; every member sits under it
    published_name: str  # the deck-facing executable name at <version>/<name>
    tuples: Mapping[str, CliTuple]  # keyed by every supported_tuple_keys() entry
    payload_prefix_template: str  # root-relative; "" is the root itself; may carry {triple}
    payload_members: tuple[CliMember, ...]  # prefix-relative; the published set's source
    root_members: tuple[CliMember, ...]  # outside the prefix: validated, never published
    executable_member: str  # prefix-relative
    marker: CliMarkerSpec  # a payload member; published product-rendered, never as archived
    root_marker: CliMarkerSpec | None  # a root member; validated and discarded
    vendored: bool  # publish a relative link <published_name> -> executable_member

    def payload_prefix(self, resolved: CliTuple) -> str:
        return self.archive_root + self.payload_prefix_template.format(triple=resolved.triple)


_CLAUDE_TUPLES = MappingProxyType(
    {
        "linux-x64-glibc": CliTuple("@anthropic-ai/claude-code-linux-x64", "", ""),
        "linux-arm64-glibc": CliTuple("@anthropic-ai/claude-code-linux-arm64", "", ""),
        "linux-x64-musl": CliTuple("@anthropic-ai/claude-code-linux-x64-musl", "", ""),
        "linux-arm64-musl": CliTuple("@anthropic-ai/claude-code-linux-arm64-musl", "", ""),
    }
)
# One static musl tarball serves both libc tuples of an architecture.
_CODEX_X64 = CliTuple("@openai/codex", "-linux-x64", "x86_64-unknown-linux-musl")
_CODEX_ARM64 = CliTuple("@openai/codex", "-linux-arm64", "aarch64-unknown-linux-musl")
_CODEX_TUPLES = MappingProxyType(
    {
        "linux-x64-glibc": _CODEX_X64,
        "linux-x64-musl": _CODEX_X64,
        "linux-arm64-glibc": _CODEX_ARM64,
        "linux-arm64-musl": _CODEX_ARM64,
    }
)

# The rows, verified against the real registry tarballs 2026-09-04 (claude
# 2.1.260; codex 0.150.0 and 0.153.3, both Linux architectures). A member the
# real archive gains, loses, or renames fails closed until the adapter's
# validated-CLI review records the new row — the deliberate price of a
# closed contract (ADR-0055 Decision 4).
CLI_ARCHIVE_CONTRACT: Mapping[str, CliArchiveRow] = MappingProxyType(
    {
        "claude": CliArchiveRow(
            wrapper="@anthropic-ai/claude-code",
            archive_root=CLI_PACKAGE_PREFIX,
            published_name="claude",
            tuples=_CLAUDE_TUPLES,
            payload_prefix_template="",
            payload_members=(
                CliMember("claude", executable=True),
                CliMember("package.json", executable=False),
                CliMember("LICENSE.md", executable=False),
                CliMember("README.md", executable=False),
            ),
            root_members=(),
            executable_member="claude",
            marker=CliMarkerSpec(
                "package.json",
                (("name", CliFact.PACKAGE), ("version", CliFact.VERSION)),
                closed=False,
            ),
            root_marker=None,
            vendored=False,
        ),
        "codex": CliArchiveRow(
            wrapper="@openai/codex",
            archive_root=CLI_PACKAGE_PREFIX,
            published_name="codex",
            tuples=_CODEX_TUPLES,
            payload_prefix_template="vendor/{triple}/",
            payload_members=(
                CliMember("codex-package.json", executable=False),
                CliMember("bin/codex", executable=True),
                CliMember("bin/codex-code-mode-host", executable=True),
                CliMember("codex-path/rg", executable=True),
                CliMember("codex-resources/bwrap", executable=True),
                CliMember("codex-resources/zsh/bin/zsh", executable=True),
            ),
            root_members=(
                CliMember("package.json", executable=False),
                CliMember("README.md", executable=False),
            ),
            executable_member="bin/codex",
            # The file the binary locates its helpers by: closed, every value
            # bound to the row or the resolved coordinate.
            marker=CliMarkerSpec(
                "codex-package.json",
                (
                    ("layoutVersion", 1),
                    ("version", CliFact.VERSION),
                    ("target", CliFact.TRIPLE),
                    ("variant", "codex"),
                    ("entrypoint", CliFact.ENTRYPOINT),
                    ("resourcesDir", "codex-resources"),
                    ("pathDir", "codex-path"),
                ),
                closed=True,
            ),
            root_marker=CliMarkerSpec(
                "package.json",
                (("name", CliFact.WRAPPER), ("version", CliFact.PLATFORM_VERSION)),
                closed=False,
            ),
            vendored=True,
        ),
    }
)


class CliInstallError(RuntimeError):
    """A CLI Pin install failed. Subclass names are the stable, redacted
    heartbeat/event classes; messages carry names, versions, tuple keys,
    stages, and byte counts only — never request URLs, credentials, or
    archive member contents (ADR-0055 point 7)."""


class CliToolUnknown(CliInstallError):
    """The wire names a tool absent from the CLI archive contract."""


class CliContractInvalid(CliInstallError):
    """A contract row is malformed — a product bug, caught before any
    download so telemetry can tell it apart from a per-pin wire issue."""


class CliPlatformUnsupported(CliInstallError):
    """This node's platform tuple is unsupported or absent from the pinned map."""


class CliDownloadFailed(CliInstallError):
    """The tarball download errored or exceeded its wall-clock deadline."""


class CliDownloadTooLarge(CliInstallError):
    """The download exceeded the byte cap before completing."""


class CliIntegrityMismatch(CliInstallError):
    """The complete tarball does not match the pinned SRI integrity."""


class CliArchiveInvalid(CliInstallError):
    """The verified tarball's archive semantics are outside the contract row."""


class CliBinaryInvalid(CliInstallError):
    """An extracted executable member is not a regular file."""


class CliPublishFailed(CliInstallError):
    """A staging- or publish-side filesystem operation failed: store/staging
    setup, the staged-tarball read at the integrity check, extraction writes,
    marker rendering, link creation, export mode normalization, or the atomic
    publication rename."""


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


def _relative_parts(path: str) -> list[str] | None:
    """The components of a clean relative POSIX path — no leading slash, no
    backslash or NUL, no empty, ``.`` or ``..`` component — else ``None``."""
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return parts


def _ancestors(path: str) -> set[str]:
    found: set[str] = set()
    while "/" in path:
        path = path.rsplit("/", 1)[0]
        found.add(path)
    return found


def _allowed_members(row: CliArchiveRow, resolved: CliTuple) -> dict[str, CliMember]:
    """The concrete allowed-member set for one tuple, keyed by normalized
    archive path (payload members under the tuple's payload prefix, root
    members under the archive root)."""
    prefix = row.payload_prefix(resolved)
    allowed: dict[str, CliMember] = {}
    for base, members in ((prefix, row.payload_members), (row.archive_root, row.root_members)):
        for member in members:
            allowed[base + member.path] = member
    return allowed


def _plain_name(name: str) -> bool:
    return _relative_parts(name) == [name]


def _check_row(tool: str, row: CliArchiveRow) -> None:
    """Refuse a malformed contract row before any download (a product bug)."""

    def malformed(reason: str) -> CliContractInvalid:
        return CliContractInvalid(f"cli archive contract row {tool!r} is malformed: {reason}")

    if not _plain_name(row.published_name):
        raise malformed("published name is not a plain file name")
    root = row.archive_root
    if not root.endswith("/") or not _plain_name(root[:-1]):
        raise malformed("archive root is not one directory")
    if set(row.tuples) != set(supported_tuple_keys()):
        raise malformed("tuple table does not cover the supported tuple keys exactly")
    if not row.payload_members:
        raise malformed("no payload members")
    for member in (*row.payload_members, *row.root_members):
        if _relative_parts(member.path) is None:
            raise malformed(f"member {member.path!r} is not a clean relative path")
    executables = {member.path for member in row.payload_members if member.executable}
    plain = {member.path for member in row.payload_members if not member.executable}
    if row.executable_member not in executables:
        raise malformed("executable member is not an executable-tagged payload member")
    if row.marker.member not in plain:
        raise malformed("layout marker is not a non-executable payload member")
    if row.root_marker is not None and row.root_marker.member not in {
        member.path for member in row.root_members if not member.executable
    }:
        raise malformed("root marker is not a non-executable root member")
    if row.vendored != (row.executable_member != row.published_name):
        raise malformed("vendored flag disagrees with the executable member's location")
    if row.vendored and any(
        member.path.split("/", 1)[0] == row.published_name for member in row.payload_members
    ):
        raise malformed("published link name collides with a payload member")
    for spec in (row.marker, row.root_marker):
        if spec is None:
            continue
        names = [name for name, _binding in spec.fields]
        if not names or len(set(names)) != len(names) or not all(names):
            raise malformed(
                f"marker {spec.member!r} schema field names are not unique and non-empty"
            )
        for _name, binding in spec.fields:
            if isinstance(binding, bool) or not isinstance(binding, CliFact | int | str):
                raise malformed(f"marker {spec.member!r} schema binds an unsupported value type")
    for key, resolved in row.tuples.items():
        if not resolved.package:
            raise malformed(f"tuple {key} names no package")
        try:
            prefix = row.payload_prefix(resolved)
        except (KeyError, IndexError, ValueError) as exc:
            raise malformed("payload prefix template is malformed") from exc
        if not prefix.startswith(root) or not prefix.endswith("/"):
            raise malformed(f"payload prefix for tuple {key} is not under the archive root")
        if prefix != root and _relative_parts(prefix[:-1]) is None:
            raise malformed(f"payload prefix for tuple {key} is not a clean relative path")
        if row.root_members and prefix == root:
            raise malformed("root members cannot exist when the payload prefix is the root")
        allowed = _allowed_members(row, resolved)
        if len(allowed) != len(row.payload_members) + len(row.root_members):
            raise malformed(f"two members share one archive path for tuple {key}")
        if set().union(*(_ancestors(path) for path in allowed)) & set(allowed):
            raise malformed(f"a member is also another member's directory for tuple {key}")


def _published_directories(row: CliArchiveRow) -> list[str]:
    """The interior directories of the published set, parents first."""
    return sorted(set().union(*(_ancestors(member.path) for member in row.payload_members)))


def _installed_healthy(version_dir: Path, row: CliArchiveRow) -> bool:
    """Whether a version directory holds the row's COMPLETE published set
    with the expected types: every payload member a regular non-symlink file
    at its prefix-relative path beneath real (non-symlink) directories, and
    for a vendored row the published link a relative symlink reading exactly
    the executable member. Modes are not health — the fast path repairs
    them — but a missing or mis-typed member, an absolute or archive-supplied
    link, or a regular file in the link's place is a broken install."""

    def lmode(path: Path) -> int:
        try:
            return os.lstat(path).st_mode
        except OSError:
            return 0

    if not stat.S_ISDIR(lmode(version_dir)):
        return False
    for directory in _published_directories(row):
        if not stat.S_ISDIR(lmode(version_dir / directory)):
            return False
    for member in row.payload_members:
        if not stat.S_ISREG(lmode(version_dir / member.path)):
            return False
    if row.vendored:
        link = version_dir / row.published_name
        if not stat.S_ISLNK(lmode(link)):
            return False
        try:
            return os.readlink(link) == row.executable_member
        except OSError:
            return False
    return True


def _normalize_published_modes(version_dir: Path, row: CliArchiveRow) -> None:
    """The version directory's interior modes, by the row's tags: directories
    0755, executable-tagged members 0755, everything else 0644. Only called
    over a tree whose shape was verified (health check or staging), so no
    chmod ever resolves through a link. A symlink itself carries no mode."""
    os.chmod(version_dir, 0o755)
    for directory in _published_directories(row):
        os.chmod(version_dir / directory, 0o755)
    for member in row.payload_members:
        os.chmod(version_dir / member.path, 0o755 if member.executable else 0o644)


def _normalize_export_modes(
    cli_root: Path, tool_root: Path, version_dir: Path, row: CliArchiveRow, tool: str, version: str
) -> None:
    """Deck-visible modes are code-owned, never umask residue: the tree is
    mounted read-only into Flight Deck containers running an arbitrary
    non-root UID, so the launch path must be world-traversable and every
    executable world-readable+executable. Doubles as the REPAIR path for an
    install published under a restrictive service umask."""
    try:
        for directory in (cli_root, tool_root):
            os.chmod(directory, 0o755)
        _normalize_published_modes(version_dir, row)
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
    """Ensure ``<cli_root>/<tool>/<version>/`` holds the tool's published set
    and return the published executable's path ``<version>/<published
    name>`` — the ADR-0055 point 4 gates (a)-(g), in order, held to the
    tool's contract row (an unknown tool or a malformed row fails before
    anything is read or fetched). An already published version returns
    without touching the network, re-normalizing its modes (the repair path
    for entries created under a restrictive service umask); a broken version
    directory (present, but any published-set member missing or mis-typed —
    a pre-contract claude directory holding only the binary included — or
    the version directory itself a SYMLINK, which can point outside the
    mounted cli tree and resolve differently or dangle inside the deck
    container, so it is never served) is renamed aside dot-prefixed and
    reinstalled. The TOOL directory is a trust boundary the same way: every
    operation here — the fast path, mode normalization, the retire-aside
    rename, staging, and publication — resolves through it, so a symlinked
    tool directory is refused outright with the typed publish boundary
    before anything is read or written through it (the daemon's converge
    pass owns that repair — replacing the link itself with a real directory
    — after which a retry installs normally). Every failure raises a typed
    ``CliInstallError`` with staging cleaned and nothing partial published."""
    fetch = fetch or _default_fetch
    row = CLI_ARCHIVE_CONTRACT.get(tool)
    if row is None:
        raise CliToolUnknown(
            f"{tool} {version}: tool is absent from the CLI archive contract"
            f" (known: {', '.join(sorted(CLI_ARCHIVE_CONTRACT))})"
        )
    _check_row(tool, row)
    cli_root = Path(cli_root)
    tool_root = cli_root / tool
    version_dir = tool_root / version
    published = version_dir / row.published_name

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
    resolved = row.tuples[key]
    if tool_root.is_symlink():
        raise CliPublishFailed(
            f"{tool} {version}: tool directory is a symlink — refusing to install through it"
        )
    if not version_dir.is_symlink() and _installed_healthy(version_dir, row):
        _normalize_export_modes(cli_root, tool_root, version_dir, row, tool, version)
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
        _download(package, version, resolved.version_suffix, tarball, fetch)  # (b)
        _verify_integrity(tarball, integrity, key, package, version)  # (c)
        # (d)-(g) Validate every member, extract only the published payload
        # into the assembled publish directory, render the marker, create
        # the vendored link — then publish with ONE same-filesystem rename.
        publish = staging / "publish"
        _assemble_validated(tarball, publish, row, resolved, version, package)
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


def _download(package: str, version: str, suffix: str, dest: Path, fetch) -> None:
    """(b): stream the tarball into staging under a byte cap and a wall-clock
    deadline. The URL is npm's deterministic convention over pinned data plus
    the row's tarball-version rule — ``<registry>/<package>/-/<basename>-
    <version><suffix>.tgz`` — never fetched metadata. The suffix is what an
    older daemon cannot derive: it would fetch codex's un-suffixed wrapper
    tarball, whose bytes never match the pinned platform integrity."""
    basename = package.rpartition("/")[2]
    url = f"https://registry.npmjs.org/{package}/-/{basename}-{version}{suffix}.tgz"
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


def _validated_member_path(member: tarfile.TarInfo, archive_root: str) -> str:
    """One member's normalized path, refused unless it is a plain relative
    path under the archive root (path only in messages, never content)."""
    name = member.name
    if name.startswith("/") or "\\" in name:
        raise CliArchiveInvalid(f"archive member {name!r}: absolute or non-POSIX path")
    parts = name.split("/")
    if parts and parts[-1] == "":  # a trailing slash on a directory entry
        parts = parts[:-1]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise CliArchiveInvalid(f"archive member {name!r}: empty, '.' or '..' path component")
    if parts[0] != archive_root.rstrip("/"):
        raise CliArchiveInvalid(f"archive member {name!r}: outside the {archive_root} layout")
    return "/".join(parts)


def _read_marker(tar: tarfile.TarFile, member: tarfile.TarInfo, path: str) -> bytes:
    """One layout marker's bytes from the stream — refused from the header
    when the declared size is past the cap, so the body is never read. A
    marker body is the only member content the validation pass ever holds."""
    if member.size > CLI_MARKER_MAX_BYTES:
        raise CliArchiveInvalid(
            f"archive member {path!r}: marker exceeds {CLI_MARKER_MAX_BYTES} bytes"
        )
    try:
        source = tar.extractfile(member)
        if source is None:
            raise CliArchiveInvalid(f"archive member {path!r}: unreadable")
        with source:
            raw = source.read(CLI_MARKER_MAX_BYTES + 1)
    except CliInstallError:
        raise
    except (tarfile.TarError, EOFError, ValueError) as exc:
        raise CliArchiveInvalid(
            f"archive member {path!r}: marker unreadable: {type(exc).__name__}"
        ) from exc
    if len(raw) > CLI_MARKER_MAX_BYTES:
        raise CliArchiveInvalid(f"archive member {path!r}: marker exceeds its declared size")
    return raw


def _check_marker(raw: bytes, path: str, spec: CliMarkerSpec, expected: dict) -> None:
    """Hold one layout marker to its closed schema: every bound field present
    with the bound type and value, string values plain relative (never
    empty, absolute, traversal-, backslash- or NUL-bearing), and — for a
    closed schema — no field outside it. Messages name schema fields only,
    never the archive's own keys or values."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CliArchiveInvalid(f"archive member {path!r}: marker is not a JSON document") from exc
    if not isinstance(document, dict):
        raise CliArchiveInvalid(f"archive member {path!r}: marker is not a JSON object")
    for name in expected:
        if name not in document:
            raise CliArchiveInvalid(f"archive member {path!r}: marker field {name!r} missing")
    if spec.closed and set(document) - set(expected):
        raise CliArchiveInvalid(
            f"archive member {path!r}: marker carries a field outside the closed schema"
        )
    for name, want in expected.items():
        have = document[name]
        if type(have) is not type(want):
            raise CliArchiveInvalid(f"archive member {path!r}: marker field {name!r} wrong type")
        if isinstance(want, str) and _relative_parts(have) is None:
            raise CliArchiveInvalid(
                f"archive member {path!r}: marker field {name!r} is not a plain relative value"
            )
        if have != want:
            raise CliArchiveInvalid(
                f"archive member {path!r}: marker field {name!r} does not match the contract"
            )


def _block(size: int) -> int:
    """A body's on-tar extent: its size rounded up to whole 512-byte blocks."""
    return -(-size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE


class _BoundedReader:
    """The tar parser's ONLY source of bytes, and so the resource bound on
    everything it does: a forward-only view over the decompressor that hands
    out at most its budget and refuses, typed, the first byte past it. The
    budget opens at the OVERHEAD allowance — member headers, PAX and GNU
    extension records, block padding, the end-of-archive blocks, and
    whatever a malformed archive puts between them — and grows by exactly
    one body's block-rounded extent when the header loop has validated that
    member and elects to read past (or extract) its body. An extension
    record, a sparse map, or a directory entry declaring a body can
    therefore never draw more than the allowance, and no body is read
    before its DECLARED size passed the expanded cap: the untrusted archive
    decides when the parser stops, the contract decides how much it may
    read (ADR-0055 Decision 4)."""

    def __init__(self, source, package: str, allowance: int):
        self._source = source
        self._package = package
        self._remaining = allowance
        self.consumed = 0

    def grant(self, extent: int) -> None:
        self._remaining += extent

    def read(self, size: int = -1) -> bytes:
        limit = self._remaining + 1  # one byte past the budget proves the crossing
        want = limit if size is None or size < 0 else min(size, limit)
        try:
            data = self._source.read(want)
        except (EOFError, zlib.error, gzip.BadGzipFile) as exc:
            raise CliArchiveInvalid(
                f"{self._package}: compressed stream invalid: {type(exc).__name__}"
            ) from exc
        except OSError as exc:
            raise CliPublishFailed(
                f"{self._package}: staged tarball unreadable mid-parse: {type(exc).__name__}"
            ) from exc
        self.consumed += len(data)
        self._remaining -= len(data)
        if self._remaining < 0:
            raise CliArchiveInvalid(
                f"{self._package}: tar stream exceeds its parse budget"
                f" ({self.consumed} decompressed bytes)"
            )
        return data


def _gunzip(raw):
    return gzip.GzipFile(fileobj=raw, mode="rb")


@contextlib.contextmanager
def _tar_stream(tarball: Path, package: str):
    """One forward-only bounded parse of the staged tarball: the raw file,
    the decompressor, the bounded reader, and a STREAMING ``tarfile`` over
    it — never the seekable index, whose ``getmembers`` walks every body
    before the first header can be refused."""
    try:
        raw = tarball.open("rb")
    except OSError as exc:
        raise CliPublishFailed(
            f"{package}: staged tarball unreadable: {type(exc).__name__}"
        ) from exc
    with raw, contextlib.closing(_gunzip(raw)) as source:
        reader = _BoundedReader(source, package, CLI_ARCHIVE_MAX_OVERHEAD_BYTES)
        with _open_stream(reader, package) as tar:
            yield tar, reader


def _open_stream(reader: _BoundedReader, package: str) -> tarfile.TarFile:
    """The streaming parser over the bounded reader; it reads the first
    header here, so a refusal can already surface at open."""
    try:
        return tarfile.open(fileobj=reader, mode="r|", bufsize=tarfile.RECORDSIZE)
    except CliInstallError:
        raise
    except (tarfile.TarError, EOFError, ValueError) as exc:
        raise CliArchiveInvalid(
            f"{package}: not a readable gzip tarball: {type(exc).__name__}"
        ) from exc


def _entries(tar: tarfile.TarFile, package: str):
    """The archive's entries in stream order, one header at a time: each is
    yielded before its body is read past, and the entry past the count cap
    refuses from its header without parsing further. (``tarfile`` keeps
    every header it has returned, so the cap bounds that too.)"""
    count = 0
    while True:
        try:
            member = tar.next()
        except CliInstallError:
            raise
        except (tarfile.TarError, EOFError, ValueError) as exc:
            raise CliArchiveInvalid(
                f"{package}: archive index unreadable: {type(exc).__name__}"
            ) from exc
        if member is None:
            return
        count += 1
        if count > CLI_ARCHIVE_MAX_ENTRIES:
            raise CliArchiveInvalid(
                f"{package}: archive holds more than {CLI_ARCHIVE_MAX_ENTRIES} entries"
            )
        yield member


@dataclass(frozen=True)
class _Entry:
    """One validated archive entry as the validation pass saw it — what the
    extraction pass must see again at the same position."""

    path: str
    directory: bool
    size: int


@dataclass(frozen=True)
class _ValidatedArchive:
    entries: tuple[_Entry, ...]  # every entry, in stream order
    published: Mapping[str, CliMember]  # archive path -> the payload member to extract


def _entry_of(member: tarfile.TarInfo, row: CliArchiveRow) -> _Entry:
    return _Entry(_validated_member_path(member, row.archive_root), member.isdir(), member.size)


def _validate_archive(
    tar: tarfile.TarFile,
    reader: _BoundedReader,
    row: CliArchiveRow,
    resolved: CliTuple,
    version: str,
    package: str,
) -> _ValidatedArchive:
    """(d): EVERY member validated from its header, before its body is read
    past and before any member is extracted. Rejects absolute paths,
    traversal, links of every kind, devices/FIFOs/sockets, sparse members,
    directory entries declaring a body, duplicate or file-vs-directory-
    conflicting paths, oversized archives (entry count, and a declared size
    that expands the archive past the cap — refused before that body is
    read), any member outside the row's allowed set (executable or not, a
    directory entry anywhere but above an allowed member included), any
    allowed member absent, and an executable bit that disagrees with the
    member's tag in either direction; the layout marker(s) are read from
    the stream size-capped and held to their closed schema once the member
    set is complete. Returns the validated entry sequence the extraction
    pass replays and the members to publish — the payload members other
    than the marker — keyed by archive path."""
    allowed = _allowed_members(row, resolved)
    directories = set().union(*(_ancestors(path) for path in allowed))
    prefix = row.payload_prefix(resolved)
    markers = {prefix + row.marker.member: row.marker}
    if row.root_marker is not None:
        markers[row.archive_root + row.root_marker.member] = row.root_marker
    seen: dict[str, str] = {}
    entries: list[_Entry] = []
    marker_bytes: dict[str, bytes] = {}
    expanded = 0
    for member in _entries(tar, package):
        path = _validated_member_path(member, row.archive_root)
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
        if kind == "dir":
            # tarfile never skips a directory's declared body: the next
            # header would be read from inside it, so the entry is refused
            # rather than accounted for.
            if member.size != 0:
                raise CliArchiveInvalid(f"archive member {path!r}: directory entry declares a body")
            if path not in directories:
                raise CliArchiveInvalid(f"archive member {path!r}: outside the allowed member set")
            entries.append(_Entry(path, True, 0))
            continue
        if member.issparse():
            raise CliArchiveInvalid(f"archive member {path!r}: sparse member refused")
        if member.size < 0:
            raise CliArchiveInvalid(f"archive member {path!r}: negative size")
        if member.size > CLI_ARCHIVE_MAX_EXPANDED_BYTES - expanded:
            raise CliArchiveInvalid(
                f"archive expands past {CLI_ARCHIVE_MAX_EXPANDED_BYTES} bytes at member {path!r}"
            )
        expanded += member.size
        spec = allowed.get(path)
        executable = bool(member.mode & 0o111)
        if spec is None:
            raise CliArchiveInvalid(
                f"archive member {path!r}: outside the allowed member set"
                + (" (unexpected executable payload)" if executable else "")
            )
        if executable and not spec.executable:
            raise CliArchiveInvalid(f"archive member {path!r}: unexpected executable payload")
        if spec.executable and not executable:
            raise CliArchiveInvalid(f"archive member {path!r}: executable member not executable")
        # The header passed: the parser may now read this body — the marker
        # here, the skip to the next header, the extraction pass later —
        # and not a byte more.
        reader.grant(_block(member.size))
        if path in markers:
            marker_bytes[path] = _read_marker(tar, member, path)
        entries.append(_Entry(path, False, member.size))
    for path in seen:
        for ancestor in _ancestors(path):
            if seen.get(ancestor) == "file":
                raise CliArchiveInvalid(
                    f"archive member {path!r}: conflicts with file {ancestor!r}"
                )
    for path in sorted(set(allowed) - set(seen)):
        raise CliArchiveInvalid(f"archive is missing allowed member {path!r}")
    for path, spec in markers.items():
        _check_marker(marker_bytes[path], path, spec, spec.expected(row, resolved, version))
    published = {
        prefix + member.path: member
        for member in row.payload_members
        if member.path != row.marker.member
    }
    return _ValidatedArchive(tuple(entries), MappingProxyType(published))


def _extract_validated(
    tarball: Path, publish: Path, plan: _ValidatedArchive, row: CliArchiveRow, package: str
) -> None:
    """(e): the second bounded pass over the SAME integrity-verified file,
    replaying the validated entry sequence header by header — any deviation
    from what the validation pass saw refuses — and copying only the members
    to publish, each to the path computed from its VALIDATED prefix-relative
    name (no ``extract``/``extractall`` call ever touches an archive-
    controlled name, so no link is ever followed) and held to its declared
    size, with modes set by this code from the member's tag."""
    with _tar_stream(tarball, package) as (tar, reader):
        replay = iter(plan.entries)
        for member in _entries(tar, package):
            entry = next(replay, None)
            if entry is None or _entry_of(member, row) != entry:
                raise CliArchiveInvalid(
                    f"{package}: archive changed between validation and extraction"
                )
            if entry.directory:
                continue
            reader.grant(_block(entry.size))
            spec = plan.published.get(entry.path)
            if spec is None:
                continue
            target = publish / spec.path
            try:
                source = tar.extractfile(member)
                if source is None:
                    raise CliArchiveInvalid(f"archive member {entry.path!r}: unreadable")
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, CLI_DOWNLOAD_CHUNK_BYTES)
                    copied = out.tell()
                target.chmod(0o755 if spec.executable else 0o644)
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
            if copied != entry.size:
                raise CliArchiveInvalid(
                    f"archive member {entry.path!r}: body shorter than declared"
                )
        if next(replay, None) is not None:
            raise CliArchiveInvalid(f"{package}: archive changed between validation and extraction")


def _assemble_validated(
    tarball: Path,
    publish: Path,
    row: CliArchiveRow,
    resolved: CliTuple,
    version: str,
    package: str,
) -> None:
    """(d)+(e)+(f)+(g): validate every member in one bounded pass, then build
    the publish directory from the row's closed published set in a second —
    extracting only the payload members other than the marker, member by
    member, with modes normalized by this code (executable-tagged 0o755,
    other files 0o644, directories 0o755; archive metadata is never trusted,
    and ownership is the service account by construction), then the
    product-rendered marker holding exactly the bound values — never the
    archive's bytes — and, for a vendored row, the daemon-created relative
    link ``<published name>`` -> executable member. The root members were
    validated from the stream and reach the publish directory nowhere."""
    with _tar_stream(tarball, package) as (tar, reader):
        plan = _validate_archive(tar, reader, row, resolved, version, package)
    try:
        publish.mkdir()
        # Explicit modes, set BEFORE the directory becomes visible.
        os.chmod(publish, 0o755)
        for directory in _published_directories(row):
            (publish / directory).mkdir()
            os.chmod(publish / directory, 0o755)
    except OSError as exc:
        raise CliPublishFailed(
            f"{package}: publish-directory setup failed: {type(exc).__name__}"
        ) from exc
    _extract_validated(tarball, publish, plan, row, package)
    for member in row.payload_members:
        extracted = publish / member.path
        if member.executable and (not extracted.is_file() or extracted.is_symlink()):
            raise CliBinaryInvalid(f"{package}: extracted {member.path!r} is not a regular file")
    rendered = json.dumps(row.marker.expected(row, resolved, version), indent=2) + "\n"
    try:
        marker = publish / row.marker.member
        marker.write_text(rendered, encoding="utf-8")
        marker.chmod(0o644)
        if row.vendored:
            os.symlink(row.executable_member, publish / row.published_name)
    except OSError as exc:
        raise CliPublishFailed(
            f"{package}: marker rendering or link creation failed: {type(exc).__name__}"
        ) from exc
