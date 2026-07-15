"""Decisions Section, verdict schema, and adapter contract tests."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from theozolith_worker import decisions, verdict
from theozolith_worker.adapters.claude import ClaudeAdapter
from theozolith_worker.gate.pipeline import Finding
from theozolith_worker.githubapi import Comment


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


def fake_claude(tmp_path: Path, script_body: str) -> Path:
    binary = tmp_path / "claude"
    binary.write_text(f"#!/bin/sh\n{script_body}\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


def test_claude_adapter_passes_flags_and_parses_result(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    binary = fake_claude(
        tmp_path,
        'echo "$@" > args.txt\nprintf \'%s\' \'{"type": "result", "result": "did the thing"}\'',
    )
    adapter = ClaudeAdapter(model="claude-sonnet-5", binary=str(binary))

    result = adapter.execute("build it", workdir)

    assert result.ok and result.text == "did the thing"
    args = (workdir / "args.txt").read_text()
    assert "--model claude-sonnet-5" in args
    assert "--dangerously-skip-permissions" in args
    assert "--output-format json" in args
    assert "build it" in args


def test_claude_adapter_complete_runs_outside_the_worktree(tmp_path):
    binary = fake_claude(tmp_path, "pwd; printf '%s' 'not json'")
    adapter = ClaudeAdapter(model="m", binary=str(binary))
    result = adapter.complete("judge this")
    assert result.ok
    assert "theozolith-review-" in result.text
    assert str(tmp_path) not in result.text.splitlines()[0]


def test_claude_adapter_reports_failure(tmp_path):
    binary = fake_claude(tmp_path, "echo boom >&2; exit 3")
    adapter = ClaudeAdapter(model="m", binary=str(binary))
    result = adapter.execute("x", tmp_path)
    assert not result.ok
    assert "boom" in result.transcript
