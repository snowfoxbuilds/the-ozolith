"""Gate pipeline: step contracts, findings, mechanical-fix policy.

Steps execute through an injected runner (in production: a harness job inside
the run container — agent-authored code never runs in the driver, ADR-0013).
These tests wire the runner to a local shell in the worktree.
"""

from __future__ import annotations

from pathlib import Path

from theozolith_worker.gate import run_gate
from theozolith_worker.shell import run_shell

CONFIG = Path(".theozolith") / "gate.toml"


def local_runner(worktree: Path):
    def runner(command: str, timeout: float):
        ok, _, output = run_shell(command, worktree, timeout)
        return ok, output

    return runner


def write_gate(tmp_path: Path, body: str) -> None:
    (tmp_path / CONFIG).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / CONFIG).write_text(body)


def test_unconfigured_repo_gets_info_finding_and_stays_clean(tmp_path):
    result = run_gate(tmp_path, local_runner(tmp_path))
    assert result.steps_run == []
    assert [f.severity for f in result.findings] == ["info"]
    assert result.clean


def test_steps_run_in_canonical_order_and_green_steps_leave_no_findings(tmp_path):
    write_gate(
        tmp_path,
        """
        [steps.lint]
        run = "true"
        [steps.test]
        run = "true"
        [steps.docs]
        run = "true"
        """,
    )
    result = run_gate(tmp_path, local_runner(tmp_path))
    assert result.steps_run == ["test", "docs", "lint"]
    assert result.findings == []
    assert result.clean


def test_failing_step_records_error_finding_with_output(tmp_path):
    write_gate(
        tmp_path,
        """
        [steps.test]
        run = "echo the assertion failed; exit 1"
        """,
    )
    result = run_gate(tmp_path, local_runner(tmp_path))
    assert not result.clean
    (finding,) = result.findings
    assert finding.step == "test" and finding.severity == "error"
    assert "the assertion failed" in finding.detail


def test_declared_mechanical_fix_is_applied_and_rechecked(tmp_path):
    # First run fails because marker is absent; the fix creates it; the
    # re-run passes. Only repos that declare `fix` opt into this.
    write_gate(
        tmp_path,
        """
        [steps.lint]
        run = "test -f marker"
        fix = "touch marker"
        """,
    )
    result = run_gate(tmp_path, local_runner(tmp_path))
    assert result.clean
    (finding,) = result.findings
    assert finding.fixed and finding.severity == "warning"
    assert (tmp_path / "marker").exists()


def test_fix_that_does_not_cure_leaves_an_error(tmp_path):
    write_gate(
        tmp_path,
        """
        [steps.lint]
        run = "false"
        fix = "true"
        """,
    )
    result = run_gate(tmp_path, local_runner(tmp_path))
    assert not result.clean
    (finding,) = result.findings
    assert finding.severity == "error" and not finding.fixed


def test_broken_config_is_an_error_finding_not_a_crash(tmp_path):
    write_gate(tmp_path, "steps = 'not a table'")
    result = run_gate(tmp_path, local_runner(tmp_path))
    assert not result.clean
    assert result.findings[0].step == "gate"


def test_gate_never_blocks_it_only_reports(tmp_path):
    """The best-effort contract: a red gate still returns a result the Run
    ships with; nothing raises."""
    write_gate(
        tmp_path,
        """
        [steps.test]
        run = "exit 1"
        [steps.docs]
        run = "exit 1"
        [steps.lint]
        run = "exit 1"
        """,
    )
    result = run_gate(tmp_path, local_runner(tmp_path))
    assert result.steps_run == ["test", "docs", "lint"]
    assert len(result.findings) == 3


def test_driver_sequences_commands_through_the_runner(tmp_path):
    """The gate itself never runs a subprocess: every command goes through
    the injected runner, in declaration order, fix between run and re-run."""
    write_gate(
        tmp_path,
        """
        [steps.test]
        run = "run-tests"
        [steps.lint]
        run = "run-lint"
        fix = "fix-lint"
        """,
    )
    calls: list[str] = []

    def recording_runner(command: str, timeout: float):
        calls.append(command)
        return command != "run-lint" or calls.count("run-lint") > 1, ""

    result = run_gate(tmp_path, recording_runner)

    assert calls == ["run-tests", "run-lint", "fix-lint", "run-lint"]
    assert result.clean
    (finding,) = result.findings
    assert finding.fixed
