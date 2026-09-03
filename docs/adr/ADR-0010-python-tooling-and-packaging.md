Status: ACCEPTED

Date: 2026-07-14

# ADR-0010: Monorepo Python tooling and component packaging

## Context

M1 delegates the Python tooling choices (package manager, lint/test stack), to be applied uniformly across the monorepo. [ARCHITECTURE.md](../specs/ARCHITECTURE.md) requires every top-level component to be independently installable, with `knowledge/` free of any dependency on cluster components. M1 also needs a home for the repo bootstrap tool (label vocabulary + issue forms), which the milestone ships but no spec assigns to a directory.

## Decision

- **uv** as package manager: one workspace (`[tool.uv.workspace]` at the repo root) spanning `knowledge/`, `worker/`, `control/`, `nodedaemon/`; a committed `uv.lock`; `uv sync --all-packages` for development. Everything remains standard `pyproject.toml`, so plain `pip install ./knowledge` works for consumers without uv. (Authored pre-rename against `nodeagent/`; ADR-0013 renames the component to `nodedaemon/`.)
- **Per-component packaging**: each component is its own distribution (`theozolith-knowledge`, `theozolith-worker`, `theozolith-control`, `theozolith-nodedaemon`) with a `src/` layout and the **hatchling** build backend. Independent installability is enforced in CI by a job that installs `knowledge/` alone in a fresh venv and runs its tests there.
- **Zero runtime dependencies** for the V1 components (stdlib only, including the GitHub REST calls in the bootstrap tool); test-only extras (pytest, PyYAML) live in dependency groups.
- **ruff** for linting and formatting, **pytest** for tests, configured once at the workspace root. CI runs lint, the full suite, and the isolation job on every PR.
- **Bootstrap tool placement**: in `worker/` (`theozolith-bootstrap`), because it is pipeline substrate that every adopter needs (substrate admission rule) and the M2 Worker consumes the vocabulary it applies. The Worker/Reviewer *actors* stay out of M1; `worker/` ships only this CLI. `scripts/` stays reserved for project tooling that is not product (`sync_notion.py`).

## Consequences

- **Positive**: one lockfile and one command for dev setup; component boundaries are packaging boundaries, so the laptop-only path (ADR-0007) is testable; stdlib-only runtime keeps images and laptop installs dependency-free; ruff subsumes formatter + linter in one tool.
- **Negative**: uv is a hard dev-time assumption (CI and docs), though consumers can ignore it; hatchling and ruff pin the project to the modern-Python toolchain (>= 3.11).
- **Neutral**: `control/` and `nodedaemon/` are installable empty stubs until their milestones; the workspace root package is virtual (`package = false`) and never published.

## Alternatives Considered

- **Poetry / pip-tools / plain requirements.txt**: rejected — no workspace concept for a multi-package monorepo (Poetry), or no lock-plus-install story matching per-component isolation (requirements files).
- **One repo-wide package with extras** (`theozolith[knowledge]`): rejected — extras don't prevent cross-component imports; separable installables are the ADR-0007 requirement.
- **Bootstrap tool in ****`scripts/`**: rejected — scripts/ is project tooling, not product; the bootstrap tool must be installable by adopters.
- **A new top-level ****`pipeline/`**** component for the bootstrap tool**: rejected — [ARCHITECTURE.md](../specs/ARCHITECTURE.md) fixes the component list; the pipeline component is `worker/`.

## Relevant PRs

- #1 — original decision, under the M1 delegated-decisions mandate.
