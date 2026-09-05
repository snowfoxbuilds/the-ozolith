from __future__ import annotations

import pytest
from theozolith_knowledge.claude import compile_claude
from theozolith_knowledge.codex import compile_codex
from theozolith_knowledge.compilers import COMPILERS, get_compiler
from theozolith_knowledge.model import KnowledgeError, load_knowledge_root

EXPECTED_GLOBAL_PATHS = {
    "AGENTS.md",
    "agents/grunt.toml",
    "agents/scout.toml",
    "hooks/guard.sh",
    "hooks/hooks.json",
    "prompts/triage.md",
    "skills/code-review/SKILL.md",
    "skills/code-review/references/checklist.md",
    "skills/greet/SKILL.md",
    "skills/greet/scripts/hello.sh",
}


def test_global_scope_placement(sample_knowledge):
    files = compile_codex(load_knowledge_root(sample_knowledge), "global")
    assert set(files) == EXPECTED_GLOBAL_PATHS


def test_agents_md_is_verbatim_with_no_marker(sample_knowledge):
    # Claude's CLAUDE.md is a generated derivative and carries a marker; for
    # Codex the vendor file IS the canonical format, so it ships byte-exact.
    files = compile_codex(load_knowledge_root(sample_knowledge), "global")
    assert files["AGENTS.md"].content == (sample_knowledge / "AGENTS.md").read_bytes()


def test_codex_agents_become_prompts(sample_knowledge):
    files = compile_codex(load_knowledge_root(sample_knowledge), "global")
    source = sample_knowledge / "agents" / "codex" / "triage.md"
    assert files["prompts/triage.md"].content == source.read_bytes()


@pytest.mark.parametrize("stem", ["grunt", "scout"])
def test_codex_agent_roles_become_native_agents(sample_knowledge, stem):
    # The role TOML ships verbatim under agents/ — codex's own discovery dir —
    # beside the deprecated prompts/ surface (ADR-0052 §1); scout carries a
    # full native config layer (model, effort, sandbox, mcp_servers, skills).
    files = compile_codex(load_knowledge_root(sample_knowledge), "global")
    source = sample_knowledge / "agents" / "codex" / f"{stem}.toml"
    assert files[f"agents/{stem}.toml"].content == source.read_bytes()
    assert not files[f"agents/{stem}.toml"].executable


def test_hooks_travel_verbatim_with_their_exec_bits(sample_knowledge):
    files = compile_codex(load_knowledge_root(sample_knowledge), "global")
    hooks = sample_knowledge / "hooks"
    assert files["hooks/hooks.json"].content == (hooks / "hooks.json").read_bytes()
    assert files["hooks/guard.sh"].content == (hooks / "guard.sh").read_bytes()
    assert files["hooks/guard.sh"].executable
    assert not files["hooks/hooks.json"].executable


def test_workflows_are_dropped(sample_knowledge):
    files = compile_codex(load_knowledge_root(sample_knowledge), "global")
    assert not any(path.startswith("workflows/") for path in files)


def test_executable_bit_is_preserved(sample_knowledge):
    files = compile_codex(load_knowledge_root(sample_knowledge), "global")
    assert files["skills/greet/scripts/hello.sh"].executable
    assert not files["skills/greet/SKILL.md"].executable


def test_project_scope_is_rejected(sample_knowledge):
    with pytest.raises(KnowledgeError, match="global-scope only"):
        compile_codex(load_knowledge_root(sample_knowledge), "project")


def test_unknown_scope_is_rejected(sample_knowledge):
    with pytest.raises(KnowledgeError, match="unknown scope"):
        compile_codex(load_knowledge_root(sample_knowledge), "everywhere")


def test_claude_output_is_codex_blind(sample_knowledge):
    # Adding agents/codex/ (prompts or native roles) or hooks/ to a tree must
    # not change what the claude compiler emits (per-tool selective retag
    # depends on this).
    files = compile_claude(load_knowledge_root(sample_knowledge), "global")
    assert not any(
        "triage" in path or path.startswith(("prompts/", "hooks/")) or path.endswith(".toml")
        for path in files
    )


def test_registry_dispatch():
    assert set(COMPILERS) == {"claude", "codex"}
    assert get_compiler("codex") is compile_codex
    with pytest.raises(KnowledgeError, match="no compiler for tool 'pi'"):
        get_compiler("pi")
