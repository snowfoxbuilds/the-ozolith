"""Decisions Section, verdict-file schema, and harness-adapter contracts."""

from __future__ import annotations

import json
from pathlib import Path

from theozolith_worker import decisions, jobdir, verdict
from theozolith_worker.gate.pipeline import Finding
from theozolith_worker.githubapi import Comment
from theozolith_worker.harness.adapters import ClaudeHarnessAdapter
from theozolith_worker.harness.validate import main as validate_main
from theozolith_worker.harness.validate import validate_session_verdict
from theozolith_worker.runner import RunReport, render_claim_escalation


def sample_section() -> decisions.DecisionsSection:
    return decisions.DecisionsSection(
        decisions=[decisions.Decision(what="used sqlite", why="no server available")],
        open_questions=["is auth in scope?"],
        remaining_work=["wire the CLI"],
        dead_ends=["tried ORM X; version conflict"],
        gate_findings=[Finding(step="lint", severity="warning", summary="fixed", fixed=True)],
    )


def test_decisions_render_upsert_parse_roundtrip():
    section = sample_section()
    body = decisions.upsert("Closes #7.", section)

    assert body.startswith("Closes #7.")
    assert "### Decisions made" in body
    parsed = decisions.parse(body)
    assert parsed == section

    # A second upsert replaces the block instead of stacking a second one.
    updated = decisions.upsert(body, decisions.DecisionsSection())
    assert updated.count(decisions.BEGIN) == 1
    assert decisions.parse(updated) == decisions.DecisionsSection()
    assert updated.startswith("Closes #7.")


def test_decisions_section_text_strips_machine_block():
    body = decisions.upsert("", sample_section())
    text = decisions.section_text(body)
    assert "used sqlite" in text
    assert "theozolith:decisions:data" not in text


def test_agent_decisions_file_parsing(tmp_path):
    target = tmp_path / decisions.AGENT_FILE
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "decisions": [{"what": "w", "why": "y"}, "bare string"],
                "open_questions": ["q"],
                "remaining_work": [],
                "dead_ends": ["d"],
            }
        )
    )
    section = decisions.read_agent_decisions(tmp_path)
    assert section is not None
    assert section.decisions == [
        decisions.Decision("w", "y"),
        decisions.Decision("bare string", ""),
    ]
    assert section.open_questions == ["q"]

    target.write_text("not json {")
    assert decisions.read_agent_decisions(tmp_path) is None
    assert decisions.read_agent_decisions(tmp_path / "missing") is None


def test_verdict_comment_roundtrip_and_latest():
    revise = verdict.Verdict(
        verdict=verdict.REVISE,
        round=2,
        evidence="tests missing for edge case",
        revised_plan="1. add tests\n2. fix the loop",
        resume_commit="abc123",
        cherry_pick=["def456"],
        bundle_url="https://example.com/bundle",
    )
    body = verdict.render_comment(revise)
    assert "Reviewer verdict: revise (round 2)" in body
    assert "Resume from commit `abc123`" in body
    assert verdict.parse_comment(body) == revise
    assert verdict.parse_comment("just chatting about `abc123`") is None

    comments = [
        Comment(id=1, author="reviewer", body=body, created_at="t1"),
        Comment(id=2, author="human", body="a human note", created_at="t2"),
    ]
    found = verdict.latest_verdict(comments)
    assert found is not None
    assert found[0] == revise and found[1].id == 1
    assert [c.id for c in verdict.comments_after(comments, found[1])] == [2]
    assert [c.id for c in verdict.comments_after(comments, None)] == [1, 2]


# -- the verdict FILE: strict validation (M2 acceptance 10) -------------------


def write_verdict(tmp_path: Path, data) -> Path:
    path = tmp_path / "verdict.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def validate(path: Path, *, round_number: int = 1, final_round: bool = False):
    return verdict.validate_verdict_file(
        path,
        round_number=round_number,
        final_round=final_round,
        default_resume="head-sha",
        bundle_url="https://example.com/bundle",
    )


def test_verdict_file_missing_or_malformed_is_rejected(tmp_path):
    result, reason = validate(tmp_path / "absent.json")
    assert result is None and "no verdict file" in reason

    result, reason = validate(write_verdict(tmp_path, "{truncated"))
    assert result is None and "not valid JSON" in reason

    result, reason = validate(write_verdict(tmp_path, ["a", "list"]))
    assert result is None and "JSON object" in reason

    result, reason = validate(write_verdict(tmp_path, {"verdict": "merge", "evidence": "x"}))
    assert result is None and "approve|revise|escalate" in reason

    result, reason = validate(write_verdict(tmp_path, {"verdict": "approve", "evidence": "  "}))
    assert result is None and "evidence" in reason


