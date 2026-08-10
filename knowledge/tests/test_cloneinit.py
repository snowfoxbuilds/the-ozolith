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
    """A local git repo standing in for the knowledge remote; returns its path."""
    path.mkdir(parents=True)
    _git(["init", "-q", "-b", branch], path)
    (path / "AGENTS.md").write_text("v1\n")
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
