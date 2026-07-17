"""The boot-time evidence sweep (ADR-0016).

A driver that died mid-Run leaves its job directory behind — and that
directory may hold the only copy of the Run's forensics (the control
database is a cache; the evidence branch is the sole durable audit trail).
At startup, and again on idle poll cycles while any orphan remains, the
driver (the PAT holder — the daemon never receives a credential) pushes each
orphaned job directory to the evidence branch under the original run_id
path with a ``swept: true`` marker and a sweep timestamp, so post-mortem-
recovered evidence is distinguishable from live-pushed evidence.

A job directory is deleted only after the push is confirmed on the remote;
on push failure it is left in place, logged, and retried later. The two-
phase zombie janitor waits for exactly these bundles before it escalates a
silent claim (ADR-0016: evidence first, never escalate without forensics).
"""

from __future__ import annotations

import json
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


def _issue_number(job: Path) -> int | None:
    raw = _read(job / "input" / "issue.json")
    if raw is None:
        return None
    try:
        number = json.loads(raw).get("number")
    except json.JSONDecodeError:
        return None
    return number if isinstance(number, int) else None


def _bundle_prefix(job: Path) -> str:
    """The original run_id path when the job names its issue; otherwise a
    generic sweeps/ path (e.g. review-mode workspaces, ADR-0018)."""
    issue = _issue_number(job)
    if issue is not None:
        return evidence.run_dir(issue, job.name)
    return f"sweeps/{job.name}"


# Job-dir artifacts worth preserving post-mortem (never the checkout).
SWEPT_ARTIFACTS = (
    jobdir.MANIFEST_FILE,
    jobdir.PROMPT_FILE,
    "input/issue.json",
    jobdir.STATUS_FILE,
    jobdir.TRANSCRIPT_FILE,
    jobdir.HOOK_EVENTS_FILE,
    jobdir.VERDICT_FILE,
    f"{jobdir.CHECKOUT_DIR}/.theozolith/decisions.json",
)


def _bundle_files(job: Path, swept_at: str, worker_id: str) -> dict[str, str]:
    prefix = _bundle_prefix(job)
    files = {
        f"{prefix}/swept.json": json.dumps(
            {
                "swept": True,
                "swept_at": swept_at,
                "run_id": job.name,
                "worker_id": worker_id,
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
    return files


def sweep_orphans(config: DriverConfig, *, log=_log, now=time.time) -> tuple[int, int]:
    """One sweep pass over the driver's jobs dir; (swept, kept) counts.

    Runs only while no Run is in flight (startup, idle poll cycles), so
    every directory found here is an orphan of a dead predecessor — or a
    live Run's leftover whose evidence push failed, which needs the same
    recovery.
    """
    root = Path(config.jobs_dir)
    if not root.is_dir():
        return 0, 0
    swept = kept = 0
    swept_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))
    for job in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            evidence.push_bundle(
                config.clone_url,
                _bundle_files(job, swept_at, config.worker_id),
                message=f"Evidence: swept orphaned job dir {job.name}",
                author_name=config.worker_id,
                author_email=f"{config.worker_id}@theozolith.invalid",
                env=gitops.auth_env(config.token),
            )
        except Exception as exc:
            # Delete only after a confirmed push (ADR-0016): keep and retry
            # at the next startup or poll cycle.
            kept += 1
            log(f"evidence sweep: push failed for {job.name} (kept for retry): {exc}")
            continue
        shutil.rmtree(job, ignore_errors=True)
        swept += 1
        log(f"evidence sweep: pushed orphaned job dir {job.name} (swept: true) and removed it")
    return swept, kept
