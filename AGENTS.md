# TheOzolith

TheOzolith is an open-source agent-orchestration monorepo with three separable concerns: agent-knowledge machinery (skills, subagents, workflows, per-tool compilation and sync), a staged autonomous coding pipeline (planning, execution, review, monitoring) built on GitHub issues and PRs, and the cluster substrate that runs it (Control Node, Node Daemons, Stacks). It consolidates the former snow-maker and, by reduction, the former homeserver (ADR-0007); all personal content lives in one private config repo. Project docs (this index, the glossary, specs, and ADRs) are repo-authored (ADR-0050); the Notion workspace is a read-only mirror.

## Conventions

- Personal agent configs are data in the private config repo, synced one-way into local tool config dirs (e.g. `~/.claude`) by the knowledge machinery. Never hand-edit the local config.
- Project docs (this index, `CONTEXT.md`, `docs/specs/`, `docs/adr/`) are authored in the repo and reviewed in the PR that carries them; a PR whose ADR changes a spec's content updates the spec in the same PR (ADR-0050). Notion no longer syncs into the repo — a Notion-side agent reads the repo and contributes via PR.
- Monorepo with separable, independently installable components: `knowledge/`, `worker/`, `control/`, `nodedaemon/`, `deploy/`. A laptop-only knowledge user never installs the cluster manager.

## Domain Language

See [CONTEXT.md](CONTEXT.md) for the project's domain glossary. All specs, code, and agent instructions use these terms exactly.

## Specs

Each spec's own Relevant ADRs appendix reaches the decisions behind it; the amendment and supersession graph lives in the ADRs (`docs/adr/`), never in an index here.

| File | Summary |
| --- | --- |
| docs/specs/DIRECTION.md | Motivation, core theses, and non-goals — the direction the design serves |
| docs/specs/ARCHITECTURE.md | Monorepo layout, components, and sync model |
| docs/specs/AGENTIC-CODING-PIPELINE.md | Staged autonomous development: issues, gate, human merge |
| docs/specs/NODE-SUBSTRATE.md | Cluster substrate: Control Node, Node Daemon, Config Repo, secrets, extension points |
| docs/specs/BENCH-CONTRACT.md | Bench export surface: Candidate Bundles, identity spec + verifier, standalone build, and the Run Contract for implementer and review benchmark modes |
