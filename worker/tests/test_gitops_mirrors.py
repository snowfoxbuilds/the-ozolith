"""Node-local repo mirrors + reference-clone Run checkouts (#51).

The mechanism swap sits below the ADR-0013 invariant line: every checkout
stays disposable, token-free, and — via ``--dissociate`` — fully
self-contained the instant the clone returns. These tests pin the three
rulings from the issue (lock contention, corruption recovery, checkout
self-containedness), the boot sweep of partial mirrors and stale locks, and
the two integration facts: the mirror appears in no container mount spec,
and mirror failure is a pre-session infra failure burning the normal retry.

The PR #54 amendment adds two pinned surfaces: the trust boundary (an
untrusted or symlinked cache root/entry fails closed before any
authenticated git subprocess; persistent mirror config is rewritten, never
believed) and the operation budget (flock waits and every mirror git op are
deadline-bounded; expiry cleans partial state, releases the lock, and rides
the same pre-session infra lane).

#56 pins four more: maintenance-free driver git (every subprocess through
``_run_git`` carries ``-c maintenance.auto=false -c gc.auto=0`` before the
subcommand; the reference clone alone adds ``-c core.commitGraph=false``),
graph-immune checkouts (a commit-graph planted in the mirror — git 2.54's
auto-maintenance shape — is stripped by ``sanitize_mirror`` and never
parsed through), explicit pack health (the update repacks under the lock
past ``MIRROR_REPACK_PACK_LIMIT``, never below it, never via ``gc``), and
the fallback lane (a failed mirror-backed checkout falls back once to a
full network clone and the Run ships — the mirror is an optimization,
never load-bearing; only a failed fallback rides the ADR-0016 infra lane).
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import Harness, write_proposal
from theozolith_worker import gitops, jobdir
from theozolith_worker.bootstrap.vocabulary import FAILED, NEEDS_HUMAN
from theozolith_worker.evidence import EVIDENCE_BRANCH
from theozolith_worker.gitops import GitError
from theozolith_worker.sweep import sweep_mirrors


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _subcommand(argv: list[str]) -> list[str]:
    """``argv`` minus ``git`` and every leading global ``-c key=val`` pair —
    the injected maintenance-off flags (#56) among them — so argv matching
    targets the subcommand no matter how many globals precede it."""
    i = 1
    while i < len(argv) and argv[i] == "-c":
        i += 2
    return argv[i:]


def _bulk_commits(git_dir: Path, base: str, branch: str, count: int) -> None:
    """Grow ``branch`` in a bare repo by ``count`` commits on top of
    ``base`` (the test_contexttree shape): hash-object/mktree/commit-tree +
    update-ref, no clone, no transport, each commit a distinct one-file
    tree."""
    subprocess.run(
        [
            "bash",
            "-c",
            f'set -e; export GIT_DIR="{git_dir}"'
            " GIT_AUTHOR_NAME=bulk GIT_AUTHOR_EMAIL=b@x"
            " GIT_COMMITTER_NAME=bulk GIT_COMMITTER_EMAIL=b@x;"
            f" parent={base};"
            f" for i in $(seq 1 {count}); do"
            '   blob=$(printf "content %d\\n" "$i" | git hash-object -w --stdin);'
            '   tree=$(printf "100644 blob %s\\tfile-%d\\n" "$blob" "$i" | git mktree);'
            '   parent=$(git commit-tree "$tree" -p "$parent" -m "bulk commit $i");'
            " done;"
            f' git update-ref "refs/heads/{branch}" "$parent"',
        ],
        check=True,
    )


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A seeded bare remote, the shape every Run clones. Lives in its own
    subtree: the Harness fixture builds its own remote + seed in tmp_path."""
    base = tmp_path / "mirror-remote"
    base.mkdir()
    bare = base / "origin.git"
    _git(["init", "--bare", "--quiet", "--initial-branch", "main", str(bare)], base)
    seed = base / "seed"
    seed.mkdir()
    _git(["init", "--quiet", "--initial-branch", "main"], seed)
    (seed / "README.md").write_text("# seed\n")
    _git(["add", "--all"], seed)
    identity = ["-c", "user.name=s", "-c", "user.email=s@example.com"]
    _git([*identity, "commit", "--quiet", "-m", "seed"], seed)
    _git(["push", "--quiet", str(bare), "main:main"], seed)
    return bare


def url_of(remote: Path) -> str:
    return f"file://{remote}"


# -- naming ---------------------------------------------------------------------


def test_mirror_names_are_stable_distinct_and_suffix_safe():
    a = gitops.mirror_name("https://github.com/owner/repo.git")
    assert a == gitops.mirror_name("https://github.com/owner/repo.git")  # stable
    # Same slug, different URL: the hash keeps the mirrors apart.
    b = gitops.mirror_name("http://github.com/owner/repo.git")
    assert a != b
    for name in (a, b, gitops.mirror_name("file:///x/../.git"), gitops.mirror_name("://")):
        assert not name.startswith(".")
        assert "/" not in name
        assert not name.endswith(gitops.MIRROR_TMP_SUFFIX)
        assert not name.endswith(gitops.MIRROR_LOCK_SUFFIX)


# -- the three ruled behaviors ----------------------------------------------------


def test_checkout_is_self_contained_after_the_mirror_is_deleted(tmp_path: Path, remote: Path):
    """The --dissociate ruling: object transfer is local-disk at clone time
    and the worktree has no dependency on the mirror afterwards."""
    mirrors = tmp_path / "mirrors"
    dest = tmp_path / "checkout"
    gitops.clone_with_mirror(url_of(remote), mirrors, dest)
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()

    subprocess.run(["rm", "-rf", str(mirrors)], check=True)

    fsck = subprocess.run(
        ["git", "fsck", "--no-progress", "--strict"], cwd=dest, capture_output=True
    )
    assert fsck.returncode == 0
    assert _git(["log", "--oneline", "-1"], dest)
    gitops.checkout_branch(dest, "ozolith/issue-9", create=True)
    (dest / "change.txt").write_text("x\n")
    assert gitops.commit_all(dest, "change", "w", "w@example.com")


def test_corrupt_mirror_is_deleted_and_lazily_recreated(tmp_path: Path, remote: Path):
    mirrors = tmp_path / "mirrors"
    url = url_of(remote)
    mirror = gitops.ensure_mirror(mirrors, url)
    # Gut the mirror: no HEAD, no objects — no longer parses as a bare repo.
    subprocess.run(["rm", "-rf", str(mirror / "objects"), str(mirror / "HEAD")], check=True)

    healed = gitops.ensure_mirror(mirrors, url)

    assert healed == mirror
    assert _git(["--git-dir", str(healed), "rev-parse", "refs/heads/main"], tmp_path)
    dest = tmp_path / "checkout"
    gitops.clone_with_mirror(url, mirrors, dest)  # and checkouts work again
    assert (dest / "README.md").exists()


def test_concurrent_updates_on_one_repo_serialize_on_the_lock(
    tmp_path: Path, remote: Path, monkeypatch
):
    """Two claims, one repo (the issue's contention test): the per-repo file
    lock admits one update fetch at a time across concurrent claimants."""
    mirrors = tmp_path / "mirrors"
    url = url_of(remote)
    gitops.ensure_mirror(mirrors, url)

    real_git = gitops.git
    gauge = threading.Lock()
    active = 0
    peak = 0

    def slow_git(args, cwd, **kwargs):
        nonlocal active, peak
        if args[:3] == ["fetch", "--quiet", "--prune"]:
            with gauge:
                active += 1
                peak = max(peak, active)
            time.sleep(0.2)
            try:
                return real_git(args, cwd, **kwargs)
            finally:
                with gauge:
                    active -= 1
        return real_git(args, cwd, **kwargs)

    monkeypatch.setattr(gitops, "git", slow_git)
    threads = [threading.Thread(target=gitops.update_mirror, args=(mirrors, url)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1  # never two updates inside the lock at once


def test_update_picks_up_new_remote_commits(tmp_path: Path, remote: Path):
    mirrors = tmp_path / "mirrors"
    url = url_of(remote)
    gitops.ensure_mirror(mirrors, url)

    pusher = tmp_path / "pusher"
    gitops.clone(url, pusher)
    (pusher / "new.txt").write_text("new\n")
    _git(["add", "--all"], pusher)
    _git(["-c", "user.name=p", "-c", "user.email=p@e", "commit", "--quiet", "-m", "new"], pusher)
    _git(["push", "--quiet", "origin", "main"], pusher)

    dest = tmp_path / "checkout"
    gitops.clone_with_mirror(url, mirrors, dest)
    assert (dest / "new.txt").exists()


def test_partial_staging_left_by_a_dead_creator_is_replaced(tmp_path: Path, remote: Path):
    mirrors = tmp_path / "mirrors"
    url = url_of(remote)
    staging = gitops.mirror_path(mirrors, url).with_name(
        gitops.mirror_name(url) + gitops.MIRROR_TMP_SUFFIX
    )
    staging.mkdir(parents=True)
    (staging / "half-written").write_text("junk")

    mirror = gitops.ensure_mirror(mirrors, url)

    assert not staging.exists()
    assert _git(["--git-dir", str(mirror), "rev-parse", "refs/heads/main"], tmp_path)


# -- the trust boundary (PR #54 amendment) ------------------------------------------


def _trusted_root(tmp_path: Path, name: str = "mirrors") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return root


def _forbid_subprocess(monkeypatch):
    def no_subprocess(*args, **kwargs):
        raise AssertionError("a git subprocess ran despite an untrusted mirror root")

    monkeypatch.setattr(gitops, "_run_git", no_subprocess)


def test_world_writable_root_fails_before_any_git_subprocess(
    tmp_path: Path, remote: Path, monkeypatch
):
    mirrors = _trusted_root(tmp_path)
    mirrors.chmod(0o777)
    _forbid_subprocess(monkeypatch)

    with pytest.raises(GitError, match="group/world writable"):
        gitops.clone_with_mirror(
            url_of(remote), mirrors, tmp_path / "checkout", env=gitops.auth_env("sekrit")
        )


def test_wrong_owner_root_fails_before_any_git_subprocess(
    tmp_path: Path, remote: Path, monkeypatch
):
    """Simulated by shifting the driver's effective UID: every real directory
    in the fixture now has the 'wrong' owner, and validation must refuse it
    before any subprocess — the check order (root before lock before git) is
    the pinned behavior."""
    mirrors = _trusted_root(tmp_path)
    _forbid_subprocess(monkeypatch)
    real_euid = os.geteuid()
    monkeypatch.setattr(gitops.os, "geteuid", lambda: real_euid + 1)

    with pytest.raises(GitError, match="owned by uid"):
        gitops.clone_with_mirror(
            url_of(remote), mirrors, tmp_path / "checkout", env=gitops.auth_env("sekrit")
        )


def test_symlinked_root_fails_closed(tmp_path: Path, remote: Path, monkeypatch):
    real_root = tmp_path / "real-root"
    real_root.mkdir(mode=0o700)
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real_root)
    _forbid_subprocess(monkeypatch)

    with pytest.raises(GitError, match="symlink"):
        gitops.ensure_mirror(link_root, url_of(remote))
    assert real_root.is_dir()  # the target is never deleted


def test_symlinked_lock_entry_fails_closed(tmp_path: Path, remote: Path):
    root = _trusted_root(tmp_path)
    url = url_of(remote)
    elsewhere = tmp_path / "elsewhere-lock"
    elsewhere.write_text("not ours\n")
    (root / (gitops.mirror_name(url) + gitops.MIRROR_LOCK_SUFFIX)).symlink_to(elsewhere)

    with pytest.raises(GitError, match="mirror lock"):
        gitops.ensure_mirror(root, url)
    assert elsewhere.read_text() == "not ours\n"


def test_symlinked_staging_entry_fails_closed(tmp_path: Path, remote: Path):
    root = _trusted_root(tmp_path)
    url = url_of(remote)
    elsewhere = tmp_path / "elsewhere-staging"
    elsewhere.mkdir()
    (elsewhere / "marker").write_text("keep\n")
    (root / (gitops.mirror_name(url) + gitops.MIRROR_TMP_SUFFIX)).symlink_to(elsewhere)

    with pytest.raises(GitError, match="symlink"):
        gitops.ensure_mirror(root, url)
    assert (elsewhere / "marker").read_text() == "keep\n"  # never followed, never deleted


def test_symlinked_mirror_entry_fails_closed(tmp_path: Path, remote: Path):
    root = _trusted_root(tmp_path)
    url = url_of(remote)
    elsewhere = tmp_path / "elsewhere-mirror"
    elsewhere.mkdir()
    (elsewhere / "marker").write_text("keep\n")
    gitops.mirror_path(root, url).symlink_to(elsewhere)

    with pytest.raises(GitError, match="symlink"):
        gitops.clone_with_mirror(url, root, tmp_path / "checkout")
    assert (elsewhere / "marker").read_text() == "keep\n"


def test_precreated_hostile_mirror_never_sees_credentials(
    tmp_path: Path, remote: Path, monkeypatch
):
    """A bare repo squatting the mirror path with a redirected remote, a
    credential helper, a live hook, and an alternates file: persistent config
    is not provenance. The config is rewritten to the caller's URL (hooks
    disarmed, alternates dropped) before any authenticated operation, and no
    credentialed git invocation ever carries the hostile URL."""
    root = _trusted_root(tmp_path)
    url = url_of(remote)
    hostile_url = "https://evil.invalid/exfil.git"
    hook_canary = tmp_path / "hook-ran"
    mirror = gitops.mirror_path(root, url)
    _git(["init", "--bare", "--quiet", str(mirror)], tmp_path)
    for key, value in (
        ("remote.origin.url", hostile_url),
        ("remote.origin.fetch", "+refs/*:refs/*"),
        ("remote.origin.mirror", "true"),
        ("credential.helper", f"!f() {{ touch {hook_canary}; }}; f"),
    ):
        _git(["--git-dir", str(mirror), "config", key, value], tmp_path)
    hook = mirror / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {hook_canary}\n")
    hook.chmod(0o755)
    (mirror / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (mirror / "objects" / "info" / "alternates").write_text(str(tmp_path / "foreign") + "\n")

    credentialed: list[list[str]] = []
    real_run = gitops._run_git

    def recording_run(args, cwd=None, *, env=None, timeout=None):
        if env and gitops.TOKEN_ENV in env:
            credentialed.append(list(args))
            assert hostile_url not in " ".join(args)
        return real_run(args, cwd=cwd, env=env, timeout=timeout)

    monkeypatch.setattr(gitops, "_run_git", recording_run)

    dest = tmp_path / "checkout"
    gitops.clone_with_mirror(url, root, dest, env=gitops.auth_env("sekrit"))

    assert credentialed  # the authenticated update fetch + reference clone did run
    config_text = (mirror / "config").read_text()
    assert hostile_url not in config_text
    assert url in config_text
    assert "theozolith-no-hooks" in config_text  # hooks disarmed
    assert not (mirror / "objects" / "info" / "alternates").exists()
    assert not hook_canary.exists()
    assert (dest / "README.md").exists()  # and the checkout is the real repo


# -- timeouts (PR #54 amendment) ----------------------------------------------------


def _fds_open_on(path: Path) -> int:
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}") == str(path):
                count += 1
        except OSError:
            continue
    return count


def test_lock_wait_timeout_raises_giterror_and_releases_descriptors(tmp_path: Path, remote: Path):
    root = _trusted_root(tmp_path)
    url = url_of(remote)
    lock_path = root / (gitops.mirror_name(url) + gitops.MIRROR_LOCK_SUFFIX)
    holder = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        before = _fds_open_on(lock_path)
        with pytest.raises(GitError, match=r"timed out.*mirror lock"):
            gitops.ensure_mirror(root, url, timeout=0.3)
        assert _fds_open_on(lock_path) == before  # every waiter descriptor closed
    finally:
        os.close(holder)
    # Once the holder is gone the same call recovers.
    assert gitops.ensure_mirror(root, url, timeout=30).is_dir()


def test_timed_out_mirror_clone_cleans_staging_and_recovers(
    tmp_path: Path, remote: Path, monkeypatch
):
    root = _trusted_root(tmp_path)
    url = url_of(remote)
    tripped: list[list[str]] = []
    real_run = subprocess.run

    def killing_run(argv, **kwargs):
        if _subcommand(argv)[:3] == ["clone", "--quiet", "--mirror"] and not tripped:
            tripped.append(argv)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gitops.subprocess, "run", killing_run)
    with pytest.raises(GitError, match="timed out"):
        gitops.ensure_mirror(root, url, timeout=5)

    assert list(root.glob(f"*{gitops.MIRROR_TMP_SUFFIX}")) == []  # partial stage cleaned
    # The lock was released and a later attempt recovers in full.
    assert gitops.ensure_mirror(root, url, timeout=30).is_dir()


def test_timed_out_update_deletes_the_mirror_and_recovers(
    tmp_path: Path, remote: Path, monkeypatch
):
    """A killed fetch can leave repo-internal debris that poisons later
    fetches; the cache is non-authoritative, so the timed-out mirror is
    deleted for a clean re-clone at the next attempt."""
    root = _trusted_root(tmp_path)
    url = url_of(remote)
    gitops.ensure_mirror(root, url)
    tripped: list[list[str]] = []
    real_run = subprocess.run

    def killing_run(argv, **kwargs):
        if _subcommand(argv)[:3] == ["fetch", "--quiet", "--prune"] and not tripped:
            tripped.append(argv)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gitops.subprocess, "run", killing_run)
    with pytest.raises(GitError, match="timed out"):
        gitops.update_mirror(root, url, timeout=5)

    assert not gitops.mirror_path(root, url).exists()
    dest = tmp_path / "checkout"
    gitops.clone_with_mirror(url, root, dest, timeout=30)  # lock free; full recovery
    assert (dest / "README.md").exists()


