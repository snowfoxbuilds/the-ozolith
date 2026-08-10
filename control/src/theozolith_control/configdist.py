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


def verify_artifact(path: Path) -> tuple[str, dict[str, Any]]:
    """Unpack a built ``<hash>.zip`` into a throwaway temp dir, recompute the
    drivers manifest over the unpacked tree, and validate the metadata member;
    return ``(recomputed_hash, metadata)``. This is the ONLY proof an artifact
    is publishable or servable — never the filename, never the archive bytes.

    Raises ``ConfigDistError`` when: the file is not a valid zip; any member
    name is unsafe (traversal / absolute / drive-letter); the metadata member
    is missing or malformed; ``format`` is not the current artifact format; or
    the metadata's ``drivers_hash`` does not equal the tree's recomputed hash.
    ``built_against`` is not checked — it is advisory (ADR-0042)."""
    path = Path(path)
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConfigDistError(f"config distribution is not a valid zip: {exc}") from exc
    with archive, tempfile.TemporaryDirectory(prefix=".verify-configdist-") as td:
        root = Path(td)
        resolved_root = root.resolve()
        for name in archive.namelist():
            if not safe_member(name):
                raise ConfigDistError(f"config distribution member {name!r} is unsafe to extract")
        for info in archive.infolist():
            target = (root / info.filename).resolve()
            if resolved_root != target and resolved_root not in target.parents:
                raise ConfigDistError(
                    f"config distribution member {info.filename!r} escapes the destination"
                )
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
        recomputed = drivers_hash(root)
        meta_path = root / ARTIFACT_METADATA
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigDistError(f"config distribution metadata is unreadable: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ConfigDistError("config distribution metadata is not an object")
    if metadata.get("format") != ARTIFACT_FORMAT:
        raise ConfigDistError(
            f"config distribution metadata format {metadata.get('format')!r} is not"
            f" {ARTIFACT_FORMAT}"
        )
    if metadata.get("drivers_hash") != recomputed:
        raise ConfigDistError(
            "config distribution metadata drivers_hash"
            f" {str(metadata.get('drivers_hash'))[:12]!r} does not match the unpacked"
            f" tree's recomputed hash {recomputed[:12] or '(empty)'}"
        )
    return recomputed, metadata


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
