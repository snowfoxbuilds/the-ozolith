"""clone-init: initialize the Flight Deck's shared knowledge clone at start.

ADR-0043 splits ~/.claude into per-Flight-Deck runtime state and a *shared*
knowledge clone (one per worker type per node). The Flight Deck initializes
that clone itself at container start — not the Node Daemon (which stays dumb
and holds no git credentials) and not image build (the volume does not exist
then; a build-time clone is stale by definition).

Contract (idempotent, self-healing, and safe under concurrent sibling starts):

- An exclusive ``flock`` on ``<target>/.theozolith-clone.lock`` serializes two
  same-type Flight Decks first-starting against the same knowledge volume.
- ``<target>/.git`` present -> verify ``origin`` equals ``--source``: mismatch
  is a hard error (reconcile by hand — never auto-reclone over uncommitted
  scratch); on match do **nothing** (no fetch, no pull — pulls are human acts,
  uncommitted edits are scratch).
- Empty target (or only the lock file) -> a full ``git clone`` (never shallow:
  the authoring surface must be able to push), honoring ``--branch``.
- A non-empty, non-git target is a hard error.

Credential-agnostic: it just runs ``git``. Auth for a private knowledge repo
is ambient (a config-side credential helper reading a token secret). This keeps
``knowledge/`` dependency-free — the isolation suite stays green.
"""

from __future__ import annotations

import fcntl
import shutil
import subprocess
from pathlib import Path

from theozolith_knowledge.model import KnowledgeError

LOCK_NAME = ".theozolith-clone.lock"
# A crash-safe staging dir inside the target: git clones here (git refuses a
# non-empty destination, and the target already holds the lock file), then the
# tree is moved up. Living inside the target keeps the move a same-filesystem
# rename. Ignored by the emptiness check and cleared on the next run.
_STAGE_NAME = ".theozolith-clone.tmp"


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def clone_init(source: str, target: str | Path, *, branch: str | None = None) -> str:
    """Ensure ``target`` is a git checkout of ``source``. Returns ``"cloned"``
    when it performed a clone, ``"unchanged"`` when a matching checkout already
    existed. Raises ``KnowledgeError`` (exit 1 via the CLI) on any conflict."""
    if shutil.which("git") is None:
        raise KnowledgeError("clone-init requires git on PATH")
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    lock_path = target / LOCK_NAME
    # The lock is held only for the duration of the check-and-clone; closing the
    # file (context-manager exit) releases it. Siblings serialize here.
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return _clone_init_locked(source, target, branch)


def _clone_init_locked(source: str, target: Path, branch: str | None) -> str:
    if (target / ".git").exists():
        proc = _git(["config", "--get", "remote.origin.url"], cwd=target)
        origin = proc.stdout.strip()
        if proc.returncode != 0 or not origin:
            raise KnowledgeError(
                f"{target} is a git checkout with no 'origin' remote — reconcile by"
                " hand (clone-init never re-clones over an existing checkout)"
            )
        if origin != source:
            raise KnowledgeError(
                f"{target} already clones {origin!r}, not --source {source!r} —"
                " reconcile by hand (clone-init never re-clones over an existing"
                " checkout; uncommitted scratch is never discarded)"
            )
        # Match: do nothing. No fetch, no pull — a pull is a human act and any
        # local edits are scratch until the operator pushes them (ADR-0043).
        return "unchanged"

    # Not a git checkout: it must be empty but for our own lock/stage files.
    strays = [p for p in target.iterdir() if p.name not in (LOCK_NAME, _STAGE_NAME)]
    if strays:
        raise KnowledgeError(
            f"{target} is not empty and not a git checkout (found {strays[0].name!r}) —"
            " refusing to clone over unrelated content"
        )
    _clone(source, target, branch)
    return "cloned"


def _clone(source: str, target: Path, branch: str | None) -> None:
    stage = target / _STAGE_NAME
    if stage.exists():  # a crashed prior run — clear it and start clean
        shutil.rmtree(stage)
    args = ["clone"]
    if branch:
        args += ["--branch", branch]
    args += [source, str(stage)]
    proc = _git(args)
    if proc.returncode != 0:
        shutil.rmtree(stage, ignore_errors=True)
        raise KnowledgeError(f"git clone of {source!r} failed: {proc.stderr.strip()}")
    # Same-filesystem renames: lift the checkout (including .git) up beside the
    # lock file, then drop the now-empty stage dir.
    for child in stage.iterdir():
        shutil.move(str(child), str(target / child.name))
    stage.rmdir()
    # The lock/stage files sit at the clone's working-tree root; exclude them
    # locally so the promote flow's `git add -A` never stages this scratch.
    _ignore_locally(target, [LOCK_NAME, _STAGE_NAME])


def _ignore_locally(target: Path, names: list[str]) -> None:
    """Append root-anchored ignores to the clone's local ``.git/info/exclude``
    (never committed) — best-effort: an untracked lock file is harmless."""
    exclude = target / ".git" / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        wanted = [f"/{n}" for n in names if f"/{n}" not in existing.splitlines()]
        if not wanted:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(prefix + "\n".join(wanted) + "\n")
    except OSError:
        pass
