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
claim per repo then refreshed under a per-repo file lock before each
checkout. ``--dissociate`` keeps every ADR-0013 invariant byte-identical:
object transfer is local-disk at clone time and the resulting worktree has
no dependency on the mirror afterwards — disposable, distrusted, sanitized
exactly like the full clone it replaces.

Mirror trust boundary (#51 amendment): the mirror cache is persistent state
on a multi-user node, so nothing about it is taken on faith. The cache root
must be a real directory (``lstat``, never a symlink), owned by the
effective driver UID, with no group/world write access — validated before
every use and fail-closed otherwise. Mirror, staging, and lock entries must
be real driver-owned files/directories; symlinked entries are rejected, and
nothing is recursively deleted (or handed to git) until its parent root has
proven trusted. Persistent git config is never provenance either:
``sanitize_mirror`` rewrites the mirror's config to a known-minimal form
(exact caller-supplied URL, mirror refspec, hooks disarmed, alternates
dropped) before any authenticated operation, and the update fetches the
caller-supplied URL/refspec directly rather than trusting a stored
``remote.origin``.

Timeouts (#51 amendment): every mirror operation — the flock wait, mirror
creation, the update fetch, and the reference clone — runs under an
operator-configurable budget (``THEOZOLITH_GIT_TIMEOUT_SECONDS``). Expiry
kills the subprocess, cleans partial state, releases the lock, and raises a
self-describing :class:`GitTimeout` (a :class:`GitError`), so a stuck
holder or waiter lands in the ordinary pre-session infra lane (ADR-0016)
instead of wedging the driver.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

TOKEN_ENV = "THEOZOLITH_GIT_TOKEN"

# A credential helper that reads the PAT from the environment. The helper
# string itself contains no secret, so it is safe in env/config listings.
_HELPER = f'!f() {{ echo username=x-access-token; echo password="${TOKEN_ENV}"; }}; f'


class GitError(RuntimeError):
    """A git command failed."""


class GitTimeout(GitError):
    """A git operation (or the mirror lock wait) exceeded its budget. The
    subprocess, if any, was already killed; partial on-disk state is the
    raiser's cleanup responsibility."""


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


def _run_git(
    args: list[str],
    cwd: Path | str | None = None,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """The one subprocess seam every git invocation goes through.

    ``subprocess.run`` kills the child before raising ``TimeoutExpired``, so
    no git process outlives its budget still holding the mirror lock; the
    kill is converted into a self-describing :class:`GitTimeout` here and
    partial-state cleanup stays with the caller."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=None if cwd is None else str(cwd),
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **env} if env else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout(f"git {' '.join(args)} timed out after {timeout:.0f}s") from exc


def git(
    args: list[str],
    cwd: Path | str,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> str:
    proc = _run_git(args, cwd=cwd, env=env, timeout=timeout)
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
    timeout: float | None = None,
):
    args = ["clone", "--quiet", url, str(dest)]
    if branch:
        args[2:2] = ["--branch", branch]
    if reference is not None:
        # Objects come from the local reference repo; --dissociate copies
        # them out so the checkout never depends on the mirror again (#51).
        args[2:2] = ["--reference", str(reference), "--dissociate"]
    try:
        proc = _run_git(args, env=env, timeout=timeout)
    except GitTimeout:
        # git cleans its own destination on failure, but a killed clone
        # cannot — never leave a partial checkout for a later step to trust.
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if proc.returncode != 0:
        raise GitError(f"git clone failed: {proc.stderr.strip()}")


# -- node-local repo mirrors (#51) ---------------------------------------------

MIRROR_TMP_SUFFIX = ".tmp"  # staging dir during creation; atomic-renamed away
MIRROR_LOCK_SUFFIX = ".lock"  # per-repo advisory lock beside the mirror

_LOCK_RETRY_SECONDS = 0.05  # poll cadence of the deadline-bounded flock loop


# -- the mirror trust boundary (#51 amendment) ----------------------------------


def _require_trusted_dir(path: Path, what: str) -> None:
    """Fail closed unless ``path`` is a real directory (``lstat`` — symlinks
    are rejected, never followed), owned by the effective driver UID, with
    no group/world write access."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise GitError(f"{what} {path} is unusable: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise GitError(f"{what} {path} is a symlink — refusing to use it")
    if not stat.S_ISDIR(st.st_mode):
        raise GitError(f"{what} {path} is not a directory — refusing to use it")
    if st.st_uid != os.geteuid():
        raise GitError(
            f"{what} {path} is owned by uid {st.st_uid}, not the driver"
            f" (uid {os.geteuid()}) — refusing to use it"
        )
    if st.st_mode & 0o022:
        raise GitError(
            f"{what} {path} is group/world writable"
            f" (mode {stat.S_IMODE(st.st_mode):04o}) — refusing to use it"
        )


def _require_trusted_ancestor(path: Path, what: str) -> None:
    """The containing directory's bar: nobody but root or the driver may be
    able to swap our entry out from under a passed validation. Root- or
    driver-owned with no group/world write is safe; a world-writable sticky
    directory (``/var/tmp``) is safe too — the sticky bit blocks non-owners
    from renaming or unlinking our entries."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise GitError(f"{what} {path} is unusable: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise GitError(f"{what} {path} is not a real directory — refusing to use it")
    if st.st_uid not in (0, os.geteuid()):
        raise GitError(
            f"{what} {path} is owned by uid {st.st_uid}, neither root nor the"
            f" driver (uid {os.geteuid()}) — refusing to use it"
        )
    if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
        raise GitError(
            f"{what} {path} is writable by others without the sticky bit"
            f" (mode {stat.S_IMODE(st.st_mode):04o}) — refusing to use it"
        )


def validate_mirrors_root(mirrors_dir: Path) -> Path:
    """Validate (never create) the cache root and its containing directory."""
    root = Path(mirrors_dir)
    if root.parent != root:
        _require_trusted_ancestor(root.parent, "mirror root parent")
    _require_trusted_dir(root, "mirror root")
    return root


def ensure_mirrors_root(mirrors_dir: Path) -> Path:
    """Create-if-missing, then validate, the cache root.

    Production provisions ``/var/tmp/theozolith/mirrors`` at install time
    (driver-owned, no group/world write); this lazy path exists for dev
    shapes and for ``/var/tmp`` aging having removed the cache. Missing
    components are created 0700; creation then re-validates, so a symlink
    planted before our ``mkdir`` (EEXIST) is caught by the ``lstat`` checks
    and fails closed. The cache is non-authoritative — when validation
    fails, the remedy is always: delete the root, let it recreate."""
    root = Path(mirrors_dir)
    missing: list[Path] = []
    probe = root
    while probe.parent != probe:
        try:
            os.lstat(probe)
            break
        except FileNotFoundError:
            missing.append(probe)
            probe = probe.parent
    for path in reversed(missing):
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass  # raced a sibling driver on this node — validated below
        except OSError as exc:
            raise GitError(f"cannot create mirror root {path}: {exc}") from exc
    return validate_mirrors_root(root)


def _entry_state(path: Path, what: str) -> str:
    """``"missing"`` or ``"dir"``; anything else under the (validated) root
    fails closed — a symlinked or foreign-owned entry is never followed,
    deleted, or handed to git."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise GitError(f"cannot stat {what} {path}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise GitError(f"{what} {path} is a symlink — refusing to touch it")
    if not stat.S_ISDIR(st.st_mode):
        raise GitError(f"{what} {path} is not a directory — refusing to touch it")
    if st.st_uid != os.geteuid():
        raise GitError(
            f"{what} {path} is owned by uid {st.st_uid}, not the driver"
            f" (uid {os.geteuid()}) — refusing to touch it"
        )
    return "dir"


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
    """Open (creating if needed) the lock file, refusing anything that is
    not a driver-owned regular file: ``O_NOFOLLOW`` rejects a symlinked lock
    entry outright, and the post-open ``fstat`` rejects a squatted one."""
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise GitError(f"cannot open mirror lock {lock_path}: {exc}") from exc
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        os.close(fd)
        raise GitError(
            f"mirror lock {lock_path} is not a driver-owned regular file — refusing to lock it"
        )
    return fd


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


def _acquire_lock(lock_path: Path, timeout: float | None = None) -> int:
    """Acquire the per-repo flock; with a ``timeout``, poll non-blocking
    against a monotonic deadline and raise :class:`GitTimeout` (descriptor
    closed) on expiry — a stuck holder becomes a normal pre-session infra
    failure, never a wedged driver."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        fd = _open_lock(lock_path)
        try:
            if deadline is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if _lock_still_named(fd, lock_path):
                return fd
        except BlockingIOError:
            os.close(fd)
            if time.monotonic() >= deadline:
                raise GitTimeout(
                    f"timed out after {timeout:.0f}s waiting for mirror lock"
                    f" {lock_path} — a concurrent mirror operation is stuck or slow"
                ) from None
            time.sleep(_LOCK_RETRY_SECONDS)
            continue
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)  # the sweep unlinked this inode under us — retry on the fresh file


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
def mirror_lock(mirrors_dir: Path, url: str, *, timeout: float | None = None):
    """The per-repo mirror lock (#51): every mirror access — creation, the
    update fetch, and the reference clone reading objects — happens under
    it, so concurrent Runs on one node can neither race the update nor
    watch a repack delete a pack file mid-copy. The cache root is validated
    fail-closed (created 0700 first if missing) before any lock file is
    opened, so nothing below ever touches an untrusted tree."""
    root = ensure_mirrors_root(mirrors_dir)
    fd = _acquire_lock(_lock_path(root, url), timeout)
    try:
        yield
    finally:
        os.close(fd)  # closing the last descriptor releases the flock


def sanitize_mirror(mirror: Path, url: str) -> None:
    """Rewrite the mirror's git metadata to a known-good minimum.

    The mirror is persistent cache state, so its on-disk configuration is
    never provenance: before any authenticated operation the config is
    replaced wholesale — exact caller-supplied remote URL and mirror
    refspec, hooks pointed at an empty directory, detached auto-gc off (a
    repack must finish inside the lock window, not outside it) — and a
    foreign ``objects/info/alternates`` is dropped. A pre-created or
    tampered repo therefore cannot redirect the authenticated fetch, run
    hook code in the driver, or splice a foreign object store into
    checkouts. ``sanitize_checkout`` is this function's worktree sibling."""
    no_hooks = mirror / "theozolith-no-hooks"
    no_hooks.mkdir(exist_ok=True)
    alternates = mirror / "objects" / "info" / "alternates"
    alternates.unlink(missing_ok=True)
    config = "\n".join(
        [
            "[core]",
            "\trepositoryformatversion = 0",
            "\tfilemode = true",
            "\tbare = true",
            f"\thooksPath = {no_hooks}",
            "[gc]",
            "\tautoDetach = false",
            '[remote "origin"]',
            f"\turl = {url}",
            "\tfetch = +refs/*:refs/*",
            "\tmirror = true",
            "",
        ]
    )
    config_path = mirror / "config"
    config_path.unlink(missing_ok=True)  # never write through a pre-existing symlink
    config_path.write_text(config, encoding="utf-8")


def _bare_repo_shape(mirror: Path) -> bool:
    """Cheap structural probe run BEFORE any git subprocess touches the
    entry (git never runs inside a path that has not been validated): does
    this look like the skeleton of a bare repo? Corruption below this level
    surfaces as a GitError from the update or reference clone."""
    return (
        (mirror / "HEAD").is_file() and (mirror / "objects").is_dir() and (mirror / "refs").is_dir()
    )


def _mirror_parses(mirror: Path, timeout: float | None) -> bool:
    """Post-sanitize repo check — by this point the entry is a validated,
    driver-owned directory whose config we just wrote ourselves."""
    proc = _run_git(
        ["--git-dir", str(mirror), "rev-parse", "--is-bare-repository"], timeout=timeout
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _ensure_mirror_locked(
    mirrors_dir: Path, url: str, env: dict[str, str] | None, timeout: float | None = None
) -> Path:
    """Create-or-heal the bare mirror; caller holds the per-repo lock and
    has validated the cache root.

    Creation stages into a ``.tmp`` sibling and atomically renames into
    place, so a final-named mirror is never partial; a mirror that no longer
    looks like a bare repo is deleted and lazily re-created (corruption
    recovery is delete + re-clone, never repair-in-place). An existing
    mirror is adopted only after ``_entry_state`` proves it a real,
    driver-owned directory and ``sanitize_mirror`` has replaced its config —
    ``rev-parse`` alone is never treated as provenance. A timed-out creation
    clone removes its partial stage before re-raising."""
    mirror = mirror_path(mirrors_dir, url)
    staging = mirror.with_name(mirror.name + MIRROR_TMP_SUFFIX)
    if _entry_state(mirror, "mirror") == "dir":
        if _bare_repo_shape(mirror):
            sanitize_mirror(mirror, url)
            if _mirror_parses(mirror, timeout):
                return mirror
        shutil.rmtree(mirror)  # driver-owned, inside the validated root — safe to discard
    if _entry_state(staging, "mirror staging") == "dir":
        shutil.rmtree(staging)  # a dead predecessor's partial stage (we hold the lock)
    try:
        proc = _run_git(
            ["clone", "--quiet", "--mirror", url, str(staging)], env=env, timeout=timeout
        )
    except GitTimeout:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if proc.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise GitError(f"git clone --mirror failed: {proc.stderr.strip()}")
    sanitize_mirror(staging, url)
    staging.rename(mirror)
    return mirror


def _update_mirror_locked(
    mirror: Path, url: str, env: dict[str, str] | None, timeout: float | None = None
) -> None:
    """Refresh the mirror by fetching the CALLER-supplied URL and mirror
    refspec directly on argv — the authenticated operation never consults
    stored remote configuration (which ``sanitize_mirror`` just rewrote
    anyway; this is the second belt on the same trust boundary)."""
    try:
        git(
            ["fetch", "--quiet", "--prune", url, "+refs/*:refs/*"],
            mirror,
            env=env,
            timeout=timeout,
        )
    except GitTimeout:
        # A killed fetch can leave repo-internal lock/tmp debris that would
        # poison later fetches. The cache is non-authoritative: delete the
        # mirror so the next attempt re-clones clean.
        shutil.rmtree(mirror, ignore_errors=True)
        raise


def ensure_mirror(
    mirrors_dir: Path, url: str, *, env: dict[str, str] | None = None, timeout: float | None = None
) -> Path:
    """Lazily create (or heal) the repo's driver-owned bare mirror."""
    with mirror_lock(mirrors_dir, url, timeout=timeout):
        return _ensure_mirror_locked(mirrors_dir, url, env, timeout)


def update_mirror(
    mirrors_dir: Path, url: str, *, env: dict[str, str] | None = None, timeout: float | None = None
) -> Path:
    """Refresh the mirror (direct-URL fetch, mirror refspec) under the lock."""
    with mirror_lock(mirrors_dir, url, timeout=timeout):
        mirror = _ensure_mirror_locked(mirrors_dir, url, env, timeout)
        _update_mirror_locked(mirror, url, env, timeout)
        return mirror


def clone_with_mirror(
    url: str,
    mirrors_dir: Path,
    dest: Path,
    *,
    branch: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> None:
    """The Run checkout (#51): ensure + update the mirror, then reference-
    clone off it with ``--dissociate``, all under the per-repo lock. The
    result is byte-for-byte the disposable, self-contained checkout a full
    clone produced — only the download shrinks. ``branch`` selects the
    checkout base (a Chained-Base Run clones its blocker's branch,
    ADR-0053); the mirror update precedes the reference clone, so a
    freshly pushed blocker tip is always present. Any failure here — a
    trust validation refusal, a git failure, or a ``timeout`` expiry on the
    lock wait or any single git operation — is a pre-session infra failure
    (ADR-0016): the caller's Run fails and burns the normal retry; a
    half-context checkout is never handed to an agent."""
    with mirror_lock(mirrors_dir, url, timeout=timeout):
        mirror = _ensure_mirror_locked(mirrors_dir, url, env, timeout)
        _update_mirror_locked(mirror, url, env, timeout)
        clone(url, dest, branch=branch, reference=mirror, env=env, timeout=timeout)


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


def is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    """Is ``ancestor`` reachable from ``descendant``? False on any failure
    (unknown commit or ref included) — callers treat containment as a proof
    obligation, so an unverifiable ancestry is a plain no."""
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(cwd),
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def diff_stat(cwd: Path, base_ref: str) -> str:
    """Numstat of HEAD against the merge base with ``base_ref``."""
    return git(["diff", "--numstat", f"{base_ref}...HEAD"], cwd, check=False)
