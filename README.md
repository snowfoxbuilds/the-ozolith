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
The deployment footprint per box is docker + the TheOzolith package + a `.env`
(the deletion test, NODE-SUBSTRATE.md). Operations, product updates, the daemon-less
one-box dev shape, and cleanup are covered in [deploy/README.md](deploy/README.md).

Prerequisites:

- **Control Node**: any box with docker + the compose plugin (the Pi in the reference
  deployment) and a checkout of this repo.
- **Every physical node**: systemd Linux, docker with the compose plugin, python3 ≥ 3.11.
- **For the coding pipeline**: a target GitHub repo and machine-user PATs for the
  Worker, the Reviewer, and the Control Node — three distinct GitHub identities, so
  no self-grading by construction (ADR-0008, ADR-0017).

### 1. Set up and provision the Control Node

From the repo checkout on the Control Node box:

```sh
cp deploy/.env.example .env    # set THEOZOLITH_NODE_TOKEN + THEOZOLITH_ADMIN_TOKEN;
                               # for the pipeline also THEOZOLITH_REPO + CONTROL_GITHUB_TOKEN

docker compose -f deploy/compose/control.yml build

# One-time provisioning, BEFORE the service is healthy (until both one-shots
# have run, `serve` exits and the container restarts — that is expected):
docker compose -f deploy/compose/control.yml run --rm control origin-init
docker compose -f deploy/compose/control.yml run --rm control tls-init

docker compose --env-file .env -f deploy/compose/control.yml up -d
docker compose -f deploy/compose/control.yml cp control:/data/tls/ca.pem .
```

- `origin-init` mints the deployment's one public origin —
  `https://<128-bit-random-slug>.theozolith.internal` by default (`--base-domain` to
  change). Give its hostname a **trusted-network-only** DNS record (or hosts entries)
  pointing at the Control Node, which must have no public ingress path; browsers and
  nodes must use exactly this origin.
- `tls-init` mints the self-signed CA and a certificate covering the origin's hostname
  (`--host` adds extra names). TLS is mandatory: secrets transit this channel.
- Keep the copied `ca.pem` at hand — every node and driver pins it (step 2).
- Back up and guard the `control-data` volume: it holds the SQLite DB, the TLS
  material, and the master key protecting every secret.

### 2. Provision the physical nodes

On every box that should run Stacks, with the `ca.pem` from step 1:

```sh
sudo THEOZOLITH_NODE_TOKEN=... deploy/install-nodedaemon.sh \
  --control-url https://<slug>.theozolith.internal --ca ca.pem
```

The installer creates the `ozolith` service user, installs the product distribution
into a venv at `/opt/theozolith` (daemon + drivers + knowledge machinery — one
versioned distribution, ADR-0013), pins the CA at `/etc/theozolith/ca.pem`, installs
the systemd unit (`KillMode=control-group`: every TheOzolith process on the node dies
with the daemon), and starts heartbeating. The node registers within one heartbeat
interval (60 s default). `--node <name>` overrides the node name (default: the
hostname); `--source <checkout>` installs from a local checkout instead of the
published release. If `THEOZOLITH_NODE_TOKEN` is not in the environment the installer
prompts for it — it is never passed as an argument.

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
CONTROL_NODE_URL=https://<slug>.theozolith.internal THEOZOLITH_ADMIN_TOKEN=... \
  theozolith-control secret set github-worker --ca ca.pem
```

Secrets are encrypted at rest on the Control Node, pulled node-scoped over TLS (only
nodes whose Stacks reference a name may pull it), and materialized to tmpfs — never
on node disk.

### 5. Bootstrap the target repo and verify

```sh
GITHUB_TOKEN=... theozolith-bootstrap --repo owner/name   # labels + issue forms, one-time
theozolith-control status                                 # fleet state
```

The dashboard (at the minted origin, behind the admin token) shows the fleet, Run
progress, `theozolith.error` summaries, secret entry, and the web terminal.

## Development

```sh
uv sync --all-packages   # workspace venv with all components + dev tools
uv run pytest            # all tests
uv run ruff check .      # lint
uv run ruff format --check .
```
