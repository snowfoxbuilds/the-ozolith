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
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

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
    """One path component the config-distribution file set ignores — dot-prefixed,
    ``__pycache__``, or ``*.pyc``. Mirror of the control side (pinned by the
    cross-package contract test)."""
    return part.startswith(".") or part == "__pycache__" or part.endswith(".pyc")


def _regular_files(root: Path) -> list[Path]:
    """Sorted regular files under ``root`` passing ``excluded_part`` on every
    component. Symlinks and other non-regular entries are skipped (a verified
    artifact never contains them — see ``safe_member`` — so this only ever sees
    plain files)."""
    root = Path(root)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not excluded_part(name) and not (Path(dirpath) / name).is_symlink()
        )
        for name in sorted(filenames):
            if excluded_part(name):
                continue
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
                continue
            found.append(full)
    return sorted(found)


def manifest_hash_of_tree(dist_root: Path) -> str:
    """Recompute the drivers-hash over an unpacked config-distribution root
    (which holds ``drivers/`` plus the ``config-dist.json`` metadata member).
    The manifest covers only the ``drivers/`` file set, with relpaths INCLUDING
    the ``drivers/`` prefix — the metadata member at the root is excluded by
    construction. An empty tree hashes to ``""``. Byte-for-byte the control
    side's ``manifest_hash(drivers_manifest(...))``."""
    dist_root = Path(dist_root)
    root = dist_root / DRIVERS_DIR
    entries: list[list[str]] = []
    for path in _regular_files(root):
        relpath = path.relative_to(dist_root).as_posix()
        entries.append([relpath, hashlib.sha256(path.read_bytes()).hexdigest()])
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


def extract_zip(data: bytes, dest: Path) -> None:
    """Extract a config-distribution zip into ``dest`` (which must exist),
    validating every member name with ``safe_member`` FIRST — a single unsafe
    member fails the whole extraction before anything is written outside the
    check. Directory members are honored; file members are written under
    ``dest``."""
    dest = Path(dest)
    root = dest.resolve()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ConfigDistError(f"config distribution is not a valid zip: {exc}") from exc
    with archive:
        names = archive.namelist()
        for name in names:
            if not safe_member(name):
                raise ConfigDistError(f"config distribution member {name!r} is unsafe to extract")
        for info in archive.infolist():
            target = (dest / info.filename).resolve()
            if root != target and root not in target.parents:
                # Defence in depth: safe_member already rejected traversal.
                raise ConfigDistError(
                    f"config distribution member {info.filename!r} escapes the destination"
                )
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
