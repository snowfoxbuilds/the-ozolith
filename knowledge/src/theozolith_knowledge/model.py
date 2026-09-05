"""Knowledge-root loading and validation.

A knowledge root is a directory of pure data — no machinery (ADR-0007):

    <root>/
    ├── AGENTS.md            # optional: canonical instruction file
    ├── skills/<name>/       # each a folder containing SKILL.md (+ scripts, references)
    ├── agents/<tool>/       # subagent sources, namespaced per tool (claude and
    │                        # codex load; unknown namespaces are tolerated):
    │                        # *.md everywhere; *.toml codex custom agent roles
    ├── hooks/               # codex hooks: hooks.json + the scripts it references
    └── workflows/<name>     # one file or folder per workflow

Every section is optional, but a root with no knowledge at all is rejected.
See ADR-0009 for the format decision and ADR-0052 for the codex sections.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Names become path components in sync targets; keep them to a safe slug.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

IGNORED_NAMES = {".git", "__pycache__", ".DS_Store"}

# The codex custom agent role schema (codex-cli 0.153.3, module agent-roles).
# The field set is CLOSED on purpose: codex warn-and-skips a role file that
# carries an unknown field, so accepting one here would silently shrink the
# deck's roster at startup instead of failing the ingest (ADR-0052 §1).
CODEX_ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]+$")
CODEX_ROLE_REQUIRED_FIELDS = ("name", "description", "developer_instructions")
CODEX_ROLE_OPTIONAL_FIELDS = ("model_reasoning_effort", "nickname_candidates")

HOOKS_CONFIG = "hooks.json"


class KnowledgeError(ValueError):
    """A knowledge root failed validation."""


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path


@dataclass(frozen=True)
class ClaudeAgent:
    name: str
    path: Path


@dataclass(frozen=True)
class CodexAgent:
    name: str
    path: Path


@dataclass(frozen=True)
class CodexAgentRole:
    # `name` is the source file stem (the compiled file name); `declared_name`
    # is the TOML `name` codex identifies the role by, unique across the root.
    name: str
    declared_name: str
    path: Path


@dataclass(frozen=True)
class HookFile:
    # Path relative to hooks/, POSIX-separated; carried verbatim.
    relpath: str
    path: Path


@dataclass(frozen=True)
class Workflow:
    name: str
    path: Path


@dataclass(frozen=True)
class KnowledgeRoot:
    path: Path
    agents_md: Path | None
    skills: tuple[Skill, ...]
    claude_agents: tuple[ClaudeAgent, ...]
    codex_agents: tuple[CodexAgent, ...]
    codex_agent_roles: tuple[CodexAgentRole, ...]
    hooks: tuple[HookFile, ...]
    workflows: tuple[Workflow, ...]


def _checked_name(kind: str, name: str, where: Path) -> str:
    if not NAME_RE.match(name):
        raise KnowledgeError(f"invalid {kind} name {name!r} in {where}")
    return name


def _visible(entries: list[Path]) -> list[Path]:
    return sorted(
        (e for e in entries if e.name not in IGNORED_NAMES),
        key=lambda e: e.name,
    )


def _regular_file(entry: Path, what: str) -> Path:
    """Refuse a symlink or any non-regular entry where a source file belongs:
    a symlink could pull content from outside the root into a compiled view."""
    if entry.is_symlink():
        raise KnowledgeError(f"{what} is a symlink: {entry}")
    if not entry.is_file():
        raise KnowledgeError(f"{what} is not a regular file: {entry}")
    return entry


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _codex_agent_role(path: Path) -> CodexAgentRole:
    what = f"codex agent role {path}"
    try:
        table = tomllib.loads(path.read_bytes().decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise KnowledgeError(f"{what} is not valid TOML: {exc}") from exc

    unknown = sorted(set(table) - set(CODEX_ROLE_REQUIRED_FIELDS) - set(CODEX_ROLE_OPTIONAL_FIELDS))
    if unknown:
        raise KnowledgeError(
            f"{what} has unknown field(s) {', '.join(unknown)} — codex ignores a role "
            f"file with an unknown field (known: "
            f"{', '.join(CODEX_ROLE_REQUIRED_FIELDS + CODEX_ROLE_OPTIONAL_FIELDS)})"
        )
    for field in CODEX_ROLE_REQUIRED_FIELDS:
        if _blank(table.get(field)):
            raise KnowledgeError(f"{what}: {field!r} must be a non-blank string")
    name = table["name"]
    if not CODEX_ROLE_NAME_RE.match(name):
        raise KnowledgeError(
            f"{what}: name {name!r} may contain only ASCII letters, digits, spaces, "
            "hyphens, and underscores"
        )
    if "model_reasoning_effort" in table and _blank(table["model_reasoning_effort"]):
        raise KnowledgeError(f"{what}: 'model_reasoning_effort' must be a non-blank string")
    if "nickname_candidates" in table:
        nicknames = table["nickname_candidates"]
        if not isinstance(nicknames, list) or not nicknames:
            raise KnowledgeError(f"{what}: 'nickname_candidates' must be a non-empty list")
        for nickname in nicknames:
            if _blank(nickname) or not CODEX_ROLE_NAME_RE.match(nickname):
                raise KnowledgeError(
                    f"{what}: nickname {nickname!r} must be non-blank and use only ASCII "
                    "letters, digits, spaces, hyphens, and underscores"
                )
        if len(set(nicknames)) != len(nicknames):
            raise KnowledgeError(f"{what}: 'nickname_candidates' has duplicates")
    return CodexAgentRole(name=path.stem, declared_name=name, path=path)


def _codex_agent_roles(paths: list[Path]) -> list[CodexAgentRole]:
    roles = [_codex_agent_role(path) for path in paths]
    seen: dict[str, Path] = {}
    for role in roles:
        if role.declared_name in seen:
            raise KnowledgeError(
                f"codex agent roles {seen[role.declared_name]} and {role.path} both "
                f"declare name {role.declared_name!r}; role names must be unique"
            )
        seen[role.declared_name] = role.path
    return roles


def _hook_files(hooks_dir: Path) -> list[HookFile]:
    if hooks_dir.is_symlink() or not hooks_dir.is_dir():
        raise KnowledgeError(f"hooks/ must be a directory (not a symlink): {hooks_dir}")
    config = hooks_dir / HOOKS_CONFIG
    if not config.is_file():
        raise KnowledgeError(
            f"hooks/ is missing {HOOKS_CONFIG} (codex reads nothing else): {config}"
        )

    found: list[HookFile] = []

    def walk(directory: Path) -> None:
        for entry in _visible(list(directory.iterdir())):
            rel = entry.relative_to(hooks_dir).as_posix()
            _checked_name("hooks/ entry", entry.name, directory)
            if entry.is_symlink():
                raise KnowledgeError(f"hooks/ entry is a symlink: {entry}")
            if entry.is_dir():
                walk(entry)
            else:
                found.append(HookFile(rel, _regular_file(entry, "hooks/ entry")))

    walk(hooks_dir)
    try:
        document = json.loads(config.read_bytes().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KnowledgeError(f"hooks/{HOOKS_CONFIG} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise KnowledgeError(f"hooks/{HOOKS_CONFIG} must be a JSON object: {config}")
    return found


def load_knowledge_root(path: Path | str) -> KnowledgeRoot:
    """Load and validate a knowledge root. Raises KnowledgeError on problems."""
    root = Path(path)
    if not root.is_dir():
        raise KnowledgeError(f"knowledge root is not a directory: {root}")

    agents_md = root / "AGENTS.md"
    agents_md = agents_md if agents_md.is_file() else None

    skills: list[Skill] = []
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for entry in _visible(list(skills_dir.iterdir())):
            if not entry.is_dir():
                raise KnowledgeError(f"skills/ entry is not a skill folder: {entry}")
            if not (entry / "SKILL.md").is_file():
                raise KnowledgeError(f"skill {entry.name!r} is missing SKILL.md: {entry}")
            skills.append(Skill(_checked_name("skill", entry.name, skills_dir), entry))

    claude_agents: list[ClaudeAgent] = []
    codex_agents: list[CodexAgent] = []
    codex_agent_roles: list[CodexAgentRole] = []
    agents_dir = root / "agents"

    def _tool_entries(tool: str, kind: str, suffixes: tuple[str, ...]) -> list[Path]:
        tool_dir = agents_dir / tool
        entries: list[Path] = []
        if tool_dir.is_dir():
            for entry in _visible(list(tool_dir.iterdir())):
                _regular_file(entry, f"agents/{tool}/ entry")
                if entry.suffix not in suffixes:
                    raise KnowledgeError(
                        f"agents/{tool}/ entry is not a {' or '.join(suffixes)} file: {entry}"
                    )
                _checked_name(kind, entry.stem, tool_dir)
                entries.append(entry)
        return entries

    if agents_dir.is_dir():
        for entry in _visible(list(agents_dir.iterdir())):
            # agents/ holds one namespace folder per tool; unknown tools are
            # tolerated (data may serve compilers this version doesn't have).
            if not entry.is_dir():
                raise KnowledgeError(f"agents/ entry is not a tool namespace folder: {entry}")
        claude_agents = [
            ClaudeAgent(p.stem, p) for p in _tool_entries("claude", "Claude agent", (".md",))
        ]
        codex_entries = _tool_entries("codex", "Codex agent", (".md", ".toml"))
        codex_agents = [CodexAgent(p.stem, p) for p in codex_entries if p.suffix == ".md"]
        codex_agent_roles = _codex_agent_roles([p for p in codex_entries if p.suffix == ".toml"])

    hooks: list[HookFile] = []
    hooks_dir = root / "hooks"
    if hooks_dir.exists() or hooks_dir.is_symlink():
        hooks = _hook_files(hooks_dir)

    workflows: list[Workflow] = []
    workflows_dir = root / "workflows"
    if workflows_dir.is_dir():
        for entry in _visible(list(workflows_dir.iterdir())):
            workflows.append(Workflow(_checked_name("workflow", entry.name, workflows_dir), entry))

    sections = (skills, claude_agents, codex_agents, codex_agent_roles, hooks, workflows)
    if agents_md is None and not any(sections):
        raise KnowledgeError(
            "not a knowledge root (no AGENTS.md, skills/, agents/, hooks/, or workflows/ "
            f"content): {root}"
        )

    return KnowledgeRoot(
        path=root,
        agents_md=agents_md,
        skills=tuple(skills),
        claude_agents=tuple(claude_agents),
        codex_agents=tuple(codex_agents),
        codex_agent_roles=tuple(codex_agent_roles),
        hooks=tuple(hooks),
        workflows=tuple(workflows),
    )
