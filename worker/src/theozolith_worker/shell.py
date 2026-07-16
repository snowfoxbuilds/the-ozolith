"""One shell-command runner, shared by the harness's job loop and tests.

Agent-authored code (gate steps) only ever runs through this helper on the
credential-free side of the container boundary — the driver sequences jobs
but never executes them (ADR-0013).
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

OUTPUT_LIMIT = 4000


def run_shell(
    command: str,
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, int, str]:
    """Run ``command`` in a shell; (ok, exit code, last OUTPUT_LIMIT chars)."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired:
        return False, -1, f"timed out after {timeout:.0f}s"
    output = (proc.stdout + proc.stderr)[-OUTPUT_LIMIT:]
    return proc.returncode == 0, proc.returncode, output
