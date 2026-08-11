"""clone-init contract (ADR-0043): the Flight Deck's shared knowledge clone.

Full git round-trips against a local-path "remote"; no network, no cluster
dependency (the isolation suite covers the dependency-free guarantee).
"""

from __future__ import annotations

import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest
from theozolith_knowledge import cloneinit
from theozolith_knowledge.cli import main
from theozolith_knowledge.cloneinit import LOCK_NAME, clone_init
from theozolith_knowledge.model import KnowledgeError

_PATH = os.environ.get("PATH", "/usr/bin:/bin")


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(cwd),
            "PATH": _PATH,
        },
    )


def make_source_repo(path: Path, *, branch: str = "main") -> str:
    """A local git repo standing in for the knowledge remote; returns its path.
    Several top-level entries, so an interrupted promotion has boundaries to
    crash at between the first child and the final ``.git`` move."""
    path.mkdir(parents=True)
    _git(["init", "-q", "-b", branch], path)
    (path / "AGENTS.md").write_text("v1\n")
    (path / "extra.md").write_text("extra\n")
    (path / "skills").mkdir()
    (path / "skills" / "skill.md").write_text("skill\n")
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "one"], path)
    return str(path)


def head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_fresh_clone_into_empty_target(tmp_path):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"

    assert clone_init(source, target) == "cloned"
    assert (target / ".git").is_dir()
    assert (target / "AGENTS.md").read_text() == "v1\n"
    # origin is wired for the human push/pull promote flow.
    origin = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=target,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert origin == source
    # The promote flow is `git add -A`: the lock file must not pollute it — the
    # working tree reads clean right after the clone.
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True, check=True
    ).stdout
    assert porcelain == "", porcelain
    assert (target / LOCK_NAME).exists()  # ...but the lock is still there


def test_second_run_is_a_no_op_even_when_the_source_moved_ahead(tmp_path):
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    clone_init(str(source), target)
    pinned = head(target)

    # Source advances; clone-init must NOT fetch or pull (a pull is a human act).
    (source / "AGENTS.md").write_text("v2\n")
    _git(["commit", "-qam", "two"], source)

    assert clone_init(str(source), target) == "unchanged"
    assert head(target) == pinned
    assert (target / "AGENTS.md").read_text() == "v1\n"  # untouched


def test_origin_mismatch_is_a_hard_error(tmp_path):
    source = make_source_repo(tmp_path / "src")
    other = make_source_repo(tmp_path / "other")
    target = tmp_path / "clone"
    clone_init(source, target)

    with pytest.raises(KnowledgeError, match="already clones"):
        clone_init(other, target)


def test_non_empty_non_git_target_is_rejected(tmp_path):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    (target / "scratch.txt").write_text("mine\n")

    with pytest.raises(KnowledgeError, match="not empty and not a git checkout"):
        clone_init(source, target)
    assert (target / "scratch.txt").exists()  # never clobbered


def test_a_lone_lock_file_still_counts_as_empty(tmp_path):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    (target / LOCK_NAME).write_text("")  # a prior aborted acquisition

    assert clone_init(source, target) == "cloned"
    assert (target / "AGENTS.md").read_text() == "v1\n"


def test_branch_is_honored(tmp_path):
    source = make_source_repo(tmp_path / "src", branch="authoring")
    target = tmp_path / "clone"

    clone_init(source, target, branch="authoring")
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=target,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert current == "authoring"


def test_lock_serializes_concurrent_siblings_so_exactly_one_clones(tmp_path):
    """Two same-type Flight Decks first-starting concurrently share the one
    knowledge volume; the flock must serialize them so exactly one clone runs
    and the second sees the finished checkout and no-ops."""
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"

    clones = []
    real_clone = cloneinit._clone

    def slow_clone(src, tgt, branch):
        clones.append(src)
        # Widen the window a naive (lockless) racer would both slip through.
        threading.Event().wait(0.2)
        return real_clone(src, tgt, branch)

    cloneinit._clone = slow_clone
    results: list[str] = []
    try:

        def run():
            results.append(clone_init(source, target))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        cloneinit._clone = real_clone

    assert len(clones) == 1  # the lock prevented a double clone
    assert sorted(results) == ["cloned", "unchanged"]
    assert (target / "AGENTS.md").read_text() == "v1\n"