def test_timed_out_reference_clone_cleans_the_partial_checkout(
    tmp_path: Path, remote: Path, monkeypatch
):
    root = _trusted_root(tmp_path)
    url = url_of(remote)
    dest = tmp_path / "checkout"
    tripped: list[list[str]] = []
    real_run = subprocess.run

    def killing_run(argv, **kwargs):
        if _subcommand(argv)[:1] == ["clone"] and "--reference" in argv and not tripped:
            tripped.append(argv)
            # What a kill leaves behind: git cannot clean its own destination.
            (dest / ".git").mkdir(parents=True)
            (dest / ".git" / "partial").write_text("x")
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gitops.subprocess, "run", killing_run)
    with pytest.raises(GitError, match="timed out"):
        gitops.clone_with_mirror(url, root, dest, timeout=5)

    assert not dest.exists()  # the partial checkout is cleaned
    assert gitops.mirror_path(root, url).is_dir()  # the mirror only READ — kept
    gitops.clone_with_mirror(url, root, dest, timeout=30)
    assert (dest / "README.md").exists()


def test_stuck_lock_holder_is_a_pre_session_infra_failure(harness: Harness, monkeypatch):
    """The end-to-end timeout ruling, #56-adjusted: a stuck holder times
    out the mirror path and the Run falls back to a full clone — only when
    that TOO fails (broken here) does it ride the existing ADR-0016 lane:
    both budgeted Runs fail `infra` before any Run container launches, the
    fallback events name the timeout, and the claim escalates."""
    number = harness.file_issue("Mirror stuck", CRITERIA_BODY)
    harness.worker_config = dataclasses.replace(harness.worker_config, git_timeout_seconds=0.4)
    root = Path(harness.worker_config.mirrors_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = root / (
        gitops.mirror_name(harness.worker_config.clone_url) + gitops.MIRROR_LOCK_SUFFIX
    )
    real_clone = gitops.clone

    def broken_clone(url, dest, *, branch=None, **kwargs):
        # Break the Run-checkout fallback; the evidence channel (its
        # checkout clones the evidence branch) stays intact.
        if branch == EVIDENCE_BRANCH:
            return real_clone(url, dest, branch=branch, **kwargs)
        raise GitError("git clone failed: network down")

    monkeypatch.setattr(gitops, "clone", broken_clone)
    holder = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        assert harness.worker_once() == 2  # the original Run and the one local retry
    finally:
        os.close(holder)

    run_specs = [s for s in harness.record.specs if s.name.startswith("ozolith-run-")]
    assert run_specs == []  # pre-session: no Run container ever launched
    labels = harness.fake.labels_of(number)
    assert FAILED in labels and NEEDS_HUMAN in labels
    run_reports = [
        json.loads(harness.evidence_file(path))
        for path in harness.evidence_paths()
        if path.endswith("/run.json")
    ]
    assert len(run_reports) == 2
    assert {report["failure_class"] for report in run_reports} == {"infra"}
    # The lock-wait timeout stays visible: it rides the advisory
    # mirror-fallback events, while the Run's reason names the fallback
    # failure that actually sank it.
    fallbacks = [e for e in harness.sink.events if e.get("error_class") == "mirror-fallback"]
    assert len(fallbacks) == 2
    assert all("timed out" in e["message"] for e in fallbacks)
    assert all("network down" in report["reason"] for report in run_reports)


# -- maintenance-free driver git (#56) ----------------------------------------------


def test_reference_clone_survives_a_mirror_commit_graph(tmp_path: Path, remote: Path):
    """The git 2.54 regression: auto-maintenance used to write a commit-graph
    into the mirror once a fetch brought >= 100 ungraphed commits, and the
    reference clone then parsed the branch tip through the borrowed graph —
    --dissociate closed it mid-clone and checkout died 'unable to parse
    commit' with an fsck-clean object store and an empty worktree. Driver
    git never writes a graph anymore, so this plants one exactly where 2.54
    did and proves the checkout path strips and ignores it — deterministic
    on every git version (on unfixed 2.54 it is the live bug)."""
    mirrors = tmp_path / "mirrors"
    url = url_of(remote)
    base = _git(["--git-dir", str(remote), "rev-parse", "refs/heads/main"], tmp_path)
    _bulk_commits(remote, base, "graphed", 120)  # past the 100-commit graph boundary
    mirror = gitops.ensure_mirror(mirrors, url)
    _git(["--git-dir", str(mirror), "commit-graph", "write", "--reachable"], tmp_path)
    info = mirror / "objects" / "info"
    assert (info / "commit-graph").is_file() or (info / "commit-graphs").is_dir()

    dest = tmp_path / "checkout"
    gitops.clone_with_mirror(url, mirrors, dest)

    fsck = subprocess.run(
        ["git", "fsck", "--no-progress", "--strict"], cwd=dest, capture_output=True
    )
    assert fsck.returncode == 0
    assert (dest / "README.md").exists()  # the worktree is complete, not empty
    assert _git(["log", "--oneline", "-1"], dest)
    # And the mirror is graph-free again: sanitize_mirror stripped it.
    assert not (info / "commit-graph").exists()
    assert not (info / "commit-graphs").exists()


def test_update_repacks_only_past_the_pack_limit(tmp_path: Path, remote: Path, monkeypatch):
    """With auto-maintenance off, pack health is owned explicitly: the
    update repacks under the lock once objects/pack exceeds the threshold —
    never below it, always ``repack`` (gc would write the commit-graph
    back) — and checkouts keep working off the collapsed pack."""
    root = tmp_path / "mirrors"
    url = url_of(remote)
    mirror = gitops.ensure_mirror(root, url)
    base = _git(["--git-dir", str(remote), "rev-parse", "refs/heads/main"], tmp_path)
    _bulk_commits(remote, base, "main", 150)

    recorded: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(argv, **kwargs):
        recorded.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gitops.subprocess, "run", recording_run)
    # The fetch stores its news as a second pack (fetch.unpackLimit=1,
    # composed through GIT_CONFIG_* exactly like auth_env would be) — still
    # under the threshold: no repack may run.
    overlay = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "fetch.unpackLimit",
        "GIT_CONFIG_VALUE_0": "1",
    }
    gitops.update_mirror(root, url, env=overlay)
    assert len(list((mirror / "objects" / "pack").glob("*.pack"))) == 2
    assert not any("repack" in argv for argv in recorded)

    monkeypatch.setattr(gitops, "MIRROR_REPACK_PACK_LIMIT", 1)
    recorded.clear()
    gitops.update_mirror(root, url)
    assert any("repack" in argv for argv in recorded)
    assert not any("gc" in argv for argv in recorded)  # repack, never gc
    assert len(list((mirror / "objects" / "pack").glob("*.pack"))) == 1
    dest = tmp_path / "checkout"
    gitops.clone_with_mirror(url, root, dest)
    assert (dest / "file-150").exists()


