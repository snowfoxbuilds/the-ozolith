from __future__ import annotations

from theozolith_knowledge.claude import GENERATED_MARKER, compile_claude
from theozolith_knowledge.model import load_knowledge_root

EXPECTED_GLOBAL_PATHS = {
    "CLAUDE.md",
    "agents/planner.md",
    "agents/reviewer.md",
    "skills/code-review/SKILL.md",
    "skills/code-review/references/checklist.md",
    "skills/greet/SKILL.md",
    "skills/greet/scripts/hello.sh",
    "workflows/pair-review.md",
}


def test_global_scope_placement(sample_knowledge):
    files = compile_claude(load_knowledge_root(sample_knowledge), "global")
    assert set(files) == EXPECTED_GLOBAL_PATHS


def test_project_scope_places_assets_under_dot_claude(sample_knowledge):
    files = compile_claude(load_knowledge_root(sample_knowledge), "project")
    assert set(files) == {"CLAUDE.md"} | {
        f".claude/{p}" for p in EXPECTED_GLOBAL_PATHS if p != "CLAUDE.md"
    }


def test_claude_md_is_marker_plus_verbatim_agents_md(sample_knowledge):
    files = compile_claude(load_knowledge_root(sample_knowledge), "global")
    body = (sample_knowledge / "AGENTS.md").read_bytes()
    assert files["CLAUDE.md"].content == GENERATED_MARKER.encode() + b"\n\n" + body


def test_executable_bit_is_preserved(sample_knowledge):
    files = compile_claude(load_knowledge_root(sample_knowledge), "global")
    assert files["skills/greet/scripts/hello.sh"].executable
    assert not files["skills/greet/SKILL.md"].executable


def test_root_without_agents_md_has_no_claude_md(tmp_path):
    skill = tmp_path / "skills" / "solo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Solo\n")
    files = compile_claude(load_knowledge_root(tmp_path), "global")
    assert set(files) == {"skills/solo/SKILL.md"}
