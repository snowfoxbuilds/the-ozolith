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

Scratch-path safety. The lock, stage, and marker names are reserved
clone-init infrastructure, and nothing at those paths is ever trusted by name
alone. The lock is opened without truncation and without following symlinks,
then fstat-validated as exactly what the protocol creates: an empty,
single-link regular file owned by the caller with no group/other write bits —
a symlink or hard-link alias aimed at operator content (say ``AGENTS.md``) is
a hard error with the alias target untouched. The marker and stage are
accepted only in the EXACT shapes the protocol writes, checked with ``lstat``
before any recovery, deletion, touch, or traversal: the stage a real
directory, the marker an empty, single-hard-link, caller-owned, mode-0600
regular file (``_create_marker`` pins that mode against the umask) — an
operator file, a hard-link alias, or a loosened mode wearing the marker name
is never adopted as protocol state. Recovery itself preflights before moving
anything: a checkout ``.git`` coexisting with a promotable staged ``.git``
under one marker is a state the protocol (which moves ``.git`` last, each
name exactly once) cannot have produced and is a hard error, and every
promotion destination is validated before the FIRST rename, so a collision on
a later-sorted child never strands earlier children at the root. A source
repository — or a pre-existing checkout — that TRACKS a reserved name is
rejected before the marker, the excludes, or any promotion step, and
promotion itself refuses to move a reserved name. Every rejection leaves all
involved paths byte-for-byte unchanged.

Credential-agnostic: it just runs ``git``. Auth for a private knowledge repo
is ambient (a config-side credential helper reading a token secret). This keeps
``knowledge/`` dependency-free — the isolation suite stays green.
"""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import stat
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

# Seams for fault-injection tests: promotion renames and the lock open go
# through these names.
_rename = os.rename
_os_open = os.open


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _open_lock(lock_path: Path) -> int:
    """Create-or-open the lock file WITHOUT truncation and without following
    symlinks, then fstat-validate the descriptor as exactly the file the
    protocol creates. A ``"w"``-mode open here would follow a planted symlink
    and truncate whatever it points at (e.g. ``AGENTS.md``) before the lock is
    even held — so nothing is trusted by name, and every rejection leaves the
    pre-existing path and whatever it aliases untouched."""
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = _os_open(str(lock_path), flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise KnowledgeError(
                f"{lock_path} is a symlink, which clone-init's lock never is —"
                " refusing to follow it; reconcile by hand (the link and its"
                " destination were not opened or changed)"
            ) from exc
        if exc.errno == errno.EISDIR:
            raise KnowledgeError(
                f"{lock_path} is a directory, not clone-init's lock file —"
                " reconcile by hand (nothing was changed)"
            ) from exc
        raise KnowledgeError(
            f"could not open the clone-init lock {lock_path}: {exc} (nothing was changed)"
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise KnowledgeError(
                f"{lock_path} is not a regular file — refusing to use it as"
                " clone-init's lock; reconcile by hand (nothing was changed)"
            )
        if st.st_nlink != 1:
            raise KnowledgeError(
                f"{lock_path} has {st.st_nlink} hard links — a lock aliased to"
                " another file is never clone-init's; reconcile by hand (the"
                " file and its other names were not changed)"
            )
        if st.st_size != 0:
            raise KnowledgeError(
                f"{lock_path} is not empty — clone-init's lock never holds"
                " content; reconcile by hand (nothing was changed)"
            )
        if st.st_uid != os.getuid():
            raise KnowledgeError(
                f"{lock_path} is owned by uid {st.st_uid}, not uid {os.getuid()} —"
                " refusing to use it as clone-init's lock; reconcile by hand"
                " (nothing was changed)"
            )
        if st.st_mode & 0o022:
            raise KnowledgeError(
                f"{lock_path} is group- or world-writable"
                f" (mode {stat.filemode(st.st_mode)}) — refusing to use it as"
                " clone-init's lock; reconcile by hand (nothing was changed)"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


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
    # The lock is held only for the duration of the check-and-clone; closing the
    # descriptor releases it. Siblings serialize here. The descriptor stays open
    # across the whole locked scope — revalidating a path instead would reopen
    # the TOCTOU the fstat validation just closed.
    fd = _open_lock(target / LOCK_NAME)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _clone_init_locked(source, target, branch)
    finally:
        os.close(fd)


def _scratch_state(path: Path) -> str:
    """Classify a scratch path via ``lstat`` — never following a symlink —
    into ``"missing"``, ``"file"``, ``"dir"``, ``"symlink"``, or another
    st_mode type name. Protocol decisions key on this, so a planted symlink or
    irregular file is seen as itself, never as what it points at."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISREG(st.st_mode):
        return "file"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISFIFO(st.st_mode):
        return "FIFO"
    if stat.S_ISSOCK(st.st_mode):
        return "socket"
    return "special file"


