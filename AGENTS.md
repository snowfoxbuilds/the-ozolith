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
| docs/adr/ADR-0014-m2-execution-contracts.md | M2 execution contracts: job directory, harness, gate-as-jobs, strict one-strike verdict validation, run outcomes (empty PR / failed-Run retry), evidence bundles (retry path amended by ADR-0016) |
| docs/adr/ADR-0015-m3-substrate-contracts.md | M3 substrate contracts: control-plane API, TOML Config Repo, Fernet secret store, liveness/janitor defaults, daemon packaging; records control/'s runtime-dependency amendment of ADR-0010 (amended by ADR-0016/0017) |
| docs/adr/ADR-0016-failure-handling.md | Failure handling: local retry with uniform budget, failed + needs_human label, control-side node quarantine, progress telemetry, cache-not-archive control DB, boot-time evidence sweep, evidence-first zombie escalation |
| docs/adr/ADR-0017-control-node-claim-dispatch.md | Control Node is the single writer of claim creation on GitHub (write-through dispatch); GitHub stays the source of truth; supersedes ADR-0002 in part |
| docs/adr/ADR-0018-m4-dashboard-terminal-and-dispatch-contracts.md | M4 contracts: HTMX dashboard, admin session, PTY bridge with audit log, quarantine release, dispatch/activation/telemetry conformance; amends ADR-0015's API |
| docs/adr/ADR-0019-headless-runs-and-flight-deck.md | Implementer and Reviewer Runs execute headless (adapter one-shot mode); interactivity lives only in the Flight Deck — a credentialed, human-driven agent container with its own no-merge machine identity |
| docs/adr/ADR-0020-worker-taxonomy-and-naming.md | Worker is the base abstraction for automated actors; Implementer, Reviewer, and Initializer are worker types expressed via inheritance; Pilot renamed Flight Deck; Planner reserved |
| docs/adr/ADR-0021-initializer-worker-type.md | Initializer contract: dispatch discovery-only (no claim write), one analysis comment updated in place (issue body never edited), applies the initialized label; deferred past the current testing scope |
| docs/adr/ADR-0022-control-web-hardening.md | M5 control web hardening: structured attach argv, server-derived targets, explicit randomized public origin, exact Host/Origin enforcement, bounded PTY resources, best-effort evidence parking, and per-Stack jobs directories |
| docs/adr/ADR-0023-first-run-setup-and-node-provisioning.md | First-run setup: unified `theozolith-control init`, four-tier settings surface (control.toml in the Config Repo, .env deleted), admin password + stateful sessions, join-string node provisioning with machine-verified CA fingerprint, per-node tokens (provisioning is registration), dedicated plaintext bootstrap listener, Control-Node-only human CLI with `python3 build.py` source bootstrap; amends ADR-0015, supersedes ADR-0018's session section (bootstrap amended by ADR-0041: the shim owns the environment — the managed invocation is `sudo python3 build.py`) |
| docs/adr/ADR-0024-control-node-storage-partition-and-recovery.md | Control Node storage partition by durability class (configs/ git repo, secrets/ never in git, cache/ deletable, logs/), store.db/cache.db split, local-copy backup doctrine (GitHub is never a full backup), and `theozolith-control recover` for restore-and-reconnect recovery |
| docs/adr/ADR-0025-control-plane-supervision.md | The Control Node always runs as its own systemd unit on every deployment shape; the built-in control Stack kind is deleted — the substrate never supervises its own control plane |
| docs/adr/ADR-0041-managed-bootstrap-environment.md | build.py owns the bootstrap environment: `sudo python3 build.py` creates-or-reuses the /opt/theozolith venv, re-executes inside it, installs the built wheels, and atomically publishes the theozolith / theozolith-control / theozolith-nodedaemon links into /usr/local/bin (foreign paths refused, never overwritten); `--venv PATH` is the unmanaged, unprivileged escape hatch with no links; amends ADR-0023/0030 |
| docs/adr/ADR-0042-custom-driver-code-in-config-repo.md | Custom drivers live in the Config Repo `drivers/` (git-native only; the web UI refuses to touch it); defaults are referenced, never copied (`driver = "builtin:<name>"` / `"drivers/<name>"`); hash-pinned config distribution with advisory version skew — off-hash nodes are dispatch-ineligible; amends ADR-0007/0013 |
| docs/adr/ADR-0043-flight-deck-knowledge-authoring.md | Two-class `~/.claude` split: runtime state per Flight Deck, knowledge via one shared writable clone per worker type per node (symlink carve-out is Flight-Deck-only); promote = commit/push + re-pin + rebuild; git is the cross-node transport — no sync daemon |
| docs/adr/ADR-0044-worker-type-def-owns-customization.md | The worker-type definition owns the customization tuple (base image, Knowledge Source, driver reference, Agent adapter, workspace, secret names); the harness is immutable plumbing — the Agent adapter is the variable; Stacks stay thin (worker type + placement + desired state) |
| docs/adr/ADR-0045-model-effort-baked-into-derived-image.md | Model and reasoning effort are typed worker-type-definition fields materialized by the compiler into the adapter's native config at derived-image build time as ENFORCEMENT, not defaults (Claude: availableModels allowlist + managed-env effort pin; every selection surface constrained, live-verified); mappable means enforceable — unenforceable values and driverless effort fail the load/build, as does a pre-enforcement in-image CLI; the instruction hash covers the materialized config; evidence reports the reconciled observed model; amends ADR-0044/0014 |