def test_every_git_subprocess_is_maintenance_free(tmp_path: Path, remote: Path, monkeypatch):
    """The #56 seam is total: every subprocess through gitops — mirror
    creation clone, update fetch, reference clone, plain git() calls, and
    the boolean probes that used to bypass _run_git — carries the
    maintenance-off pairs immediately after ``git``, ahead of any
    subcommand; the reference clone (and only it) is additionally
    graph-blind."""
    recorded: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(argv, **kwargs):
        recorded.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(gitops.subprocess, "run", recording_run)
    root = tmp_path / "mirrors"
    url = url_of(remote)
    dest = tmp_path / "checkout"
    gitops.clone_with_mirror(url, root, dest)
    gitops.git(["status", "--porcelain"], dest)
    assert gitops.ref_exists(dest, "HEAD")
    assert gitops.commit_exists(dest, gitops.head_sha(dest))
    assert gitops.is_ancestor(dest, "HEAD", "HEAD")

    assert any(_subcommand(argv)[:3] == ["clone", "--quiet", "--mirror"] for argv in recorded)
    assert any(_subcommand(argv)[:3] == ["fetch", "--quiet", "--prune"] for argv in recorded)
    assert any(_subcommand(argv)[:1] == ["rev-parse"] for argv in recorded)
    assert any(_subcommand(argv)[:1] == ["cat-file"] for argv in recorded)
    assert any(_subcommand(argv)[:1] == ["merge-base"] for argv in recorded)
    for argv in recorded:
        assert argv[0] == "git"
        assert argv[1:5] == ["-c", "maintenance.auto=false", "-c", "gc.auto=0"]
    (ref_clone,) = [argv for argv in recorded if "--reference" in argv]
    assert "core.commitGraph=false" in ref_clone
    assert ref_clone.index("core.commitGraph=false") < ref_clone.index("clone")
    for argv in recorded:
        if "--reference" not in argv:
            assert "core.commitGraph=false" not in argv


