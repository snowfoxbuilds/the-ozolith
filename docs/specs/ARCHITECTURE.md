Status: DRAFT

Last updated: 2026-08-23

# Architecture

Monorepo layout, components, and sync model for TheOzolith.

## Context

TheOzolith consolidates the coding pipeline, the cluster substrate, and the agent-knowledge machinery into one open-source monorepo. All personal content is data in one private config repo. Components must stay separable: a laptop-only user of the knowledge machinery never installs the cluster manager.

## Design

### Repository layout

```javascript
theozolith/
├── AGENTS.md            # project index    (repo-authored)
├── CONTEXT.md           # domain glossary  (repo-authored)
├── knowledge/           # agent-knowledge machinery: config format (skills, subagents, workflows), per-tool compilers (claude: AGENTS.md -> CLAUDE.md; codex: AGENTS.md verbatim, agents/codex/*.toml -> agents, agents/codex/*.md -> prompts, hooks/ verbatim), sync engine (~/.claude, ~/.codex)
├── worker/              # Worker + Reviewer actors: node-resident drivers, agent harness + run-container Dockerfiles, per-Agent adapters, first-party gate
├── control/            # Control Node: dashboard, heartbeat/command + run-event API, janitor jobs
├── nodedaemon/          # Node Daemon: uncontainerized host daemon (systemd unit) — heartbeat, command reconciliation, local builds, stack + driver supervision
├── deploy/              # compose files + .env.example (full config surface)
└── docs/
    ├── specs/           # repo-authored
    └── adr/             # repo-authored
```

### Components

- Every top-level component is independently installable. knowledge/ has no dependency on the cluster components and vice versa; the only coupling is that worker-type setup instructions may invoke the knowledge machinery to bake a Knowledge Source into a derived image (see [NODE-SUBSTRATE.md](NODE-SUBSTRATE.md)).

### Private side

- One private config repo holds all operator content: deployment declarations (Stacks, worker types, overlays, secret names — the Config Repo), agent knowledge (skills, subagents, workflows — the Config Repo's `knowledge/` tree; the separate knowledge repo, `knowledge_source`/`knowledge_pin`, and the KNOWLEDGE_GIT_TOKEN slot are retired), and custom driver code (`drivers/`, delivered to nodes as a hash-pinned config distribution). The "pure data" charter is narrowed for driver code: it is operator content that must not be published in a public checkout; machinery still never lives here.
- The former homeserver sunsets by reduction to this private repo once its workloads migrate onto the Node Daemon (migration deferred).

### Sync flows

- knowledge sync: private config repo -> local tool config dirs (~/.claude, etc.) via the knowledge machinery. One-way. The private repo is the source of truth. Two skill scopes: global skills live in the private repo and travel with the operator; project skills live in the target project's repo and travel with the project.
- config ingest: human Config Repo -> machine-owned pinned build (`theozolith config ingest`, the only write path — settings included). Ingest lints, resolves mechanical pins (per-tree knowledge content hashes; base tag -> digest, credentialed for private bases; the tailscale sha256 stays human-entered), compiles knowledge once per registered tool compiler into `knowledge/<name>/<tool>/` (at ingest; per-tool), and commits stamped with the source commit SHA. Control loads and config distribution serve only the pinned build; rollback is `git revert` there.
- Project docs have no sync flow: [AGENTS.md](../../AGENTS.md), [CONTEXT.md](../../CONTEXT.md), docs/specs/, and docs/adr/ are authored in the repo and reviewed in PRs. The Notion workspace is a read-only mirror; a Notion-side agent reads the repo for documentation and contributes changes via PR. (docs/adr/ has been repo-authored since 2026-07-30; the former `scripts/sync_notion.py` is deleted.)

## Relevant ADRs

| ADR | Decision |
| --- | --- |
| ADR-0006-config-repo-source-of-truth | The git-backed Config Repo is the deployment source of truth |
| ADR-0007-consolidate-into-theozolith | One public monorepo (TheOzolith); the homeserver reduces to a private config repo |
| ADR-0009-knowledge-format-compiler-sync | Knowledge-root layout, per-tool compiler mapping, manifest-based mirror sync |
| ADR-0033-repo-owned-adrs | ADRs are authored and reviewed in the repo |
| ADR-0042-custom-driver-code-in-config-repo | Custom driver code lives in the Config Repo `drivers/` and ships as a hash-pinned config distribution |
| ADR-0048-config-ingestion-pinned-build | `theozolith config ingest` compiles the human Config Repo into the machine-owned pinned build |
| ADR-0049-managed-registry-credentials | Private base digests resolve at ingest via a managed `registry:<host>` pull credential |
| ADR-0050-repo-owned-project-docs | All project docs are repo-authored and PR-reviewed; the Notion sync is retired |
| ADR-0052-codex-adapter-per-tool-knowledge | The codex adapter and a per-tool knowledge compiler registry |
