"""The config distribution: packaging ``drivers/`` into a hash-pinned artifact.

ADR-0042: custom driver code lives in the private Config Repo under
``drivers/``. On a config change the Control Node packages that tree into a
content-addressed zip served over the artifact-pull path, and the heartbeat
channel carries only the drivers-hash reference. Nodes fetch by hash, verify
by RECOMPUTING the manifest over the unpacked tree (never by hashing archive
bytes), and converge like the product pin.

The canonical hash is content-only — file relpaths and sha256 of file bytes,
nothing about mtimes, modes, or the archive envelope — so it is stable across
checkouts by construction. The algorithm is implemented twice (nodedaemon is
stdlib-only and cannot import this package); the two are pinned together by a
mandatory cross-package contract test.

File set: every regular file under ``<repo>/drivers/``, recursive, excluding
dot-prefixed path components, ``__pycache__`` components, and ``*.pyc``.
Symlinks and other non-regular files are a packaging error (fail closed — a
symlink could escape the repo). A missing or effectively-empty ``drivers/``
hashes to ``""`` (no artifact, no gate, byte-for-byte the deployment shape
that predates this feature).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The single git-native code exception (ADR-0042); the folder name whose
# contents ride the config distribution.
DRIVERS_DIR = "drivers"

# The metadata member at the archive root — metadata ABOUT the manifest, never
# part of it (the contract hash is the manifest hash, not archive bytes).
ARTIFACT_METADATA = "config-dist.json"
ARTIFACT_FORMAT = 1

# A fixed ZipInfo timestamp: the archive envelope never feeds the hash, so a
# stable date_time only keeps the bytes tidy across rebuilds.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


class ConfigDistError(RuntimeError):
    """The ``drivers/`` tree cannot be packaged (a symlink, an unreadable file)."""


def excluded_part(part: str) -> bool:
    """One path component the config-distribution file set ignores: dot-prefixed
    (covers ``.git``, dot-temp siblings, editor droppings), a ``__pycache__``
    directory, or a ``*.pyc`` file. Shared by the drivers manifest and
    folder-mode config-commit hashing so the two never diverge on what counts
    as content (ADR-0042)."""
    return part.startswith(".") or part == "__pycache__" or part.endswith(".pyc")


def regular_files(root: Path, *, refuse_irregular: bool = False) -> list[Path]:
    """Sorted regular files under ``root`` (recursive) whose every path
    component relative to ``root`` passes ``excluded_part``. Deterministic
    order, so the manifest is stable.

    With ``refuse_irregular`` a symlink or other non-regular entry that passes
    the name filter raises ``ConfigDistError`` — ``drivers/`` fails closed
    because a symlink could package a file from outside the repo. Without it
    (folder-mode commit hashing) such entries are simply skipped: a stray
    symlink in the wider Config Repo must never fail-close heartbeats.

    The ROOT itself is guarded the same way under ``refuse_irregular``: a
    ``drivers`` that is a symlink (even to a directory), a regular file, or any
    other non-directory entry is refused — a symlinked root would otherwise
    package a whole external tree, and a non-directory root would silently read
    as the empty sentinel. Only a genuinely missing root is the empty
    distribution; a real, non-symlink directory is walked."""
    root = Path(root)
    if refuse_irregular:
        if root.is_symlink():
            raise ConfigDistError(
                f"{root} is a symlink — the config distribution refuses a symlinked"
                " drivers root (it could package a tree from outside the repo, ADR-0042)"
            )
        if not root.exists():
            return []  # a genuinely missing drivers/ is the empty sentinel
        if not root.is_dir():
            raise ConfigDistError(
                f"{root} is not a directory — the config distribution refuses a"
                " non-directory drivers root (regular file, FIFO, socket, device;"
                " ADR-0042)"
            )
    elif not root.is_dir():
        return []
    # Explicit DFS rather than os.walk: os.walk's default onerror SWALLOWS a
    # scandir failure, silently omitting an unreadable subtree — which would
    # change the manifest without a sound. Under refuse_irregular a failure to
    # enumerate the root or any descended directory raises ConfigDistError
    # (fail closed); folder-mode (refuse_irregular=False) keeps the broader
    # skip-the-unreadable-subtree posture unchanged (ADR-0042).
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            if refuse_irregular:
                raise ConfigDistError(
                    f"cannot enumerate {current} for the config distribution: {exc}"
                    " (an unreadable subtree must never silently drop from the"
                    " manifest, ADR-0042)"
                ) from exc
            continue
        for entry in entries:
            if excluded_part(entry.name):
                continue
            full = Path(entry.path)
            if entry.is_symlink():
                if refuse_irregular:
                    raise ConfigDistError(
                        f"{full} is a symlink — the config distribution refuses"
                        " symlinks (a symlink could escape the repo, ADR-0042)"
                    )
                continue  # a symlinked dir/file is skipped, never descended
            if entry.is_dir(follow_symlinks=False):
                stack.append(full)
                continue
            if not entry.is_file(follow_symlinks=False):
                if refuse_irregular:
                    raise ConfigDistError(
                        f"{full} is not a regular file — the config distribution"
                        " refuses symlinks and other non-regular files (ADR-0042)"
                    )
                continue
            found.append(full)
    return sorted(found)


def drivers_manifest(repo_dir: Path) -> list[list[str]]:
    """The manifest: sorted ``[relpath, sha256hex]`` entries over the
    ``drivers/`` file set. ``relpath`` is POSIX and INCLUDES the ``drivers/``
    prefix; the digest is sha256 over the file bytes."""
    repo_dir = Path(repo_dir)
    root = repo_dir / DRIVERS_DIR
    entries: list[list[str]] = []
    for path in regular_files(root, refuse_irregular=True):
        relpath = path.relative_to(repo_dir).as_posix()
        try:
            data = path.read_bytes()
        except OSError as exc:
            # An unreadable drivers/ file is a packaging error, normalized to
            # ConfigDistError so the config-loading boundary can convert it into
            # the established ConfigRepoError path (ADR-0042).
            raise ConfigDistError(
                f"cannot read {relpath!r} for the config distribution: {exc}"
            ) from exc
        entries.append([relpath, hashlib.sha256(data).hexdigest()])
    return sorted(entries)


def manifest_hash(entries: list[list[str]]) -> str:
    """The content-only distribution hash. An empty manifest (missing or
    effectively-empty ``drivers/``) hashes to ``""`` — the no-distribution
    sentinel, distinct from any real 64-hex digest."""
    if not entries:
        return ""
    canonical = json.dumps(entries, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def drivers_hash(repo_dir: Path) -> str:
    """The recorded distribution hash for a Config Repo (``""`` when none)."""
    return manifest_hash(drivers_manifest(repo_dir))


def build_artifact(
    repo_dir: Path, out_dir: Path, *, built_against: str, built_at: str | None = None
) -> tuple[str, Path | None]:
    """Package ``drivers/`` into ``<out_dir>/<hash>.zip`` (atomic tempfile +
    ``os.replace``). Members ride under their ``drivers/...`` arcnames plus a
    ``config-dist.json`` metadata member at the archive root. Returns
    ``(hash, path)``; ``("", None)`` when there is no distribution to build.

    ``built_against`` stamps the product version the artifact was built against
    (advisory skew, never fail-closed). It is metadata about the manifest and
    never enters the hash, so the artifact name is fully determined by content."""
    repo_dir = Path(repo_dir)
    out_dir = Path(out_dir)
    entries = drivers_manifest(repo_dir)
    digest = manifest_hash(entries)
    if not digest:
        return "", None
    out_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "format": ARTIFACT_FORMAT,
        "drivers_hash": digest,
        "built_against": built_against,
        "built_at": built_at or datetime.now(UTC).isoformat(),
    }
    # Snapshot each file's bytes ONCE and drive both the manifest digest and
    # the archive members off that single snapshot, so a member can never carry
    # bytes that disagree with the hash it was packaged under (the manifest was
    # computed from a first read, the archive from a second — a file mutated
    # between them would otherwise poison the artifact). The completed archive
    # is then verified by unpack-and-recompute before it is published, which
    # also catches any file that changed after this snapshot was taken.
    snapshot: dict[str, bytes] = {}
    for relpath, expected in entries:
        try:
            data = (repo_dir / relpath).read_bytes()
        except OSError as exc:
            raise ConfigDistError(
                f"cannot read {relpath!r} for the config distribution: {exc}"
            ) from exc
        if hashlib.sha256(data).hexdigest() != expected:
            # The file changed between manifest creation and this snapshot: the
            # working tree moved past ``digest``. Fail the build; the node
            # retries and converges to the fresh hash on the next heartbeat.
            raise ConfigDistError(
                f"{relpath!r} changed while packaging {digest[:12]} — the working"
                " Config Repo moved on; retry the build"
            )
        snapshot[relpath] = data
    fd, tmp = tempfile.mkstemp(dir=str(out_dir), prefix=f".{digest}.", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            for relpath, _ in entries:
                info = zipfile.ZipInfo(relpath, date_time=_ZIP_DATE_TIME)
                archive.writestr(info, snapshot[relpath])
            meta = zipfile.ZipInfo(ARTIFACT_METADATA, date_time=_ZIP_DATE_TIME)
            archive.writestr(meta, json.dumps(metadata, sort_keys=True).encode("utf-8"))
        # Publish ONLY a completed archive that unpacks and recomputes to the
        # requested hash with structurally valid metadata naming that same hash
        # (ADR-0042): never the filename, never the archive bytes. verify_artifact
        # raises on any disagreement, and the tempfile is cleaned below.
        recomputed, _ = verify_artifact(Path(tmp))
        if recomputed != digest:  # defensive: verify_artifact already enforces this
            raise ConfigDistError(
                f"built artifact recomputes to {recomputed[:12] or '(empty)'},"
                f" expected {digest[:12]}"
            )
        target = out_dir / f"{digest}.zip"
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return digest, target


def safe_member(name: str) -> bool:
    """True for a zip member name safe to extract under a destination dir:
    non-empty, POSIX-relative, no parent-traversal component, no absolute or
    Windows drive-letter prefix. Validated explicitly — mirror of the node
    side's ``theozolith_nodedaemon.configdist.safe_member`` (verification must
    apply the SAME safety rule the node applies on extraction)."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        return False
    parts = normalized.split("/")
    return not any(part in ("", ".", "..") for part in parts)


