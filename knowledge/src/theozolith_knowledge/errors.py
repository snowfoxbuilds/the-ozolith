"""The one error type a knowledge root raises (shared so the role-file
loader and the root loader can import it without a cycle)."""

from __future__ import annotations


class KnowledgeError(ValueError):
    """A knowledge root failed validation."""
