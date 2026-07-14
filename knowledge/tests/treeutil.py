"""Tree-comparison helpers for the knowledge tests."""

from __future__ import annotations

from pathlib import Path

GOLDEN = Path(__file__).parent / "golden"
PACKAGE_ROOT = Path(__file__).parents[1]


def tree_snapshot(
    root: Path, ignore: frozenset[str] = frozenset()
) -> dict[str, tuple[bytes, bool]]:
    """Map of relative path -> (content, executable) for every file under root."""
    out: dict[str, tuple[bytes, bool]] = {}
    for path in sorted(root.rglob("*")):
        if path.name in ignore or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        out[rel] = (path.read_bytes(), bool(path.stat().st_mode & 0o100))
    return out


def stat_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Map of relative path -> (mtime_ns, size); any write changes it."""
    return {
        p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
