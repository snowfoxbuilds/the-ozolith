"""First-party quality gate: test → docs → lint as driver-sequenced jobs.

Borrowed no-mistakes concepts, no no-mistakes dependency (ADR-0008). The gate
NEVER blocks PR creation: every outcome is a structured Finding, and
unresolved findings ride to the Reviewer in the Decisions Section.

Gate steps run agent-authored code, so they never execute in the credentialed
driver (ADR-0013): the driver reads the step *declarations* here and submits
each command through a ``StepRunner`` — in production, a harness job executed
inside the run container.

Step commands come from the target repo's ``.theozolith/gate.toml``:

    [steps.test]
    run = "uv run pytest -q"
    [steps.lint]
    run = "uv run ruff check ."
    fix = "uv run ruff check --fix ."

Mechanical-fix policy: a fix is auto-applied only when the target repo's own
gate config declares a ``fix`` command for the step — opting in is the repo
author's call, never the Worker's. After a fix the step's ``run`` is repeated;
only a now-green step counts as fixed.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(".theozolith") / "gate.toml"
STEP_ORDER = ("test", "docs", "lint")
DEFAULT_TIMEOUT_SECONDS = 900

# (command, timeout seconds) -> (ok, output). The driver wires this to a
# harness job inside the run container.
StepRunner = Callable[[str, float], tuple[bool, str]]


@dataclass(frozen=True)
class Finding:
    step: str
    severity: str  # "error" | "warning" | "info"
    summary: str
    detail: str = ""
    fixed: bool = False


@dataclass(frozen=True)
class StepSpec:
    name: str
    run: str
    fix: str | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS


@dataclass
class GateResult:
    steps_run: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)


class GateConfigError(RuntimeError):
    """The target repo's gate config is unreadable."""


def load_steps(worktree: Path) -> list[StepSpec]:
    """Ordered steps from ``.theozolith/gate.toml``; [] when unconfigured."""
    path = worktree / CONFIG_PATH
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GateConfigError(f"unreadable {CONFIG_PATH}: {exc}") from exc

    raw_steps = data.get("steps", {})
    if not isinstance(raw_steps, dict):
        raise GateConfigError(f"{CONFIG_PATH}: [steps.*] tables expected")
    ordered = [name for name in STEP_ORDER if name in raw_steps]
    ordered += sorted(name for name in raw_steps if name not in STEP_ORDER)

    specs = []
    for name in ordered:
        entry = raw_steps[name]
        if not isinstance(entry, dict) or not entry.get("run"):
            raise GateConfigError(f"{CONFIG_PATH}: steps.{name} needs a run command")
        specs.append(
            StepSpec(
                name=name,
                run=str(entry["run"]),
                fix=str(entry["fix"]) if entry.get("fix") else None,
                timeout=float(entry.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
            )
        )
    return specs


def run_gate(worktree: Path, runner: StepRunner) -> GateResult:
    """Sequence every configured step through ``runner``; never raise."""
    result = GateResult()
    try:
        steps = load_steps(worktree)
    except GateConfigError as exc:
        result.findings.append(Finding(step="gate", severity="error", summary=str(exc)))
        return result
    if not steps:
        result.findings.append(
            Finding(
                step="gate",
                severity="info",
                summary=f"no gate config ({CONFIG_PATH}); test/docs/lint not run",
            )
        )
        return result

    for spec in steps:
        result.steps_run.append(spec.name)
        ok, output = runner(spec.run, spec.timeout)
        if ok:
            continue
        if spec.fix:
            runner(spec.fix, spec.timeout)
            fixed_ok, fixed_output = runner(spec.run, spec.timeout)
            if fixed_ok:
                result.findings.append(
                    Finding(
                        step=spec.name,
                        severity="warning",
                        summary=f"{spec.name} failed; declared mechanical fix applied",
                        detail=output,
                        fixed=True,
                    )
                )
                continue
            output = fixed_output
        result.findings.append(
            Finding(
                step=spec.name,
                severity="error",
                summary=f"{spec.name} failed ({spec.run})",
                detail=output,
            )
        )
    return result
