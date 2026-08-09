Status: DRAFT

Last updated: 2026-08-09

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
├── worker/              # Worker + Reviewer actors: node-resident drivers, agent harness + run-container Dockerfiles, per-Agent adapters, first-party gate
├── control/             # Control Node: dashboard, heartbeat/command + run-event API, janitor jobs
├── nodedaemon/          # Node Daemon: uncontainerized host daemon (systemd unit) — heartbeat, command reconciliation, local builds, stack + driver supervision
├── deploy/              # compose files + .env.example (full config surface)
├── scripts/
│   └── sync_notion.py   # Notion -> repo docs (project tooling, not product)
└── docs/
    ├── specs/           # <- Notion Specs/
    └── adr/             # repo-authored (ADR-0033); frozen mirror in Notion
```

### Components

- Every top-level component is independently installable. knowledge/ has no dependency on the cluster components and vice versa; the only coupling is that worker-type setup instructions may invoke the knowledge machinery to bake a Knowledge Source into a derived image (see [NODE-SUBSTRATE.md](http://node-substrate.md/)).
### Private side

- One private config repo holds all operator content: deployment declarations (Stacks, worker types, overlays, secret names — the Config Repo of ADR-0006), agent knowledge (skills, subagents, workflows), and custom driver code (`drivers/`, delivered to nodes as a hash-pinned config distribution; ADR-0042). Pure operator content, no machinery — ADR-0042 narrows the former "pure data" charter: driver code is operator content; machinery still never lives here.
- The former homeserver sunsets by reduction to this private repo once its workloads migrate onto the Node Daemon (migration deferred).
### Sync flows

- knowledge sync: private config repo -> local tool config dirs (~/.claude, etc.) via the knowledge machinery. One-way. The private repo is the source of truth. Two skill scopes: global skills live in the private repo and travel with the operator; project skills live in the target project's repo and travel with the project.
- sync_notion: Notion project docs -> repo ([CONTEXT.md](http://context.md/), [AGENTS.md](http://agents.md/), docs/specs/). One-way. Notion is the source of truth. docs/adr/ is repo-authored since 2026-07-30 and no longer exported; Notion's ADR pages are a frozen historical mirror (ADR-0033).
## Decision history

Settled rulings are integrated into the sections above; decision records live in docs/adr/ (repo-authored since ADR-0033). Specs no longer carry a grilling log (ruled 2026-08-09).