# -- boot sweep -------------------------------------------------------------------


def test_sweep_removes_partial_mirrors_and_stale_locks(harness: Harness, remote: Path):
    root = Path(harness.worker_config.mirrors_dir)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)  # explicit: the sweep validates the root (umask-proof)
    # A live mirror keeps both its directory and its lock file.
    live = gitops.ensure_mirror(root, url_of(remote))
    live_lock = root / (live.name + gitops.MIRROR_LOCK_SUFFIX)
    # A dead creator's debris: staging dir + lock, no mirror.
    (root / f"dead-abc123{gitops.MIRROR_TMP_SUFFIX}").mkdir()
    (root / f"dead-abc123{gitops.MIRROR_LOCK_SUFFIX}").touch()
    # A stale lock alone (creator died before staging even appeared).
    (root / f"gone-def456{gitops.MIRROR_LOCK_SUFFIX}").touch()

    removed, skipped = sweep_mirrors(harness.worker_config, log=harness.logs.append)

    assert (removed, skipped) == (3, 0)
    assert live.is_dir() and live_lock.exists()  # live infrastructure untouched
    assert not (root / f"dead-abc123{gitops.MIRROR_TMP_SUFFIX}").exists()
    assert not (root / f"dead-abc123{gitops.MIRROR_LOCK_SUFFIX}").exists()
    assert not (root / f"gone-def456{gitops.MIRROR_LOCK_SUFFIX}").exists()


