"""Agent adapters: the vendor-specific slice of a run-container image (ADR-0044).

An adapter is the per-worker-type variable the immutable harness invokes: it
knows three things about its agent CLI — the headless one-shot argv to invoke
(a constant-size pointer prompt at invocation — the task content stays in the
mounted job directory; completion is process exit, ADR-0019 as amended), how
to read counters and token usage out of the structured output stream, and
which session outputs to copy into the job directory. One adapter ships per
image; M2 ships Claude only.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from theozolith_worker.jobdir import MODE_REVIEW, VERDICT_FILE, Manifest


class AgentAdapterError(RuntimeError):
    """No adapter for the requested agent."""


@dataclass(frozen=True)
class StreamStats:
    """Counters read from the structured output stream (ADR-0019).

    ``tokens`` is None until the stream carries usage — this is how an
    adapter that reports usage closes ADR-0018's null-token gap.
    """

    tool_calls: int = 0
    tokens: int | None = None


class AgentAdapter(Protocol):
    name: str

    def command(self, manifest: Manifest, prompt: str) -> list[str]:
        """The headless one-shot argv; ``prompt`` is the harness's
        constant-size pointer at the mounted task file, never the task."""
        ...

    def prepare(self, workdir: Path, job: Path) -> dict[str, str]:
        """Extra environment for the agent process."""
        ...

    def collect(self, workdir: Path, job: Path, mode: str) -> None:
        """Copy mode-specific session outputs into ``output/``."""
        ...

    def stream_stats(self, transcript: Path) -> StreamStats:
        """Tool-call and token counters from the structured output stream."""
        ...


class ClaudeAdapter:
    """Drives the Claude Code CLI. All Claude-specific mechanics live here.

    The session runs headless: ``claude -p`` with the pointer prompt as the
    argument and ``--output-format stream-json`` (which requires
    ``--verbose`` in one-shot mode), so stdout is a line-per-event JSON
    stream. That stream is the transcript, and its usage records supply the
    token counts progress telemetry carries.
    """

    name = "claude"

    def __init__(self, binary: str = "claude"):
        self._binary = binary

    def command(self, manifest: Manifest, prompt: str) -> list[str]:
        # Headless on purpose: completion is process exit (ADR-0019), and
        # the structured stream is both transcript and usage source.
        return [
            self._binary,
            "-p",
            prompt,
            "--model",
            manifest.model,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
        ]

    def prepare(self, workdir: Path, job: Path) -> dict[str, str]:
        return {}

    def collect(self, workdir: Path, job: Path, mode: str) -> None:
        if mode == MODE_REVIEW:
            verdict = workdir / "verdict.json"
            if verdict.is_file():
                target = job / VERDICT_FILE
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(verdict, target)

    def stream_stats(self, transcript: Path) -> StreamStats:
        """Scan the stream-json transcript for tool calls and token usage.

        The final ``result`` event carries the session's cumulative usage;
        if the session was killed before emitting one, per-call assistant
        usage records are summed instead. Unparseable lines are skipped —
        the stream is agent output and never trusted to be well-formed.
        """
        tool_calls = 0
        summed: int | None = None
        final: int | None = None
        try:
            with transcript.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "assistant":
                        message = event.get("message")
                        if isinstance(message, dict):
                            content = message.get("content")
                            if isinstance(content, list):
                                tool_calls += sum(
                                    1
                                    for block in content
                                    if isinstance(block, dict) and block.get("type") == "tool_use"
                                )
                            call = _usage_total(message.get("usage"))
                            if call is not None:
                                summed = (summed or 0) + call
                    elif event.get("type") == "result":
                        total = _usage_total(event.get("usage"))
                        if total is not None:
                            final = total
        except OSError:
            return StreamStats()
        return StreamStats(tool_calls=tool_calls, tokens=final if final is not None else summed)


def _usage_total(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    total = 0
    seen = False
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
            seen = True
    return total if seen else None


def make_agent_adapter(name: str) -> AgentAdapter:
    if name == "claude":
        return ClaudeAdapter()
    raise AgentAdapterError(f"unknown Agent adapter {name!r} (M2 ships: claude)")