def _tracked_reserved(checkout: Path) -> list[str]:
    """Root-level reserved scratch names the checkout TRACKS (files, or
    directories via their contents). Tracked reserved names are repository
    content wearing protocol names — clone-init must never delete, exclude,
    or promote over them."""
    proc = _git(["ls-files", "--", *_SCRATCH_NAMES], cwd=checkout)
    if proc.returncode != 0:
        raise KnowledgeError(
            f"could not inspect {checkout} for reserved clone-init names:"
            f" {proc.stderr.strip()} (nothing was changed)"
        )
    return sorted({line.split("/", 1)[0] for line in proc.stdout.splitlines() if line.strip()})


def _clone_init_locked(source: str, target: Path, branch: str | None) -> str:
    stage = target / _STAGE_NAME
    marker_state = _scratch_state(target / _MARKER_NAME)
    if marker_state not in ("missing", "file"):
        raise KnowledgeError(
            f"{target / _MARKER_NAME} is a {marker_state}, not the regular file"
            " the promotion protocol writes — refusing to treat it as clone-init"
            " state; reconcile by hand (nothing was changed or followed)"
        )
    stage_state = _scratch_state(stage)
    if stage_state not in ("missing", "dir"):
        raise KnowledgeError(
            f"{stage} is a {stage_state}, not the staging directory the promotion"
            " protocol writes — refusing to treat it as clone-init state;"
            " reconcile by hand (nothing was changed or followed)"
        )
    recovered = False
    if marker_state == "file":
        _validate_marker(target / _MARKER_NAME)
        _resume_promotion(target)
        recovered = True

    if (target / ".git").exists():
        if stage_state != "missing" and not recovered:
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
        tracked = _tracked_reserved(target)
        if tracked:
            raise KnowledgeError(
                f"{target} tracks reserved clone-init name(s)"
                f" {', '.join(tracked)} — these names are clone-init"
                " infrastructure; remove them from the repository (nothing was"
                " changed)"
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
    # operator-authored — clearing it and starting clean is safe. The caller
    # already validated the shape: at this point the stage is a real
    # directory or absent, never a symlink.
    if os.path.lexists(stage):
        shutil.rmtree(stage)
    args = ["clone"]
    if branch:
        args += ["--branch", branch]
    args += [source, str(stage)]
    proc = _git(args)
    if proc.returncode != 0:
        shutil.rmtree(stage, ignore_errors=True)
        raise KnowledgeError(f"git clone of {source!r} failed: {proc.stderr.strip()}")
    # A source tracking a reserved scratch name would promote repository
    # content onto the lock/stage/marker paths; reject it while the stage is
    # still marker-less clone output that the next run simply re-clones.
    tracked = _tracked_reserved(stage)
    if tracked:
        shutil.rmtree(stage)
        raise KnowledgeError(
            f"source {source!r} tracks reserved clone-init name(s)"
            f" {', '.join(tracked)} — these names are clone-init infrastructure;"
            " remove them from the knowledge repo (the target was not changed)"
        )
    # The excludes are written INSIDE the stage, before promotion: the
    # clean-promote invariant travels with the .git dir, so no post-promotion
    # crash window can leave a checkout whose `git add -A` stages our scratch.
    _ignore_locally(stage)
    _create_marker(target)
    _promote(stage, target)
    (target / _MARKER_NAME).unlink()


def _validate_marker(marker: Path) -> None:
    """A pre-existing marker is adopted as protocol state only in the EXACT
    shape ``_create_marker`` writes: an empty, single-hard-link, mode-0600
    regular file owned by the caller. Anything else at the marker name —
    a non-empty operator file, a hard-link alias of one, a loosened mode —
    is NOT ours to unlink or to recover from: reject before any recovery
    step, leaving the file, its content, and its link count untouched. The
    caller established via ``lstat`` that a regular file exists here."""
    st = os.lstat(marker)
    if st.st_size != 0:
        raise KnowledgeError(
            f"{marker} is not empty — the promotion marker never holds content;"
            " refusing to treat it as clone-init state; reconcile by hand"
            " (nothing was changed or deleted)"
        )
    if st.st_nlink != 1:
        raise KnowledgeError(
            f"{marker} has {st.st_nlink} hard links — a marker aliased to"
            " another file is never clone-init's; reconcile by hand (the file"
            " and its other names were not changed)"
        )
    if st.st_uid != os.getuid():
        raise KnowledgeError(
            f"{marker} is owned by uid {st.st_uid}, not uid {os.getuid()} —"
            " refusing to treat it as clone-init state; reconcile by hand"
            " (nothing was changed or deleted)"
        )
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise KnowledgeError(
            f"{marker} has mode {stat.filemode(st.st_mode)}, not the owner-only"
            " 0600 shape the promotion protocol writes — refusing to treat it"
            " as clone-init state; reconcile by hand (nothing was changed or"
            " deleted)"
        )


def _create_marker(target: Path) -> None:
    """Create the promotion marker exclusively: ``O_EXCL`` never follows a
    symlink and fails on ANY pre-existing entry, so the marker can neither
    write through a planted link nor adopt someone else's file as protocol
    state (a plain ``touch`` would do both). The mode is pinned to exactly
    0600 (``fchmod`` — the open's mode argument is umask-masked) because
    ``_validate_marker`` accepts exactly that shape on resume."""
    marker = target / _MARKER_NAME
    try:
        fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except OSError as exc:
        raise KnowledgeError(
            f"could not create the promotion marker {marker}: {exc} — reconcile"
            " by hand (nothing was changed)"
        ) from exc


def _promote(stage: Path, target: Path) -> None:
    """Rename the staged clone's children up to the target root, ``.git``
    last — a visible ``.git`` therefore proves every sibling already moved.
    Each rename is atomic (same filesystem), and EVERY destination is
    validated before the first rename — so a rejected promotion moves
    nothing: a collision on a later-sorted child cannot strand the
    earlier-sorted children at the root."""
    children = sorted(stage.iterdir())
    reserved = sorted(c.name for c in children if c.name in _SCRATCH_NAMES)
    if reserved:
        # A staged reserved name would land ON the live lock/stage/marker
        # paths. Refuse before ANY rename so a rejected promotion moves
        # nothing.
        raise KnowledgeError(
            f"the staged clone holds reserved clone-init name(s)"
            f" {', '.join(reserved)} — refusing to promote repository content"
            " onto clone-init's own paths; reconcile by hand (nothing was"
            " moved)"
        )
    ordered = [c for c in children if c.name != ".git"] + [c for c in children if c.name == ".git"]
    # The protocol moves each name exactly once, so an occupied destination is
    # external content — never overwrite it, and never discover it mid-loop.
    occupied = [c.name for c in ordered if os.path.lexists(target / c.name)]
    if occupied:
        raise KnowledgeError(
            f"promotion found existing content at"
            f" {', '.join(repr(n) for n in occupied)} in {target} — reconcile by"
            " hand (nothing was moved, nothing was overwritten)"
        )
    for child in ordered:
        _rename(str(child), str(target / child.name))
    stage.rmdir()


def _resume_promotion(target: Path) -> None:
    """Finish (or clean up after) a promotion the marker says was interrupted.
    Converges: any further interruption lands back in one of these states.
    The caller validated the marker's exact shape and the stage's;
    ``stage/.git`` is additionally required to be a real directory
    (``lstat``) before the stage is treated as promotable — a symlinked
    ``.git`` is tampering, not ours."""
    stage = target / _STAGE_NAME
    stage_state = _scratch_state(stage)
    stage_promotable = stage_state == "dir" and _scratch_state(stage / ".git") == "dir"
    if stage_promotable and (target / ".git").exists():
        # The protocol moves each name exactly once, .git LAST — so a
        # checkout .git can never coexist with a promotable staged .git.
        # Promoting anyway would collide somewhere mid-tree; reject the
        # impossible state before anything moves.
        raise KnowledgeError(
            f"{target}: promotion marker present with BOTH a checkout .git and a"
            f" promotable {_STAGE_NAME!r} — a state the promotion protocol (which"
            " moves .git last) never produces; reconcile by hand (nothing was"
            " changed or moved)"
        )
    if stage_promotable:
        # Interrupted before .git moved: the stage still holds a complete-
        # minus-already-moved clone — finish the ordered promotion.
        _promote(stage, target)
    elif (target / ".git").exists():
        # Interrupted after .git moved: promotion complete (.git goes last),
        # only cleanup remains — but a checkout that TRACKS the marker name is
        # repository content wearing it, not our protocol state: deleting it
        # would destroy a working-tree file. Reject before unlinking anything.
        tracked = _tracked_reserved(target)
        if tracked:
            raise KnowledgeError(
                f"{target} tracks reserved clone-init name(s)"
                f" {', '.join(tracked)} — refusing to treat repository content"
                " as promotion state; reconcile by hand (nothing was changed"
                " or deleted)"
            )
        # Tolerate an empty stage remnant; content in it at this point is not
        # ours.
        if stage_state == "dir":
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
