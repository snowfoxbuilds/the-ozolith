"""A thin tmux wrapper: the attachable session every agent runs in.

The agent session contract (AGENTIC-CODING-PIPELINE.md) requires every agent
process to run in an interactive, discoverable tmux session. The harness
never scrapes the terminal — the transcript comes from ``pipe-pane`` and
completion from the adapter hook — so this wrapper is deliberately small:
create, pipe, paste, poll, kill.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Protocol


class TmuxError(RuntimeError):
    """A tmux command failed."""


class Tmux(Protocol):
    """What the harness needs from tmux. Tests provide a fake."""

    def new_session(self, session: str, command: str, cwd: Path, env: dict[str, str]) -> None: ...

    def pipe_pane(self, session: str, capture_file: Path) -> None: ...

    def paste(self, session: str, text: str) -> None:
        """Inject ``text`` via buffer paste (never per-key send), then Enter."""
        ...

    def has_session(self, session: str) -> bool: ...

    def kill(self, session: str) -> None: ...


class RealTmux:
    def __init__(self, binary: str = "tmux"):
        self._binary = binary

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [self._binary, *args], capture_output=True, text=True, check=False, timeout=30
        )
        if check and proc.returncode != 0:
            raise TmuxError(f"tmux {args[0]} failed: {proc.stderr.strip()}")
        return proc

    def new_session(self, session: str, command: str, cwd: Path, env: dict[str, str]) -> None:
        args = ["new-session", "-d", "-s", session, "-c", str(cwd)]
        for key, value in sorted(env.items()):
            args += ["-e", f"{key}={value}"]
        args.append(command)
        self._run(args)

    def pipe_pane(self, session: str, capture_file: Path) -> None:
        self._run(["pipe-pane", "-t", session, "-o", f"exec cat >> '{capture_file}'"])

    def paste(self, session: str, text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".prompt", delete=False) as handle:
            handle.write(text)
            prompt_file = handle.name
        try:
            self._run(["load-buffer", "-b", "theozolith-prompt", prompt_file])
            self._run(["paste-buffer", "-d", "-b", "theozolith-prompt", "-t", session])
            self._run(["send-keys", "-t", session, "Enter"])
        finally:
            Path(prompt_file).unlink(missing_ok=True)

    def has_session(self, session: str) -> bool:
        return self._run(["has-session", "-t", session], check=False).returncode == 0

    def kill(self, session: str) -> None:
        self._run(["kill-session", "-t", session], check=False)