def test_verdict_file_approve_requires_grades(tmp_path):
    result, reason = validate(
        write_verdict(tmp_path, {"verdict": "approve", "evidence": "fine", "risk": "low"})
    )
    assert result is None and "deviation" in reason

    result, _ = validate(
        write_verdict(
            tmp_path,
            {"verdict": "approve", "evidence": "fine", "deviation": "low", "risk": "medium"},
        )
    )
    assert result is not None
    assert result.deviation == "low" and result.risk == "medium"
    assert result.resume_commit == "head-sha"  # default: PR head at verdict time
    assert result.bundle_url == "https://example.com/bundle"


def test_verdict_file_revise_requires_plan(tmp_path):
    result, reason = validate(
        write_verdict(tmp_path, {"verdict": "revise", "evidence": "off plan"})
    )
    assert result is None and "revised_plan" in reason

    result, _ = validate(
        write_verdict(
            tmp_path,
            {
                "verdict": "revise",
                "evidence": "off plan",
                "revised_plan": "1. do it right",
                "resume_commit": "abc",
                "cherry_pick": ["def"],
            },
        ),
        round_number=2,
    )
    assert result is not None
    assert result.round == 2 and result.resume_commit == "abc" and result.cherry_pick == ["def"]


def test_final_round_rule_rejects_revise(tmp_path):
    """A revise that would commission a Run with no remaining review round
    is invalid; the driver rejects the verdict file (M2 brief)."""
    path = write_verdict(
        tmp_path,
        {"verdict": "revise", "evidence": "still wrong", "revised_plan": "1. again"},
    )
    result, reason = validate(path, round_number=3, final_round=True)
    assert result is None and "final-round" in reason

    # approve and escalate remain valid at the final round.
    ok, _ = validate(
        write_verdict(
            tmp_path,
            {"verdict": "escalate", "evidence": "needs a human decision"},
        ),
        round_number=3,
        final_round=True,
    )
    assert ok is not None and ok.verdict == verdict.ESCALATE


# -- the Claude harness adapter ----------------------------------------------


def test_claude_adapter_interactive_command():
    adapter = ClaudeHarnessAdapter()
    manifest = jobdir.Manifest(
        run_id="r1",
        mode=jobdir.MODE_RUN,
        session="run-r1",
        adapter="claude",
        model="claude-sonnet-5",
    )
    command = adapter.command(manifest)
    assert "--model claude-sonnet-5" in command
    assert "--dangerously-skip-permissions" in command
    assert " -p " not in f" {command} "  # headless one-shot is banned
    assert "--output-format" not in command  # no machine mode: it's a session


def test_claude_adapter_installs_stop_hook(tmp_path):
    adapter = ClaudeHarnessAdapter()
    workdir = tmp_path / "checkout"
    workdir.mkdir()
    env = adapter.prepare(workdir, tmp_path)

    hook_log = Path(env["THEOZOLITH_HOOK_LOG"])
    assert hook_log == tmp_path / jobdir.HOOK_EVENTS_FILE
    assert hook_log.exists()
    settings = json.loads((workdir / ".claude" / "settings.local.json").read_text())
    stop_command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "stop" in stop_command and "THEOZOLITH_HOOK_LOG" in stop_command
    assert settings["hooks"]["UserPromptSubmit"]  # human input re-arms the wait


def test_claude_adapter_collect_copies_verdict_only_in_review_mode(tmp_path):
    adapter = ClaudeHarnessAdapter()
    workdir = tmp_path / jobdir.WORK_DIR
    workdir.mkdir()
    (workdir / "verdict.json").write_text('{"verdict": "approve"}')

    adapter.collect(workdir, tmp_path, jobdir.MODE_RUN)
    assert not (tmp_path / jobdir.VERDICT_FILE).exists()

    adapter.collect(workdir, tmp_path, jobdir.MODE_REVIEW)
    assert (tmp_path / jobdir.VERDICT_FILE).read_text() == '{"verdict": "approve"}'


