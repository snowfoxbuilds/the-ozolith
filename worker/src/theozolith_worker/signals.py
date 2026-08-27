"""Mechanical diff signals: computed evidence for the Reviewer.

Diff size, files touched, dependency-manifest changes, and sensitive paths
are computed here and fed to the Reviewer as evidence — they inform the
grades but are never an independent grader (AGENTIC-CODING-PIPELINE.md,
two-layer risk assessment). Since ADR-0053's workspace parity the inputs
are the driver's own git reads against the PR's base commit (``--numstat``
and ``--name-status``), never the PR-files API: the workspace is complete,
so the "file without a patch" concept died with the truncated diff blob.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEPENDENCY_MANIFESTS = {
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "setup.py",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
}

SENSITIVE_FRAGMENTS = (
    "auth",
    "secret",
    "token",
    "credential",
    "migration",
    ".github/workflows",
    "dockerfile",
    "docker-compose",
)


@dataclass
class DiffSignals:
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    dependency_files: list[str] = field(default_factory=list)
    sensitive_files: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"- files changed: {self.files_changed} (+{self.additions} / -{self.deletions})",
            "- dependency manifests touched: " + (", ".join(self.dependency_files) or "none"),
            "- sensitive paths touched: " + (", ".join(self.sensitive_files) or "none"),
        ]
        return "\n".join(lines)


def _classify(signals: DiffSignals, path: str) -> None:
    name = path.rsplit("/", 1)[-1]
    if name in DEPENDENCY_MANIFESTS and path not in signals.dependency_files:
        signals.dependency_files.append(path)
    lowered = path.lower()
    if (
        any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS)
        and path not in signals.sensitive_files
    ):
        signals.sensitive_files.append(path)


def signals_from_git(numstat_lines: list[str], name_status_lines: list[str]) -> DiffSignals:
    """Signals from the driver's own diff reads (ADR-0053): ``numstat_lines``
    carry per-file adds/deletes ("-" for binary entries — counted as zero,
    the file still counts as changed); ``name_status_lines`` carry status
    plus path(s) — renames and copies carry both endpoints, and each is
    classified (a file moved OUT of a sensitive path matters as much as one
    moved in)."""
    signals = DiffSignals()
    for line in numstat_lines:
        if not line.strip():
            continue
        added, deleted, _path = line.split("\t", 2)
        if added != "-":
            signals.additions += int(added)
        if deleted != "-":
            signals.deletions += int(deleted)
    for line in name_status_lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        signals.files_changed += 1
        for path in parts[1:]:
            _classify(signals, path)
    return signals
