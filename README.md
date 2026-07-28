# TheOzolith

An open-source agent-orchestration monorepo with three separable concerns:

- **Agent-knowledge machinery** (`knowledge/`) — a tool-agnostic config format for skills,
  subagents, and workflows; per-tool compilers (`AGENTS.md` → `CLAUDE.md`, skill placement);
  a one-way sync engine into local tool config dirs; and a bake CLI for installing a pinned
  Knowledge Source into a container image at build time.
- **Agentic coding pipeline** (`worker/`) — staged autonomous development on GitHub issues
  and PRs: Implementer and Reviewer drivers, per-Run containers with the agent harness as
  PID 1 running the agent **headless** (one-shot invocation, exit-is-completion, the
  structured output stream as the evidence transcript — ADR-0019), the Claim Protocol
  (Control Node write-through dispatch, ADR-0017), a first-party gate, best-effort PRs
  with Decisions Sections (including advisory `process_issues`), and verdict-file review
  rounds. Plus the repo bootstrap tool from M1.
- **Cluster substrate** (`control/`, `nodedaemon/`, `deploy/`) — Control Node, Node Daemons,
  and Stacks (ADR-0015). The Control Node serves the heartbeat/command channel, claim
  dispatch, typed events (Run progress and `theozolith.error` summaries), the zombie-claim
  janitor, the encrypted node-scoped secret store, the dashboard + web terminal (the
  Flight Deck is the terminal's primary target; run containers are never attachable), and
  the two product-update paths (`theozolith update` / `theozolith build`, ADR-0015 as
  amended). The Node Daemon registers a box as a Container-Host: declarative Stacks
  (container + supervised driver processes, kill-the-tree), local derived-image builds,
  tmpfs secrets, orphan reaping. `deploy/` carries the installer, the control compose
  file, and a starter Config Repo (including a Flight Deck example); the daemon-less
  one-box dev shape runs `theozolith-control serve` beside the drivers (claims dispatch
  through the Control Node — ADR-0017).

Every top-level component is independently installable. A laptop-only user of the knowledge
machinery never installs the cluster manager.

The project is governed by anchor docs authored in Notion and synced one-way into this repo
([AGENTS.md](AGENTS.md) index, [CONTEXT.md](CONTEXT.md) glossary, [docs/specs/](docs/specs),
[docs/adr/](docs/adr)); see ADR-0001. Synced docs are never hand-edited here.

## Quick start (knowledge machinery)

```sh
uv pip install ./knowledge          # or: pip install ./knowledge

# Sync a knowledge repo into your global Claude config dir
theozolith-knowledge sync --source /path/to/knowledge-repo --scope global

# Generate a project's CLAUDE.md (+ .claude assets) from its AGENTS.md
theozolith-knowledge sync --source . --scope project --target .

# Inside a Dockerfile: bake a pinned Knowledge Source into the image
theozolith-knowledge bake --source https://example.com/knowledge.git --pin <commit> --target /root/.claude
```

## Setup (full substrate)

One Control Node plus a Node Daemon on every physical box that should run Stacks.
The deployment footprint per box is docker + the TheOzolith package +
`theozolith-control init` output (the deletion test as restated by ADR-0023 — there
is no `.env`). Operations, product updates, backup/recovery, the daemon-less one-box
dev shape, and cleanup are covered in [deploy/README.md](deploy/README.md).

Prerequisites:

- **Control Node**: any box with docker + the compose plugin (the Pi in the reference
  deployment) and a checkout of this repo.
- **Every physical node**: systemd Linux, docker with the compose plugin, python3 ≥ 3.11.
- **For the coding pipeline**: a target GitHub repo and machine-user PATs for the
  Worker, the Reviewer, and the Control Node — three distinct GitHub identities, so
  no self-grading by construction (ADR-0008, ADR-0017).

### 1. Initialize the Control Node

From the repo checkout on the Control Node box:

```sh
docker compose -f deploy/compose/control.yml build
docker compose -f deploy/compose/control.yml run --rm control init
docker compose -f deploy/compose/control.yml up -d
```

`init` (ADR-0023) composes the whole first run: master key → the deployment's one
public origin (`https://<128-bit-random-slug>.theozolith.internal`; `--base-domain`
to change) → per-deployment CA + server certificate with the box's IP in the SAN →
admin password prompt (only its scrypt hash is stored) → the **operator handoff**:
the dashboard URL, the exact DNS/hosts line, the CA download URL, and per-OS trust
one-liners. The two irreducibly manual actions — the trusted-network-only DNS record
and CA trust per operator device — are copy-paste from that printout. All state
lands under `~/.theozolith/` on the host, partitioned by durability class
(ADR-0024); backup is a copy of that folder minus `cache/`.

### 2. Provision the physical nodes — one paste each

```sh
theozolith join-token create     # on the Control Node (or the dashboard)
```

Paste the printed line on the box. For a fresh box it fetches the installer over
GitHub release HTTPS and hands off to `theozolith-nodedaemon provision` — which
verifies the CA against the join string's pinned fingerprint **before transmitting
anything**, exchanges the short-lived single-use join token for the node's own
non-expiring per-node token, persists everything under `/var/lib/theozolith`, and
enables the systemd unit (`KillMode=control-group`: every TheOzolith process on the
node dies with the daemon). Provisioning **is** registration (ADR-0023): the node
exists the moment the exchange succeeds and heartbeats within the interval (60 s
default). Join tokens default to 1 hour / single use; `--ttl`/`--uses` widen them
for batches, `theozolith join-token revoke` is the backstop.

### 3. Declare desired state (Config Repo)

The git-backed Config Repo at `~/.theozolith/configs` on the Control Node (ADR-0006)
is the deployment's source of truth: Stacks, derived images, and the product version
pin. `deploy/configs-example/` is a complete starter — copy it in and adjust the
`node = "..."` placements to your node names. The Implementer/Reviewer drivers are
process-kind Stacks; `control` and the Flight Deck are container Stacks. Desired
state distributes over the heartbeat channel; nodes cache it for degraded mode.

### 4. Enter secrets

Stacks reference secrets by name (e.g. `github-worker`); enter each value once:

```sh
theozolith-control secret set github-worker    # on the Control Node; or the dashboard
```

Secrets are encrypted at rest on the Control Node, pulled node-scoped over TLS (only
nodes whose Stacks reference a name may pull it), and materialized to tmpfs — never
on node disk.

### 5. Bootstrap the target repo and verify

```sh
GITHUB_TOKEN=... theozolith-bootstrap --repo owner/name   # labels + issue forms, one-time
theozolith-control status                                 # fleet state
```

The dashboard (at the minted origin, behind the admin password) shows the fleet —
including unregistered nodes awaiting a join-string paste — Run progress,
`theozolith.error` summaries, secret entry, tier-2 settings (committed to
`control.toml` in the Config Repo), join tokens, and the web terminal.

## Development

```sh
uv sync --all-packages   # workspace venv with all components + dev tools
uv run pytest            # all tests
uv run ruff check .      # lint
uv run ruff format --check .
```
