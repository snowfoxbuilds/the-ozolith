Status: DRAFT

Last updated: 2026-07-14

# Architecture

Monorepo layout, components, and sync model for TheOzolith.

## Context

TheOzolith consolidates the coding pipeline, the cluster substrate, and the agent-knowledge machinery into one open-source monorepo (ADR-0007). All personal content is data in one private config repo. Components must stay separable: a laptop-only user of the knowledge machinery never installs the cluster manager.

## Design

### Repository layout

```javascript
theozolith/
├── AGENTS.md            # project index    (synced from Notion)
├── CONTEXT.md           # domain glossary  (synced from Notion)
├── knowledge/           # agent-knowledge machinery: config format (skills, subagents, workflows), per-tool compilers (AGENTS.md -> CLAUDE.md, skill placement), sync engine (~/.claude etc.)
├── worker/              # Worker + Reviewer actors: Dockerfiles, poll-claim-run entrypoint, per-Agent adapters, first-party gate
├── control/             # Control Node: dashboard, heartbeat/command + run-event API, janitor jobs
├── nodeagent/           # uncontainerized host daemon: heartbeat, command reconciliation, local builds, stack lifecycle
├── deploy/              # compose files + .env.example (full config surface)
├── scripts/
│   └── sync_notion.py   # Notion -> repo docs (project tooling, not product)
└── docs/
    ├── specs/           # <- Notion Specs/
    └── adr/             # <- Notion ADRs/
```

### Components

- Every top-level component is independently installable. knowledge/ has no dependency on the cluster components and vice versa; the only coupling is that worker-type setup instructions may invoke the knowledge machinery to bake a Knowledge Source into a derived image (see [NODE-SUBSTRATE.md](http://node-substrate.md/)).
### Private side

- One private config repo holds all operator content: deployment declarations (Stacks, worker types, overlays, secret names — the Config Repo of ADR-0006) plus agent knowledge (skills, subagents, workflows). Pure data, no machinery.
- The former homeserver sunsets by reduction to this private repo once its workloads migrate onto the Node Agent (migration deferred).
### Sync flows

- knowledge sync: private config repo -> local tool config dirs (~/.claude, etc.) via the knowledge machinery. One-way. The private repo is the source of truth.
- sync_notion: Notion project docs -> repo ([CONTEXT.md](http://context.md/), [AGENTS.md](http://agents.md/), docs/specs/, docs/adr/). One-way. Notion is the source of truth.
## Decisions

- **Grilling 2026-06-08**: source-of-truth split -> the authoring side owns content; sync targets are never hand-edited. See ADR-0001 (amended by ADR-0007: personal configs are authored in the private config repo). [SETTLED]
- **Grilling 2026-07-14**: one public monorepo (TheOzolith) with separable components; snow-maker renamed and absorbed; homeserver sunsets by reduction. See ADR-0007. [SETTLED]
- **Grilling 2026-07-14**: all machinery is public-side (knowledge/); all personal content is data in one private config repo. See ADR-0007. [SETTLED]
- **Two skill scopes**: global skills (private repo, travel with the operator) vs project skills (target project's repo, travel with the project). [SETTLED]
