"""Codex compiler: map a knowledge root onto the Codex config layout.

Mapping rules (ADR-0052, extending ADR-0009's per-tool namespacing):

    AGENTS.md              -> <target>/AGENTS.md          (verbatim, NO marker)
    skills/<name>/         -> <target>/skills/<name>/     (verbatim)
    agents/codex/<n>.md    -> <target>/prompts/<n>.md     (Codex custom prompts)
    workflows/<name>       -> dropped (no Codex equivalent)

where <target> is the Codex home itself (e.g. ~/.codex). AGENTS.md carries no
generated marker: Claude's CLAUDE.md is a derivative of AGENTS.md and the
marker records that, but for Codex the vendor format IS the canonical source
format — managed-ness is carried by the sync manifest (and, in the pinned
build, by the per-tree pins), not by a comment that would burn context every
session. Skills map verbatim: Codex consumes the same skills format
(skills/<name>/SKILL.md) natively.

V1 is GLOBAL SCOPE ONLY. In project scope the knowledge root *is* the project
repo, whose AGENTS.md already sits exactly where Codex reads it — the
transform degenerates into a self-copy — and Codex custom prompts have no
project-scope home. The deployment paths (ingest compile, image bake) are all
global-scope; revisit if Codex grows a project config dir.

The dropped workflows/ section is deliberate and silent here (the compiler is
a pure root -> FileSet function); `theozolith-knowledge validate` surfaces
per-tool section counts.
"""

from __future__ import annotations

from theozolith_knowledge.claude import SCOPES, FileSet, _entry, _walk_files
from theozolith_knowledge.model import KnowledgeError, KnowledgeRoot


def compile_codex(root: KnowledgeRoot, scope: str) -> FileSet:
    """Compile a knowledge root into the files Codex expects, keyed by
    target-root-relative path."""
    if scope not in SCOPES:
        raise KnowledgeError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    if scope != "global":
        raise KnowledgeError(
            "the codex compiler is global-scope only: in project scope the root's "
            "AGENTS.md already sits where Codex reads it, and Codex custom prompts "
            "have no project-scope home (ADR-0052)"
        )

    files: FileSet = {}
    if root.agents_md is not None:
        files["AGENTS.md"] = _entry(root.agents_md)
    for skill in root.skills:
        for rel, entry in _walk_files(skill.path):
            files[f"skills/{skill.name}/{rel}"] = entry
    for agent in root.codex_agents:
        files[f"prompts/{agent.name}.md"] = _entry(agent.path)
    return files
