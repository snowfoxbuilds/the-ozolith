"""Git operations for disposable, token-free Run checkouts (stdlib only).

Every Run works in a fresh clone (statelessness: the only carryover between
Runs is PR branch content at the Reviewer-designated resume commit). Commits
are made with an explicit per-command identity so no global git config leaks
in.

Credential handling: the checkout's ``.git/config`` never contains a token.
The driver authenticates network operations through ``auth_env`` — an inline
credential helper injected via git's ``GIT_CONFIG_*`` environment variables,
with the PAT itself in a separate env var — so nothing secret lands in argv,
in the worktree, or in any config file. The run container therefore mounts a
checkout with no credential anywhere (M2 acceptance 8).

Trust handling: after a run container has touched a checkout, its git
metadata is hostile input — hooks, ``core.fsmonitor``, credential helpers,
or a rewritten remote URL in ``.git/config`` would otherwise execute in (or
redirect the credentials of) the driver. ``sanitize_checkout`` rewrites
``.git/config`` to a known-good minimum and points ``core.hooksPath`` at an
empty directory before the driver runs any further git command there.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

TOKEN_ENV = "THEOZOLITH_GIT_TOKEN"

# A credential helper that reads the PAT from the environment. The helper
# string itself contains no secret, so it is safe in env/config listings.
_HELPER = f'!f() {{ echo username=x-access-token; echo password="${TOKEN_ENV}"; }}; f'


class GitError(RuntimeError):
    """A git command failed."""


def auth_env(token: str) -> dict[str, str]:
    """Environment overlay that authenticates git network operations."""
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": _HELPER,
        TOKEN_ENV: token,
        # Never fall through to an interactive prompt on a driver box.
        "GIT_TERMINAL_PROMPT": "0",
    }


def git(
    args: list[str],
    cwd: Path | str,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def clone(url: str, dest: Path, *, branch: str | None = None, env: dict[str, str] | None = None):
    args = ["clone", "--quiet", url, str(dest)]
    if branch:
        args[2:2] = ["--branch", branch]
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if proc.returncode != 0:
        raise GitError(f"git clone failed: {proc.stderr.strip()}")


def sanitize_checkout(workdir: Path, remote_url: str) -> None:
    """Rewrite the checkout's git metadata to a known-good minimum.

    Called right after clone (so the container starts from a clean baseline)
    and again after the run container exits, before any driver-side git
    command touches the tree: agent-written hooks, fsmonitor daemons,
    credential helpers, URL rewrites, and filter drivers all live in files
    this replaces or disarms.
    """
    git_dir = workdir / ".git"
    no_hooks = git_dir / "theozolith-no-hooks"
    no_hooks.mkdir(parents=True, exist_ok=True)
    config = "\n".join(
        [
            "[core]",
            "\trepositoryformatversion = 0",
            "\tfilemode = true",
            "\tbare = false",
            "\tlogallrefupdates = true",
            f"\thooksPath = {no_hooks}",
            '[remote "origin"]',
            f"\turl = {remote_url}",
            "\tfetch = +refs/heads/*:refs/remotes/origin/*",
            "",
        ]
    )
    (git_dir / "config").write_text(config, encoding="utf-8")


def checkout_branch(cwd: Path, branch: str, *, create: bool = False) -> None:
    if create:
        git(["checkout", "--quiet", "-b", branch], cwd)
    else:
        git(["checkout", "--quiet", branch], cwd)


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


def fetch(cwd: Path, ref: str, *, env: dict[str, str] | None = None) -> None:
    git(["fetch", "--quiet", "origin", ref], cwd, env=env)


def push(cwd: Path, branch: str, *, force: bool = False, env: dict[str, str] | None = None):
    args = ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"]
    if force:
        args.insert(2, "--force-with-lease")
    git(args, cwd, env=env)


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
