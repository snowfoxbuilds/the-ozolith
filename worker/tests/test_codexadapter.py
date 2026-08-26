"""CodexAdapter contract tests (ADR-0052): argv, classification, materialize
discipline, stream stats over 0.150.0-shaped fixtures, and the registry.

The stream fixtures mirror events captured live from codex-cli 0.150.0
(thread.started / turn.started / item.* / turn.completed / error); the spike
(#76) re-captures them on any CLI bump — the version pin in the base image
and MIN_ENFORCING_CLI are the policy that keeps these representative."""

from __future__ import annotations

import json
import tomllib

import pytest
from theozolith_worker import codexidentity, jobdir
from theozolith_worker.adapters import (
    MODEL_PINNED,
    MODEL_UNMAPPABLE,
    AgentAdapterError,
    ClaudeAdapter,
    CodexAdapter,
    make_agent_adapter,
    materialize_instruction,
    stream_stats,
)


def _manifest() -> jobdir.Manifest:
    return jobdir.Manifest(run_id="r1", mode=jobdir.MODE_RUN, adapter="codex")


def test_codex_adapter_headless_command():
    adapter = CodexAdapter()
    pointer = "Work on the task specified in /job/input/prompt.md. Read that file first."
    argv = adapter.command(_manifest(), pointer)
    assert argv[:3] == ["codex", "exec", pointer]
    assert "--json" in argv
    # The run container IS the sandbox (ADR-0013): codex's own Landlock
    # sandbox is unavailable inside it.
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert "--skip-git-repo-check" in argv
    # ADR-0045: nothing on the argv selects a model — the baked config in
    # the prepared CODEX_HOME does.
    for flag in ("-m", "--model", "-c", "--profile", "-p"):
        assert flag not in argv


def test_codex_adapter_monitored_command_is_the_plain_command(tmp_path):
    adapter = CodexAdapter()
    hooks = adapter.materialize_hooks(tmp_path, tmp_path)
    assert adapter.monitored_command(_manifest(), "ptr", hooks) == adapter.command(
        _manifest(), "ptr"
    )
    # No hook surface: materialize_hooks wrote nothing.
    assert list(tmp_path.iterdir()) == []


def test_codex_adapter_model_classification():
    adapter = CodexAdapter()
    for model in ("gpt-5.2-codex", "gpt-5.1-codex-max", "o3", "codex-mini-latest"):
        assert adapter.classify_model(model) == MODEL_PINNED, model
    # No aliases, no foreign or empty shapes.
    for model in ("", "sonnet", "claude-fable-5", "davinci", "GPT-5"):
        assert adapter.classify_model(model) == MODEL_UNMAPPABLE, model


def test_codex_adapter_efforts_are_the_config_vocabulary():
    assert CodexAdapter().mappable_efforts() == frozenset(
        {"minimal", "low", "medium", "high", "xhigh"}
    )


def test_codex_pair_error_rejects_every_nonempty_effort_today():
    """The capability table is EMPTY until spike #76 S7 proves a model
    honors a level (turn_context echoes the configured effort, so
    enforcement is not yet provable) — any (model, effort) pair fails with
    the actionable message; effort "" always passes."""
    adapter = CodexAdapter()
    assert adapter.pair_error("gpt-5.2-codex", "") == ""
    message = adapter.pair_error("gpt-5.2-codex", "high")
    assert "no proven effort capability" in message and "S7" in message
    assert "effort binds to the model" in adapter.pair_error("", "high")


def test_codex_adapter_materialize_writes_the_baked_selection(tmp_path):
    adapter = CodexAdapter()
    written = adapter.materialize("gpt-5.2-codex", "", root=tmp_path, scope="managed")
    assert (tmp_path / "etc/theozolith/model").read_text() == "gpt-5.2-codex\n"
    assert not (tmp_path / "etc/theozolith/effort").exists()
    config = tmp_path / codexidentity.BAKED_CONFIG_FILE
    assert config in written
    document = tomllib.loads(config.read_text())
    assert document == {"model": "gpt-5.2-codex"}
    # Nothing under a ~/.codex-shaped path: image bytes must not be
    # runtime-writable, and the CLI's home is assembled per Run.
    assert not (tmp_path / "home").exists()


def test_codex_adapter_materialize_rejects_interactive_scope(tmp_path):
    with pytest.raises(AgentAdapterError, match="no interactive-scope"):
        CodexAdapter().materialize("gpt-5.2-codex", "", root=tmp_path, scope="interactive")
    assert not (tmp_path / "etc").exists()


def test_codex_adapter_materialize_rejects_unproven_effort(tmp_path):
    with pytest.raises(AgentAdapterError, match="no proven effort capability"):
        CodexAdapter().materialize("gpt-5.2-codex", "high", root=tmp_path, scope="managed")
    assert not (tmp_path / "etc").exists()


def test_codex_adapter_materialize_preserves_operator_content(tmp_path):
    """A pre-existing baked config without selection keys survives the merge
    verbatim below the prepended identity block; the merged document still
    parses and carries both halves."""
    config = tmp_path / codexidentity.BAKED_CONFIG_FILE
    config.parent.mkdir(parents=True)
    config.write_text('sandbox_mode = "read-only"\n[tools]\nweb_search = true\n')
    CodexAdapter().materialize("gpt-5.2-codex", "", root=tmp_path, scope="managed")
    document = tomllib.loads(config.read_text())
    assert document["model"] == "gpt-5.2-codex"
    assert document["sandbox_mode"] == "read-only"
    assert document["tools"] == {"web_search": True}
    assert 'sandbox_mode = "read-only"' in config.read_text()


