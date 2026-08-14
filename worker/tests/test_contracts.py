"""Decisions Section, verdict-file schema, and Agent-adapter contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from theozolith_worker import decisions, jobdir, verdict
from theozolith_worker.adapters import (
    MODEL_ALIAS,
    MODEL_PINNED,
    MODEL_UNMAPPABLE,
    AgentAdapterError,
    ClaudeAdapter,
    materialize_instruction,
)
from theozolith_worker.gate.pipeline import Finding
from theozolith_worker.githubapi import Comment
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
        process_issues=[
            decisions.ProcessIssue(
                friction="gate step timed out on cold cache",
                suggested_fix="warm the dependency volume in the image",
            )
        ],
    )


def test_decisions_render_upsert_parse_roundtrip():
    section = sample_section()
    body = decisions.upsert("Closes #7.", section)

    assert body.startswith("Closes #7.")
    assert "### Decisions made" in body
    # process_issues renders as its own block (2026-07-22 grilling)…
    assert "### Process issues" in body
    assert "gate step timed out on cold cache — suggested fix: warm the dependency" in body
    parsed = decisions.parse(body)
    assert parsed == section

    # A second upsert replaces the block instead of stacking a second one.
    updated = decisions.upsert(body, decisions.DecisionsSection())
    assert updated.count(decisions.BEGIN) == 1
    assert decisions.parse(updated) == decisions.DecisionsSection()
    assert updated.startswith("Closes #7.")
    # …and an empty field renders nothing at all.
    assert "Process issues" not in updated


def test_process_issues_parse_is_lenient_and_advisory():
    # Malformed entries are dropped, never errors — advisory content cannot
    # invalidate a decisions file.
    raw = {
        "process_issues": [
            {"friction": "prompt was ambiguous"},
            {"suggested_fix": "no friction key"},  # dropped
            "bare string friction",
            42,  # dropped
        ]
    }
    section = decisions.section_from_dict(raw)
    assert section.process_issues == [
        decisions.ProcessIssue(friction="prompt was ambiguous"),
        decisions.ProcessIssue(friction="bare string friction"),
    ]


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
        process_issues=[
            decisions.ProcessIssue(
                friction="diff arrived without context lines",
                suggested_fix="materialize diffs with -U10",
            )
        ],
    )
    body = verdict.render_comment(revise)
    assert "Reviewer verdict: revise (round 2)" in body
    assert "Resume from commit `abc123`" in body
    assert "#### Process issues" in body
    assert "diff arrived without context lines — suggested fix" in body
    assert verdict.parse_comment(body) == revise
    assert verdict.parse_comment("just chatting about `abc123`") is None
    # An empty field renders no block at all.
    plain = verdict.render_comment(verdict.Verdict(verdict=verdict.ESCALATE, round=1))
    assert "Process issues" not in plain

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


# -- the Claude Agent adapter ----------------------------------------------


def test_claude_adapter_headless_command():
    adapter = ClaudeAdapter()
    manifest = jobdir.Manifest(
        run_id="r1",
        mode=jobdir.MODE_RUN,
        adapter="claude",
    )
    pointer = "Work on the task specified in /job/input/prompt.md. Read that file first."
    argv = adapter.command(manifest, pointer)
    # Headless one-shot (ADR-0019 as amended): the constant-size POINTER
    # rides the invocation and the structured output stream is the
    # transcript. --verbose is load-bearing: the claude CLI requires it for
    # -p with --output-format stream-json.
    assert argv[argv.index("-p") + 1] == pointer
    # No --model (ADR-0045): the CLI reads the image's baked managed settings;
    # nothing on the argv can select a model.
    assert "--model" not in argv
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv


def test_claude_adapter_prepare_installs_no_hooks(tmp_path):
    adapter = ClaudeAdapter()
    workdir = tmp_path / "checkout"
    workdir.mkdir()
    env = adapter.prepare(workdir, tmp_path)

    assert env == {}  # no completion hooks: process exit is completion
    assert not (workdir / ".claude").exists()


def test_claude_adapter_stream_stats_reads_usage_and_tool_calls(tmp_path):
    adapter = ClaudeAdapter()
    stream = tmp_path / "transcript.txt"
    stream.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "working"},
                                {"type": "tool_use", "name": "Edit", "input": {}},
                                {"type": "tool_use", "name": "Bash", "input": {}},
                            ],
                            "usage": {"input_tokens": 90, "output_tokens": 10},
                        },
                    }
                ),
                "not json at all {",
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "usage": {"input_tokens": 120, "output_tokens": 60},
                    }
                ),
            ]
        )
        + "\n"
    )
    stats = adapter.stream_stats(stream)
    assert stats.tool_calls == 2
    assert stats.tokens == 180  # the final result's cumulative usage wins


def test_claude_adapter_stream_stats_survives_killed_sessions(tmp_path):
    adapter = ClaudeAdapter()
    # No result event (session killed): per-call assistant usage is summed.
    stream = tmp_path / "transcript.txt"
    stream.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [], "usage": {"input_tokens": 50, "output_tokens": 5}},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {"content": [], "usage": {"input_tokens": 60, "output_tokens": 15}},
            }
        )
        + "\n"
    )
    assert adapter.stream_stats(stream).tokens == 130
    # An empty or missing stream reports no usage, never a crash.
    assert adapter.stream_stats(tmp_path / "absent.txt").tokens is None


def test_claude_adapter_model_classification():
    adapter = ClaudeAdapter()
    # Shape-based (ADR-0045): full claude-* IDs pass through pinned — the CLI
    # itself does no allowlisting, so a closed list here would need a product
    # release per model launch. The unmappable class is the cross-adapter
    # mistake the build must catch.
    for pinned in ("claude-sonnet-5", "claude-fable-5", "claude-3-5-sonnet-20241022"):
        assert adapter.classify_model(pinned) == MODEL_PINNED
    # Exactly the model-family aliases the managed model default accepts
    # (each expands to the newest model of one family — verified live).
    for alias in ("sonnet", "opus", "haiku", "fable"):
        assert adapter.classify_model(alias) == MODEL_ALIAS
    for bad in ("gpt-5", "", "claude", "claude-", "Claude-Sonnet-5", "claude sonnet"):
        assert adapter.classify_model(bad) == MODEL_UNMAPPABLE
    # The CLI accepts these as selections, but neither names the single model
    # ADR-0045 bakes: "default" floats with the account tier and a session
    # pinned to it FAILS under an allowlist; "opusplan" is a two-model mode
    # that silently degrades to plain Sonnet under enforcement (both verified
    # live). Unenforceable = unmappable = the build fails.
    for unenforceable in ("default", "opusplan"):
        assert adapter.classify_model(unenforceable) == MODEL_UNMAPPABLE


def test_claude_adapter_efforts_are_the_settings_persistable_set():
    adapter = ClaudeAdapter()
    assert adapter.mappable_efforts() == frozenset({"low", "medium", "high", "xhigh"})
    # max is session-only in the claude CLI — declaring it would bake a value
    # the settings layer cannot hold, so a build asking for it must fail.
    assert "max" not in adapter.mappable_efforts()


def test_claude_adapter_materialize_managed_scope(tmp_path):
    adapter = ClaudeAdapter()
    written = adapter.materialize("claude-sonnet-5", "high", root=tmp_path, scope="managed")
    assert (tmp_path / "etc/theozolith/model").read_text() == "claude-sonnet-5\n"
    assert (tmp_path / "etc/theozolith/effort").read_text() == "high\n"
    settings = json.loads((tmp_path / "etc/claude-code/managed-settings.json").read_text())
    # The selection contract (ADR-0045, best-effort doctrine): a managed
    # "model" session DEFAULT for the main agent — the managed tier outranks
    # checkout/user settings for the same key, and the harness passes no
    # --model — plus the managed env's CLAUDE_CODE_EFFORT_LEVEL (which
    # overrides /effort, --effort, the process environment, and any
    # settings-file effortLevel). Deliberately NO availableModels allowlist:
    # enforcement is main-agent-only — subagents, skills routed through
    # subagents, and background helpers stay free to run other models.
    assert settings == {
        "model": "claude-sonnet-5",
        "effortLevel": "high",
        "env": {"CLAUDE_CODE_EFFORT_LEVEL": "high"},
    }
    assert set(written) == {
        tmp_path / "etc/theozolith/model",
        tmp_path / "etc/theozolith/effort",
        tmp_path / "etc/claude-code/managed-settings.json",
    }


def test_claude_adapter_materialize_merges_unrelated_operator_settings(tmp_path):
    adapter = ClaudeAdapter()
    target = tmp_path / "etc/claude-code/managed-settings.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"permissions": {"deny": ["WebSearch"]}, "env": {"OTHER": "kept"}}')

    adapter.materialize("claude-fable-5", "low", root=tmp_path, scope="managed")
    settings = json.loads(target.read_text())
    # Deep merge: unrelated keys an operator setup step pre-wrote survive —
    # including foreign entries inside "env" — beside the identity keys.
    assert settings["permissions"] == {"deny": ["WebSearch"]}
    assert settings["env"] == {"OTHER": "kept", "CLAUDE_CODE_EFFORT_LEVEL": "low"}
    assert settings["model"] == "claude-fable-5"
    assert settings["effortLevel"] == "low"
    # And nothing beyond the selection keys: no allowlist, no freshness key
    # (main-agent-only enforcement, best-effort doctrine).
    assert "availableModels" not in settings
    assert "enforceAvailableModels" not in settings
    assert "forceRemoteSettingsRefresh" not in settings


def test_claude_adapter_materialize_rejects_conflicting_operator_identity(tmp_path):
    adapter = ClaudeAdapter()
    target = tmp_path / "etc/claude-code/managed-settings.json"
    target.parent.mkdir(parents=True)
    before = (
        '{"permissions": {"deny": ["WebSearch"]},'
        ' "availableModels": ["claude-old-1"], "enforceAvailableModels": false}'
    )
    target.write_text(before)
    # Fail-closed doctrine (ADR-0045 amendment): a pre-existing identity key
    # is a conflicting operator policy — the build fails naming the file and
    # key, and nothing is silently deleted or overwritten.
    with pytest.raises(AgentAdapterError, match="availableModels"):
        adapter.materialize("claude-fable-5", "low", root=tmp_path, scope="managed")
    assert target.read_text() == before  # never clobbered
    assert not (tmp_path / "etc/theozolith").exists()  # nothing half-baked


def test_claude_adapter_materialize_rejects_operator_effort_when_none_is_baked(tmp_path):
    adapter = ClaudeAdapter()
    target = tmp_path / "etc/claude-code/managed-settings.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"effortLevel": "high"}')
    # effort "" means "the model's own default" (WorkerTypeDef.effort). A
    # pre-existing managed effortLevel would silently convert that empty
    # value into an enforced non-default effort — rejected, not inherited.
    with pytest.raises(AgentAdapterError, match="effortLevel"):
        adapter.materialize("claude-fable-5", "", root=tmp_path, scope="managed")
    assert json.loads(target.read_text()) == {"effortLevel": "high"}


def test_claude_adapter_materialize_refuses_non_object_managed_settings(tmp_path):
    adapter = ClaudeAdapter()
    target = tmp_path / "etc/claude-code/managed-settings.json"
    target.parent.mkdir(parents=True)
    target.write_text("[]")
    with pytest.raises(AgentAdapterError):
        adapter.materialize("claude-sonnet-5", "", root=tmp_path, scope="managed")


def test_claude_adapter_materialize_refuses_malformed_managed_settings(tmp_path):
    adapter = ClaudeAdapter()
    target = tmp_path / "etc/claude-code/managed-settings.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"permissions": ')  # truncated by a broken setup step
    with pytest.raises(AgentAdapterError, match="not valid JSON"):
        adapter.materialize("claude-sonnet-5", "", root=tmp_path, scope="managed")
    assert target.read_text() == '{"permissions": '  # never clobbered


def test_claude_adapter_materialize_refuses_non_object_env(tmp_path):
    adapter = ClaudeAdapter()
    target = tmp_path / "etc/claude-code/managed-settings.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"env": ["NOT", "AN", "OBJECT"]}')
    with pytest.raises(AgentAdapterError, match="'env' is list, not an object"):
        adapter.materialize("claude-sonnet-5", "high", root=tmp_path, scope="managed")


def test_claude_adapter_materialize_interactive_rejects_effort(tmp_path):
    adapter = ClaudeAdapter()
    # Driverless effort fails closed end to end: config load rejects it,
    # materialize_instruction refuses to render it, and the adapter refuses
    # to write it even if invoked by hand.
    with pytest.raises(AgentAdapterError, match="interactive scope"):
        adapter.materialize("claude-opus-5", "high", root=tmp_path, scope="interactive")
    assert not (tmp_path / "etc").exists()


def test_claude_adapter_materialize_interactive_scope_never_touches_claude_config(tmp_path):
    adapter = ClaudeAdapter()
    written = adapter.materialize("claude-opus-5", "", root=tmp_path, scope="interactive")
    # Interactive (Flight Deck) images bake ONLY the well-known files: anything
    # under /home/ozolith/.claude would be shadowed by the claude-state volume
    # on first mount (ADR-0043), and managed settings would lock the model
    # against in-session /model switching (ADR-0045 §Flight Deck).
    assert written == [tmp_path / "etc/theozolith/model"]
    assert (tmp_path / "etc/theozolith/model").read_text() == "claude-opus-5\n"
    assert not (tmp_path / "etc/claude-code").exists()
    assert not (tmp_path / "home").exists()


def test_materialize_instruction_golden():
    # GOLDEN (ADR-0045): the rendered instruction enters the instruction hash,
    # so this format is identity-bearing — editing it re-tags and rebuilds
    # every model-bearing derived image in a fleet. Change it only on purpose.
    assert (
        materialize_instruction("claude", "claude-sonnet-5", "", "managed")
        == "theozolith-adapter materialize --adapter claude"
        " --model claude-sonnet-5 --scope managed"
    )
    assert (
        materialize_instruction("claude", "claude-fable-5", "high", "managed")
        == "theozolith-adapter materialize --adapter claude"
        " --model claude-fable-5 --effort high --scope managed"
    )
    assert (
        materialize_instruction("claude", "claude-opus-5", "", "interactive")
        == "theozolith-adapter materialize --adapter claude"
        " --model claude-opus-5 --scope interactive"
    )
    with pytest.raises(AgentAdapterError):
        materialize_instruction("claude", "claude-sonnet-5", "", "global")
    with pytest.raises(AgentAdapterError):
        materialize_instruction("claude", "", "", "managed")
    # Driverless effort is rejected at config load; refuse to even render an
    # instruction the in-image CLI would refuse to run.
    with pytest.raises(AgentAdapterError):
        materialize_instruction("claude", "claude-opus-5", "high", "interactive")


def test_claude_adapter_verify_enforceable_gates_on_the_cli_version(monkeypatch):
    """The managed config only means anything on a CLI whose behavior the
    identity machinery was verified against — an older CLI could silently
    void an observation channel, and a fake selection is worse than a
    failed build (ADR-0045)."""
    adapter = ClaudeAdapter()

    monkeypatch.setattr(ClaudeAdapter, "_cli_version", lambda self: "2.1.240 (Claude Code)")
    assert adapter.verify_enforceable() == "2.1.240 (Claude Code)"

    # The floor is 2.1.232 exactly: the managed-over-project model-default
    # precedence, the per-key managed env merge (2.1.223), the Stop-hook
    # applied-effort payload, the ConfigChange hook, and the dry-run probe's
    # isolation flags were all verified live there.
    assert ClaudeAdapter.MIN_ENFORCING_CLI == (2, 1, 232)
    monkeypatch.setattr(ClaudeAdapter, "_cli_version", lambda self: "2.1.232 (Claude Code)")
    assert adapter.verify_enforceable() == "2.1.232 (Claude Code)"

    monkeypatch.setattr(ClaudeAdapter, "_cli_version", lambda self: "2.1.231 (Claude Code)")
    with pytest.raises(AgentAdapterError, match="predates the identity behavior"):
        adapter.verify_enforceable()

    monkeypatch.setattr(ClaudeAdapter, "_cli_version", lambda self: "2.1.174 (Claude Code)")
    with pytest.raises(AgentAdapterError, match="predates the identity behavior"):
        adapter.verify_enforceable()

    monkeypatch.setattr(ClaudeAdapter, "_cli_version", lambda self: "not a version")
    with pytest.raises(AgentAdapterError, match="cannot parse a version"):
        adapter.verify_enforceable()


def test_claude_adapter_verify_enforceable_fails_without_the_cli():
    # A base image without the Claude CLI beside the adapter cannot enforce
    # anything — the build must fail, not bake a config nothing reads.
    adapter = ClaudeAdapter(binary="/nonexistent/claude")
    with pytest.raises(AgentAdapterError, match="must ship the Claude Code CLI"):
        adapter.verify_enforceable()


def _stream(tmp_path, *events) -> Path:
    path = tmp_path / "transcript.txt"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def _init_event(model):
    return {"type": "system", "subtype": "init", "model": model}


def _turn(model):
    return {"type": "assistant", "message": {"model": model, "content": []}}


def _result(*models):
    return {"type": "result", "modelUsage": {model: {"inputTokens": 1} for model in models}}


def test_claude_adapter_observed_model_all_signals_agree(tmp_path):
    adapter = ClaudeAdapter()
    stats = adapter.stream_stats(
        _stream(
            tmp_path,
            _init_event("claude-sonnet-5"),
            _turn("claude-sonnet-5"),
            # Background helper models bill into modelUsage on every real
            # session (verified live) — corroboration must not read the
            # helper as a second executed model.
            _result("claude-sonnet-5", "claude-haiku-4-5-20251001"),
        )
    )
    assert stats.model == "claude-sonnet-5"
    assert stats.model_note == ""


def test_claude_adapter_observed_model_synthetic_turns_are_not_executions(tmp_path):
    adapter = ClaudeAdapter()
    # The CLI stamps synthetic error notices with model "<synthetic>" — the
    # old last-one-wins scan would have recorded that literal as the
    # session's model whenever the last event was an error notice.
    stats = adapter.stream_stats(
        _stream(
            tmp_path,
            _init_event("claude-sonnet-5"),
            _turn("claude-sonnet-5"),
            _turn("<synthetic>"),
        )
    )
    assert stats.model == "claude-sonnet-5"
    assert stats.model_note == ""


def test_claude_adapter_observed_model_no_assistant_turns(tmp_path):
    adapter = ClaudeAdapter()
    # A session killed before its first turn: the init announcement is the
    # only signal — reported, but flagged as unconfirmed by any execution.
    stats = adapter.stream_stats(_stream(tmp_path, _init_event("claude-sonnet-5")))
    assert stats.model == "claude-sonnet-5"
    assert "no assistant turns" in stats.model_note

    # Usage records alone attribute no turn — naming a model as "the" model
    # from billing alone would be guessing, so the note names them instead.
    stats = adapter.stream_stats(
        _stream(tmp_path, _result("claude-haiku-4-5-20251001", "claude-sonnet-5"))
    )
    assert stats.model == ""
    assert "usage records name" in stats.model_note
    assert "claude-haiku-4-5-20251001, claude-sonnet-5" in stats.model_note

    empty = adapter.stream_stats(_stream(tmp_path))
    assert empty.model == ""
    assert empty.model_note == "the stream carried no model signal"
    assert adapter.stream_stats(tmp_path / "absent.txt").model_note == ""


def test_claude_adapter_observed_model_remap_is_surfaced(tmp_path):
    adapter = ClaudeAdapter()
    # The session announced one model and executed another (provider-side
    # remap or fallback): telemetry reports what ran and says so.
    stats = adapter.stream_stats(
        _stream(
            tmp_path,
            _init_event("claude-sonnet-5"),
            _turn("claude-sonnet-4-5-20250929"),
            _result("claude-sonnet-4-5-20250929"),
        )
    )
    assert stats.model == "claude-sonnet-4-5-20250929"
    assert "initialized as claude-sonnet-5" in stats.model_note
    assert "executed claude-sonnet-4-5-20250929" in stats.model_note


def test_claude_adapter_observed_model_multiple_models_are_surfaced(tmp_path):
    adapter = ClaudeAdapter()
    stats = adapter.stream_stats(
        _stream(
            tmp_path,
            _init_event("claude-sonnet-5"),
            _turn("claude-sonnet-5"),
            _turn("claude-opus-5"),
        )
    )
    # The model the session ended on, never presented as the whole story.
    assert stats.model == "claude-opus-5"
    assert "multiple models executed turns: claude-sonnet-5, claude-opus-5" in stats.model_note


def test_claude_adapter_observed_model_drift_and_recovery_ends_where_it_ended(tmp_path):
    adapter = ClaudeAdapter()
    # sonnet -> opus -> sonnet: the headline observed model is the turn the
    # session actually ENDED on, not the most-recently-first-seen one.
    stats = adapter.stream_stats(
        _stream(
            tmp_path,
            _init_event("claude-sonnet-5"),
            _turn("claude-sonnet-5"),
            _turn("claude-opus-5"),
            _turn("claude-sonnet-5"),
        )
    )
    assert stats.model == "claude-sonnet-5"
    assert "multiple models executed turns: claude-sonnet-5, claude-opus-5" in stats.model_note


def test_claude_adapter_observed_model_conflicting_signals_are_surfaced(tmp_path):
    adapter = ClaudeAdapter()
    # Turns and usage records disagree outright — a malformed or tampered
    # stream. Never silently trust either half.
    stats = adapter.stream_stats(
        _stream(
            tmp_path,
            _init_event("claude-sonnet-5"),
            _turn("claude-sonnet-5"),
            _result("claude-haiku-4-5-20251001"),
        )
    )
    assert stats.model == "claude-sonnet-5"
    assert "usage records" in stats.model_note
    assert "do not include any executed model" in stats.model_note


def test_claude_adapter_collect_copies_verdict_only_in_review_mode(tmp_path):
    adapter = ClaudeAdapter()
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
    # process_issues (advisory): populated and malformed alike stay valid.
    (
        {
            "verdict": "approve",
            "deviation": "low",
            "risk": "low",
            "evidence": "fine",
            "process_issues": [{"friction": "slow clone", "suggested_fix": "cache it"}],
        },
        1,
        3,
    ),
    (
        {
            "verdict": "approve",
            "deviation": "low",
            "risk": "low",
            "evidence": "fine",
            "process_issues": "not even a list",
        },
        1,
        3,
    ),
]


def test_process_issues_never_invalidate_a_verdict(tmp_path):
    """Advisory only (2026-07-22): a populated field is carried, a malformed
    one is dropped — neither can fail validation and trigger an escalation."""
    populated_dir = tmp_path / "populated"
    populated_dir.mkdir()
    populated = write_verdict(
        populated_dir,
        {
            "verdict": "approve",
            "deviation": "low",
            "risk": "low",
            "evidence": "fine",
            "process_issues": [{"friction": "slow clone", "suggested_fix": "cache it"}],
        },
    )
    result, reason = validate(populated)
    assert result is not None, reason
    assert result.process_issues == [
        decisions.ProcessIssue(friction="slow clone", suggested_fix="cache it")
    ]

    malformed_dir = tmp_path / "malformed"
    malformed_dir.mkdir()
    malformed = write_verdict(
        malformed_dir,
        {
            "verdict": "approve",
            "deviation": "low",
            "risk": "low",
            "evidence": "fine",
            "process_issues": [42, {"no": "friction"}],
        },
    )
    result, reason = validate(malformed)
    assert result is not None, reason
    assert result.process_issues == []


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
            adapter="claude",
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
            run_id="r-1",
            issue=7,
            round=1,
            phase="failed",
            reason="agent session timed out",
            failure_class="timeout",
            evidence_pushed=True,
        ),
        RunReport(
            run_id="r-2",
            issue=7,
            round=1,
            phase="failed",
            reason="run container exited early",
            failure_class="harness",
            evidence_pushed=True,
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


def test_claim_escalation_names_unpushed_bundles_without_dead_links():
    """ADR-0019 acceptance 13: an exhausted evidence push never blocks the
    escalation and never produces a dead link — the comment names the
    bundle path the boot sweep will publish to, as text."""
    reports = [
        RunReport(
            run_id="r-1",
            issue=7,
            round=1,
            phase="failed",
            reason="agent session timed out",
            failure_class="timeout",
            evidence_pushed=True,
        ),
        RunReport(
            run_id="r-2",
            issue=7,
            round=1,
            phase="failed",
            reason="evidence remote was down",
            failure_class="harness",
            evidence_pushed=False,
        ),
    ]
    body = render_claim_escalation("acme/sandbox", 7, reports)
    assert "tree/theozolith/evidence/runs/issue-7/r-1" in body  # the landed bundle links
    assert "tree/theozolith/evidence/runs/issue-7/r-2" not in body  # no dead link
    assert "runs/issue-7/r-2" in body  # …but the bundle is still named
    assert "not yet published" in body and "boot-sweep recovery" in body


# -- the terminal run-event schema (ADR-0040 amendment) ------------------------


def _driver_config():
    from theozolith_worker.config import load_config

    return load_config(
        {
            "THEOZOLITH_REPO": "acme/sandbox",
            "THEOZOLITH_CLONE_URL": "file:///tmp/remote",
            "THEOZOLITH_JOBS_DIR": "/tmp/jobs",
            "THEOZOLITH_WORKER_ID": "worker-a",
            "CONTROL_NODE_URL": "https://control.invalid:8443",
            "THEOZOLITH_NODE_TOKEN": "node-token",
            "THEOZOLITH_NODE_NAME": "box1",
            "GITHUB_TOKEN": "tok",
            "ANTHROPIC_API_KEY": "key",
        },
        role="implementer",
    )


def test_run_event_carries_failure_class_only_when_one_exists():
    """Terminal failed/escalated events carry the canonical class verbatim;
    pr-open (and any event built without one) omits the key entirely —
    absence means "no failure", never an empty value (ADR-0040)."""
    from theozolith_worker import events

    config = _driver_config()
    failed = events.run_event(
        config,
        issue=7,
        run_id="r-1",
        phase=events.PHASE_FAILED,
        attempt=2,
        failure_class="session-died",
    )
    assert failed["failure_class"] == "session-died"
    escalated = events.run_event(
        config,
        issue=7,
        run_id="r-1",
        phase=events.PHASE_ESCALATED,
        attempt=2,
        failure_class="timeout",
    )
    assert escalated["failure_class"] == "timeout"
    shipped = events.run_event(
        config, issue=7, run_id="r-2", phase=events.PHASE_PR_OPEN, attempt=1, pr=41
    )
    assert "failure_class" not in shipped
    # Defensive: an empty class never lands as an empty field.
    empty = events.run_event(
        config, issue=7, run_id="r-3", phase=events.PHASE_FAILED, failure_class=""
    )
    assert "failure_class" not in empty
