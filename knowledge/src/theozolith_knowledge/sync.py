"""One-way sync engine: place a compiled FileSet into a target directory.

Deterministic and idempotent. A manifest kept next to the synced files records
the hash of every managed file, so re-syncs update or delete only what the
machinery itself placed; foreign files in the target are never touched.

Overwrite semantics (ADR-0009): sync targets are never hand-edited (ADR-0001).
When a managed file differs from what the last sync wrote (or a symlink sits
where a file belongs), the divergence is reported and the source wins — unless
strict mode is on, in which case the sync fails before writing anything.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from theozolith_knowledge.claude import FileEntry, FileSet
from theozolith_knowledge.model import KnowledgeError

MANIFEST_NAME = ".theozolith-knowledge-manifest.json"
MANIFEST_VERSION = 1


@dataclass
class SyncReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    # Managed paths whose target-side state had diverged from the last sync
    # (hand-edits, or a symlink where a file belongs). Always also counted in
    # created/updated/deleted.
    hand_edited: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.deleted)

    def summary(self) -> str:
        return (
            f"{len(self.created)} created, {len(self.updated)} updated, "
            f"{len(self.deleted)} deleted, {self.unchanged} unchanged"
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_relpath(path: str) -> None:
    parts = PurePosixPath(path).parts
    if not parts or parts[0] in ("/", "~", "..") or ".." in parts:
        raise KnowledgeError(f"unsafe sync path: {path!r}")


def _load_manifest(manifest_path: Path) -> dict[str, str]:
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = data["files"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise KnowledgeError(f"corrupt sync manifest: {manifest_path}") from exc
    for path in files:
        _validate_relpath(path)
    return files


def _dump_manifest(files: dict[str, str]) -> str:
    return (
        json.dumps(
            {"version": MANIFEST_VERSION, "files": dict(sorted(files.items()))},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    current = start
    while current != stop and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


def apply_fileset(
    target_root: Path,
    files: FileSet,
    manifest_path: Path | None = None,
    *,
    check: bool = False,
    strict: bool = False,
) -> SyncReport:
    """Mirror `files` into `target_root`, tracking managed files in a manifest.

    check: compute and return the report without touching the target.
    strict: raise instead of overwriting/deleting diverged managed files;
            nothing is written when strict fails.
    """
    target_root = Path(target_root)
    if manifest_path is None:
        manifest_path = target_root / MANIFEST_NAME
    for path in files:
        _validate_relpath(path)
    manifest = _load_manifest(manifest_path)
    report = SyncReport()

    writes: list[tuple[str, FileEntry]] = []
    deletes: list[str] = []

    for path in sorted(files):
        entry = files[path]
        dest = target_root / path
        if dest.is_symlink():
            # Never write through a symlink: whatever it points at is not ours.
            report.updated.append(path)
            report.hand_edited.append(path)
            writes.append((path, entry))
        elif not dest.exists():
            report.created.append(path)
            writes.append((path, entry))
        elif not dest.is_file():
            raise KnowledgeError(f"sync target exists and is not a file: {dest}")
        else:
            current = dest.read_bytes()
            current_exec = bool(dest.stat().st_mode & 0o100)
            if current == entry.content and current_exec == entry.executable:
                report.unchanged += 1
            else:
                recorded = manifest.get(path)
                if recorded is None or _sha256(current) != recorded:
                    report.hand_edited.append(path)
                report.updated.append(path)
                writes.append((path, entry))

    for path in sorted(manifest):
        if path in files:
            continue
        dest = target_root / path
        if dest.is_symlink() or dest.is_file():
            if dest.is_symlink() or _sha256(dest.read_bytes()) != manifest[path]:
                report.hand_edited.append(path)
            report.deleted.append(path)
            deletes.append(path)
        # Already gone: silently drop from the manifest.

    if strict and report.hand_edited:
        raise KnowledgeError(
            "sync target has diverged from the last sync (strict mode, nothing written): "
            + ", ".join(sorted(report.hand_edited))
        )
    if check:
        return report

    for path, entry in writes:
        dest = target_root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        dest.write_bytes(entry.content)
        mode = dest.stat().st_mode
        dest.chmod(mode | 0o111 if entry.executable else mode & ~0o111)
    for path in deletes:
        dest = target_root / path
        dest.unlink()
        _prune_empty_dirs(dest.parent, target_root)

    new_manifest = _dump_manifest({path: _sha256(e.content) for path, e in files.items()})
    if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != new_manifest:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(new_manifest, encoding="utf-8")
    return report


def manifest_location(target_root: Path, scope: str) -> Path:
    """Where the manifest lives: the target root in global scope, inside the
    tool dir in project scope. The project branch is Claude-specific by
    construction — the only compiler that accepts project scope is Claude's
    (the codex compiler is global-only, ADR-0052)."""
    target_root = Path(target_root)
    if scope == "project":
        return target_root / ".claude" / MANIFEST_NAME
    return target_root / MANIFEST_NAME


def sync(
    source: Path | str,
    scope: str,
    target: Path | str,
    *,
    tool: str = "claude",
    check: bool = False,
    strict: bool = False,
) -> SyncReport:
    """Load, compile, and apply a knowledge root into a target directory."""
    from theozolith_knowledge.compilers import get_compiler
    from theozolith_knowledge.model import load_knowledge_root

    root = load_knowledge_root(source)
    files = get_compiler(tool)(root, scope)
    return apply_fileset(
        Path(target),
        files,
        manifest_location(Path(target), scope),
        check=check,
        strict=strict,
    )