def test_sweep_skips_debris_whose_lock_is_held_by_a_live_process(harness: Harness):
    """A held lock means a live driver mid-creation somewhere on the node:
    the sweep must skip, never wait, never delete under it."""
    root = Path(harness.worker_config.mirrors_dir)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)  # explicit: the sweep validates the root (umask-proof)
    staging = root / f"busy-0f0f0f{gitops.MIRROR_TMP_SUFFIX}"
    staging.mkdir()
    lock = root / f"busy-0f0f0f{gitops.MIRROR_LOCK_SUFFIX}"
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        removed, skipped = sweep_mirrors(harness.worker_config, log=harness.logs.append)
    finally:
        os.close(fd)

    # The .tmp is skipped (lock held); its lock is not even a stale-lock
    # candidate while the staging dir stands behind it.
    assert (removed, skipped) == (0, 1)
    assert staging.exists() and lock.exists()


def test_sweep_of_missing_mirror_root_is_a_no_op(harness: Harness):
    assert sweep_mirrors(harness.worker_config, log=harness.logs.append) == (0, 0)


def test_sweep_declines_an_untrusted_root_without_touching_it(harness: Harness):
    """Fail-closed but boot-safe (#54 amendment): the sweep never walks an
    untrusted root — it logs and leaves everything, including debris, alone;
    the Run path is where the loud fail-closed refusal lives."""
    root = Path(harness.worker_config.mirrors_dir)
    root.mkdir(parents=True, exist_ok=True)
    debris = root / f"dead-abc123{gitops.MIRROR_TMP_SUFFIX}"
    debris.mkdir()
    root.chmod(0o777)
    try:
        assert sweep_mirrors(harness.worker_config, log=harness.logs.append) == (0, 0)
    finally:
        root.chmod(0o755)
    assert debris.exists()
    assert any("untrusted mirror root" in line for line in harness.logs)


