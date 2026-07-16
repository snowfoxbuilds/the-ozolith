"""Mechanical diff signals: computed evidence for the Reviewer.

Diff size, files touched, dependency-manifest changes, and sensitive paths
are computed here and fed to the Reviewer as evidence — they inform the
grades but are never an independent grader (AGENTIC-CODING-PIPELINE.md,
two-layer risk assessment).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from theozolith_worker.githubapi import PrFile

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


def compute_signals(files: list[PrFile]) -> DiffSignals:
    signals = DiffSignals(files_changed=len(files))
    for entry in files:
        signals.additions += entry.additions
        signals.deletions += entry.deletions
        name = entry.path.rsplit("/", 1)[-1]
        if name in DEPENDENCY_MANIFESTS:
            signals.dependency_files.append(entry.path)
        lowered = entry.path.lower()
        if any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS):
            signals.sensitive_files.append(entry.path)
    return signals