def artifact_structure_error(names: list[str]) -> str | None:
    """The complete structural rule for a config-distribution archive's member
    list (ADR-0042), returning a human reason on the first violation or ``None``
    when the shape is valid. Mirrored byte-for-byte by
    ``theozolith_nodedaemon.configdist.artifact_structure_error`` — control
    validates before publishing OR serving, the node independently before
    extraction, so the two must agree on what an artifact may contain.

    A valid archive holds EXACTLY the single ``config-dist.json`` metadata
    member at the root plus zero or more ``drivers/...`` files that the canonical
    manifest counts. Every member must therefore be either that one metadata
    file or a hash-covered drivers file — anything the manifest would ignore
    (a dot-prefixed component, a ``__pycache__`` directory, a ``*.pyc``, a bare
    directory entry, a second top-level file, an unsafe/traversal name, or a
    duplicate) is rejected, so no ignored-but-importable content can ride along
    outside ``drivers_hash``."""
    seen: set[str] = set()
    saw_metadata = False
    for name in names:
        if name in seen:
            return f"member {name!r} is a duplicate"
        seen.add(name)
        if not safe_member(name):
            # Also rejects directory entries (a trailing '/' yields an empty
            # final component) and traversal/absolute/drive-letter names.
            return f"member {name!r} is unsafe or not a plain relative file"
        if name == ARTIFACT_METADATA:
            saw_metadata = True
            continue
        parts = name.split("/")
        if parts[0] != DRIVERS_DIR or len(parts) < 2:
            return (
                f"member {name!r} is neither the {ARTIFACT_METADATA} metadata nor a"
                f" {DRIVERS_DIR}/ file"
            )
        if any(excluded_part(part) for part in parts):
            return (
                f"member {name!r} is ignored by the manifest (a dot component,"
                " __pycache__, or *.pyc) yet rides the archive"
            )
    if not saw_metadata:
        return f"missing the {ARTIFACT_METADATA} metadata member"
    return None


