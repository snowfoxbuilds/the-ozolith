"""The boot-time evidence sweep (ADR-0016): orphaned job dirs are pushed to
the evidence branch (swept: true) and deleted only after a confirmed push —
and a driver only ever sweeps its OWN directories."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import Harness
from theozolith_worker import jobdir
from theozolith_worker.sweep import pending_dir, sweep_orphans

RUN_ID = "20260716T1200-worker-a-9"  # carries this Worker's id: owned


def orphan_job(harness: Harness, run_id: str, issue: int | None = 5) -> Path:
    """A job dir the way a dead driver leaves one."""
    job = jobdir.create_job_dir(Path(harness.worker_config.jobs_dir), run_id)
    jobdir.atomic_write(job / jobdir.PROMPT_FILE, "the prompt\n")
    jobdir.atomic_write(job / jobdir.TRANSCRIPT_FILE, "[tmux] half a session\n")
    if issue is not None:
        jobdir.atomic_write(
            job / "input" / "issue.json", json.dumps({"number": issue, "title": "t"})
        )
    return job


def test_orphans_are_pushed_under_the_original_run_id_and_deleted(harness: Harness):
    job = orphan_job(harness, RUN_ID, issue=5)

    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)

    assert (swept, kept) == (1, 0)
    assert not job.exists()  # deleted only after the confirmed push
    prefix = f"runs/issue-5/{RUN_ID}"
    paths = harness.evidence_paths()
    assert f"{prefix}/swept.json" in paths
    marker = json.loads(harness.evidence_file(f"{prefix}/swept.json"))
    assert marker["swept"] is True and marker["swept_at"]  # distinguishable
    assert marker["run_id"] == RUN_ID
    assert f"{prefix}/swept-transcript.txt" in paths
    assert harness.evidence_file(f"{prefix}/swept-transcript.txt") == "[tmux] half a session"


def test_push_failure_parks_the_job_dir_for_retry(harness: Harness, monkeypatch):
    """Delete only after a confirmed push — and park the dir OUTSIDE the
    jobs dir so queue-behind's in-flight signal never sees a dead Run."""
    job = orphan_job(harness, "20260716T1200-worker-a-11", issue=6)

    def down(*args, **kwargs):
        raise RuntimeError("evidence remote down")

    monkeypatch.setattr("theozolith_worker.sweep.evidence.push_bundle", down)
    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert (swept, kept) == (0, 1)
    parked = pending_dir(harness.worker_config) / job.name
    assert not job.exists() and parked.exists()  # parked, not lost
    assert any("kept for retry" in line for line in harness.logs)

    # The remote comes back: the next pass (startup or poll cycle) sweeps it.
    monkeypatch.undo()
    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert (swept, kept) == (1, 0)
    assert not parked.exists()
    assert "runs/issue-6/20260716T1200-worker-a-11/swept.json" in harness.evidence_paths()


def test_another_drivers_directories_are_never_touched(harness: Harness):
    """Worker and Reviewer share the default jobs dir: a live review
    workspace (or another worker's dir) must never be swept out from under
    a running container."""
    other_worker = orphan_job(harness, "20260716T1200-worker-b-1", issue=7)
    review = orphan_job(harness, "review-11-round-2", issue=None)

    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert (swept, kept) == (0, 0)
    assert other_worker.exists() and review.exists()

    # The Reviewer's own sweep owns review workspaces (generic sweeps/ path).
    swept, _ = sweep_orphans(harness.reviewer_config, log=harness.logs.append)
    assert swept == 1
    assert not review.exists() and other_worker.exists()
    assert "sweeps/review-11-round-2/swept.json" in harness.evidence_paths()


def test_run_dir_without_issue_metadata_still_sweeps(harness: Harness):
    """A driver dead before its clone finished: issue.json is written before
    the clone now, but if even that is missing the sweep falls back to the
    generic path (which the janitor's evidence check also probes)."""
    orphan_job(harness, "20260716T1200-worker-a-3", issue=None)
    swept, _ = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert swept == 1
    assert "sweeps/20260716T1200-worker-a-3/swept.json" in harness.evidence_paths()


def test_worker_startup_sweeps_before_polling(harness: Harness):
    """The driver's boot pass recovers a dead predecessor's forensics even
    when there is nothing to claim."""
    orphan_job(harness, "20260716T1200-worker-a-5", issue=9)
    assert harness.worker_once() == 0  # no plan_ready issues exist
    assert "runs/issue-9/20260716T1200-worker-a-5/swept.json" in harness.evidence_paths()
    assert list(Path(harness.worker_config.jobs_dir).iterdir()) == []
