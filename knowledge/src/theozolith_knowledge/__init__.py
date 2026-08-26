"""TheOzolith agent-knowledge machinery.

Knowledge config format loading, per-tool compilation (Claude and Codex),
one-way sync into tool config dirs, and build-time baking into container
images.
"""

from theozolith_knowledge.bake import BakeResult, bake
from theozolith_knowledge.claude import GENERATED_MARKER, FileEntry, compile_claude
from theozolith_knowledge.codex import compile_codex
from theozolith_knowledge.compilers import COMPILERS, get_compiler
from theozolith_knowledge.model import KnowledgeError, KnowledgeRoot, load_knowledge_root
from theozolith_knowledge.sync import MANIFEST_NAME, SyncReport, apply_fileset, sync

__version__ = "0.3.0"

__all__ = [
    "COMPILERS",
    "GENERATED_MARKER",
    "MANIFEST_NAME",
    "BakeResult",
    "FileEntry",
    "KnowledgeError",
    "KnowledgeRoot",
    "SyncReport",
    "apply_fileset",
    "bake",
    "compile_claude",
    "compile_codex",
    "get_compiler",
    "load_knowledge_root",
    "sync",
]
