"""Job-dir wire format: the schemas both sides of the container boundary speak."""

from __future__ import annotations

from theozolith_worker import jobdir


def test_manifest_roundtrip(tmp_path):
    manifest = jobdir.Manifest(
        run_id="r1",
        mode=jobdir.MODE_RUN,
        adapter="claude",
        agent_timeout_seconds=10.0,
    )
    jobdir.write_manifest(tmp_path, manifest)
    assert jobdir.read_manifest(tmp_path) == manifest
    assert manifest.serve_jobs  # run mode serves gate jobs


def test_review_manifest_does_not_serve_jobs():
    manifest = jobdir.Manifest(
        run_id="review-7-round-1",
        mode=jobdir.MODE_REVIEW,
        adapter="claude",
        workdir=jobdir.WORK_DIR,
    )
    assert not manifest.serve_jobs


def test_missing_or_bad_manifest_raises(tmp_path):
    try:
        jobdir.read_manifest(tmp_path)
        raise AssertionError("expected JobDirError")
    except jobdir.JobDirError:
        pass
    (tmp_path / "input").mkdir()
    (tmp_path / jobdir.MANIFEST_FILE).write_text("{not json")
    try:
        jobdir.read_manifest(tmp_path)
        raise AssertionError("expected JobDirError")
    except jobdir.JobDirError:
        pass


def test_status_roundtrip_and_partial_read(tmp_path):
    status = jobdir.Status(
        phase=jobdir.PHASE_SERVING_JOBS,
        agent=jobdir.AgentOutcome(completed=True),
    )
    jobdir.write_status(tmp_path, status)
    read = jobdir.read_status(tmp_path)
    assert read == status
    assert read.agent.describe() == "completed"

    (tmp_path / jobdir.STATUS_FILE).write_text("{trunc")
    assert jobdir.read_status(tmp_path) is None  # never a crash on partial


def test_job_request_result_roundtrip_and_pending_order(tmp_path):
    job = jobdir.create_job_dir(tmp_path, "r1")
    jobdir.write_job_request(job, jobdir.JobRequest("002-gate", "true", 5.0))
    jobdir.write_job_request(job, jobdir.JobRequest("001-gate", "false", 5.0))
    pending = jobdir.pending_job_requests(job)
    assert [p.stem for p in pending] == ["001-gate", "002-gate"]  # driver order

    jobdir.write_job_result(job, jobdir.JobResult("001-gate", ok=False, exit_code=1, output="no"))
    pending = jobdir.pending_job_requests(job)
    assert [p.stem for p in pending] == ["002-gate"]  # answered drops out
    result = jobdir.read_job_result(job, "001-gate")
    assert result is not None and not result.ok and result.exit_code == 1


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out" / "status.json"
    jobdir.atomic_write(target, "{}")
    assert target.read_text() == "{}"
    assert [p.name for p in target.parent.iterdir()] == ["status.json"]
