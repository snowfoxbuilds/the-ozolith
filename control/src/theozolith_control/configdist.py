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
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

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
    symlink in the wider Config Repo must never fail-close heartbeats."""
    root = Path(root)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        kept_dirs = []
        for name in sorted(dirnames):
            if excluded_part(name):
                continue
            if (Path(dirpath) / name).is_symlink():
                if refuse_irregular:
                    raise ConfigDistError(
                        f"{Path(dirpath) / name} is a symlink — the config"
                        " distribution refuses symlinks (a symlink could escape"
                        " the repo, ADR-0042)"
                    )
                continue  # a symlinked dir is skipped, never descended
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            if excluded_part(name):
                continue
            full = Path(dirpath) / name
            if full.is_symlink() or not full.is_file():
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
        entries.append([relpath, hashlib.sha256(path.read_bytes()).hexdigest()])
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
    fd, tmp = tempfile.mkstemp(dir=str(out_dir), prefix=f".{digest}.", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            for relpath, _ in entries:
                info = zipfile.ZipInfo(relpath, date_time=_ZIP_DATE_TIME)
                archive.writestr(info, (repo_dir / relpath).read_bytes())
            meta = zipfile.ZipInfo(ARTIFACT_METADATA, date_time=_ZIP_DATE_TIME)
            archive.writestr(meta, json.dumps(metadata, sort_keys=True).encode("utf-8"))
        target = out_dir / f"{digest}.zip"
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return digest, target


def prune_config_artifacts(out_dir: Path, keep: int = 2) -> list[str]:
    """Cache, not archive (ADR-0024): keep at most the ``keep`` most recently
    built ``<hash>.zip`` files (the current and the previous distribution).
    Returns the pruned filenames."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    zips = [
        p
        for p in out_dir.iterdir()
        if p.is_file() and p.suffix == ".zip" and not p.name.startswith(".")
    ]
    zips.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    pruned: list[str] = []
    for stale in zips[max(0, keep) :]:
        with contextlib.suppress(OSError):
            stale.unlink()
            pruned.append(stale.name)
    return sorted(pruned)
