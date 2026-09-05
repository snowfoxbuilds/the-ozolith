"""agents/codex/*.toml — native codex custom agent roles (ADR-0052 §1).

A role file is a codex subagent definition: role metadata flattened over a
config.toml layer. The loader decides *schema validity* — the role-parser
metadata rules plus the canonical keys of the vendored 0.153.3 config schema
— and ships the bytes untouched. It promises no parser equivalence (codex's
serde aliases are refused on purpose), and schema validity says nothing
about which keys 0.153.3 applies to the child (see codexrole)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
from dataclasses import replace
from importlib import resources

import pytest
from theozolith_knowledge import codexrole, schemacheck
from theozolith_knowledge.codex import compile_codex
from theozolith_knowledge.codexrole import (
    CODEX_SCHEMA_BASELINE,
    ROLE_METADATA_FIELDS,
    SCHEMA_RESOURCE,
    codex_config_schema,
    parse_codex_role,
    rust_trim,
)
from theozolith_knowledge.model import KnowledgeError, load_knowledge_root
from theozolith_knowledge.sync import sync

VALID_ROLE = 'name = "grunt"\ndescription = "Runs checks."\ndeveloper_instructions = "Run it."\n'

FULL_ROLE = (
    'name = "scout"\n'
    'description = "Reads the codebase and reports what it finds; never edits."\n'
    'developer_instructions = """\nAnswer from the code alone. Never edit.\n"""\n'
    'nickname_candidates = ["lookout", "ranger"]\n'
    'model = "gpt-6-astra"\n'
    'model_reasoning_effort = "medium"\n'
    'sandbox_mode = "read-only"\n'
    "\n[mcp_servers.docs]\n"
    'command = "npx"\n'
    'args = ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]\n'
    'env = { LOG_LEVEL = "warn" }\n'
    "\n[[skills.config]]\n"
    'path = "../skills/greet/SKILL.md"\n'
    "enabled = true\n"
)


def _role(root, filename: str, text: str):
    codex = root / "agents" / "codex"
    codex.mkdir(parents=True, exist_ok=True)
    path = codex / filename
    path.write_text(text, encoding="utf-8")
    return path


def _rejects(root, problem: str) -> str:
    with pytest.raises(KnowledgeError, match=re.escape(problem)) as info:
        load_knowledge_root(root)
    return str(info.value)


# -- the vendored schema baseline ------------------------------------------------


def test_vendored_schema_is_the_recorded_codex_baseline():
    data = resources.files("theozolith_knowledge").joinpath(SCHEMA_RESOURCE).read_bytes()
    assert hashlib.sha256(data).hexdigest() == CODEX_SCHEMA_BASELINE.sha256
    schema = json.loads(data)
    assert schema["title"] == "ConfigToml"
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    # codex denies unknown fields on the role wrapper: the top level is closed.
    assert schema["additionalProperties"] is False
    assert CODEX_SCHEMA_BASELINE.cli_version == "0.153.3"
    assert CODEX_SCHEMA_BASELINE.git_tag == "rust-v0.153.3"


def test_checker_supports_every_keyword_the_vendored_schema_uses():
    # A baseline move that brings new vocabulary must fail here, not validate less.
    assert schemacheck.schema_problems(codex_config_schema()) == []


def test_role_metadata_fields_are_not_config_keys():
    # The split codex makes: name/description/nickname_candidates belong to the
    # role wrapper, developer_instructions to the config layer.
    properties = codex_config_schema()["properties"]
    assert not set(ROLE_METADATA_FIELDS) & set(properties)
    assert properties["developer_instructions"]["type"] == "string"


def test_damaged_vendored_schema_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(
        codexrole, "CODEX_SCHEMA_BASELINE", replace(CODEX_SCHEMA_BASELINE, sha256="0" * 64)
    )
    codex_config_schema.cache_clear()
    try:
        _role(tmp_path, "grunt.toml", VALID_ROLE)
        _rejects(tmp_path, "vendored codex config schema")
        _rejects(tmp_path, "not the " + "0" * 64 + " recorded for codex-cli 0.153.3")
    finally:
        codex_config_schema.cache_clear()


# -- acceptance --------------------------------------------------------------------


def test_minimal_role_loads(tmp_path):
    _role(tmp_path, "grunt.toml", VALID_ROLE)
    root = load_knowledge_root(tmp_path)
    assert root.codex_agents == ()
    (role,) = root.codex_agent_roles
    assert (role.name, role.declared_name) == ("grunt", "grunt")


def test_full_native_role_loads_and_compiles_byte_for_byte(tmp_path):
    source = _role(tmp_path, "scout.toml", FULL_ROLE)
    root = load_knowledge_root(tmp_path)
    (role,) = root.codex_agent_roles
    assert (role.name, role.declared_name) == ("scout", "scout")
    assert parse_codex_role(source).nickname_candidates == ("lookout", "ranger")
    assert compile_codex(root, "global")["agents/scout.toml"].content == FULL_ROLE.encode()


@pytest.mark.parametrize(
    "layer",
    [
        'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n'
        'sandbox_mode = "workspace-write"\n',
        'model_verbosity = "low"\nservice_tier = "flex"\npersonality = "pragmatic"\n',
        '[mcp_servers.docs]\ncommand = "npx"\nargs = ["-y", "x"]\nenv = { A = "1" }\n',
        '[mcp_servers.remote]\nurl = "https://example.invalid/mcp"\nbearer_token_env_var = "T"\n',
        '[[skills.config]]\npath = "../skills/greet/SKILL.md"\nenabled = false\n',
        "[skills]\ninclude_instructions = false\nmax_context_tokens = 2000\n",
        '[sandbox_workspace_write]\nnetwork_access = false\nwritable_roots = ["/tmp"]\n',
        "[features]\nmulti_agent_v2 = true\n",
        'notify = ["notify-send", "codex"]\n',
        '[profiles.fast]\nmodel = "gpt-6-astra"\n',
        "[agents]\nmax_concurrent_threads_per_session = 4\n",
        '[agents.helper]\ndescription = "a role declared inline"\n',
        '[shell_environment_policy]\ninherit = "core"\nexclude = ["AWS_*"]\n',
        "[hooks]\n",
    ],
)
def test_representative_native_config_layers_load(tmp_path, layer):
    # Schema-valid means codex can parse the layer and the view transports
    # it. Which of these keys 0.153.3 applies to the child is a separate,
    # narrower fact — sandbox_mode, for one, is transported, never applied.
    _role(tmp_path, "grunt.toml", VALID_ROLE + layer)
    assert load_knowledge_root(tmp_path).codex_agent_roles[0].declared_name == "grunt"


def test_sample_root_roles_compile_byte_for_byte(sample_knowledge):
    files = compile_codex(load_knowledge_root(sample_knowledge), "global")
    for stem in ("grunt", "scout"):
        source = sample_knowledge / "agents" / "codex" / f"{stem}.toml"
        assert files[f"agents/{stem}.toml"].content == source.read_bytes()


def test_role_declared_name_may_differ_from_the_file_stem(tmp_path):
    # The stem names the compiled file (a path slug); the TOML name is codex's
    # identity for the role and may carry spaces.
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


# -- rejection ---------------------------------------------------------------------

REJECTED = [
    ("name = [unterminated\n", "is not valid TOML"),
    ('name = "grunt"\ndescription = "x"\n', "'developer_instructions' must be a non-blank string"),
    (
        VALID_ROLE.replace('"Run it."', '"  "'),
        "'developer_instructions' must be a non-blank string",
    ),
    ('name = "grunt"\ndeveloper_instructions = "x"\n', "'description' must be a non-blank string"),
    (VALID_ROLE.replace('"Runs checks."', '" "'), "'description' must be a non-blank string"),
    ('description = "x"\ndeveloper_instructions = "x"\n', "'name' must be a non-blank string"),
    (VALID_ROLE.replace('"grunt"', '""'), "'name' must be a non-blank string"),
    (VALID_ROLE.replace('"Runs checks."', "7"), "'description' must be a string, got integer"),
    (VALID_ROLE.replace('"grunt"', "7"), "'name' must be a string, got integer"),
    (
        VALID_ROLE.replace('"Run it."', "true"),
        "developer_instructions: expected string, got boolean",
    ),
    (VALID_ROLE + "model = 7\n", "model: expected string, got integer"),
    (VALID_ROLE + "model = 2026-09-05\n", "model: expected string, got date"),
    (
        VALID_ROLE + 'sandbox_mode = "yolo"\n',
        "sandbox_mode: 'yolo' is not one of 'read-only', 'workspace-write', 'danger-full-access'",
    ),
    (VALID_ROLE + 'model_reasoning_effort = ""\n', "model_reasoning_effort: must be at least 1"),
    (
        VALID_ROLE + "model_context_window = true\n",
        "model_context_window: expected integer, got boolean",
    ),
    (
        VALID_ROLE + "tool_output_token_limit = -1\n",
        "tool_output_token_limit: -1 is below the minimum of 0",
    ),
    (
        VALID_ROLE + "model_context_window = 99999999999999999999\n",
        "outside TOML's 64-bit integer range",
    ),
    (VALID_ROLE + 'modle = "gpt"\n', "unknown field 'modle'"),
    (VALID_ROLE + 'config_file = "x.toml"\n', "unknown field 'config_file'"),
    (
        VALID_ROLE + '[mcp_servers.docs]\ncomand = "npx"\n',
        "mcp_servers.docs: unknown field 'comand'",
    ),
    (
        VALID_ROLE + '[mcp_servers.docs]\nargs = "x"\n',
        "mcp_servers.docs.args: expected array, got string",
    ),
    (
        VALID_ROLE + '[mcp_servers.docs]\ncommand = "x"\nauth = 5\n',
        "mcp_servers.docs.auth: matches none",
    ),
    (VALID_ROLE + "[skills]\nconfig = 3\n", "skills.config: expected array, got integer"),
    (
        VALID_ROLE + '[[skills.config]]\npath = "x"\n',
        "skills.config[0]: missing required field 'enabled'",
    ),
    (
        VALID_ROLE + '[[skills.config]]\nenabled = true\nname = "x"\npth = "y"\n',
        "skills.config[0]: unknown field 'pth'",
    ),
    (VALID_ROLE + 'notify = ["a", 3]\n', "notify[1]: expected string, got integer"),
    (
        VALID_ROLE
        + '[shell_environment_policy]\nexclude = ["A"]\n[shell_environment_policy.filters]\n',
        "shell_environment_policy: fields 'exclude' and 'filters' cannot be set together",
    ),
    (
        VALID_ROLE + 'nickname_candidates = "solo"\n',
        "'nickname_candidates' must be an array of strings",
    ),
    (
        VALID_ROLE + "nickname_candidates = [1]\n",
        "'nickname_candidates' must be an array of strings",
    ),
    (
        VALID_ROLE + "nickname_candidates = []\n",
        "'nickname_candidates' must contain at least one name",
    ),
    (
        VALID_ROLE + 'nickname_candidates = ["ok", " "]\n',
        "'nickname_candidates' cannot contain blank names",
    ),
    (
        VALID_ROLE + 'nickname_candidates = ["a", "a"]\n',
        "'nickname_candidates' has duplicates after trimming: 'a'",
    ),
    (
        VALID_ROLE + 'nickname_candidates = ["run/2"]\n',
        "nickname 'run/2' may contain only ASCII letters",
    ),
    (
        VALID_ROLE + "x = " + "[" * 3000 + "]" * 3000 + "\n",
        "is not valid TOML: it nests too deeply",
    ),
]


@pytest.mark.parametrize(("text", "problem"), REJECTED, ids=[p[:40] for _, p in REJECTED])
def test_invalid_role_is_refused_naming_file_and_field(tmp_path, text, problem):
    path = _role(tmp_path, "grunt.toml", text)
    message = _rejects(tmp_path, problem)
    assert str(path) in message


def test_pathological_validation_depth_is_a_data_error(tmp_path, monkeypatch):
    # Even if the checker itself blew the stack, the caller sees a KnowledgeError
    # naming the file, never a bare RecursionError.
    def explode(*_args, **_kwargs):
        raise RecursionError

    monkeypatch.setattr(schemacheck, "check", explode)
    path = _role(tmp_path, "grunt.toml", VALID_ROLE)
    assert str(path) in _rejects(tmp_path, "nests too deeply to validate")


# -- schema-valid is not parser-compatible ----------------------------------------

# codex's serde parser accepts these compatibility aliases; the generated
# schema lists only the canonical keys, and so does Ozolith, on purpose.
PARSER_ALIASES = [
    (
        "[agents]\nmax_threads = 4\n",
        "[agents]\nmax_concurrent_threads_per_session = 4\n",
        # Under the schema an unknown [agents] key is an inline role table.
        "agents.max_threads: expected table, got integer",
    ),
    (
        "[memories]\nno_memories_if_mcp_or_web_search = true\n",
        "[memories]\ndisable_on_external_context = true\n",
        "memories: unknown field 'no_memories_if_mcp_or_web_search'",
    ),
]


@pytest.mark.parametrize(
    ("alias", "canonical", "problem"), PARSER_ALIASES, ids=["agents", "memories"]
)
def test_parser_aliases_are_refused_and_canonical_keys_ship_verbatim(
    tmp_path, alias, canonical, problem
):
    path = _role(tmp_path, "grunt.toml", VALID_ROLE + alias)
    assert str(path) in _rejects(tmp_path, problem)
    # Refusal is the whole response: the file is never rewritten toward the
    # canonical spelling, on disk or in a compiled view.
    assert path.read_text(encoding="utf-8") == VALID_ROLE + alias

    path.write_text(VALID_ROLE + canonical, encoding="utf-8")
    files = compile_codex(load_knowledge_root(tmp_path), "global")
    assert files["agents/grunt.toml"].content == (VALID_ROLE + canonical).encode()


# -- codex-equivalent normalization ------------------------------------------------


def test_rust_trim_strips_unicode_white_space_and_nothing_else():
    assert rust_trim("\u00a0\t grunt \u3000\n") == "grunt"
    # U+001F is whitespace to Python's str.strip() but not to Rust's str::trim.
    assert rust_trim("\x1fgrunt") == "\x1fgrunt"
    assert "\x1fgrunt".strip() == "grunt"


def test_name_is_trimmed_for_identity_but_ships_verbatim(tmp_path):
    text = VALID_ROLE.replace('"grunt"', '"\u00a0 grunt \u3000"')
    source = _role(tmp_path, "grunt.toml", text)
    root = load_knowledge_root(tmp_path)
    assert root.codex_agent_roles[0].declared_name == "grunt"
    assert compile_codex(root, "global")["agents/grunt.toml"].content == source.read_bytes()


def test_names_that_collide_after_trimming_are_one_role(tmp_path):
    _role(tmp_path, "a.toml", VALID_ROLE)
    _role(tmp_path, "b.toml", VALID_ROLE.replace('"grunt"', '" grunt "'))
    _rejects(tmp_path, "both declare name 'grunt'")


def test_duplicate_role_names_rejected(tmp_path):
    _role(tmp_path, "a.toml", VALID_ROLE)
    _role(tmp_path, "b.toml", VALID_ROLE)
    _rejects(tmp_path, "both declare name 'grunt'")


def test_nicknames_are_trimmed_before_blank_and_duplicate_checks(tmp_path):
    _role(tmp_path, "a.toml", VALID_ROLE + 'nickname_candidates = [" checker ", "runner"]\n')
    assert parse_codex_role(tmp_path / "agents/codex/a.toml").nickname_candidates == (
        "checker",
        "runner",
    )
    _role(tmp_path, "a.toml", VALID_ROLE + 'nickname_candidates = ["runner", " runner "]\n')
    _rejects(tmp_path, "'nickname_candidates' has duplicates after trimming: 'runner'")
    _role(tmp_path, "a.toml", VALID_ROLE + 'nickname_candidates = ["\u00a0"]\n')
    _rejects(tmp_path, "'nickname_candidates' cannot contain blank names")


def test_blank_after_trimming_name_is_refused(tmp_path):
    _role(tmp_path, "a.toml", VALID_ROLE.replace('"grunt"', '"\u00a0\u3000"'))
    _rejects(tmp_path, "'name' must be a non-blank string")


def test_name_characters_follow_codex_only_nicknames_are_restricted(tmp_path):
    # codex trims the role name and requires it non-empty, nothing more; the
    # ASCII letters/digits/space/hyphen/underscore rule is codex's for nicknames.
    for name in ("Grunt Runner-2_x", "grunt/x", "grünt"):
        _role(tmp_path, "a.toml", VALID_ROLE.replace('"grunt"', f'"{name}"'))
        assert load_knowledge_root(tmp_path).codex_agent_roles[0].declared_name == name
    _role(tmp_path, "a.toml", VALID_ROLE + 'nickname_candidates = ["run 2", "run-2_x"]\n')
    load_knowledge_root(tmp_path)
    _role(tmp_path, "a.toml", VALID_ROLE + 'nickname_candidates = ["grünt"]\n')
    _rejects(tmp_path, "may contain only ASCII letters")


# -- source boundary -------------------------------------------------------------


def _outside_role_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.toml").write_text(VALID_ROLE, encoding="utf-8")
    return outside


def test_symlinked_agents_root_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside-agents"
    (outside / "codex").mkdir(parents=True)
    (outside / "codex" / "leak.toml").write_text(VALID_ROLE, encoding="utf-8")
    (root / "agents").symlink_to(outside)
    _rejects(root, "agents/ is a symlink")


def test_agents_that_is_not_a_directory_rejected(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# root\n", encoding="utf-8")
    (tmp_path / "agents").write_text("not a folder\n", encoding="utf-8")
    _rejects(tmp_path, "agents/ is not a directory")


def test_symlinked_tool_namespace_rejected(tmp_path):
    root = tmp_path / "root"
    (root / "agents").mkdir(parents=True)
    (root / "agents" / "codex").symlink_to(_outside_role_dir(tmp_path))
    _rejects(root, "agents/ tool namespace is a symlink")


def test_symlinked_claude_namespace_rejected_too(tmp_path):
    root = tmp_path / "root"
    (root / "agents").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "helper.md").write_text("x\n", encoding="utf-8")
    (root / "agents" / "claude").symlink_to(outside)
    _rejects(root, "agents/ tool namespace is a symlink")


def test_symlinked_role_file_rejected(tmp_path):
    real = _role(tmp_path, "grunt.toml", VALID_ROLE)
    (real.parent / "link.toml").symlink_to(real)
    _rejects(tmp_path, "agents/codex/ entry is a symlink")


def test_symlinked_prompt_file_rejected(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("prompt\n", encoding="utf-8")
    codex = tmp_path / "agents" / "codex"
    codex.mkdir(parents=True)
    (codex / "triage.md").symlink_to(outside)
    _rejects(tmp_path, "agents/codex/ entry is a symlink")


@pytest.mark.parametrize("kind", ["directory", "fifo", "socket"])
def test_irregular_entry_where_a_role_belongs_rejected(tmp_path, monkeypatch, kind):
    _role(tmp_path, "grunt.toml", VALID_ROLE)
    odd = tmp_path / "agents" / "codex" / "odd.toml"
    if kind == "directory":
        odd.mkdir()
    elif kind == "fifo":
        os.mkfifo(odd)
    else:
        monkeypatch.chdir(tmp_path)  # AF_UNIX paths are short; bind relative
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind("agents/codex/odd.toml")
    _rejects(tmp_path, "agents/codex/ entry is not a regular file")


def test_symlinked_namespace_never_reaches_a_sync_target(tmp_path):
    root = tmp_path / "root"
    (root / "agents").mkdir(parents=True)
    (root / "AGENTS.md").write_text("# root\n", encoding="utf-8")
    (root / "agents" / "codex").symlink_to(_outside_role_dir(tmp_path))
    target = tmp_path / "codex-home"
    with pytest.raises(KnowledgeError, match="agents/ tool namespace is a symlink"):
        sync(root, "global", target, tool="codex")
    assert not target.exists()


def test_removing_roles_and_hooks_prunes_the_codex_target(tmp_path, sample_knowledge):
    source = tmp_path / "source"
    shutil.copytree(sample_knowledge, source)
    target = tmp_path / "codex-home"
    sync(source, "global", target, tool="codex")
    assert (target / "agents" / "scout.toml").is_file()

    (source / "agents" / "codex" / "scout.toml").unlink()
    shutil.rmtree(source / "hooks")
    report = sync(source, "global", target, tool="codex")

    assert sorted(report.deleted) == ["agents/scout.toml", "hooks/guard.sh", "hooks/hooks.json"]
    assert not (target / "hooks").exists()
    assert not (target / "agents" / "scout.toml").exists()
    assert (target / "agents" / "grunt.toml").is_file()
