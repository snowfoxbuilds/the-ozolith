"""Node-side config-distribution machinery: recompute-and-verify + safe unpack.

ADR-0042: the Node Daemon fetches a config-distribution artifact by its
drivers-hash and VERIFIES it by recomputing the manifest over the unpacked
tree — never by trusting the archive bytes or the served filename. This module
mirrors the SUBSET of ``theozolith_control.configdist`` the node needs: the
canonical hash algorithm. It cannot import the control package (nodedaemon is
stdlib-only, ADR-0010), so the algorithm is duplicated here and pinned to the
control side by a mandatory cross-package contract test — any drift between
the two is a test failure, not a silent convergence stall.

Zip extraction validates every member name EXPLICITLY (relative, no ``..``, no
absolute or drive-letter prefix) rather than delegating to ``zipfile`` — a
malicious artifact must never write outside the destination directory.

Verification over an on-disk tree is FAIL CLOSED: the applied state directory
can be malformed after a restore, interrupted maintenance, or local corruption,
so a symlink, irregular entry (FIFO/socket/device), or unenumerable subtree
raises ``ConfigDistError`` rather than being skipped — an entry either
participates in the recomputed hash or fails verification outright.

Source exclusion and applied-tree validation are DIFFERENT LAYERS (ADR-0042
amendment). Control-side SOURCE selection excludes dot-prefixed components,
``__pycache__``, and ``*.pyc`` from the manifest (the working repo may hold
them legitimately). An accepted ARTIFACT can never contain such members
(``artifact_structure_error``), so a verified tree on this side — always the
product of an extraction — must be a structural instance of an accepted
artifact: an excluded-name entry found under the applied ``drivers/`` is a
planted foreign entry (an importable ``*.pyc`` rides ``sys.path`` without ever
entering the hash) and FAILS verification rather than being skipped. The
clean-tree hash stays byte-for-byte the control side's — a tree with no such
entries hashes identically under both walks.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
import zlib
from pathlib import Path
from typing import Any

DRIVERS_DIR = "drivers"

# The metadata member at the archive/unpacked root (control side writes it):
# metadata ABOUT the manifest, excluded from the hash by construction.
ARTIFACT_METADATA = "config-dist.json"
# The metadata envelope version the control side stamps (configdist.ARTIFACT_FORMAT).
# Validated when the node reads config-dist.json — a foreign format is not
# trusted for built_against — while the drivers_hash proves content integrity.
ARTIFACT_FORMAT = 1


class ConfigDistError(RuntimeError):
    """A config-distribution artifact is malformed or unsafe to unpack."""


def excluded_part(part: str) -> bool:
    """One path component the config-distribution SOURCE file set ignores —
    dot-prefixed, ``__pycache__``, or ``*.pyc``. Mirror of the control side
    (pinned by the cross-package contract test). On this side the predicate is
    a REJECTION rule, not a skip rule: an accepted artifact can never contain
    such a member, so finding one in a verified tree is a structural failure."""
    return part.startswith(".") or part == "__pycache__" or part.endswith(".pyc")


def _regular_files(root: Path) -> list[Path]:
    """Sorted regular files under ``root``, enforcing the POST-EXTRACTION
    structure of an accepted artifact — FAIL CLOSED (ADR-0042). Extraction
    never writes an excluded name, a symlink, or an irregular entry
    (``artifact_structure_error`` + ``safe_member``), but the APPLIED tree this
    verifies can be anything after a restore, interrupted maintenance, or local
    corruption — so nothing is skipped silently:

    - a ``root`` that is a symlink (even to a directory), a regular file, or
      any other non-directory entry raises ``ConfigDistError``; only a
      genuinely missing root is the empty distribution,
    - any entry with an excluded name (dot-prefixed, ``__pycache__``, ``*.pyc``)
      raises — an accepted artifact can never contain one, and skipping it
      would let an importable planted ``*.pyc`` coexist with a converged
      report,
    - every symlink, FIFO, socket, or device raises,
    - a failure to enumerate the root or any descended directory raises —
      an unreadable subtree must never silently drop from the manifest.

    STRICTER than the control side's ``regular_files(refuse_irregular=True)``
    by design: control selects from a working SOURCE tree where excluded names
    are legitimate and skipped; this side validates the product of an
    extraction, where they cannot legitimately exist. The two walks agree
    byte-for-byte on any clean tree (pinned by the cross-package contract
    tests): an entry can only be hashed content or a verification failure,
    never unhashed-but-present."""
    root = Path(root)
    if root.is_symlink():
        raise ConfigDistError(
            f"{root} is a symlink — a verified tree refuses a symlinked drivers"
            " root (it could alias a tree from outside the applied state, ADR-0042)"
        )
    if not root.exists():
        return []  # a genuinely missing drivers/ is the empty sentinel
    if not root.is_dir():
        raise ConfigDistError(
            f"{root} is not a directory — a verified tree refuses a non-directory"
            " drivers root (regular file, FIFO, socket, device; ADR-0042)"
        )
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise ConfigDistError(
                f"cannot enumerate {current} while verifying the config"
                f" distribution: {exc} (an unreadable subtree must never silently"
                " drop from the manifest, ADR-0042)"
            ) from exc
        for entry in entries:
            full = Path(entry.path)
            if excluded_part(entry.name):
                raise ConfigDistError(
                    f"{full} has a name an accepted artifact can never contain (a dot"
                    " component, __pycache__, or *.pyc) — a planted entry in the"
                    " applied tree, never skipped (ADR-0042)"
                )
            if entry.is_symlink():
                raise ConfigDistError(
                    f"{full} is a symlink — a verified tree refuses symlinks"
                    " (unhashed reachable content, ADR-0042)"
                )
            if entry.is_dir(follow_symlinks=False):
                stack.append(full)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ConfigDistError(
                    f"{full} is not a regular file — a verified tree refuses"
                    " FIFOs, sockets, and devices (ADR-0042)"
                )
            found.append(full)
    return sorted(found)


def manifest_hash_of_tree(dist_root: Path) -> str:
    """Recompute the drivers-hash over an unpacked config-distribution root
    (which holds ``drivers/`` plus the ``config-dist.json`` metadata member).
    The manifest covers only the ``drivers/`` file set, with relpaths INCLUDING
    the ``drivers/`` prefix — the metadata member at the root is excluded by
    construction. An empty tree hashes to ``""``. Byte-for-byte the control
    side's ``manifest_hash(drivers_manifest(...))``.

    FAIL CLOSED (ADR-0042): a symlinked or otherwise irregular ``drivers``
    root, any symlink/FIFO/socket/device below it, any entry whose name an
    accepted artifact can never contain (a dot component, ``__pycache__``, a
    ``*.pyc``), a failure to enumerate any included directory, or an unreadable
    file all raise ``ConfigDistError`` — the caller reads that as non-converged
    and repairs; an entry is never silently omitted from the hash."""
    dist_root = Path(dist_root)
    root = dist_root / DRIVERS_DIR
    entries: list[list[str]] = []
    for path in _regular_files(root):
        relpath = path.relative_to(dist_root).as_posix()
        try:
            data = path.read_bytes()
        except OSError as exc:
            # Normalize a read failure over the unpacked tree to ConfigDistError
            # so the caller treats it as non-converged / a repair trigger rather
            # than an unhandled crash (ADR-0042).
            raise ConfigDistError(
                f"cannot read {relpath!r} while recomputing the config distribution: {exc}"
            ) from exc
        entries.append([relpath, hashlib.sha256(data).hexdigest()])
    entries.sort()
    if not entries:
        return ""
    canonical = json.dumps(entries, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def safe_member(name: str) -> bool:
    """True for a zip member name safe to extract under a destination dir:
    non-empty, POSIX-relative, no parent-traversal component, no absolute or
    Windows drive-letter prefix. Validated explicitly, never delegated to
    ``zipfile`` (ADR-0042)."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        return False
    parts = normalized.split("/")
    return not any(part in ("", ".", "..") for part in parts)


