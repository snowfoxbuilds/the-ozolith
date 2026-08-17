"""The boot-time evidence sweep (ADR-0016).

A driver that died mid-Run leaves its job directory behind — and that
directory may hold the only copy of the Run's forensics (the control
database is a cache; the evidence branch is the sole durable audit trail).
At startup, and again on idle poll cycles while any orphan remains, the
driver (the PAT holder — the daemon never receives a credential) pushes each
orphaned job directory to the evidence branch under the original run_id
path with a ``swept: true`` marker and a sweep timestamp, so post-mortem-
recovered evidence is distinguishable from live-pushed evidence.

Ownership: a driver sweeps only its OWN directories — run dirs carrying its
worker id in the run_id, plus (for the Reviewer role) review workspaces.
Two drivers sharing a jobs dir (the default deploy shape) must never sweep
each other's live work.

A job directory is deleted only after the push is confirmed on the remote.
On push failure it is moved aside to a ``<jobs-dir>-pending`` sibling (so
the Node Daemon's job-dir in-flight signal for queue-behind never sees a
dead dir as a live Run), logged, and retried later. The two-phase zombie
janitor waits for exactly these bundles before it escalates a silent claim
(ADR-0016: evidence first, never escalate without forensics).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from theozolith_worker import evidence, gitops, jobdir
from theozolith_worker.config import DriverConfig


def _log(message: str) -> None:
    print(message, flush=True)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def pending_dir(config: DriverConfig) -> Path:
    """Failed-push parking, outside the jobs dir so queue-behind's
    in-flight signal stays clean."""
    jobs = Path(config.jobs_dir)
    return jobs.with_name(jobs.name + "-pending")


def park_job_dir(config: DriverConfig, job: Path, *, target_name: str | None = None) -> Path:
    """Move a retained job dir into the parking sibling (same filesystem,
    so the rename is atomic); returns the parked path. The one mover, shared
    by the runner's post-Run retention and this sweep's failed-push retry.
    ``target_name`` is the runner's collision-safe fallback when the plain
    name is already taken in the parking directory."""
    parking = pending_dir(config)
    parking.mkdir(parents=True, exist_ok=True)
    target = parking / (target_name or job.name)
    job.rename(target)
    return target


# The runner's collision-safe parking rename appends this suffix (M5); the
# bundle must still publish under the ORIGINAL run_id path (ADR-0016 — the
# janitor's evidence-first wait and every escalation comment name exactly
# that path).
_PARKED_SUFFIX_RE = re.compile(r"-parked-[0-9a-f]{8}$")

# Bottom of the runner's parking ladder (ADR-0019): a completed dir whose
# parking AND removal both failed is renamed in place to this dot-prefixed
# tombstone form. Dot-prefixed names are never live Runs — the Node
# Daemon's queue-behind in-flight signal skips them (nodedaemon daemon.py,
# same rule stated there), and this sweep skips them too: the evidence is
# declared lost, and re-pushing a gutted, undeletable dir every poll cycle
# would flap forever.
TOMBSTONE_PREFIX = ".evidence-lost-"


def original_run_id(name: str) -> str:
    return _PARKED_SUFFIX_RE.sub("", name)


def owned_by(config: DriverConfig, name: str) -> bool:
    """Is this job directory this driver's to sweep? Run dirs carry the
    worker id inside the run_id; review workspaces belong to the Reviewer."""
    if f"-{config.worker_id}-" in name:
        return True
    return config.role == "reviewer" and name.startswith("review-")


def _issue_number(input_files: dict[str, bytes]) -> int | None:
    raw = input_files.get("input/issue.json")
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    number = data.get("number") if isinstance(data, dict) else None
    return number if isinstance(number, int) else None


def _bundle_prefix(job: Path, input_files: dict[str, bytes]) -> str:
    """The original run_id path when the input names its issue; otherwise a
    generic sweeps/ path (e.g. review-mode workspaces, ADR-0018). A
    collision-parked dir publishes under its original run_id, suffix
    stripped. The issue number comes from the same trusted input set the
    bundle embeds (#52): a session that rewrites or deletes the job dir's
    issue.json must not be able to redirect where evidence publishes."""
    run_id = original_run_id(job.name)
    issue = _issue_number(input_files)
    if issue is not None:
        return evidence.run_dir(issue, run_id)
    return f"sweeps/{run_id}"


# Job-dir output artifacts worth preserving post-mortem (never the
# checkout). The Run's INPUT — prompt, issue metadata, Context Tree — is
# collected separately by evidence.input_artifacts under the same relative
# paths a live-pushed bundle uses (#52).
SWEPT_ARTIFACTS = (
    jobdir.MANIFEST_FILE,
    jobdir.STATUS_FILE,
    jobdir.TRANSCRIPT_FILE,
    jobdir.VERDICT_FILE,
    f"{jobdir.CHECKOUT_DIR}/.theozolith/decisions.json",
)


def _bundle_files(config: DriverConfig, job: Path, swept_at: str) -> dict[str, str | bytes]:
    # The exact Run input survives interruption (#52): same relative paths
    # as a live-pushed bundle. The pre-launch trusted snapshot is
    # authoritative — the job dir's own input/ was agent-writable through
    # the /job bind mount from launch onward. A missing snapshot proves no
    # container ever launched for this run_id (capture happens strictly
    # before launch), so only then is the job dir's input still
    # driver-authored and safe to collect.
    trusted = evidence.load_input_snapshot(Path(config.jobs_dir), original_run_id(job.name))
    input_files = trusted if trusted is not None else evidence.input_artifacts(job)
    prefix = _bundle_prefix(job, input_files)
    files: dict[str, str | bytes] = {
        f"{prefix}/swept.json": json.dumps(
            {
                "swept": True,
                "swept_at": swept_at,
                "run_id": original_run_id(job.name),
                "worker_id": config.worker_id,
                "reason": "orphaned job directory recovered by the boot-time evidence sweep",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    }
    for relpath in SWEPT_ARTIFACTS:
        content = _read(job / relpath)
        if content is not None:
            files[f"{prefix}/swept-{Path(relpath).name}"] = content
    for relpath, content in input_files.items():
        files[f"{prefix}/{relpath}"] = content
    return files


def sweep_mirrors(config: DriverConfig, *, log=_log) -> tuple[int, int]:
    """Boot-time crash-clean of the mirror root (#51); (removed, skipped).

    Two kinds of debris, both only ever left by a dead process: partial
    mirrors (a ``.tmp`` staging dir whose creator died before the atomic
    rename) and stale locks (a ``.lock`` file with no mirror and no staging
    behind it). Every removal happens while HOLDING the per-repo lock,
    acquired non-blocking: mirror creation stages under that lock, so a
    ``.tmp`` observed under it is provably orphaned — and a held lock means
    a live driver somewhere on the node, which is skipped, never awaited.
    The mirrors themselves are never swept: a final-named mirror is never
    partial (creation is staged + atomic rename), and corruption heals
    lazily at the next claim (delete + re-clone in ``ensure_mirror``).

    Trust boundary (#51 amendment): the sweep never walks — let alone
    deletes inside — an untrusted root. A root that fails validation is
    logged and left alone (boot must not crash; the Run path re-validates
    and fails the Run loudly into the infra lane), and a symlinked or
    foreign-owned entry is skipped, never followed or removed."""
    root = Path(config.mirrors_dir)
    try:
        os.lstat(root)
    except OSError:
        return 0, 0  # no cache yet — nothing to sweep, nothing to create at boot
    try:
        gitops.validate_mirrors_root(root)
    except gitops.GitError as exc:
        log(f"mirror sweep: skipping untrusted mirror root: {exc}")
        return 0, 0
    removed = skipped = 0
    for staging in sorted(root.glob(f"*{gitops.MIRROR_TMP_SUFFIX}")):
        name = staging.name.removesuffix(gitops.MIRROR_TMP_SUFFIX)
        fd = gitops.try_mirror_lock(root / (name + gitops.MIRROR_LOCK_SUFFIX))
        if fd is None:
            skipped += 1
            continue
        try:
            try:
                state = gitops._entry_state(staging, "mirror staging")
            except gitops.GitError as exc:
                skipped += 1
                log(f"mirror sweep: left in place: {exc}")
                continue
            if state != "dir":
                continue  # vanished between glob and lock — nothing to do
            shutil.rmtree(staging, ignore_errors=True)
            removed += 1
            log(f"mirror sweep: removed partial mirror {staging.name}")
        finally:
            os.close(fd)
    for lock in sorted(root.glob(f"*{gitops.MIRROR_LOCK_SUFFIX}")):
        name = lock.name.removesuffix(gitops.MIRROR_LOCK_SUFFIX)
        if (root / name).exists() or (root / (name + gitops.MIRROR_TMP_SUFFIX)).exists():
            continue  # a live mirror keeps its lock file — that is not stale
        fd = gitops.try_mirror_lock(lock)
        if fd is None:
            skipped += 1
            continue
        try:
            # Unlinked while held; late waiters detect the vanished inode
            # (fstat validation in the lock helpers) and retry on a fresh
            # file, so mutual exclusion survives the removal.
            lock.unlink(missing_ok=True)
            removed += 1
            log(f"mirror sweep: removed stale lock {lock.name}")
        finally:
            os.close(fd)
    return removed, skipped


def sweep_orphans(config: DriverConfig, *, log=_log, now=time.time) -> tuple[int, int]:
    """One sweep pass over this driver's orphans; (swept, kept) counts.

    Runs only while no Run is in flight (startup, idle poll cycles), so
    every owned directory found here is an orphan of a dead predecessor —
    or a live Run's leftover whose evidence push failed, which needs the
    same recovery.
    """
    jobs = Path(config.jobs_dir)
    parking = pending_dir(config)
    candidates = [
        entry
        for root in (jobs, parking)
        if root.is_dir()
        for entry in sorted(root.iterdir())
        if entry.is_dir()
        and not entry.name.startswith(".")  # tombstones: loss already declared
        and owned_by(config, entry.name)
    ]
    swept = kept = 0
    swept_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))
    for job in candidates:
        try:
            evidence.push_bundle(
                config.clone_url,
                _bundle_files(config, job, swept_at),
                message=f"Evidence: swept orphaned job dir {job.name}",
                author_name=config.worker_id,
                author_email=f"{config.worker_id}@theozolith.invalid",
                env=gitops.auth_env(config.token),
            )
        except Exception as exc:
            # Delete only after a confirmed push (ADR-0016): keep and retry
            # at the next startup or poll cycle — parked outside the jobs
            # dir so it never reads as an in-flight Run.
            kept += 1
            log(f"evidence sweep: push failed for {job.name} (kept for retry): {exc}")
            if job.parent != parking:
                try:
                    park_job_dir(config, job)
                except OSError as move_error:
                    log(f"evidence sweep: could not park {job.name}: {move_error}")
            continue
        shutil.rmtree(job, ignore_errors=True)
        # The trusted input snapshot outlives the job dir until the dir is
        # fully gone: remnants would re-enter the candidate list, and the
        # retried bundle must keep coming from the snapshot.
        if not job.exists():
            evidence.discard_input_snapshot(jobs, original_run_id(job.name))
        swept += 1
        log(f"evidence sweep: pushed orphaned job dir {job.name} (swept: true) and removed it")
    _gc_stale_snapshots(config, jobs, parking, log)
    return swept, kept


def _gc_stale_snapshots(config: DriverConfig, jobs: Path, parking: Path, log) -> None:
    """Remove this driver's trusted input snapshots that no longer have a
    job dir anywhere (jobs dir or parking): their Run's evidence is either
    durable or declared lost, so the snapshot is garbage. Runs only from
    the sweep — i.e. while this driver has no Run in flight — so an owned
    snapshot without a dir can never belong to live work; another driver's
    snapshots are never touched (``owned_by``, same rule as the dirs)."""
    root = jobs / evidence.SNAPSHOT_PARENT
    if not root.is_dir():
        return
    retained = {
        original_run_id(entry.name)
        for base in (jobs, parking)
        if base.is_dir()
        for entry in base.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    }
    for snap in sorted(root.iterdir()):
        if not snap.is_dir() or snap.is_symlink():
            continue
        # A crashed capture leaves a dot-prefixed staging dir; its Run never
        # launched (capture precedes launch), so it is pure debris.
        run_id = snap.name.removeprefix(evidence.SNAPSHOT_STAGING_PREFIX)
        if not owned_by(config, run_id):
            continue
        if snap.name == run_id and run_id in retained:
            continue
        shutil.rmtree(snap, ignore_errors=True)
        log(f"evidence sweep: removed stale trusted input snapshot {snap.name}")
