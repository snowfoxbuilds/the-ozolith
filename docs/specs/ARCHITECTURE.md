Status: DRAFT

Last updated: 2026-08-23

# Architecture

Monorepo layout, components, and sync model for TheOzolith.

## Context

TheOzolith consolidates the coding pipeline, the cluster substrate, and the agent-knowledge machinery into one open-source monorepo (ADR-0007). All personal content is data in one private config repo. Components must stay separable: a laptop-only user of the knowledge machinery never installs the cluster manager.

## Design

### Repository layout

```javascript
theozolith/
├── AGENTS.md            # project index    (repo-authored; ADR-0050)
├── CONTEXT.md           # domain glossary  (repo-authored; ADR-0050)
├── knowledge/           # agent-knowledge machinery: config format (skills, subagents, workflows), per-tool compilers (claude: AGENTS.md -> CLAUDE.md; codex: AGENTS.md verbatim, agents/codex/ -> prompts; ADR-0052), sync engine (~/.claude, ~/.codex)
├── worker/              # Worker + Reviewer actors: node-resident drivers, agent harness + run-container Dockerfiles, per-Agent adapters, first-party gate
├── control/             # Control Node: dashboard, heartbeat/command + run-event API, janitor jobs
├── nodedaemon/          # Node Daemon: uncontainerized host daemon (systemd unit) — heartbeat, command reconciliation, local builds, stack + driver supervision
├── deploy/              # compose files + .env.example (full config surface)
└── docs/
    ├── specs/           # repo-authored (ADR-0050)
    └── adr/             # repo-authored (ADR-0033)
```

### Components

- Every top-level component is independently installable. knowledge/ has no dependency on the cluster components and vice versa; the only coupling is that worker-type setup instructions may invoke the knowledge machinery to bake a Knowledge Source into a derived image (see [NODE-SUBSTRATE.md](NODE-SUBSTRATE.md)).
### Private side

- One private config repo holds all operator content: deployment declarations (Stacks, worker types, overlays, secret names — the Config Repo of ADR-0006), agent knowledge (skills, subagents, workflows — the Config Repo's `knowledge/` tree since ADR-0048; the separate knowledge repo, `knowledge_source`/`knowledge_pin`, and the KNOWLEDGE_GIT_TOKEN slot are retired), and custom driver code (`drivers/`, delivered to nodes as a hash-pinned config distribution; ADR-0042). Pure operator content, no machinery — ADR-0042 narrows the former "pure data" charter: driver code is operator content; machinery still never lives here.
- The former homeserver sunsets by reduction to this private repo once its workloads migrate onto the Node Daemon (migration deferred).
### Sync flows

- knowledge sync: private config repo -> local tool config dirs (~/.claude, etc.) via the knowledge machinery. One-way. The private repo is the source of truth. Two skill scopes: global skills live in the private repo and travel with the operator; project skills live in the target project's repo and travel with the project.
- config ingest: human Config Repo -> machine-owned pinned build (`theozolith config ingest`, the only write path — settings included; ADR-0048). Ingest lints, resolves mechanical pins (per-tree knowledge content hashes; base tag -> digest, credentialed for private bases per ADR-0049; the tailscale sha256 stays human-entered), compiles knowledge once per registered tool compiler into `knowledge/<name>/<tool>/` (ADR-0009 at ingest; per-tool since ADR-0052), and commits stamped with the source commit SHA. Control loads and config distribution serve only the pinned build; rollback is `git revert` there.
- Project docs have no sync flow (ADR-0050): [AGENTS.md](../../AGENTS.md), [CONTEXT.md](../../CONTEXT.md), docs/specs/, and docs/adr/ are authored in the repo and reviewed in PRs. The Notion workspace is a read-only mirror; a Notion-side agent reads the repo for documentation and contributes changes via PR. (docs/adr/ has been repo-authored since 2026-07-30, ADR-0033; the former `scripts/sync_notion.py` is deleted.)
## Decision history

Settled rulings are integrated into the sections above; decision records live in docs/adr/ (repo-authored since ADR-0033). Specs no longer carry a grilling log (ruled 2026-08-09).