def test_codex_adapter_materialize_refuses_operator_identity_keys(tmp_path):
    config = tmp_path / codexidentity.BAKED_CONFIG_FILE
    config.parent.mkdir(parents=True)
    config.write_text('model = "o3"\nprofile = "fast"\n')
    with pytest.raises(AgentAdapterError, match="model, profile"):
        CodexAdapter().materialize("gpt-5.2-codex", "", root=tmp_path, scope="managed")
    # Operator content untouched by the refusal.
    assert tomllib.loads(config.read_text()) == {"model": "o3", "profile": "fast"}


def test_codex_adapter_materialize_refuses_malformed_operator_config(tmp_path):
    config = tmp_path / codexidentity.BAKED_CONFIG_FILE
    config.parent.mkdir(parents=True)
    config.write_text("model = [broken\n")
    with pytest.raises(AgentAdapterError, match="not valid TOML"):
        CodexAdapter().materialize("gpt-5.2-codex", "", root=tmp_path, scope="managed")


def test_codex_adapter_materialize_never_writes_through_a_symlink(tmp_path):
    """The shared bake discipline applies: a planted destination symlink
    fails the build with nothing written through it."""
    target = tmp_path / "elsewhere.toml"
    target.write_text("innocent\n")
    config = tmp_path / codexidentity.BAKED_CONFIG_FILE
    config.parent.mkdir(parents=True)
    config.symlink_to(target)
    with pytest.raises(AgentAdapterError, match="not a regular file"):
        CodexAdapter().materialize("gpt-5.2-codex", "", root=tmp_path, scope="managed")
    assert target.read_text() == "innocent\n"


def test_codex_adapter_verify_enforceable_gates_on_the_cli_version(monkeypatch):
    import subprocess as subprocess_mod

    def fake_run(argv, **kwargs):
        class Proc:
            returncode = 0
            stdout = "codex-cli 0.149.0\n"
            stderr = ""

        return Proc()

    monkeypatch.setattr(subprocess_mod, "run", fake_run)
    with pytest.raises(AgentAdapterError, match="predates the behavior"):
        CodexAdapter().verify_enforceable()

    def fake_run_new(argv, **kwargs):
        class Proc:
            returncode = 0
            stdout = "codex-cli 0.150.0\n"
            stderr = ""

        return Proc()

    monkeypatch.setattr(subprocess_mod, "run", fake_run_new)
    assert CodexAdapter().verify_enforceable() == "codex-cli 0.150.0"


# -- stream stats over 0.150.0-shaped fixtures --------------------------------

_STREAM = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "reasoning", "text": "thinking"},
    },
    {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "echo hi",
            "exit_code": 0,
        },
    },
    {
        "type": "item.completed",
        "item": {"id": "item_2", "type": "agent_message", "text": "done"},
    },
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 900, "cached_input_tokens": 100, "output_tokens": 40},
    },
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {"id": "item_3", "type": "mcp_tool_call", "server": "s", "tool": "t"},
    },
    {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 10}},
]


def test_codex_adapter_stream_stats_counts_tools_and_tokens(tmp_path):
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("\n".join(json.dumps(event) for event in _STREAM) + "\n")
    stats = CodexAdapter().stream_stats(transcript)
    # reasoning and agent_message are not tool-ish; the command execution
    # and the MCP call are. Usage sums input+output across turns (cached
    # input is already inside input_tokens accounting upstream — counted
    # once, mirroring the Claude adapter's input+output convention).
    assert stats.tool_calls == 2
    assert stats.tokens == 900 + 40 + 50 + 10
    # The stream carries no model signal by construction; the note says so
    # instead of a silently-empty model.
    assert stats.model == ""
    assert "no model signal" in stats.model_note


def test_codex_adapter_stream_stats_survives_garbage_and_absence(tmp_path):
    transcript = tmp_path / "transcript.txt"
    transcript.write_text('not json\n{"type": "turn.completed"}\n[]\n')
    stats = CodexAdapter().stream_stats(transcript)
    assert stats.tool_calls == 0 and stats.tokens is None
    missing = CodexAdapter().stream_stats(tmp_path / "nope.txt")
    assert missing.tool_calls == 0 and missing.tokens is None and missing.model == ""


def test_stream_stats_seam_dispatches_codex(tmp_path):
    transcript = tmp_path / "transcript.txt"
    transcript.write_text(json.dumps(_STREAM[5]) + "\n")
    assert stream_stats("codex", transcript).tokens == 940


# -- registry and instruction rendering ---------------------------------------


def test_registry_makes_codex():
    assert isinstance(make_agent_adapter("codex"), CodexAdapter)
    assert isinstance(make_agent_adapter("claude"), ClaudeAdapter)
    with pytest.raises(AgentAdapterError, match="known: claude, codex"):
        make_agent_adapter("pi")


def test_materialize_instruction_codex_golden():
    # GOLDEN (ADR-0045/0052): the renderer is adapter-parameterized and
    # format-frozen — these literals move only when the format deliberately
    # changes (which re-tags every model-bearing derived image).
    assert (
        materialize_instruction("codex", "gpt-5.2-codex", "", "managed")
        == "theozolith-adapter materialize --adapter codex --model gpt-5.2-codex --scope managed"
    )
