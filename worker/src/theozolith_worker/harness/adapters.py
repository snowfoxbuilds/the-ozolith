"""Harness adapters: the vendor-specific slice of a run-container image.

An adapter knows three things about its agent CLI: the interactive command
to run in tmux (headless one-shot modes are banned by the agent session
contract), how to detect completion mechanically (a hook, never terminal
scraping), and which session outputs to copy into the job directory. One
adapter ships per image; M2 ships Claude only.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Protocol

from theozolith_worker.jobdir import HOOK_EVENTS_FILE, MODE_REVIEW, VERDICT_FILE, Manifest

# Event lines the completion hooks append to output/hook-events.log.
EVENT_STOP = "stop"
EVENT_PROMPT = "prompt"


class HarnessAdapterError(RuntimeError):
    """No adapter for the requested agent."""


class HarnessAdapter(Protocol):
    name: str

    def command(self, manifest: Manifest) -> str:
        """The interactive agent command tmux runs in the session."""
        ...

    def prepare(self, workdir: Path, job: Path) -> dict[str, str]:
        """Install the completion hook; extra env for the tmux session."""
        ...

    def collect(self, workdir: Path, job: Path, mode: str) -> None:
        """Copy mode-specific session outputs into ``output/``."""
        ...


class ClaudeHarnessAdapter:
    """Drives the Claude Code CLI. All Claude-specific mechanics live here.

    Completion detection uses Claude's Stop hook: a settings file in the
    session working directory appends ``stop`` to the hook-events log every
    time the agent finishes responding. A ``UserPromptSubmit`` hook appends
    ``prompt`` so that queued human input (an attached operator) re-arms the
    completion wait instead of ending the Run early.
    """

    name = "claude"

    def __init__(self, binary: str = "claude"):
        self._binary = binary

    def command(self, manifest: Manifest) -> str:
        # Interactive on purpose: -p (headless one-shot) is banned.
        return shlex.join(
            [self._binary, "--model", manifest.model, "--dangerously-skip-permissions"]
        )

    def prepare(self, workdir: Path, job: Path) -> dict[str, str]:
        hook_log = job / HOOK_EVENTS_FILE
        hook_log.parent.mkdir(parents=True, exist_ok=True)
        hook_log.touch()

        def hook(event: str) -> dict:
            command = f"printf '%s\\n' {event} >> \"$THEOZOLITH_HOOK_LOG\""
            return {"hooks": [{"type": "command", "command": command}]}

        settings = workdir / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [hook(EVENT_STOP)],
                        "UserPromptSubmit": [hook(EVENT_PROMPT)],
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return {"THEOZOLITH_HOOK_LOG": str(hook_log)}

    def collect(self, workdir: Path, job: Path, mode: str) -> None:
        if mode == MODE_REVIEW:
            verdict = workdir / "verdict.json"
            if verdict.is_file():
                target = job / VERDICT_FILE
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(verdict, target)


def make_harness_adapter(name: str) -> HarnessAdapter:
    if name == "claude":
        return ClaudeHarnessAdapter()
    raise HarnessAdapterError(f"unknown harness adapter {name!r} (M2 ships: claude)")
