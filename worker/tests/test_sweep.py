"""The boot-time evidence sweep (ADR-0016): orphaned job dirs are pushed to
the evidence branch (swept: true) and deleted only after a confirmed push."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import Harness
from theozolith_worker import jobdir
from theozolith_worker.sweep import sweep_orphans


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
    job = orphan_job(harness, "20260716T1200-worker-a-9", issue=5)

    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)

    assert (swept, kept) == (1, 0)
    assert not job.exists()  # deleted only after the confirmed push
    prefix = "runs/issue-5/20260716T1200-worker-a-9"
    paths = harness.evidence_paths()
    assert f"{prefix}/swept.json" in paths
    marker = json.loads(harness.evidence_file(f"{prefix}/swept.json"))
    assert marker["swept"] is True and marker["swept_at"]  # distinguishable
    assert marker["run_id"] == "20260716T1200-worker-a-9"
    assert f"{prefix}/swept-transcript.txt" in paths
    assert harness.evidence_file(f"{prefix}/swept-transcript.txt") == "[tmux] half a session"


def test_push_failure_keeps_the_job_dir_for_retry(harness: Harness, monkeypatch):
    job = orphan_job(harness, "r-kept", issue=6)

    def down(*args, **kwargs):
        raise RuntimeError("evidence remote down")

    monkeypatch.setattr("theozolith_worker.sweep.evidence.push_bundle", down)
    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert (swept, kept) == (0, 1)
    assert job.exists()  # left in place, logged, retried later
    assert any("kept for retry" in line for line in harness.logs)

    # The remote comes back: the next pass (startup or poll cycle) sweeps it.
    monkeypatch.undo()
    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert (swept, kept) == (1, 0)
    assert not job.exists()


def test_job_dir_without_issue_metadata_sweeps_generically(harness: Harness):
    orphan_job(harness, "review-11-round-2", issue=None)
    swept, _ = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert swept == 1
    assert "sweeps/review-11-round-2/swept.json" in harness.evidence_paths()


def test_worker_startup_sweeps_before_polling(harness: Harness):
    """The driver's boot pass recovers a dead predecessor's forensics even
    when there is nothing to claim."""
    orphan_job(harness, "r-boot", issue=9)
    assert harness.worker_once() == 0  # no plan_ready issues exist
    assert "runs/issue-9/r-boot/swept.json" in harness.evidence_paths()
    assert list(Path(harness.worker_config.jobs_dir).iterdir()) == []
