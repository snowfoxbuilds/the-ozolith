"""clone-init: initialize the Flight Deck's shared knowledge clone at start.

ADR-0043 splits ~/.claude into per-Flight-Deck runtime state and a *shared*
knowledge clone (one per worker type per node). The Flight Deck initializes
that clone itself at container start — not the Node Daemon (which stays dumb
and holds no git credentials) and not image build (the volume does not exist
then; a build-time clone is stale by definition).

Contract (idempotent, crash-recoverable, and safe under concurrent siblings):

- An exclusive ``flock`` on ``<target>/.theozolith-clone.lock`` serializes two
  same-type Flight Decks first-starting against the same knowledge volume.
- ``<target>/.git`` present -> verify ``origin`` equals ``--source``: mismatch
  is a hard error (reconcile by hand — never auto-reclone over uncommitted
  scratch); on match do **nothing** (no fetch, no pull — pulls are human acts,
  uncommitted edits are scratch) beyond ensuring the scratch excludes.
- Empty target (or only clone-init's own scratch files) -> a full ``git
  clone`` (never shallow: the authoring surface must be able to push),
  honoring ``--branch``.
- A non-empty, non-git target is a hard error.

Crash recovery. A clone lands via a staged, marker-guarded promotion so that
an interruption at ANY point leaves a state the next run resolves — it never
accepts a partial checkout and never discards operator content:

1. clone into ``.theozolith-clone.tmp`` (the stage) inside the target;
2. write the scratch excludes into the stage's ``.git/info/exclude``;
3. create the ``.theozolith-clone.promoting`` marker — the ONLY state in
   which files at the target root may be clone-init's own half-finished
   promotion rather than operator content;
4. rename the stage's children up beside the lock file, ``.git`` LAST — so
   a visible ``.git`` means every sibling already made it;
5. drop the empty stage, then the marker.

The next run resumes an interrupted promotion (marker present), re-clones
over marker-less stage debris (an interrupted step 1/2 — the stage is always
git output, never operator-authored), and hard-errors on states this protocol
cannot have produced (e.g. a checkout WITH stage debris but no marker — the
one shape the pre-recovery code could leak, or external tampering) rather
than trusting ``.git`` alone as proof of a completed promotion.

Credential-agnostic: it just runs ``git``. Auth for a private knowledge repo
is ambient (a config-side credential helper reading a token secret). This keeps
``knowledge/`` dependency-free — the isolation suite stays green.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from pathlib import Path

from theozolith_knowledge.model import KnowledgeError

LOCK_NAME = ".theozolith-clone.lock"
# The crash-safe staging dir inside the target: git clones here (git refuses a
# non-empty destination, and the target already holds the lock file), then the
# tree is promoted up. Living inside the target keeps every promotion step a
# same-filesystem rename.
_STAGE_NAME = ".theozolith-clone.tmp"
# Present exactly while a staged clone is being promoted to the target root.
_MARKER_NAME = ".theozolith-clone.promoting"
_SCRATCH_NAMES = (LOCK_NAME, _STAGE_NAME, _MARKER_NAME)

# Seam for fault-injection tests: promotion goes through this name.
_rename = os.rename


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def clone_init(source: str, target: str | Path, *, branch: str | None = None) -> str:
    """Ensure ``target`` is a git checkout of ``source``. Returns ``"cloned"``
    when it performed a clone, ``"recovered"`` when it completed a promotion an
    earlier run left unfinished, ``"unchanged"`` when a matching checkout
    already existed. Raises ``KnowledgeError`` (exit 1 via the CLI) on any
    conflict."""
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
    stage = target / _STAGE_NAME
    recovered = False
    if (target / _MARKER_NAME).exists():
        _resume_promotion(target)
        recovered = True

    if (target / ".git").exists():
        if stage.exists() and not recovered:
            # .git next to stage debris with NO marker: this protocol never
            # produces it (the stage only outlives the marker window as
            # marker-less clone debris, and then only when .git is absent).
            # It is a pre-recovery-protocol crash or external tampering — the
            # checkout may be a partial promotion, so .git alone is NOT
            # accepted as proof of completeness.
            raise KnowledgeError(
                f"{target} holds both a .git and the {_STAGE_NAME!r} staging area with"
                " no promotion marker — the checkout may be a partial promotion;"
                " reconcile by hand (nothing was changed or deleted)"
            )
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
        # Match: do nothing to the tree. No fetch, no pull — a pull is a human
        # act and any local edits are scratch until the operator pushes them
        # (ADR-0043). The scratch excludes ARE (re)established here so a
        # checkout that predates clone-init — or lost its exclude file — keeps
        # the promote flow's `git add -A` clean.
        _ignore_locally(target)
        return "recovered" if recovered else "unchanged"

    # Not a git checkout: it must be empty but for our own scratch files.
    strays = [p for p in target.iterdir() if p.name not in _SCRATCH_NAMES]
    if strays:
        raise KnowledgeError(
            f"{target} is not empty and not a git checkout (found {strays[0].name!r}) —"
            " refusing to clone over unrelated content"
        )
    _clone(source, target, branch)
    return "cloned"


def _clone(source: str, target: Path, branch: str | None) -> None:
    stage = target / _STAGE_NAME
    # Marker-less stage debris is an interrupted CLONE (the marker is created
    # only once a complete stage exists): pure git output, never
    # operator-authored — clearing it and starting clean is safe.
    if stage.exists():
        shutil.rmtree(stage)
    args = ["clone"]
    if branch:
        args += ["--branch", branch]
    args += [source, str(stage)]
    proc = _git(args)
    if proc.returncode != 0:
        shutil.rmtree(stage, ignore_errors=True)
        raise KnowledgeError(f"git clone of {source!r} failed: {proc.stderr.strip()}")
    # The excludes are written INSIDE the stage, before promotion: the
    # clean-promote invariant travels with the .git dir, so no post-promotion
    # crash window can leave a checkout whose `git add -A` stages our scratch.
    _ignore_locally(stage)
    (target / _MARKER_NAME).touch()
    _promote(stage, target)
    (target / _MARKER_NAME).unlink()


def _promote(stage: Path, target: Path) -> None:
    """Rename the staged clone's children up to the target root, ``.git``
    last — a visible ``.git`` therefore proves every sibling already moved.
    Each rename is atomic (same filesystem), so an interruption leaves every
    child wholly in the stage or wholly at the target, never split."""
    children = sorted(stage.iterdir())
    ordered = [c for c in children if c.name != ".git"] + [c for c in children if c.name == ".git"]
    for child in ordered:
        dest = target / child.name
        if os.path.lexists(dest):
            # The protocol moves each name exactly once, so an occupied
            # destination is external content — never overwrite it.
            raise KnowledgeError(
                f"promotion of {child.name!r} found existing content at {dest} —"
                " reconcile by hand (nothing was overwritten)"
            )
        _rename(str(child), str(dest))
    stage.rmdir()


def _resume_promotion(target: Path) -> None:
    """Finish (or clean up after) a promotion the marker says was interrupted.
    Converges: any further interruption lands back in one of these states."""
    stage = target / _STAGE_NAME
    if (stage / ".git").exists():
        # Interrupted before .git moved: the stage still holds a complete-
        # minus-already-moved clone — finish the ordered promotion.
        _promote(stage, target)
    elif (target / ".git").exists():
        # Interrupted after .git moved: promotion complete (.git goes last),
        # only cleanup remains. Tolerate an empty stage remnant; content in it
        # at this point is not ours.
        if stage.exists():
            if any(stage.iterdir()):
                raise KnowledgeError(
                    f"{target}: promotion marker present, the checkout is complete,"
                    f" but {_STAGE_NAME!r} still holds content — reconcile by hand"
                    " (nothing was changed or deleted)"
                )
            stage.rmdir()
    else:
        raise KnowledgeError(
            f"{target}: promotion marker {_MARKER_NAME!r} present but there is neither"
            " a promotable staging area nor a checkout — reconcile by hand"
            " (nothing was changed or deleted)"
        )
    (target / _MARKER_NAME).unlink()


def _ignore_locally(checkout: Path) -> None:
    """Root-anchor clone-init's scratch names in the checkout's local
    ``.git/info/exclude`` (never committed) so the promote flow's
    ``git add -A`` never stages them. Failure is a hard error: silently
    proceeding would break the clean-promote invariant (ADR-0043)."""
    exclude = checkout / ".git" / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        wanted = [f"/{n}" for n in _SCRATCH_NAMES if f"/{n}" not in existing.splitlines()]
        if not wanted:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(prefix + "\n".join(wanted) + "\n")
    except OSError as exc:
        raise KnowledgeError(
            f"could not establish the scratch excludes in {exclude} ({exc}) — the"
            " promote flow's `git add -A` would stage clone-init scratch; fix the"
            " checkout's .git and re-run"
        ) from exc