def validate_metadata_bytes(raw: bytes, recomputed: str) -> dict[str, Any]:
    """Validate the complete ``config-dist.json`` envelope against a tree hash
    that was INDEPENDENTLY recomputed over the unpacked content: the bytes must
    decode as UTF-8, parse as JSON, decode to an object, carry ``format`` equal
    to ``ARTIFACT_FORMAT`` (a JSON ``true`` is not the integer 1), and carry a
    STRING ``drivers_hash`` equal to ``recomputed`` — the metadata never proves
    content, content proves the metadata (ADR-0042). Mirror of
    ``theozolith_nodedaemon.configdist.validate_metadata_bytes`` (pinned by the
    cross-package contract tests): control validates before publishing or
    serving, the node independently before stopping any live driver.

    Every data-format failure — invalid UTF-8, malformed or pathologically
    nested JSON, a scalar/array instead of an object, a wrong format, an
    absent/non-string/mismatching hash — raises ``ConfigDistError`` and only
    ConfigDistError. ``built_against`` is NOT validated: it is advisory
    (ADR-0042), so a missing or non-string stamp is the caller's empty state,
    never a rejection."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigDistError(f"config distribution metadata is not valid UTF-8: {exc}") from exc
    try:
        metadata = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        # RecursionError covers a hostile deeply-nested document — a data-format
        # failure like any other malformed metadata, not a programming error.
        raise ConfigDistError(f"config distribution metadata is not valid JSON: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ConfigDistError("config distribution metadata is not an object")
    declared_format = metadata.get("format")
    if isinstance(declared_format, bool) or declared_format != ARTIFACT_FORMAT:
        raise ConfigDistError(
            f"config distribution metadata format {declared_format!r} is not {ARTIFACT_FORMAT}"
        )
    declared = metadata.get("drivers_hash")
    if not isinstance(declared, str) or declared != recomputed:
        raise ConfigDistError(
            "config distribution metadata drivers_hash"
            f" {str(declared)[:12]!r} does not match the unpacked tree's recomputed"
            f" hash {recomputed[:12] or '(empty)'}"
        )
    return metadata


def verify_artifact(path: Path) -> tuple[str, dict[str, Any]]:
    """``verify_artifact_bytes`` over the file's contents: read the archive
    into an immutable byte snapshot, then verify that. An unreadable file is
    ``ConfigDistError`` like every other unpublishable shape."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigDistError(f"config distribution is not readable: {exc}") from exc
    return verify_artifact_bytes(data)


