"""Agent adapters: the vendor-specific slice of a Worker image.

The swap boundary is a process/artifact contract, not a code abstraction
(AGENTIC-CODING-PIPELINE.md): an adapter takes a prompt plus a repo checkout
and mutates the checkout; everything in between is a black box. One adapter
ships per Worker image; M2 ships Claude only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class AdapterError(RuntimeError):
    """The agent process could not be run at all (missing binary, etc.)."""


@dataclass(frozen=True)
class AdapterResult:
    ok: bool
    text: str  # the agent's final answer text
    transcript: str  # raw process output, for the evidence bundle


class Adapter(Protocol):
    """What the Worker and Reviewer need from an agent vendor."""

    name: str

    def execute(self, prompt: str, cwd: Path) -> AdapterResult:
        """Run the agent on a checkout; the agent edits files in ``cwd``."""
        ...

    def complete(self, prompt: str) -> AdapterResult:
        """One-shot judgment with no checkout (the Reviewer's call)."""
        ...


def make_adapter(name: str, model: str) -> Adapter:
    if name == "claude":
        from theozolith_worker.adapters.claude import ClaudeAdapter

        return ClaudeAdapter(model=model)
    raise AdapterError(f"unknown adapter {name!r} (M2 ships: claude)")
