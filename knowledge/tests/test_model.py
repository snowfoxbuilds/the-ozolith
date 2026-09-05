from __future__ import annotations

import re

import pytest
from theozolith_knowledge.model import KnowledgeError, load_knowledge_root


def test_loads_sample_root(sample_knowledge):
    root = load_knowledge_root(sample_knowledge)
    assert root.agents_md is not None
    assert [s.name for s in root.skills] == ["code-review", "greet"]
    assert [a.name for a in root.claude_agents] == ["planner", "reviewer"]
    assert [a.name for a in root.codex_agents] == ["triage"]
    assert [(r.name, r.declared_name) for r in root.codex_agent_roles] == [("grunt", "grunt")]
    assert [h.relpath for h in root.hooks] == ["guard.sh", "hooks.json"]
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
    with pytest.raises(KnowledgeError, match=r"not a \.md or \.toml file"):
        load_knowledge_root(tmp_path)


def test_non_md_claude_agent_rejected(tmp_path):
    claude = tmp_path / "agents" / "claude"
    claude.mkdir(parents=True)
    (claude / "helper.txt").write_text("x\n")
    with pytest.raises(KnowledgeError, match=r"not a \.md file"):
        load_knowledge_root(tmp_path)


# -- codex custom agent roles: agents/codex/*.toml (ADR-0052 §1) -----------------

VALID_ROLE = 'name = "grunt"\ndescription = "Runs checks."\ndeveloper_instructions = "Run it."\n'


def _role(tmp_path, filename: str, text: str):
    codex = tmp_path / "agents" / "codex"
    codex.mkdir(parents=True, exist_ok=True)
    (codex / filename).write_text(text)
    return codex / filename


def test_codex_agent_role_loads_with_every_optional_field(tmp_path):
    _role(
        tmp_path,
        "grunt.toml",
        VALID_ROLE + 'model_reasoning_effort = "low"\nnickname_candidates = ["checker", "run 2"]\n',
    )
    root = load_knowledge_root(tmp_path)
    assert root.codex_agents == ()
    (role,) = root.codex_agent_roles
    assert (role.name, role.declared_name) == ("grunt", "grunt")


def test_codex_role_declared_name_may_differ_from_the_file_stem(tmp_path):
    # The file stem names the compiled file (a path slug); the TOML name is
    # codex's identity for the role and may carry spaces.
    _role(tmp_path, "grunt.toml", VALID_ROLE.replace('"grunt"', '"Grunt Runner"'))
    (role,) = load_knowledge_root(tmp_path).codex_agent_roles
    assert (role.name, role.declared_name) == ("grunt", "Grunt Runner")


def test_prompt_and_role_may_share_a_stem(tmp_path):
    # prompts/<n>.md and agents/<n>.toml are distinct surfaces; no collision.
    _role(tmp_path, "grunt.md", "prompt\n")
    _role(tmp_path, "grunt.toml", VALID_ROLE)
    root = load_knowledge_root(tmp_path)
    assert [a.name for a in root.codex_agents] == ["grunt"]
    assert [r.name for r in root.codex_agent_roles] == ["grunt"]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("name = [unterminated\n", "is not valid TOML"),
        ('name = "grunt"\ndescription = "x"\n', "'developer_instructions' must be a non-blank"),
        (VALID_ROLE.replace('"Run it."', '"  "'), "'developer_instructions' must be a non-blank"),
        (VALID_ROLE.replace('"grunt"', '""'), "'name' must be a non-blank string"),
        (VALID_ROLE.replace('"grunt"', '"grunt/x"'), "may contain only ASCII letters"),
        (VALID_ROLE.replace('"Runs checks."', "7"), "'description' must be a non-blank string"),
        (VALID_ROLE + 'model = "gpt-6"\n', "unknown field(s) model"),
        (
            VALID_ROLE + 'model_reasoning_effort = ""\n',
            "'model_reasoning_effort' must be a non-blank",
        ),
        (VALID_ROLE + "nickname_candidates = []\n", "must be a non-empty list"),
        (VALID_ROLE + 'nickname_candidates = "solo"\n', "must be a non-empty list"),
        (VALID_ROLE + 'nickname_candidates = ["ok", ""]\n', "nickname '' must be non-blank"),
        (VALID_ROLE + 'nickname_candidates = ["a", "a"]\n', "has duplicates"),
    ],
)
def test_invalid_codex_agent_role_rejected(tmp_path, text, problem):
    _role(tmp_path, "grunt.toml", text)
    with pytest.raises(KnowledgeError, match=re.escape(problem)):
        load_knowledge_root(tmp_path)


