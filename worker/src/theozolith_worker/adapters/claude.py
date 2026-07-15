"""Claude adapter: drives the Claude Code CLI headlessly.

All Claude-specific flags live here and nowhere else. The CLI runs with
``--dangerously-skip-permissions``, which is exactly why Runs execute inside
a disposable container and a throwaway worktree.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from theozolith_worker.adapters import AdapterResult

DEFAULT_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class ClaudeAdapter:
    model: str
    binary: str = "claude"
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    name: str = "claude"

    def _invoke(self, prompt: str, cwd: Path) -> AdapterResult:
        command = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self.model,
            "--dangerously-skip-permissions",
        ]
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"claude CLI not found ({self.binary})") from exc
        except subprocess.TimeoutExpired:
            return AdapterResult(
                ok=False,
                text="",
                transcript=f"agent timed out after {self.timeout:.0f}s",
            )
        transcript = proc.stdout + (f"\n[stderr]\n{proc.stderr}" if proc.stderr else "")
        text = proc.stdout
        try:
            payload = json.loads(proc.stdout)
            if isinstance(payload, dict) and "result" in payload:
                text = str(payload["result"])
        except json.JSONDecodeError:
            pass
        return AdapterResult(ok=proc.returncode == 0, text=text, transcript=transcript)

    def execute(self, prompt: str, cwd: Path) -> AdapterResult:
        return self._invoke(prompt, cwd)

    def complete(self, prompt: str) -> AdapterResult:
        # No checkout: run in an empty scratch dir so the agent has nothing
        # to read or edit but the prompt.
        with tempfile.TemporaryDirectory(prefix="theozolith-review-") as scratch:
            return self._invoke(prompt, Path(scratch))
