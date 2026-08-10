"""clone-init contract (ADR-0043): the Flight Deck's shared knowledge clone.

Full git round-trips against a local-path "remote"; no network, no cluster
dependency (the isolation suite covers the dependency-free guarantee).
"""

from __future__ import annotations

import os
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
    (target / cloneinit._MARKER_NAME).touch()

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
    (target / cloneinit._MARKER_NAME).touch()

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