# -- crash recovery (the staged, marker-guarded promotion) ------------------------


def _plant_marker(target: Path) -> None:
    """A marker in the exact shape ``_create_marker`` writes — empty, one
    link, caller-owned, mode 0600 (chmod'd so the umask can't loosen it) —
    the only shape resume validation adopts as protocol state."""
    marker = target / cloneinit._MARKER_NAME
    marker.touch(mode=0o600)
    marker.chmod(0o600)


def _assert_complete_checkout(target: Path, source: Path) -> None:
    """The convergence bar every rerun must clear: a full, clean checkout with
    no scratch left behind and the promote flow's `git add -A` unpolluted."""
    assert (target / ".git").is_dir()
    assert (target / "AGENTS.md").read_text() == "v1\n"
    assert (target / "extra.md").read_text() == "extra\n"
    assert (target / "skills" / "skill.md").read_text() == "skill\n"
    assert head(target) == head(source)
    assert not (target / cloneinit._STAGE_NAME).exists()
    assert not (target / cloneinit._MARKER_NAME).exists()
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True, check=True
    ).stdout
    assert porcelain == "", porcelain


def _crash_after_renames(monkeypatch, n: int):
    """Let ``n`` promotion renames complete, then die on the next one."""
    real = os.rename
    seen = {"count": 0}

    def failing(src: str, dst: str) -> None:
        if seen["count"] >= n:
            raise RuntimeError("injected crash")
        seen["count"] += 1
        real(src, dst)

    monkeypatch.setattr(cloneinit, "_rename", failing)


# The source repo has 3 top-level entries + .git = 4 ordered renames; n=0 dies
# before any move, n=3 dies on the final .git move itself.
@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_interrupted_promotion_recovers_at_every_rename_boundary(tmp_path, monkeypatch, n):
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    _crash_after_renames(monkeypatch, n)

    with pytest.raises(RuntimeError, match="injected crash"):
        clone_init(str(source), target)
    # The marker is up (it precedes the first rename), so the next run must
    # treat the target as a promotion in flight — never as a finished checkout.
    assert (target / cloneinit._MARKER_NAME).exists()

    monkeypatch.setattr(cloneinit, "_rename", os.rename)
    assert clone_init(str(source), target) == "recovered"
    _assert_complete_checkout(target, source)


