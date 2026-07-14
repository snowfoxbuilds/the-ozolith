"""Knowledge-root loading and validation.

A knowledge root is a directory of pure data — no machinery (ADR-0007):

    <root>/
    ├── AGENTS.md            # optional: canonical instruction file
    ├── skills/<name>/       # each a folder containing SKILL.md (+ scripts, references)
    ├── agents/<tool>/*.md   # subagent files, namespaced per tool ("claude" in V1)
    └── workflows/<name>     # one file or folder per workflow

Every section is optional, but a root with no knowledge at all is rejected.
See ADR-0009 for the format decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Names become path components in sync targets; keep them to a safe slug.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

IGNORED_NAMES = {".git", "__pycache__", ".DS_Store"}


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
class Workflow:
    name: str
    path: Path


@dataclass(frozen=True)
class KnowledgeRoot:
    path: Path
    agents_md: Path | None
    skills: tuple[Skill, ...]
    claude_agents: tuple[ClaudeAgent, ...]
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
    agents_dir = root / "agents"
    if agents_dir.is_dir():
        for entry in _visible(list(agents_dir.iterdir())):
            # agents/ holds one namespace folder per tool; unknown tools are
            # tolerated (data may serve compilers this version doesn't have).
            if not entry.is_dir():
                raise KnowledgeError(f"agents/ entry is not a tool namespace folder: {entry}")
        claude_dir = agents_dir / "claude"
        if claude_dir.is_dir():
            for entry in _visible(list(claude_dir.iterdir())):
                if not entry.is_file() or entry.suffix != ".md":
                    raise KnowledgeError(f"agents/claude/ entry is not a .md file: {entry}")
                claude_agents.append(
                    ClaudeAgent(_checked_name("Claude agent", entry.stem, claude_dir), entry)
                )

    workflows: list[Workflow] = []
    workflows_dir = root / "workflows"
    if workflows_dir.is_dir():
        for entry in _visible(list(workflows_dir.iterdir())):
            workflows.append(Workflow(_checked_name("workflow", entry.name, workflows_dir), entry))

    if agents_md is None and not skills and not claude_agents and not workflows:
        raise KnowledgeError(
            f"not a knowledge root (no AGENTS.md, skills/, agents/, or workflows/ content): {root}"
        )

    return KnowledgeRoot(
        path=root,
        agents_md=agents_md,
        skills=tuple(skills),
        claude_agents=tuple(claude_agents),
        workflows=tuple(workflows),
    )
