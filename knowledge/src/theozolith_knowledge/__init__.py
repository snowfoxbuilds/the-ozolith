"""TheOzolith agent-knowledge machinery.

Knowledge config format loading, per-tool compilation (Claude in V1), one-way
sync into tool config dirs, and build-time baking into container images.
"""

from theozolith_knowledge.bake import BakeResult, bake
from theozolith_knowledge.claude import GENERATED_MARKER, FileEntry, compile_claude
from theozolith_knowledge.model import KnowledgeError, KnowledgeRoot, load_knowledge_root
from theozolith_knowledge.sync import MANIFEST_NAME, SyncReport, apply_fileset, sync

__version__ = "0.3.0"

__all__ = [
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
    "load_knowledge_root",
    "sync",
]