# -- the shared validator: one implementation, two call sites (ADR-0014) ------

VALIDATOR_FIXTURES = [
    # (verdict.json content, round, budget) — dicts are serialized, strings raw.
    ({"verdict": "approve", "deviation": "low", "risk": "low", "evidence": "fine"}, 1, 3),
    ({"verdict": "approve", "evidence": "fine"}, 1, 3),  # grades missing
    ({"verdict": "revise", "evidence": "off plan", "revised_plan": "1. redo"}, 2, 3),
    ({"verdict": "revise", "evidence": "off plan"}, 2, 3),  # plan missing
    ({"verdict": "revise", "evidence": "again", "revised_plan": "1. redo"}, 3, 3),  # final round
    ({"verdict": "escalate", "evidence": "human needed"}, 3, 3),
    ("this is not json {", 1, 3),
    (None, 2, 3),  # no file at all
]


def _review_job(tmp_path: Path, content, round_number: int, budget: int) -> Path:
    job = jobdir.create_job_dir(tmp_path, f"review-r{round_number}")
    (job / jobdir.WORK_DIR).mkdir(parents=True, exist_ok=True)
    if content is not None:
        text = content if isinstance(content, str) else json.dumps(content)
        (job / jobdir.WORK_DIR / "verdict.json").write_text(text)
    jobdir.write_manifest(
        job,
        jobdir.Manifest(
            run_id=f"review-r{round_number}",
            mode=jobdir.MODE_REVIEW,
            session=f"review-1-round-{round_number}",
            adapter="claude",
            model="m",
            workdir=jobdir.WORK_DIR,
            round=round_number,
            round_budget=budget,
        ),
    )
    return job


def test_driver_and_harness_job_validate_identically(tmp_path):
    for index, (content, round_number, budget) in enumerate(VALIDATOR_FIXTURES):
        job = _review_job(tmp_path / str(index), content, round_number, budget)
        driver_result, driver_reason = verdict.validate_verdict_file(
            job / jobdir.WORK_DIR / "verdict.json",
            round_number=round_number,
            final_round=round_number >= budget,
            default_resume="HEAD",
            bundle_url="",
        )
        harness_valid, harness_message = validate_session_verdict(job)
        assert harness_valid == (driver_result is not None), f"fixture {index} diverged"
        if driver_result is None:
            assert harness_message == driver_reason, f"fixture {index}: reasons diverged"


def test_final_round_rule_matches_in_session(tmp_path):
    final_revise = {"verdict": "revise", "evidence": "again", "revised_plan": "1. redo"}
    job = _review_job(tmp_path / "final", final_revise, 3, 3)
    valid, message = validate_session_verdict(job)
    assert not valid and "final-round" in message
    # The same verdict one round earlier is fine.
    job = _review_job(tmp_path / "middle", final_revise, 2, 3)
    valid, _ = validate_session_verdict(job)
    assert valid


def test_validate_verdict_cli(tmp_path, capsys):
    good = {"verdict": "approve", "deviation": "low", "risk": "low", "evidence": "fine"}
    job = _review_job(tmp_path / "good", good, 1, 3)
    assert validate_main(["--job", str(job)]) == 0
    assert "valid" in capsys.readouterr().out

    job = _review_job(tmp_path / "bad", "not json {", 1, 3)
    assert validate_main(["--job", str(job)]) == 1
    assert "INVALID" in capsys.readouterr().out


# -- the claim-escalation record (ADR-0016: forensics, never state) ------------


def test_claim_escalation_comment_carries_both_failures():
    reports = [
        RunReport(
            run_id="r-1", issue=7, round=1, phase="failed",
            reason="agent session timed out", failure_class="timeout",
        ),
        RunReport(
            run_id="r-2", issue=7, round=1, phase="failed",
            reason="run container exited early", failure_class="harness",
        ),
    ]
    body = render_claim_escalation("acme/sandbox", 7, reports)
    assert "local-retry budget is spent" in body
    for run_id, cls, reason in (
        ("r-1", "timeout", "timed out"),
        ("r-2", "harness", "exited early"),
    ):
        assert f"Run `{run_id}`" in body and cls in body and reason in body
        assert f"tree/theozolith/evidence/runs/issue-7/{run_id}" in body
    assert "removing `failed` and restoring `plan_ready`" in body
    # Forensics, never machine state: no marker comment survives ADR-0016.
    assert "<!--" not in body