def test_sweep_leaves_symlinked_staging_debris_in_place(harness: Harness, tmp_path: Path):
    root = Path(harness.worker_config.mirrors_dir)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    elsewhere = tmp_path / "sweep-elsewhere"
    elsewhere.mkdir()
    (elsewhere / "marker").write_text("keep\n")
    (root / f"evil-abc123{gitops.MIRROR_TMP_SUFFIX}").symlink_to(elsewhere)

    removed, skipped = sweep_mirrors(harness.worker_config, log=harness.logs.append)

    assert (removed, skipped) == (0, 1)
    assert (elsewhere / "marker").read_text() == "keep\n"  # never followed, never deleted


# -- integration with the Run lane --------------------------------------------------


CRITERIA_BODY = "Do the thing.\n\n## Acceptance criteria\n\n- [ ] done\n"


def test_run_checkout_uses_the_mirror_and_mounts_none_of_it(harness: Harness):
    harness.file_issue("Mirror-backed Run", CRITERIA_BODY)

    def agent(prompt: str, cwd: Path) -> None:
        (cwd / "change.txt").write_text("x\n")
        write_proposal(cwd)

    harness.worker_behaviors.append(agent)
    assert harness.worker_once() == 1

    (pr_number,) = harness.fake.open_pr_numbers()
    assert pr_number
    # The mirror exists, driver-owned, beside its lock — and nothing of it
    # appears in the container mount spec (acceptance criterion #51).
    root = Path(harness.worker_config.mirrors_dir)
    mirrors = [p for p in root.iterdir() if p.is_dir()]
    assert len(mirrors) == 1
    spec = harness.record.specs[0]
    mounted = json.dumps(list(spec.mounts) + [list(v) for v in spec.volumes])
    assert str(root) not in mounted
    # And the checkout the container saw was already dissociated.
    job_mount = spec.mounts[0][0]
    assert not (
        Path(job_mount) / jobdir.CHECKOUT_DIR / ".git" / "objects" / "info" / "alternates"
    ).exists()


