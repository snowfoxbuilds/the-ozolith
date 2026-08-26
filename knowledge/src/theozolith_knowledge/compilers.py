"""The per-tool compiler registry (ADR-0009, ADR-0052).

One entry per tool a knowledge root can be compiled for. Every consumer that
maps a tool name to a compiler goes through here — the sync engine, the CLI's
--tool choices, the image bake, and control's ingest-time compile — so adding
a tool is one registry entry plus its compiler module.
"""

from __future__ import annotations

from collections.abc import Callable

from theozolith_knowledge.claude import FileSet, compile_claude
from theozolith_knowledge.codex import compile_codex
from theozolith_knowledge.model import KnowledgeError, KnowledgeRoot

COMPILERS: dict[str, Callable[[KnowledgeRoot, str], FileSet]] = {
    "claude": compile_claude,
    "codex": compile_codex,
}

# Where each tool's global-scope config dir lives by default (the CLI's
# --target default; deployments pass explicit targets).
DEFAULT_GLOBAL_TARGETS = {
    "claude": "~/.claude",
    "codex": "~/.codex",
}


def get_compiler(tool: str) -> Callable[[KnowledgeRoot, str], FileSet]:
    try:
        return COMPILERS[tool]
    except KeyError:
        raise KnowledgeError(
            f"no compiler for tool {tool!r} (known: {', '.join(sorted(COMPILERS))})"
        ) from None
