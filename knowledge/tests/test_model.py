from __future__ import annotations

import pytest
from theozolith_knowledge.model import KnowledgeError, load_knowledge_root


def test_loads_sample_root(sample_knowledge):
    root = load_knowledge_root(sample_knowledge)
    assert root.agents_md is not None
    assert [s.name for s in root.skills] == ["code-review", "greet"]
    assert [a.name for a in root.claude_agents] == ["planner", "reviewer"]
    assert [w.name for w in root.workflows] == ["pair-review.md"]


def test_missing_directory_rejected(tmp_path):
    with pytest.raises(KnowledgeError, match="not a directory"):
        load_knowledge_root(tmp_path / "nope")


def test_empty_root_rejected(tmp_path):
    with pytest.raises(KnowledgeError, match="not a knowledge root"):
        load_knowledge_root(tmp_path)


def test_skill_without_skill_md_rejected(tmp_path):
    (tmp_path / "skills" / "broken").mkdir(parents=True)
    with pytest.raises(KnowledgeError, match=r"missing SKILL\.md"):
        load_knowledge_root(tmp_path)


def test_loose_file_in_skills_rejected(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "loose.md").write_text("not a folder\n")
    with pytest.raises(KnowledgeError, match="not a skill folder"):
        load_knowledge_root(tmp_path)


def test_unsafe_skill_name_rejected(tmp_path):
    bad = tmp_path / "skills" / "bad name"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("x\n")
    with pytest.raises(KnowledgeError, match="invalid skill name"):
        load_knowledge_root(tmp_path)


def test_loose_file_in_agents_rejected(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "planner.md").write_text("must be namespaced per tool\n")
    with pytest.raises(KnowledgeError, match="tool namespace"):
        load_knowledge_root(tmp_path)


def test_unknown_tool_namespace_tolerated(tmp_path):
    pi = tmp_path / "agents" / "pi"
    pi.mkdir(parents=True)
    (pi / "helper.md").write_text("future tool\n")
    (tmp_path / "AGENTS.md").write_text("# Root\n")
    root = load_knowledge_root(tmp_path)
    assert root.claude_agents == ()
    assert root.codex_agents == ()


def test_codex_agents_load(tmp_path):
    codex = tmp_path / "agents" / "codex"
    codex.mkdir(parents=True)
    (codex / "helper.md").write_text("codex subagent\n")
    root = load_knowledge_root(tmp_path)
    assert [a.name for a in root.codex_agents] == ["helper"]
    assert root.claude_agents == ()


def test_codex_only_root_is_loadable(tmp_path):
    # agents/codex content counts as knowledge: a codex-only tree is a root.
    codex = tmp_path / "agents" / "codex"
    codex.mkdir(parents=True)
    (codex / "solo.md").write_text("x\n")
    assert load_knowledge_root(tmp_path).codex_agents[0].name == "solo"


def test_non_md_codex_agent_rejected(tmp_path):
    codex = tmp_path / "agents" / "codex"
    codex.mkdir(parents=True)
    (codex / "helper.txt").write_text("x\n")
    with pytest.raises(KnowledgeError, match=r"not a \.md file"):
        load_knowledge_root(tmp_path)


def test_non_md_claude_agent_rejected(tmp_path):
    claude = tmp_path / "agents" / "claude"
    claude.mkdir(parents=True)
    (claude / "helper.txt").write_text("x\n")
    with pytest.raises(KnowledgeError, match=r"not a \.md file"):
        load_knowledge_root(tmp_path)