def test_second_run_reuses_the_mirror_without_a_second_full_download(harness: Harness, monkeypatch):
    numbers = [
        harness.file_issue("First", CRITERIA_BODY),
        harness.file_issue("Second", CRITERIA_BODY),
    ]
    assert numbers

    def agent(prompt: str, cwd: Path) -> None:
        (cwd / "change.txt").write_text("x\n")
        write_proposal(cwd)

    harness.worker_behaviors += [agent, agent]
    assert harness.worker_once() == 1

    creations = []
    real_ensure = gitops._ensure_mirror_locked

    def counting_ensure(mirrors_dir, url, env, timeout=None):
        mirror = gitops.mirror_path(mirrors_dir, url)
        creations.append(not mirror.is_dir())
        return real_ensure(mirrors_dir, url, env, timeout)

    monkeypatch.setattr(gitops, "_ensure_mirror_locked", counting_ensure)
    assert harness.worker_once() == 1
    assert creations == [False]  # warm: validated and reused, not re-cloned


def test_mirror_failure_falls_back_to_a_full_clone_and_the_run_ships(harness: Harness, monkeypatch):
    """The #56 fallback lane: the mirror is an optimization, never
    load-bearing for Run success. A failed mirror-backed checkout falls
    back exactly once to a full network clone and the Run ships normally —
    one advisory mirror-fallback error event on the telemetry channel, the
    mirror left in place for the next Run's _ensure_mirror_locked to
    heal."""
    number = harness.file_issue("Mirror flaky", CRITERIA_BODY)
    root = Path(harness.worker_config.mirrors_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    mirror = gitops.ensure_mirror(root, harness.worker_config.clone_url)

    def broken_mirror(url, mirrors_dir, dest, *, branch=None, env=None, timeout=None):
        raise GitError("git fetch --quiet --prune failed: remote hung up")

    monkeypatch.setattr(gitops, "clone_with_mirror", broken_mirror)

    def agent(prompt: str, cwd: Path) -> None:
        (cwd / "change.txt").write_text("x\n")
        write_proposal(cwd)

    harness.worker_behaviors.append(agent)
    assert harness.worker_once() == 1  # no retry burned: the fallback shipped

    (pr_number,) = harness.fake.open_pr_numbers()
    assert pr_number
    fallbacks = [e for e in harness.sink.events if e.get("error_class") == "mirror-fallback"]
    assert len(fallbacks) == 1  # once, not a loop
    assert fallbacks[0]["type"] == "theozolith.error"
    assert f"issue #{number}" in fallbacks[0]["message"]
    assert "remote hung up" in fallbacks[0]["message"]
    assert mirror.is_dir()  # left in place, never deleted by the fallback


def test_mirror_failure_is_a_pre_session_infra_failure(harness: Harness, monkeypatch):
    """ADR-0016 lane, no new class — now behind the #56 fallback, so BOTH
    lanes break here: the mirror path and the full-clone fallback. Both
    budgeted Runs fail 'infra' before any container launches, evidence
    carries the class, and the claim escalates to failed + needs_human."""
    number = harness.file_issue("Mirror down", CRITERIA_BODY)

    def broken_update(mirror, url, env, timeout=None):
        raise GitError("git fetch --quiet --prune failed: remote hung up")

    real_clone = gitops.clone

    def broken_clone(url, dest, *, branch=None, **kwargs):
        # Break the Run-checkout fallback; the evidence channel (its
        # checkout clones the evidence branch) stays intact — the lane
        # under test must still push its bundles.
        if branch == EVIDENCE_BRANCH:
            return real_clone(url, dest, branch=branch, **kwargs)
        raise GitError("git clone failed: network down")

    monkeypatch.setattr(gitops, "_update_mirror_locked", broken_update)
    monkeypatch.setattr(gitops, "clone", broken_clone)
    assert harness.worker_once() == 2  # the original Run and the one local retry

    # Pre-session: no RUN container ever launched (the boot-time identity
    # dry-run container, ADR-0045, is the only spec on record).
    run_specs = [s for s in harness.record.specs if s.name.startswith("ozolith-run-")]
    assert run_specs == []
    labels = harness.fake.labels_of(number)
    assert FAILED in labels and NEEDS_HUMAN in labels
    run_reports = [
        json.loads(harness.evidence_file(path))
        for path in harness.evidence_paths()
        if path.endswith("/run.json")
    ]
    assert len(run_reports) == 2  # the original and the one local retry
    assert {report["failure_class"] for report in run_reports} == {"infra"}
    assert all(report["phase"] == "failed" for report in run_reports)
    # The fallback ran (and was advertised) before each Run failed — once
    # per Run, never a loop.
    fallbacks = [e for e in harness.sink.events if e.get("error_class") == "mirror-fallback"]
    assert len(fallbacks) == 2
    # Every bundle names the node's git: the #56 evidence field.
    assert all(report["git_version"].startswith("git version") for report in run_reports)
