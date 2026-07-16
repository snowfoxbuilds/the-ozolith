# TheOzolith

TheOzolith is an open-source agent-orchestration monorepo with three separable concerns: agent-knowledge machinery (skills, subagents, workflows, per-tool compilation and sync), a staged autonomous coding pipeline (planning, execution, review, monitoring) built on GitHub issues and PRs, and the cluster substrate that runs it (Control Node, Node Daemons, Stacks). It consolidates the former snow-maker and, by reduction, the former homeserver (ADR-0007); all personal content lives in one private config repo. Project docs (this index, the glossary, specs, and ADRs) are authored in Notion and synced down into the repo.

## Conventions

- Personal agent configs are data in the private config repo, synced one-way into local tool config dirs (e.g. `~/.claude`) by the knowledge machinery. Never hand-edit the local config.
- Project docs are authored in Notion and synced one-way into the repo. Never hand-edit synced docs in the repo.
- Monorepo with separable, independently installable components: `knowledge/`, `worker/`, `control/`, `nodedaemon/`, `deploy/`. A laptop-only knowledge user never installs the cluster manager.
## Domain Language

See [CONTEXT.md](http://context.md/) for the project's domain glossary. All specs, code, and agent instructions use these terms exactly.

## Specs

| File | Summary |
| --- | --- |
| docs/specs/[ARCHITECTURE.md](http://architecture.md/) | Monorepo layout, components, and sync model |
| docs/specs/[AGENTIC-CODING-PIPELINE.md](http://agentic-coding-pipeline.md/) | Staged autonomous development: issues, gate, human merge |
| docs/specs/[NODE-SUBSTRATE.md](http://node-substrate.md/) | Cluster substrate: Control Node, Node Daemon, Config Repo, secrets, extension points |

## ADRs

| File | Summary |
| --- | --- |
| docs/adr/[ADR-0001-config-vs-doc-source-of-truth.md](http://adr-0001-config-vs-doc-source-of-truth.md/) | Repo owns configs, Notion owns docs; one-way sync |
| docs/adr/[ADR-0002-advisory-control-node.md](http://adr-0002-advisory-control-node.md/) | GitHub owns coordination state; Control Node is advisory |
| docs/adr/[ADR-0003-handoff-doc-over-session-restore.md](http://adr-0003-handoff-doc-over-session-restore.md/) | Externalized handoff schema, not vendor sessions (amended by ADR-0008: schema lives in the PR Decisions Section) |
| docs/adr/[ADR-0004-private-public-repo-boundary.md](http://adr-0004-private-public-repo-boundary.md/) | snow-maker self-contained; homeserver hosts pinned releases |
| docs/adr/[ADR-0005-theozolith-owns-node-substrate.md](http://adr-0005-theozolith-owns-node-substrate.md/) | The product owns the node substrate; the private deployment extends it |
| docs/adr/[ADR-0006-config-repo-source-of-truth.md](http://adr-0006-config-repo-source-of-truth.md/) | Git-backed Config Repo is the deployment source of truth |
| docs/adr/[ADR-0007-consolidate-into-theozolith.md](http://adr-0007-consolidate-into-theozolith.md/) | One public monorepo (TheOzolith); snow-maker renamed, homeserver reduces to a private config repo |
| docs/adr/[ADR-0008-reviewer-owned-state-best-effort-pr.md](http://adr-0008-reviewer-owned-state-best-effort-pr.md/) | Best-effort PRs with Decisions Sections; a separate Reviewer actor owns all post-PR state and drives review rounds |
| docs/adr/ADR-0009-knowledge-format-compiler-sync.md | Knowledge-root layout, Claude compiler mapping, manifest-based mirror sync (source wins on hand-edits) |
| docs/adr/ADR-0010-python-tooling-and-packaging.md | uv workspace, per-component hatchling packages, ruff + pytest; bootstrap CLI lives in worker/ |
| docs/adr/[ADR-0013-node-resident-drivers-per-run-containers.md](http://adr-0013-node-resident-drivers-per-run-containers.md/) | Actors split into node-resident drivers (Node Daemon children) and credential-free agent harnesses; Runs execute in ephemeral, attachable containers |
| docs/adr/ADR-0014-m2-execution-contracts.md | M2 execution contracts: job directory, harness, gate-as-jobs, strict one-strike verdict validation, run outcomes (empty PR / failed-Run retry), evidence bundles |
