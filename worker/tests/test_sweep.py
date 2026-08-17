"""The boot-time evidence sweep (ADR-0016): orphaned job dirs are pushed to
the evidence branch (swept: true) and deleted only after a confirmed push —
and a driver only ever sweeps its OWN directories."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import Harness
from theozolith_worker import jobdir
from theozolith_worker.sweep import pending_dir, sweep_orphans

RUN_ID = "20260716T1200-worker-a-9"  # carries this Worker's id: owned


def orphan_job(harness: Harness, run_id: str, issue: int | None = 5) -> Path:
    """A job dir the way a dead driver leaves one."""
    job = jobdir.create_job_dir(Path(harness.worker_config.jobs_dir), run_id)
    jobdir.atomic_write(job / jobdir.PROMPT_FILE, "the prompt\n")
    jobdir.atomic_write(job / jobdir.TRANSCRIPT_FILE, '{"type":"system"} half a stream\n')
    if issue is not None:
        jobdir.atomic_write(
            job / "input" / "issue.json", json.dumps({"number": issue, "title": "t"})
        )
    return job


def test_orphans_are_pushed_under_the_original_run_id_and_deleted(harness: Harness):
    job = orphan_job(harness, RUN_ID, issue=5)
    # The Context Tree a dead driver left behind (#52): the sweep preserves
    # it under the same relative paths a live-pushed bundle uses — and never
    # walks the checkout beside it. No trusted snapshot exists here, which
    # proves the driver died BEFORE launching a container (capture precedes
    # launch), so collecting from the job dir itself is the safe fallback.
    jobdir.atomic_write(job / "input" / "issue" / "body.md", "# Issue #5: t\n\nthe body\n")
    jobdir.atomic_write(job / "input" / "issue" / "comments" / "0001-sean.md", "a decision\n")
    jobdir.atomic_write(job / "checkout" / "src" / "app.py", "NEVER-PUBLISHED\n")

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
    swept = harness.evidence_file(f"{prefix}/swept-transcript.txt")
    assert swept == '{"type":"system"} half a stream'
    # The exact Run input survived, same paths and content as a live bundle.
    assert harness.evidence_file(f"{prefix}/input/prompt.md") == "the prompt"
    assert harness.evidence_file(f"{prefix}/input/issue/body.md").endswith("the body")
    assert harness.evidence_file(f"{prefix}/input/issue/comments/0001-sean.md") == "a decision"
    # The checkout stays out of evidence, always.
    assert not any("checkout" in p or "NEVER-PUBLISHED" in p for p in paths)
    assert "NEVER-PUBLISHED" not in harness.evidence_file(f"{prefix}/swept.json")


def test_sweep_builds_input_from_the_trusted_snapshot_not_the_job_dir(harness: Harness):
    """A driver that died AFTER launching the container left a trusted
    pre-launch snapshot — and a job dir whose input/ the agent could have
    rewritten through the /job mount. The swept bundle's input is the
    snapshot's bytes, exactly (CRLF and non-ASCII included); overwrites,
    replacements, and planted files in the job dir never surface."""
    from test_contexttree import _evidence_bytes
    from theozolith_worker import evidence

    run_id = "20260817T0900-worker-a-21"
    job = orphan_job(harness, run_id, issue=12)
    original = "the real prompt\r\nnaïve café ☃\n"
    jobdir.atomic_write(job / jobdir.PROMPT_FILE, original)
    jobdir.atomic_write(job / "input" / "issue" / "comments" / "0001-sean.md", "real comment\n")
    jobs_dir = Path(harness.worker_config.jobs_dir)
    snapshot = evidence.capture_input_snapshot(jobs_dir, job, run_id)
    assert b"\r\n" in snapshot["input/prompt.md"]

    # The session tampers, then the driver dies before pushing evidence.
    (job / "input" / "prompt.md").write_text("PWNED-PROMPT")
    (job / "input" / "issue" / "comments" / "0001-sean.md").write_text("PWNED-COMMENT")
    (job / "input" / "issue" / "comments" / "9999-evil.md").write_text("PWNED-PLANTED")
    (job / "input" / "issue.json").unlink()

    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert (swept, kept) == (1, 0)
    prefix = f"runs/issue-12/{run_id}"
    paths = harness.evidence_paths()
    assert _evidence_bytes(harness, f"{prefix}/input/prompt.md") == snapshot["input/prompt.md"]
    assert (
        _evidence_bytes(harness, f"{prefix}/input/issue/comments/0001-sean.md")
        == snapshot["input/issue/comments/0001-sean.md"]
    )
    # The deleted issue.json is restored from the snapshot; the planted file
    # never appears; no PWNED byte reaches the branch.
    assert f"{prefix}/input/issue.json" in paths
    assert not any("9999-evil" in p for p in paths)
    bundled = [p for p in paths if p.startswith(f"{prefix}/")]
    assert not any(b"PWNED" in _evidence_bytes(harness, p) for p in bundled)
    # Settled: both the job dir and its snapshot are gone.
    assert not job.exists()
    assert not evidence.snapshot_dir(jobs_dir, run_id).exists()


def test_sweep_garbage_collects_stale_snapshots(harness: Harness, monkeypatch):
    """Snapshot lifecycle debris: an owned snapshot with no job dir and an
    owned staging leftover are removed; another driver's snapshot and a
    snapshot whose (push-failed, parked) job dir still needs it are kept."""
    from theozolith_worker import evidence

    jobs_dir = Path(harness.worker_config.jobs_dir)
    kept_run = "20260817T0900-worker-a-31"
    kept_job = orphan_job(harness, kept_run, issue=13)
    evidence.capture_input_snapshot(jobs_dir, kept_job, kept_run)
    stale = evidence.snapshot_dir(jobs_dir, "20260817T0900-worker-a-32")
    (stale / "input").mkdir(parents=True)
    (stale / "input" / "prompt.md").write_text("orphaned snapshot")
    staging = jobs_dir / evidence.SNAPSHOT_PARENT / ".staging-20260817T0900-worker-a-33"
    staging.mkdir(parents=True)
    foreign = evidence.snapshot_dir(jobs_dir, "20260817T0900-worker-b-1")
    foreign.mkdir(parents=True)

    def down(*args, **kwargs):
        raise RuntimeError("evidence remote down")

    monkeypatch.setattr("theozolith_worker.sweep.evidence.push_bundle", down)
    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert (swept, kept) == (0, 1)
    # The parked run's snapshot survives — its bundle still owes a retry —
    # and the other driver's snapshot is never touched.
    assert evidence.snapshot_dir(jobs_dir, kept_run).is_dir()
    assert foreign.is_dir()
    assert not stale.exists() and not staging.exists()

    # The remote returns: the retry publishes from the snapshot, then the
    # snapshot goes too.
    monkeypatch.undo()
    swept, kept = sweep_orphans(harness.worker_config, log=harness.logs.append)
    assert (swept, kept) == (1, 0)
    assert f"runs/issue-13/{kept_run}/swept.json" in harness.evidence_paths()
    assert not evidence.snapshot_dir(jobs_dir, kept_run).exists()
    assert foreign.is_dir()


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


def test_runner_parking_remnants_are_tombstoned_out_of_the_inflight_signal(
    harness: Harness, monkeypatch
):
    """M5 parking ladder, remnant rung: when removal cannot delete the dir
    (container-owned files), it is renamed in place to the dot-prefixed
    tombstone — which queue-behind and this sweep both ignore — so a
    completed Run can never read as active, even undeletable."""
    from theozolith_worker import runner
    from theozolith_worker.sweep import TOMBSTONE_PREFIX

    config = harness.worker_config
    job = orphan_job(harness, "20260721T1400-worker-a-5", issue=9)
    parking = pending_dir(config)
    parking.parent.mkdir(parents=True, exist_ok=True)
    parking.write_text("a file where the parking dir must go")  # both parks fail
    monkeypatch.setattr(runner.shutil, "rmtree", lambda *a, **k: None)  # removal fails

    logs: list[str] = []
    report = runner.RunReport(run_id=job.name, issue=9, round=1)
    runner._park_for_sweep(config, job, report, logs.append)

    assert not job.exists()  # gone from under its Run name
    assert report.evidence_discarded
    jobs = Path(config.jobs_dir)
    tombstones = [p for p in jobs.iterdir() if p.name.startswith(TOMBSTONE_PREFIX)]
    assert len(tombstones) == 1 and job.name in tombstones[0].name
    assert [p for p in jobs.iterdir() if p.is_dir() and not p.name.startswith(".")] == []
    assert [r["event"] for r in _park_records(logs)] == [
        "theozolith.evidence-park-failed",
        "theozolith.evidence-lost",
    ]
    assert any("tombstoned" in line for line in logs)

    # The sweep never resurrects a tombstone: loss is already declared.
    parking.unlink()  # clear the squatter so only the tombstone remains
    monkeypatch.undo()
    swept, kept = sweep_orphans(config, log=harness.logs.append)
    assert (swept, kept) == (0, 0)
    assert tombstones[0].exists()


def test_runner_parking_unwritable_jobs_dir_records_remnants_truthfully(
    harness: Harness,
):
    """The one irreducible residual: an unwritable jobs dir defeats parking,
    removal, AND the tombstone rename. The dir stays under its Run name
    (the sweep may still publish it — the retained comment branch stays
    truthful), a distinct structured record says so, and no loss is
    claimed."""
    if os.geteuid() == 0:
        pytest.skip("permission-based failure injection is a no-op as root")
    from theozolith_worker import runner

    config = harness.worker_config
    job = orphan_job(harness, "20260721T1500-worker-a-7", issue=3)
    jobs = Path(config.jobs_dir)
    jobs.chmod(0o555)  # every rename/unlink of entries now fails
    logs: list[str] = []
    report = runner.RunReport(run_id=job.name, issue=3, round=1)
    try:
        runner._park_for_sweep(config, job, report, logs.append)
    finally:
        jobs.chmod(0o755)

    assert job.exists()  # still under its Run name — sweep-recoverable
    assert not report.evidence_discarded  # no loss is claimed
    assert [r["event"] for r in _park_records(logs)] == [
        "theozolith.evidence-park-failed",
        "theozolith.evidence-lost",
        "theozolith.evidence-remnants",
    ]
    assert any("operator" in line for line in logs)


def test_worker_startup_sweeps_before_polling(harness: Harness):
    """The driver's boot pass recovers a dead predecessor's forensics even
    when there is nothing to claim."""
    orphan_job(harness, "20260716T1200-worker-a-5", issue=9)
    assert harness.worker_once() == 0  # no plan_ready issues exist
    assert "runs/issue-9/20260716T1200-worker-a-5/swept.json" in harness.evidence_paths()
    assert list(Path(harness.worker_config.jobs_dir).iterdir()) == []
