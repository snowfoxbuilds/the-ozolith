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


def _park_records(logs: list[str]) -> list[dict]:
    return [
        json.loads(line.partition("evidence parking failed: ")[2])
        for line in logs
        if line.startswith("evidence parking failed: ")
    ]


def test_runner_parking_falls_back_to_a_unique_destination(harness: Harness):
    """M5 parking ladder, middle rung: the plain rename collides in the
    pending dir → one structured failure record, then the dir lands at a
    collision-safe unique destination that is still this driver's to
    sweep."""
    from theozolith_worker import runner

    config = harness.worker_config
    job = orphan_job(harness, "20260721T1200-worker-a-9", issue=5)
    (pending_dir(config) / job.name / "leftover").mkdir(parents=True)  # collision

    logs: list[str] = []
    report = runner.RunReport(run_id=job.name, issue=5, round=1)
    runner._park_for_sweep(config, job, report, logs.append)

    assert not job.exists()
    parked = [
        p.name for p in pending_dir(config).iterdir() if p.name.startswith(f"{job.name}-parked-")
    ]
    assert len(parked) == 1 and f"-{config.worker_id}-" in parked[0]
    assert not report.evidence_discarded  # nothing was lost
    records = _park_records(logs)
    assert [r["event"] for r in records] == ["theozolith.evidence-park-failed"]
    record = records[0]
    assert record["run_id"] == job.name and record["issue"] == 5
    assert record["source"].endswith(job.name) and job.name in record["destination"]
    assert record["error"]

    # The sweep recognizes the uniquely named parked dir and recovers it
    # (the collision decoy carries this worker's id too, so both sweep) —
    # publishing under the ORIGINAL run_id path, suffix stripped, so the
    # janitor's evidence-first wait and the escalation comment's named
    # path both still resolve (ADR-0016).
    swept, kept = sweep_orphans(config, log=harness.logs.append)
    assert (swept, kept) == (2, 0)
    assert not any(pending_dir(config).iterdir())
    marker_path = f"runs/issue-5/{job.name}/swept.json"
    assert marker_path in harness.evidence_paths()
    assert json.loads(harness.evidence_file(marker_path))["run_id"] == job.name
    assert not any("-parked-" in p for p in harness.evidence_paths())


def test_runner_parking_compound_failure_discards_and_records(harness: Harness):
    """M5 parking ladder, bottom rung: the fallback fails too → a second
    structured record, and the completed dir is REMOVED from the active
    jobs dir (evidence loss accepted over a false in-flight signal)."""
    from theozolith_worker import runner

    config = harness.worker_config
    job = orphan_job(harness, "20260721T1300-worker-a-3", issue=8)
    parking = pending_dir(config)
    parking.parent.mkdir(parents=True, exist_ok=True)
    parking.write_text("a file where the parking dir must go")  # both attempts fail

    logs: list[str] = []
    report = runner.RunReport(run_id=job.name, issue=8, round=1)
    runner._park_for_sweep(config, job, report, logs.append)

    assert not job.exists()  # gone from the active jobs dir
    assert report.evidence_discarded
    records = _park_records(logs)
    assert [r["event"] for r in records] == [
        "theozolith.evidence-park-failed",
        "theozolith.evidence-lost",
    ]
    for record in records:
        assert record["run_id"] == job.name and record["issue"] == 8
        assert record["source"] and record["destination"] and record["error"]
    # The two records name distinct destinations (plain, then unique).
    assert records[0]["destination"] != records[1]["destination"]


def test_worker_startup_sweeps_before_polling(harness: Harness):
    """The driver's boot pass recovers a dead predecessor's forensics even
    when there is nothing to claim."""
    orphan_job(harness, "20260716T1200-worker-a-5", issue=9)
    assert harness.worker_once() == 0  # no plan_ready issues exist
    assert "runs/issue-9/20260716T1200-worker-a-5/swept.json" in harness.evidence_paths()
    assert list(Path(harness.worker_config.jobs_dir).iterdir()) == []