def test_interrupted_recovery_recovers_again(tmp_path, monkeypatch):
    """A crash during the RESUME converges too — recovery is re-entrant."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    _crash_after_renames(monkeypatch, 1)
    with pytest.raises(RuntimeError):
        clone_init(str(source), target)

    _crash_after_renames(monkeypatch, 1)  # one more child, then die again
    with pytest.raises(RuntimeError):
        clone_init(str(source), target)

    monkeypatch.setattr(cloneinit, "_rename", os.rename)
    assert clone_init(str(source), target) == "recovered"
    _assert_complete_checkout(target, source)


def test_interrupted_clone_before_marker_is_recloned(tmp_path):
    """Marker-less stage debris — an interruption mid-clone or anywhere before
    the marker (incl. before/after the exclude write) — is git output, never
    operator content: the rerun clears it and clones fresh."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    stage = target / cloneinit._STAGE_NAME

    # Before exclusion setup: a torn partial clone.
    stage.mkdir(parents=True)
    (stage / ".git").mkdir()
    (stage / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (stage / "AGENTS.md").write_text("torn partial clone\n")
    assert clone_init(str(source), target) == "cloned"
    _assert_complete_checkout(target, source)

    # After exclusion setup, before the marker: a COMPLETE staged clone.
    target2 = tmp_path / "clone2"
    stage2 = target2 / cloneinit._STAGE_NAME
    target2.mkdir()
    _git(["clone", "-q", str(source), str(stage2)], tmp_path)
    with (stage2 / ".git" / "info" / "exclude").open("a") as handle:
        handle.write(f"/{LOCK_NAME}\n")
    assert clone_init(str(source), target2) == "cloned"
    _assert_complete_checkout(target2, source)


def test_crash_after_git_move_before_cleanup_recovers(tmp_path):
    """Marker + empty stage + complete checkout = the window between the last
    rename and the scratch cleanup; the rerun just finishes the cleanup."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    clone_init(str(source), target)
    (target / cloneinit._STAGE_NAME).mkdir()
    _plant_marker(target)

    assert clone_init(str(source), target) == "recovered"
    _assert_complete_checkout(target, source)


def test_git_alone_is_never_proof_of_promotion(tmp_path):
    """REGRESSION (the pre-recovery bug): .git present beside stage debris with
    no marker meant a half-promoted tree, and the old code answered
    'unchanged'. It must be a hard, non-destructive error — never acceptance
    of a partial checkout."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    clone_init(str(source), target)
    # Reconstruct the old crash shape: .git moved, a sibling still staged.
    stage = target / cloneinit._STAGE_NAME
    stage.mkdir()
    (target / "AGENTS.md").rename(stage / "AGENTS.md")

    for _ in range(2):  # converges to the same clear error, touching nothing
        with pytest.raises(KnowledgeError, match="partial promotion"):
            clone_init(str(source), target)
        assert (stage / "AGENTS.md").read_text() == "v1\n"
        assert (target / ".git").is_dir()


def test_marker_with_nothing_promotable_is_a_hard_error(tmp_path):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    _plant_marker(target)

    for _ in range(2):
        with pytest.raises(KnowledgeError, match="neither a promotable staging area"):
            clone_init(source, target)
        assert (target / cloneinit._MARKER_NAME).exists()  # left for the human


def test_promotion_never_overwrites_external_content(tmp_path, monkeypatch):
    """Operator content that appeared at a staged name mid-crash is never
    clobbered by the resume — loud error, both copies intact."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    _crash_after_renames(monkeypatch, 0)
    with pytest.raises(RuntimeError):
        clone_init(str(source), target)
    monkeypatch.setattr(cloneinit, "_rename", os.rename)
    (target / "AGENTS.md").write_text("operator scratch\n")

    with pytest.raises(KnowledgeError, match="nothing was overwritten"):
        clone_init(str(source), target)
    assert (target / "AGENTS.md").read_text() == "operator scratch\n"
    assert (target / cloneinit._STAGE_NAME / "AGENTS.md").read_text() == "v1\n"


def test_pre_existing_checkout_gains_the_scratch_excludes(tmp_path):
    """The clean-promote invariant holds for checkouts that predate clone-init
    (or lost their exclude file), not only fresh clones: the unchanged path
    (re)establishes the excludes so `git add -A` stays clean."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    _git(["clone", "-q", str(source), str(target)], tmp_path)  # not via clone-init
    exclude = target / ".git" / "info" / "exclude"
    exclude.write_text("")  # and no excludes survive

    assert clone_init(str(source), target) == "unchanged"
    content = exclude.read_text()
    for name in (LOCK_NAME, cloneinit._STAGE_NAME, cloneinit._MARKER_NAME):
        assert f"/{name}" in content
    _git(["add", "-A"], target)
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True, check=True
    ).stdout
    assert porcelain == "", porcelain  # the lock clone_init created is excluded