def verify_artifact_bytes(data: bytes) -> tuple[str, dict[str, Any]]:
    """Unpack an artifact's bytes into a throwaway temp dir, recompute the
    drivers manifest over the unpacked tree, and validate the metadata member;
    return ``(recomputed_hash, metadata)``. This is the ONLY proof an artifact
    is publishable or servable — never the filename, never trusted archive
    bytes. Verifying a BYTE SNAPSHOT (not a pathname) also means a caller that
    goes on to serve these bytes owns them through response completion — a
    concurrent prune of the cache file can never invalidate what was verified
    (ADR-0042).

    Raises ``ConfigDistError`` — and ONLY ConfigDistError — for every way an
    artifact can be unpublishable: the bytes are not a valid zip; the member
    list fails the structural rule (``artifact_structure_error``); a member's
    bytes cannot be read (bad CRC, corrupt compression, encryption); extraction
    hits a filesystem error; or the metadata member is missing, unreadable, or
    fails the complete envelope rule (``validate_metadata_bytes``: invalid
    UTF-8, malformed JSON, a non-object, a wrong ``format``, an absent,
    non-string, or mismatching ``drivers_hash``). Programming errors outside
    these failure modes are NOT caught. ``built_against`` is not checked — it
    is advisory (ADR-0042)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConfigDistError(f"config distribution is not a valid zip: {exc}") from exc
    with archive, tempfile.TemporaryDirectory(prefix=".verify-configdist-") as td:
        root = Path(td)
        resolved_root = root.resolve()
        try:
            names = archive.namelist()
            infos = archive.infolist()
        except (OSError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
            raise ConfigDistError(f"config distribution member list is unreadable: {exc}") from exc
        structure_error = artifact_structure_error(names)
        if structure_error:
            raise ConfigDistError(f"config distribution {structure_error}")
        for info in infos:
            target = (root / info.filename).resolve()
            if resolved_root != target and resolved_root not in target.parents:
                raise ConfigDistError(
                    f"config distribution member {info.filename!r} escapes the destination"
                )
            # The structural rule already forbids directory members (a trailing
            # slash fails safe_member), so every info here is a plain file.
            try:
                data = archive.read(info)
            except (OSError, zipfile.BadZipFile, EOFError, zlib.error, RuntimeError) as exc:
                # RuntimeError covers an encrypted member ("password required");
                # BadZipFile covers a bad CRC; zlib.error a corrupt deflate stream.
                raise ConfigDistError(
                    f"config distribution member {info.filename!r} is unreadable: {exc}"
                ) from exc
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            except OSError as exc:
                raise ConfigDistError(
                    f"config distribution member {info.filename!r} cannot be extracted: {exc}"
                ) from exc
        recomputed = drivers_hash(root)
        # The metadata is read as BYTES and validated by the shared envelope
        # rule — decoding is part of validation, so invalid UTF-8 is a
        # ConfigDistError like any other malformed metadata, never a leaked
        # UnicodeDecodeError (ADR-0042).
        meta_path = root / ARTIFACT_METADATA
        try:
            raw_metadata = meta_path.read_bytes()
        except OSError as exc:
            raise ConfigDistError(f"config distribution metadata is unreadable: {exc}") from exc
    return recomputed, validate_metadata_bytes(raw_metadata, recomputed)


def prune_config_artifacts(out_dir: Path, keep: int = 2) -> list[str]:
    """Cache, not archive (ADR-0024): keep at most the ``keep`` most recently
    built ``<hash>.zip`` files (the current and the previous distribution).
    Returns the pruned filenames.

    LOCK-FREE and disappearance-tolerant (ADR-0042): the cache churns under
    concurrent publication, replacement, and other pruners, so an entry that
    vanishes between enumeration and its metadata lookup — or between selection
    and its unlink — is ordinary concurrent churn (another request already
    replaced or removed it), never an error. Sort metadata is collected with
    per-entry OSError handling, and stale-survivor deletions suppress OSError
    the same way; nothing here can fail an otherwise valid artifact response.
    Ties on mtime break by name, so survivors are deterministic for any fixed
    directory state."""
    out_dir = Path(out_dir)
    try:
        candidates = list(out_dir.iterdir())
    except OSError:
        return []  # the cache dir is missing or unreadable — nothing to prune
    entries: list[tuple[float, str]] = []
    for path in candidates:
        if path.name.startswith(".") or path.suffix != ".zip":
            continue
        try:
            status = path.stat()
        except OSError:
            continue  # vanished since enumeration: concurrent churn, not an error
        if not stat.S_ISREG(status.st_mode):
            continue
        entries.append((status.st_mtime, path.name))
    entries.sort(key=lambda entry: (-entry[0], entry[1]))
    pruned: list[str] = []
    for _, name in entries[max(0, keep) :]:
        try:
            (out_dir / name).unlink()
        except OSError:
            continue  # another pruner or a replacement got here first
        pruned.append(name)
    return sorted(pruned)
