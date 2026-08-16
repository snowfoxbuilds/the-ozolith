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

Mirrors (#51): each Run checkout is a ``git clone --reference <mirror>
--dissociate`` off a driver-owned bare mirror per repo, so the per-Run
download is a ref advertisement, not the whole history. The mirror lives on
the node, outside every container mount, and is lazily created on the first
claim per repo then refreshed (``git remote update --prune``) under a
per-repo file lock before each checkout. ``--dissociate`` keeps every
ADR-0013 invariant byte-identical: object transfer is local-disk at clone
time and the resulting worktree has no dependency on the mirror afterwards
— disposable, distrusted, sanitized exactly like the full clone it
replaces.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import shutil
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


def clone(
    url: str,
    dest: Path,
    *,
    branch: str | None = None,
    reference: Path | None = None,
    env: dict[str, str] | None = None,
):
    args = ["clone", "--quiet", url, str(dest)]
    if branch:
        args[2:2] = ["--branch", branch]
    if reference is not None:
        # Objects come from the local reference repo; --dissociate copies
        # them out so the checkout never depends on the mirror again (#51).
        args[2:2] = ["--reference", str(reference), "--dissociate"]
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if proc.returncode != 0:
        raise GitError(f"git clone failed: {proc.stderr.strip()}")


# -- node-local repo mirrors (#51) ---------------------------------------------

MIRROR_TMP_SUFFIX = ".tmp"  # staging dir during creation; atomic-renamed away
MIRROR_LOCK_SUFFIX = ".lock"  # per-repo advisory lock beside the mirror


def mirror_name(url: str) -> str:
    """Stable per-repo directory name: a readable slug plus a hash of the
    exact URL (two remotes that slug identically must not share a mirror).
    The hex suffix also guarantees the name never collides with the ``.tmp``
    / ``.lock`` siblings and never starts with a dot."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", url.split("://")[-1])
    slug = slug.strip("-.").removesuffix(".git")[:60].strip("-.") or "repo"
    return f"{slug}-{digest}"


def mirror_path(mirrors_dir: Path, url: str) -> Path:
    return Path(mirrors_dir) / mirror_name(url)


def _lock_path(mirrors_dir: Path, url: str) -> Path:
    return Path(mirrors_dir) / (mirror_name(url) + MIRROR_LOCK_SUFFIX)


def _open_lock(lock_path: Path) -> int:
    try:
        return os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise GitError(f"cannot open mirror lock {lock_path}: {exc}") from exc


def _lock_still_named(fd: int, lock_path: Path) -> bool:
    """The fstat validation: after flock, the path must still name the
    locked inode. The boot sweep unlinks stale lock files under the lock, so
    a waiter can win the flock on an already-unlinked inode — it must retry
    on the fresh file, or two processes end up 'holding' the same lock."""
    st_fd = os.fstat(fd)
    try:
        st_path = os.stat(lock_path)
    except FileNotFoundError:
        return False
    return (st_fd.st_dev, st_fd.st_ino) == (st_path.st_dev, st_path.st_ino)


def _acquire_lock(lock_path: Path) -> int:
    while True:
        fd = _open_lock(lock_path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if _lock_still_named(fd, lock_path):
                return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)


def try_mirror_lock(lock_path: Path) -> int | None:
    """Non-blocking, validated acquire for the boot sweep; None = held (or
    just recycled) by a live process — skip, never wait at boot."""
    try:
        fd = _open_lock(lock_path)
    except GitError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _lock_still_named(fd, lock_path):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


@contextlib.contextmanager
def mirror_lock(mirrors_dir: Path, url: str):
    """The per-repo mirror lock (#51): every mirror access — creation,
    ``remote update``, and the reference clone reading objects — happens
    under it, so concurrent Runs on one node can neither race the update nor
    watch a repack delete a pack file mid-copy."""
    Path(mirrors_dir).mkdir(parents=True, exist_ok=True)
    fd = _acquire_lock(_lock_path(mirrors_dir, url))
    try:
        yield
    finally:
        os.close(fd)  # closing the last descriptor releases the flock


def _mirror_valid(mirror: Path) -> bool:
    if not mirror.is_dir():
        return False
    probe = subprocess.run(
        ["git", "--git-dir", str(mirror), "rev-parse", "--is-bare-repository"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def _ensure_mirror_locked(mirrors_dir: Path, url: str, env: dict[str, str] | None) -> Path:
    """Create-or-heal the bare mirror; caller holds the per-repo lock.

    Creation stages into a ``.tmp`` sibling and atomically renames into
    place, so a final-named mirror is never partial; a mirror that no longer
    parses as a bare repo is deleted and lazily re-created (corruption
    recovery is delete + re-clone, never repair-in-place)."""
    mirror = mirror_path(mirrors_dir, url)
    if _mirror_valid(mirror):
        return mirror
    if mirror.exists():
        shutil.rmtree(mirror)
    staging = mirror.with_name(mirror.name + MIRROR_TMP_SUFFIX)
    if staging.exists():
        shutil.rmtree(staging)  # a dead predecessor's partial stage (we hold the lock)
    clone_args = ["clone", "--quiet", "--mirror", url, str(staging)]
    proc = subprocess.run(
        ["git", *clone_args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if proc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise GitError(f"git clone --mirror failed: {proc.stderr.strip()}")
    # Auto-gc kicked off by a later `remote update` must finish under the
    # lock, not detach into the window where a reference clone reads packs.
    git(["config", "gc.autoDetach", "false"], staging)
    staging.rename(mirror)
    return mirror


def _update_mirror_locked(mirror: Path, env: dict[str, str] | None) -> None:
    git(["remote", "update", "--prune"], mirror, env=env)


def ensure_mirror(mirrors_dir: Path, url: str, *, env: dict[str, str] | None = None) -> Path:
    """Lazily create (or heal) the repo's driver-owned bare mirror."""
    with mirror_lock(mirrors_dir, url):
        return _ensure_mirror_locked(mirrors_dir, url, env)


def update_mirror(mirrors_dir: Path, url: str, *, env: dict[str, str] | None = None) -> Path:
    """Refresh the mirror (``git remote update --prune``) under the lock."""
    with mirror_lock(mirrors_dir, url):
        mirror = _ensure_mirror_locked(mirrors_dir, url, env)
        _update_mirror_locked(mirror, env)
        return mirror


def clone_with_mirror(
    url: str, mirrors_dir: Path, dest: Path, *, env: dict[str, str] | None = None
) -> None:
    """The Run checkout (#51): ensure + update the mirror, then reference-
    clone off it with ``--dissociate``, all under the per-repo lock. The
    result is byte-for-byte the disposable, self-contained checkout a full
    clone produced — only the download shrinks. Any failure here is a
    pre-session infra failure (ADR-0016): the caller's Run fails and burns
    the normal retry; a half-context checkout is never handed to an agent."""
    with mirror_lock(mirrors_dir, url):
        mirror = _ensure_mirror_locked(mirrors_dir, url, env)
        _update_mirror_locked(mirror, env)
        clone(url, dest, reference=mirror, env=env)


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


def commit_empty(cwd: Path, message: str, name: str, email: str) -> None:
    """One empty commit: how a concluded no-change Run ships (ADR-0014)."""
    identity = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    git([*identity, "commit", "--quiet", "--allow-empty", "-m", message], cwd)


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