def test_failure_to_establish_excludes_is_loud(tmp_path):
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    _git(["clone", "-q", str(source), str(target)], tmp_path)
    exclude = target / ".git" / "info" / "exclude"
    exclude.write_text("")
    exclude.chmod(0o444)
    try:
        with pytest.raises(KnowledgeError, match="scratch excludes"):
            clone_init(str(source), target)
    finally:
        exclude.chmod(0o644)


def test_cli_clone_init_exit_codes(tmp_path, capsys):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"

    assert main(["clone-init", "--source", source, "--target", str(target)]) == 0
    assert "cloned" in capsys.readouterr().out
    assert main(["clone-init", "--source", source, "--target", str(target)]) == 0
    assert "unchanged" in capsys.readouterr().out
    # Mismatch surfaces as the CLI's uniform error exit 1.
    other = make_source_repo(tmp_path / "other")
    assert main(["clone-init", "--source", other, "--target", str(target)]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_clone_init_reports_recovery_distinctly(tmp_path, capsys, monkeypatch):
    """A completed recovery is an event the operator should see — the CLI must
    not fold it into 'unchanged'."""
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    _crash_after_renames(monkeypatch, 1)
    with pytest.raises(RuntimeError, match="injected crash"):
        clone_init(source, target)
    monkeypatch.setattr(cloneinit, "_rename", os.rename)

    assert main(["clone-init", "--source", source, "--target", str(target)]) == 0
    out = capsys.readouterr().out
    assert "recovered" in out
    assert "unchanged" not in out


# -- scratch-path safety (reserved names, safe lock open, non-following checks) ----


def _tree_snapshot(root: Path) -> list[tuple]:
    """Every entry under ``root`` with its type, bytes (files), link
    destination (symlinks), and hard-link count — the byte-for-byte
    preservation bar rejected runs must clear."""
    entries = []
    for path in sorted(root.rglob("*")):
        st = os.lstat(path)
        content: object = None
        if stat.S_ISLNK(st.st_mode):
            content = os.readlink(path)
        elif stat.S_ISREG(st.st_mode):
            content = path.read_bytes()
        entries.append((str(path.relative_to(root)), stat.S_IFMT(st.st_mode), content, st.st_nlink))
    return entries


def _assert_only_a_fresh_lock_appeared(before: list[tuple], root: Path) -> None:
    """Nothing pre-existing changed; the only tolerated difference is the
    empty lock file clone-init legitimately creates for itself."""
    after = _tree_snapshot(root)
    removed = [e for e in before if e not in after]
    added = [e for e in after if e not in before]
    assert removed == [], removed
    assert all(e[0] == LOCK_NAME and e[2] == b"" for e in added), added


def test_lock_symlinked_to_operator_content_is_rejected_untouched(tmp_path):
    """REGRESSION: the old text-mode "w" open followed the symlink and
    truncated AGENTS.md before the lock was even held."""
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    clone_init(source, target)
    (target / LOCK_NAME).unlink()
    (target / LOCK_NAME).symlink_to(target / "AGENTS.md")

    with pytest.raises(KnowledgeError, match="symlink"):
        clone_init(source, target)
    assert (target / "AGENTS.md").read_text() == "v1\n"  # NOT truncated
    assert (target / LOCK_NAME).is_symlink()  # the link itself also untouched


def test_lock_dangling_symlink_is_rejected_and_nothing_created_through_it(tmp_path):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    (target / LOCK_NAME).symlink_to(target / "victim")

    with pytest.raises(KnowledgeError, match="symlink"):
        clone_init(source, target)
    assert not (target / "victim").exists()  # O_CREAT never wrote through
    assert (target / LOCK_NAME).is_symlink()
    assert not (target / ".git").exists()


def test_lock_hard_linked_to_another_file_is_rejected_untouched(tmp_path):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    clone_init(source, target)
    (target / LOCK_NAME).unlink()
    os.link(target / "AGENTS.md", target / LOCK_NAME)

    with pytest.raises(KnowledgeError, match="hard link"):
        clone_init(source, target)
    assert (target / "AGENTS.md").read_text() == "v1\n"
    assert (target / "AGENTS.md").stat().st_nlink == 2  # the alias still stands


def _plant_lock_directory(lock: Path) -> None:
    lock.mkdir()
    (lock / "keep.txt").write_text("keep\n")


def _plant_lock_fifo(lock: Path) -> None:
    os.mkfifo(lock)


def _plant_lock_nonempty(lock: Path) -> None:
    lock.write_text("stale content\n")


@pytest.mark.parametrize(
    ("plant", "match"),
    [
        (_plant_lock_directory, "directory"),
        (_plant_lock_fifo, "not a regular file"),
        (_plant_lock_nonempty, "never holds content"),
    ],
    ids=["directory", "fifo", "non-empty"],
)
def test_lock_irregular_shapes_are_rejected_untouched(tmp_path, plant, match):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    plant(target / LOCK_NAME)
    before = _tree_snapshot(target)

    with pytest.raises(KnowledgeError, match=match):
        clone_init(source, target)
    assert _tree_snapshot(target) == before
    assert not (target / ".git").exists()


def test_lock_group_or_world_writable_is_rejected(tmp_path):
    """Validated from the fstat mode bits, not by attempting access — so the
    check (and this test) holds under root too."""
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    (target / LOCK_NAME).touch()
    (target / LOCK_NAME).chmod(0o666)

    with pytest.raises(KnowledgeError, match="writable"):
        clone_init(source, target)
    assert (target / LOCK_NAME).stat().st_mode & 0o777 == 0o666  # untouched


def test_lock_open_failure_is_a_clear_knowledge_error(tmp_path, monkeypatch):
    """An EACCES-style open failure is injected rather than provoked via
    chmod, which root would ignore."""
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"

    def denied(path, flags, mode=0o777):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(cloneinit, "_os_open", denied)
    with pytest.raises(KnowledgeError, match="could not open the clone-init lock"):
        clone_init(source, target)


def _track_reserved_name(repo: Path, name: str) -> None:
    (repo / name).write_text("repo content\n")
    _git(["add", "--", name], repo)
    _git(["commit", "-qm", "reserved name"], repo)


@pytest.mark.parametrize("name", cloneinit._SCRATCH_NAMES)
def test_source_tracking_a_reserved_name_is_rejected_before_any_promotion(tmp_path, name):
    """A knowledge repo whose root tracks a scratch name would promote
    repository content onto clone-init's own lock/stage/marker paths — the
    clone is rejected while the target still holds nothing but the lock."""
    path = tmp_path / "src"
    source = make_source_repo(path)
    _track_reserved_name(path, name)
    target = tmp_path / "clone"

    with pytest.raises(KnowledgeError, match="reserved clone-init name"):
        clone_init(source, target)
    assert [p.name for p in target.iterdir()] == [LOCK_NAME]  # no debris, no marker


@pytest.mark.parametrize(
    ("name", "match"),
    [
        # The tracked lock is caught by the safe open (it holds content) …
        (LOCK_NAME, "never holds content"),
        # … the tracked stage-name file by the non-following shape check …
        (cloneinit._STAGE_NAME, "not the staging directory"),
        # … and the tracked marker by the exact-shape validation (it holds
        # content) — BEFORE any recovery step, let alone the cleanup unlink.
        (cloneinit._MARKER_NAME, "never holds content"),
    ],
)
def test_pre_existing_checkout_tracking_a_reserved_name_is_rejected_untouched(
    tmp_path, name, match
):
    """REGRESSION (marker case): a checkout tracking the marker name looked
    exactly like the post-promotion cleanup window, and the old resume deleted
    the tracked working-tree file."""
    path = tmp_path / "src"
    make_source_repo(path)
    _track_reserved_name(path, name)
    target = tmp_path / "clone"
    _git(["clone", "-q", str(path), str(target)], tmp_path)
    before = _tree_snapshot(target)

    with pytest.raises(KnowledgeError, match=match):
        clone_init(str(path), target)
    _assert_only_a_fresh_lock_appeared(before, target)


def test_marker_symlink_is_rejected_and_never_written_through(tmp_path):
    """REGRESSION: a dangling marker symlink was invisible to ``.exists()``,
    and the later marker ``touch`` then created a file at the symlink's
    DESTINATION."""
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    victim = tmp_path / "victim.txt"
    (target / cloneinit._MARKER_NAME).symlink_to(victim)

    with pytest.raises(KnowledgeError, match="not the regular file"):
        clone_init(source, target)
    assert not victim.exists()  # nothing written through the link
    assert (target / cloneinit._MARKER_NAME).is_symlink()
    assert not (target / cloneinit._STAGE_NAME).exists()  # no clone was started


def test_marker_directory_is_rejected_untouched(tmp_path):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    marker = target / cloneinit._MARKER_NAME
    marker.mkdir()
    (marker / "keep.txt").write_text("keep\n")

    with pytest.raises(KnowledgeError, match="not the regular file"):
        clone_init(source, target)
    assert (marker / "keep.txt").read_text() == "keep\n"


def test_stage_symlink_under_a_marker_is_rejected_and_the_destination_untouched(tmp_path):
    """REGRESSION: with a promotion marker present, a stage symlinked at a
    real directory was traversed and PROMOTED — renaming the destination's
    children away through the link."""
    source = make_source_repo(tmp_path / "src")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / ".git").mkdir()
    (victim / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (victim / "precious.md").write_text("precious\n")
    target = tmp_path / "clone"
    target.mkdir()
    _plant_marker(target)
    (target / cloneinit._STAGE_NAME).symlink_to(victim)

    with pytest.raises(KnowledgeError, match="not the staging directory"):
        clone_init(source, target)
    assert (victim / "precious.md").read_text() == "precious\n"
    assert (victim / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
    assert (target / cloneinit._MARKER_NAME).exists()  # left for the human
    assert not (target / "precious.md").exists()  # nothing promoted out of it


@pytest.mark.parametrize(
    "plant",
    [lambda p: p.write_text("not a dir\n"), os.mkfifo],
    ids=["regular-file", "fifo"],
)
def test_stage_irregular_shapes_are_rejected_untouched(tmp_path, plant):
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    plant(target / cloneinit._STAGE_NAME)
    before = _tree_snapshot(target)

    with pytest.raises(KnowledgeError, match="not the staging directory"):
        clone_init(source, target)
    _assert_only_a_fresh_lock_appeared(before, target)
    assert not (target / ".git").exists()


def test_resumed_stage_holding_a_reserved_name_is_not_promoted(tmp_path):
    """A (legacy or tampered) stage whose children include a reserved scratch
    name must not be promoted onto the live lock/stage/marker paths — and the
    refusal moves nothing."""
    source = make_source_repo(tmp_path / "src")
    target = tmp_path / "clone"
    target.mkdir()
    stage = target / cloneinit._STAGE_NAME
    stage.mkdir()
    (stage / ".git").mkdir()
    (stage / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (stage / LOCK_NAME).write_text("smuggled\n")
    _plant_marker(target)

    with pytest.raises(KnowledgeError, match="refusing to promote"):
        clone_init(source, target)
    assert (stage / LOCK_NAME).read_text() == "smuggled\n"  # still in the stage
    assert (stage / ".git" / "HEAD").exists()
    assert not (target / ".git").exists()  # nothing moved
    assert (target / cloneinit._MARKER_NAME).exists()


# -- marker exact-shape validation and promotion preflight ------------------------


def test_untracked_operator_file_at_the_marker_name_is_rejected_untouched(tmp_path):
    """REGRESSION: in a complete checkout, an UNTRACKED non-empty operator
    file wearing the marker name looked exactly like the post-promotion
    cleanup window, and the old resume unlinked it and answered
    'recovered'."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    clone_init(str(source), target)
    (target / cloneinit._MARKER_NAME).write_text("operator notes — do not delete\n")
    before = _tree_snapshot(target)

    for _ in range(2):  # converges: same rejection, still byte-for-byte intact
        with pytest.raises(KnowledgeError, match="never holds content"):
            clone_init(str(source), target)
        assert _tree_snapshot(target) == before


def test_marker_hard_linked_to_another_file_is_rejected_with_link_counts_intact(tmp_path):
    """An EMPTY file at the marker name that is a hard-link alias of operator
    content passes the size check but must fail the link-count check —
    unlinking it would silently drop the alias from the operator's file."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    clone_init(str(source), target)
    alias = target / "alias"
    alias.touch()
    os.link(alias, target / cloneinit._MARKER_NAME)
    before = _tree_snapshot(target)

    with pytest.raises(KnowledgeError, match="hard link"):
        clone_init(str(source), target)
    assert _tree_snapshot(target) == before  # snapshot includes link counts
    assert os.lstat(target / cloneinit._MARKER_NAME).st_nlink == 2
    assert os.lstat(alias).st_nlink == 2


def test_marker_group_or_world_writable_is_rejected_untouched(tmp_path):
    """A marker whose mode is not exactly the 0600 shape ``_create_marker``
    pins was written by someone else — never adopted as protocol state."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    clone_init(str(source), target)
    marker = target / cloneinit._MARKER_NAME
    marker.touch(mode=0o600)
    marker.chmod(0o666)
    before = _tree_snapshot(target)

    with pytest.raises(KnowledgeError, match="0600"):
        clone_init(str(source), target)
    assert _tree_snapshot(target) == before
    assert stat.S_IMODE(os.lstat(marker).st_mode) == 0o666  # mode untouched too
    assert marker.exists()


def test_checkout_plus_promotable_stage_under_one_marker_is_rejected_untouched(tmp_path):
    """REGRESSION: a complete checkout AND a promotable staged clone under one
    marker is a state the protocol (.git moves last, each name exactly once)
    cannot have produced — the old resume promoted anyway, and where it tore
    depended on which names happened to collide. Rejected before any move."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    clone_init(str(source), target)
    stage = target / cloneinit._STAGE_NAME
    _git(["clone", "-q", str(source), str(stage)], tmp_path)
    _plant_marker(target)
    before = _tree_snapshot(target)

    for _ in range(2):
        with pytest.raises(KnowledgeError, match="never produces"):
            clone_init(str(source), target)
        assert _tree_snapshot(target) == before


def test_promotion_collision_on_a_later_child_moves_no_earlier_child(tmp_path):
    """REGRESSION: the old per-child collision check fired mid-loop, so a
    collision on a later-sorted name left every earlier-sorted child already
    renamed to the root. All destinations are validated before the first
    rename — a rejected promotion moves nothing."""
    source = Path(make_source_repo(tmp_path / "src"))
    target = tmp_path / "clone"
    target.mkdir()
    stage = target / cloneinit._STAGE_NAME
    _git(["clone", "-q", str(source), str(stage)], tmp_path)
    _plant_marker(target)
    # 'skills' sorts after 'AGENTS.md' and 'extra.md' (and .git goes last).
    (target / "skills").mkdir()
    (target / "skills" / "mine.md").write_text("operator content\n")
    before = _tree_snapshot(target)

    with pytest.raises(KnowledgeError, match="nothing was moved"):
        clone_init(str(source), target)
    _assert_only_a_fresh_lock_appeared(before, target)
    assert not (target / "AGENTS.md").exists()  # the earlier-sorted child NOT moved
    assert (stage / "AGENTS.md").read_text() == "v1\n"  # still fully staged
    assert (target / "skills" / "mine.md").read_text() == "operator content\n"
