"""Git operations for disposable Run checkouts (stdlib subprocess only).

Every Run works in a fresh clone (statelessness: the only carryover between
Runs is PR branch content at the Reviewer-designated resume commit). Commits
are made with an explicit per-command identity so no global git config leaks
into the container.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """A git command failed."""


def git(args: list[str], cwd: Path | str, *, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def clone(url: str, dest: Path, *, branch: str | None = None) -> None:
    args = ["clone", "--quiet", url, str(dest)]
    if branch:
        args[2:2] = ["--branch", branch]
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitError(f"git clone failed: {proc.stderr.strip()}")


def checkout_branch(cwd: Path, branch: str, *, create: bool = False) -> None:
    if create:
        git(["checkout", "--quiet", "-b", branch], cwd)
    else:
        git(["checkout", "--quiet", branch], cwd)


def branch_exists_on_remote(cwd: Path, branch: str) -> bool:
    return bool(git(["ls-remote", "--heads", "origin", branch], cwd))


def ref_exists(cwd: Path, ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=str(cwd),
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def reset_hard(cwd: Path, ref: str) -> None:
    git(["reset", "--hard", "--quiet", ref], cwd)


def cherry_pick(cwd: Path, name: str, email: str, *commits: str) -> None:
    git(
        ["-c", f"user.name={name}", "-c", f"user.email={email}", "cherry-pick", "-x", *commits],
        cwd,
    )


def commit_all(cwd: Path, message: str, name: str, email: str) -> bool:
    """Stage everything and commit; returns False when there is nothing to commit."""
    git(["add", "--all"], cwd)
    if not git(["status", "--porcelain"], cwd):
        return False
    identity = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    git([*identity, "commit", "--quiet", "-m", message], cwd)
    return True


def push(cwd: Path, branch: str, *, force: bool = False) -> None:
    args = ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"]
    if force:
        args.insert(2, "--force-with-lease")
    git(args, cwd)


def head_sha(cwd: Path) -> str:
    return git(["rev-parse", "HEAD"], cwd)


def commit_exists(cwd: Path, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=str(cwd),
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def diff_stat(cwd: Path, base_ref: str) -> str:
    """Numstat of HEAD against the merge base with ``base_ref``."""
    return git(["diff", "--numstat", f"{base_ref}...HEAD"], cwd, check=False)
