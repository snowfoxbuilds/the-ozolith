"""Bake: install a pinned Knowledge Source into an image at build time.

Designed to run inside a `docker build` (RUN theozolith-knowledge bake ...).
Clones the Knowledge Source (git URL + pin) to a temp dir, compiles and syncs
it into the target config dir, records provenance, and cleans up. Nothing
executes at container start (NODE-SUBSTRATE.md, extension points).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from theozolith_knowledge.compilers import get_compiler
from theozolith_knowledge.model import KnowledgeError, load_knowledge_root
from theozolith_knowledge.sync import SyncReport, apply_fileset, manifest_location

RECEIPT_NAME = ".theozolith-bake.json"


@dataclass(frozen=True)
class BakeResult:
    source: str
    pin: str
    commit: str
    report: SyncReport


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _checkout_pin(source: str, pin: str, workdir: Path) -> str:
    """Fetch exactly `pin` from `source` into `workdir`; return the commit."""
    for args in (["init", "--quiet"], ["remote", "add", "origin", source]):
        proc = _git(args, workdir)
        if proc.returncode != 0:
            raise KnowledgeError(f"git {args[0]} failed: {proc.stderr.strip()}")

    # Prefer a shallow fetch of the pin (works for branches, tags, and SHAs on
    # servers that allow it); fall back to a full fetch for servers that don't.
    proc = _git(["fetch", "--quiet", "--depth", "1", "origin", pin], workdir)
    if proc.returncode == 0:
        target = "FETCH_HEAD"
    else:
        proc = _git(["fetch", "--quiet", "--tags", "origin"], workdir)
        if proc.returncode != 0:
            raise KnowledgeError(
                f"could not fetch pin {pin!r} from {source!r}: {proc.stderr.strip()}"
            )
        target = pin
    proc = _git(["-c", "advice.detachedHead=false", "checkout", "--quiet", target], workdir)
    if proc.returncode != 0 and target == pin:
        proc = _git(
            ["-c", "advice.detachedHead=false", "checkout", "--quiet", f"origin/{pin}"], workdir
        )
    if proc.returncode != 0:
        raise KnowledgeError(f"could not check out pin {pin!r}: {proc.stderr.strip()}")

    proc = _git(["rev-parse", "HEAD"], workdir)
    if proc.returncode != 0:
        raise KnowledgeError(f"git rev-parse failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def bake(
    source: str,
    pin: str,
    target: Path | str,
    *,
    scope: str = "global",
    subdir: str | None = None,
    tool: str = "claude",
) -> BakeResult:
    """Install the knowledge repo at `source`, pinned to `pin`, into `target`."""
    if shutil.which("git") is None:
        raise KnowledgeError("bake requires git on PATH")
    target = Path(target)
    workdir = Path(tempfile.mkdtemp(prefix="theozolith-bake-"))
    try:
        commit = _checkout_pin(source, pin, workdir)
        knowledge_root = workdir / subdir if subdir else workdir
        root = load_knowledge_root(knowledge_root)
        files = get_compiler(tool)(root, scope)
        report = apply_fileset(target, files, manifest_location(target, scope))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    receipt_path = manifest_location(target, scope).parent / RECEIPT_NAME
    receipt = (
        json.dumps(
            {"source": source, "pin": pin, "commit": commit, "subdir": subdir, "tool": tool},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if not receipt_path.is_file() or receipt_path.read_text(encoding="utf-8") != receipt:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(receipt, encoding="utf-8")
    return BakeResult(source=source, pin=pin, commit=commit, report=report)