def artifact_structure_error(names: list[str]) -> str | None:
    """The complete structural rule for a config-distribution archive's member
    list (ADR-0042). Mirror of ``theozolith_control.configdist``'s function of
    the same name — the node validates the shape INDEPENDENTLY before extraction,
    so control's and the node's rules must agree byte-for-byte (pinned by a
    cross-package contract test).

    A valid archive holds EXACTLY the single ``config-dist.json`` metadata member
    at the root plus zero or more ``drivers/...`` files the canonical manifest
    counts. Anything the manifest would ignore (a dot component, ``__pycache__``,
    a ``*.pyc``, a bare directory entry, a second top-level file, an unsafe name,
    or a duplicate) is rejected, so no ignored-but-importable content can ride
    along outside ``drivers_hash``."""
    seen: set[str] = set()
    saw_metadata = False
    for name in names:
        if name in seen:
            return f"member {name!r} is a duplicate"
        seen.add(name)
        if not safe_member(name):
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
    ``theozolith_control.configdist.validate_metadata_bytes`` (pinned by the
    cross-package contract tests): control validates before publishing or
    serving, the node independently before stopping any live driver or
    exchanging the applied tree.

    Every data-format failure — invalid UTF-8, malformed or pathologically
    nested JSON, a scalar/array instead of an object, a wrong format, an
    absent/non-string/mismatching hash — raises ``ConfigDistError`` and only
    ConfigDistError. ``built_against`` is NOT validated: it is advisory
    (ADR-0042), so a missing or non-string stamp is the caller's empty state
    (``advisory_built_against``), never a rejection."""
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


def validate_metadata_tree(dist_root: Path, recomputed: str) -> dict[str, Any]:
    """``validate_metadata_bytes`` over ``<dist_root>/config-dist.json``. The
    caller passes the hash it ALREADY recomputed over the same root
    (``manifest_hash_of_tree``) — the metadata is never trusted as content
    proof. A missing or unreadable metadata file is ``ConfigDistError`` like
    every other invalid envelope: an accepted artifact always contains one, so
    a tree without it is not the product of a verified extraction."""
    meta_path = Path(dist_root) / ARTIFACT_METADATA
    try:
        raw = meta_path.read_bytes()
    except OSError as exc:
        raise ConfigDistError(f"config distribution metadata is unreadable: {exc}") from exc
    return validate_metadata_bytes(raw, recomputed)


def advisory_built_against(metadata: dict[str, Any]) -> str:
    """The advisory ``built_against`` stamp from a VALIDATED envelope: the
    string value when present, ``""`` otherwise. Missing or non-string stays
    the advisory empty state — never a failure and never convergence input
    (ADR-0042)."""
    built = metadata.get("built_against")
    return built if isinstance(built, str) else ""


def extract_zip(data: bytes, dest: Path) -> None:
    """Extract a config-distribution zip into ``dest`` (which must exist),
    validating the COMPLETE structural rule FIRST (``artifact_structure_error``)
    — a single bad member fails the whole extraction before anything is written.
    Every failure mode (a corrupt zip, a bad member list, a bad-CRC or
    corrupt/encrypted member, a filesystem error) surfaces as ConfigDistError,
    so the caller treats it as non-converged and repairs on the next fetch."""
    dest = Path(dest)
    root = dest.resolve()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConfigDistError(f"config distribution is not a valid zip: {exc}") from exc
    with archive:
        try:
            names = archive.namelist()
            infos = archive.infolist()
        except (OSError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
            raise ConfigDistError(f"config distribution member list is unreadable: {exc}") from exc
        structure_error = artifact_structure_error(names)
        if structure_error:
            raise ConfigDistError(f"config distribution {structure_error}")
        for info in infos:
            target = (dest / info.filename).resolve()
            if root != target and root not in target.parents:
                # Defence in depth: the structural rule already rejected traversal.
                raise ConfigDistError(
                    f"config distribution member {info.filename!r} escapes the destination"
                )
            # The structural rule forbids directory members, so this is a file.
            try:
                payload = archive.read(info)
            except (OSError, zipfile.BadZipFile, EOFError, zlib.error, RuntimeError) as exc:
                raise ConfigDistError(
                    f"config distribution member {info.filename!r} is unreadable: {exc}"
                ) from exc
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            except OSError as exc:
                raise ConfigDistError(
                    f"config distribution member {info.filename!r} cannot be extracted: {exc}"
                ) from exc