def test_duplicate_codex_role_names_rejected(tmp_path):
    _role(tmp_path, "a.toml", VALID_ROLE)
    _role(tmp_path, "b.toml", VALID_ROLE)
    with pytest.raises(KnowledgeError, match="both declare name 'grunt'"):
        load_knowledge_root(tmp_path)


def test_symlinked_agent_source_rejected(tmp_path):
    real = _role(tmp_path, "grunt.toml", VALID_ROLE)
    (real.parent / "link.toml").symlink_to(real)
    with pytest.raises(KnowledgeError, match="agents/codex/ entry is a symlink"):
        load_knowledge_root(tmp_path)


def test_toml_under_claude_agents_rejected(tmp_path):
    claude = tmp_path / "agents" / "claude"
    claude.mkdir(parents=True)
    (claude / "grunt.toml").write_text(VALID_ROLE)
    with pytest.raises(KnowledgeError, match=r"agents/claude/ entry is not a \.md file"):
        load_knowledge_root(tmp_path)


# -- codex hooks: hooks/ (ADR-0052 §1) -------------------------------------------

HOOKS_DOC = '{"hooks": {"PreToolUse": []}}\n'


def _hooks(tmp_path, files: dict[str, str]):
    hooks = tmp_path / "hooks"
    for rel, text in files.items():
        target = hooks / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return hooks


def test_hooks_only_root_is_loadable(tmp_path):
    _hooks(tmp_path, {"hooks.json": HOOKS_DOC, "scripts/guard.sh": "#!/bin/sh\n"})
    root = load_knowledge_root(tmp_path)
    assert [h.relpath for h in root.hooks] == ["hooks.json", "scripts/guard.sh"]


def test_hooks_without_hooks_json_rejected(tmp_path):
    _hooks(tmp_path, {"guard.sh": "#!/bin/sh\n"})
    with pytest.raises(KnowledgeError, match=r"missing hooks\.json"):
        load_knowledge_root(tmp_path)


@pytest.mark.parametrize("text", ["not json\n", "[1, 2]\n", '"string"\n'])
def test_hooks_json_must_be_a_json_object(tmp_path, text):
    _hooks(tmp_path, {"hooks.json": text})
    with pytest.raises(
        KnowledgeError, match=r"hooks\.json (is not valid JSON|must be a JSON object)"
    ):
        load_knowledge_root(tmp_path)


def test_symlink_in_hooks_rejected(tmp_path):
    hooks = _hooks(tmp_path, {"hooks.json": HOOKS_DOC})
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\n")
    (hooks / "guard.sh").symlink_to(outside)
    with pytest.raises(KnowledgeError, match="hooks/ entry is a symlink"):
        load_knowledge_root(tmp_path)


def test_symlinked_hooks_dir_rejected(tmp_path):
    real = tmp_path / "real-hooks"
    real.mkdir()
    (real / "hooks.json").write_text(HOOKS_DOC)
    (tmp_path / "hooks").symlink_to(real)
    with pytest.raises(KnowledgeError, match="hooks/ must be a directory"):
        load_knowledge_root(tmp_path)


def test_hooks_as_a_file_rejected(tmp_path):
    (tmp_path / "hooks").write_text("{}\n")
    with pytest.raises(KnowledgeError, match="hooks/ must be a directory"):
        load_knowledge_root(tmp_path)


def test_dot_prefixed_hooks_entry_rejected_not_dropped(tmp_path):
    _hooks(tmp_path, {"hooks.json": HOOKS_DOC, ".env": "SECRET=1\n"})
    with pytest.raises(KnowledgeError, match=r"invalid hooks/ entry name '\.env'"):
        load_knowledge_root(tmp_path)
