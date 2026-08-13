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
  one-box dev shape runs `theozolith serve` beside the drivers (claims dispatch
  through the Control Node — ADR-0017).

Every top-level component is independently installable. A laptop-only user of the knowledge
machinery never installs the cluster manager.

The project is governed by anchor docs authored in Notion and synced one-way into this repo
([AGENTS.md](AGENTS.md) index, [CONTEXT.md](CONTEXT.md) glossary, [docs/specs/](docs/specs),
[docs/adr/](docs/adr)); see ADR-0001. Synced docs are never hand-edited here.

## Domain vocabulary (read this first)

The substrate has three nouns, and the operational recipes below turn on the distinction:

- **Node** — a physical/virtual box running the Node Daemon. Nodes are **capacity**.
- **Worker type** — the complete customization unit for one automated actor (ADR-0044): its
  driver (built-in or custom), Agent adapter + model, target workspace repo, run-image
  recipe, knowledge pin, and the secret **names** it needs. Lives in `worker-types/` in the
  Config Repo. The Implementer, the Reviewer, the (driverless) Flight Deck, and any custom
  worker are all worker types (ADR-0020).
- **Stack** — a **thin placement**: `worker type + node + desired state (running/stopped)`,
  plus optional per-placement env/attach. A "worker in the fleet" is a Stack. The Control
  Node resolves the thin Stack against its worker type into the full container/process spec.

So **adding a worker = declare a worker type + place it with a Stack + enter its secrets**,
and **starting/stopping a worker = flip that Stack's `state` (or queue a `command`)**.

## Quick start (knowledge machinery, laptop-only)

No cluster required — this is the standalone knowledge compiler/sync.

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

One Control Node, plus a Node Daemon on every box that should run Stacks. The per-box
footprint is docker + the TheOzolith package + `theozolith init` output — there is no `.env`
(the deletion test as restated by ADR-0023/0034). Operations, backup/recovery, the
daemon-less one-box dev shape, and cleanup live in [deploy/README.md](deploy/README.md); this
is the orientation path.

Prerequisites:

- **Control Node**: any box with docker + the compose plugin (the Pi in the reference
  deployment) and a checkout of this repo. **Give it a static IP or DHCP reservation** — the
  channel is IP-only (ADR-0034): nodes and browsers dial the control IP directly, no DNS.
- **Every physical node**: systemd Linux, docker with the compose plugin, python3 ≥ 3.11.
- **For the coding pipeline**: a target GitHub repo and machine-user PATs for the Implementer,
  the Reviewer, and the Control Node — three distinct GitHub identities, so no self-grading by
  construction (ADR-0008, ADR-0017).

### 1. Set up the Control Node

From the repo checkout on the Control Node box. Docker:

```sh
docker compose -f deploy/compose/control.yml build
docker compose -f deploy/compose/control.yml run --rm control init --ip <this-box-LAN-IP>
docker compose -f deploy/compose/control.yml up -d
```

Or bare metal (root-mediated; the `theozolith` CLI must already be at a system path — see
*Build / rebuild from the repo* in [deploy/README.md](deploy/README.md)):

```sh
sudo theozolith init                         # auto-detects the IP; --ip to correct it
sudo systemctl start theozolith-control.service
```

`init` (ADR-0023/0034/0036) composes the machine surface in one run: master key → admin
bearer token → the persisted control address → per-deployment CA + server certificate with IP
SANs → the **operator handoff**. No DNS, no password prompt, no browser step: the bearer API
serves everything. `--ip` names the LAN address nodes will dial — **required in the compose
flow** (a container can't auto-detect it); bare metal auto-detects. All state lands under
`~/.theozolith/` (`/var/lib/theozolith-control/` on a root-mediated install), partitioned by
durability class (ADR-0024); backup is a copy of that folder minus `cache/`.

Turn on the browser dashboard only when you want it — it stays off until you opt in:

```sh
theozolith origin-init      # asks for the browser origin (IP by default) + admin password,
                            # re-mints the server cert from the same CA
```

**One-box shortcut** (ADR-0037): `sudo theozolith init --with-local-node` also installs a Node
Daemon on the same box and runs the join flow internally (loopback dial, machine-consumed join
string), then seeds the Config Repo with a complete worker Stack staged at `state = "stopped"`
and a README naming the finish line. Nothing deploys until you pin the image, enter secrets,
and flip to `running`.

### 2. Add nodes to the fleet (capacity) — one paste each

```sh
theozolith join-token create      # on the Control Node (sudo on a root install), or the dashboard
```

Paste the printed line on the box. A fresh box's line fetches the installer over GitHub-release
HTTPS and hands off to `theozolith-nodedaemon provision`, which verifies the CA against the
join string's pinned fingerprint **before transmitting anything**, exchanges the single-use join
token for the node's own non-expiring per-node token, persists everything under
`/var/lib/theozolith`, and enables the systemd unit (`KillMode=control-group`: every TheOzolith
process on the node dies with the daemon). Provisioning **is** registration (ADR-0023): the node
exists the moment the exchange succeeds and heartbeats within the interval (60 s default). Join
tokens default to 1 hour / single use; `--ttl`/`--uses` widen them for batches,
`theozolith join-token revoke <id>` is the backstop.

### 3. Add a worker to the fleet

A worker is a **worker type** placed by a thin **Stack**. `deploy/configs-example/` is a
complete starter — copy it into the git-backed Config Repo (`~/.theozolith/configs` on the
Control Node, `/var/lib/theozolith-control/configs` root-mediated; ADR-0006) and adjust. For a
pipeline worker, first label the target repo once:

```sh
GITHUB_TOKEN=... theozolith-bootstrap --repo owner/name    # labels + issue forms, one-time
```

Then, in the Config Repo:

1. **Define the worker type** — `worker-types/<type>.toml`: the driver (`builtin:implementer`,
   `builtin:reviewer`, or a custom `drivers/<name>`), `adapter`, `model`, `workspace` (the
   `owner/name` repo it works), the digest-pinned run-image `base` (+ optional `setup`,
   `knowledge_source`/`knowledge_pin`), and a `[secrets]` table mapping env → secret **names**.
   See `deploy/configs-example/worker-types/claude-dev.toml` (Implementer) and
   `claude-review.toml` (Reviewer — its **own** GitHub identity, no self-grading).
2. **Place it with a Stack** — `stacks/<name>.toml`: `worker_type`, `node` (exact node name),
   `state`, plus an optional `[env]` of per-placement overrides. See
   `deploy/configs-example/stacks/implementer.toml`. The Implementer/Reviewer resolve to
   **process** Stacks; the Flight Deck resolves to a **container** Stack. (The Control Node is
   never a Stack — a `stacks/control.toml` is rejected at validation, ADR-0035.)
3. **Enter its secrets** — each name the type references, once:

   ```sh
   theozolith secret set github-implementer     # on the Control Node, or the dashboard
   ```

   Encrypted at rest on the Control Node, pulled node-scoped over TLS (only nodes whose Stacks
   reference a name may pull it), materialized to tmpfs — never on node disk.
4. **Commit the Config Repo.** Desired state distributes over the heartbeat channel; the node
   builds the derived image locally, pulls its secrets, and starts the worker. Nodes cache the
   config for degraded mode.

The Implementer/Reviewer split is the pipeline in one node: dispatch → Run → best-effort PR →
review rounds → human merge. For interactive (human-driven) agent work, the **Flight Deck** is a
driverless worker type — see step 4.

### 4. Manage knowledge & custom workers

**Knowledge on a laptop** — use the `sync`/`bake` quick start above.

**Knowledge on the fleet (Flight Deck, ADR-0043)** — the Flight Deck is a driverless worker type
(`deploy/configs-example/worker-types/flightdeck.toml`) whose `~/.claude` knowledge dirs are a
**live symlinked git clone**, shared by all Flight Decks of the same type on a node (one
`knowledge-<type>` volume). Edit a skill in a Flight Deck and it is live in its siblings after an
agent-CLI restart — no sync step. To make an edit reach the pipeline's Runs (**promote**):

```sh
# in the Flight Deck:
cd ~/knowledge && git add -A && git commit && git push && git rev-parse HEAD
# on the Control Node: bump the worker type's knowledge_pin to that SHA and commit
#   -> the deterministic image tag changes -> nodes rebuild -> new Runs carry it.
```

> **Never** run `theozolith-knowledge sync` against a Flight Deck's `~/.claude` — it breaks the
> symlink carve-out and writes through the link into the shared clone. Cross-node transport is
> plain git (commit/push here, `git pull` there); there is no auto-sync daemon.

**Custom workers (ADR-0042)** — a worker type can name a driver that lives in your Config Repo
instead of a built-in one: a new pipeline worker with no product fork. Author `drivers/<name>.py`
exporting a top-level `Driver` class subclassing `theozolith_worker.api.Worker` (`api` is the one
stable import), point a worker type at it with `driver = "drivers/<name>"`, and place it with a
Stack exactly like a built-in. The `drivers/` tree ships to nodes as a hash-pinned artifact the
daemon verifies and unpacks; editing the driver + committing changes the hash and the node
restarts the driver on the new code (queued behind any in-flight Run). See
`deploy/configs-example/drivers/hello_logger.py` and its `worker-types/`/`stacks/` wiring for a
complete staged example.

> **`drivers/` is git-native only** and the web UI refuses to touch it: **Config Repo write
> access equals code execution with driver credentials on nodes** (ADR-0042). Treat it exactly as
> you treat merge access to product code.

Full depth — promote/rebuild mechanics, volume cardinality, custom-driver dispatch gate and
skew — is in [deploy/README.md](deploy/README.md).

### 5. Start, stop, and monitor workers

**Start / stop (durable, config-driven)** — flip the Stack's desired state and commit; nodes
converge on their next heartbeat:

```toml
# stacks/<name>.toml
state = "running"    # or "stopped"
```

**Start / stop / rebuild one right now (imperative nudge)** — queue a command (no config change):

```sh
theozolith command drain   --node box1 --target implementer   # graceful stop of that Stack
theozolith command recycle --node box1 --target implementer   # kill the whole driver tree
                                                              #   (run containers included) + restart
theozolith command rebuild --node box1 --target claude-dev    # rebuild the derived image
theozolith command update  --node box1                        # nudge convergence now
theozolith command restart --node box1                        # re-exec the daemon in place
```

`recycle`/`update` received mid-Run queue behind the current Run; `--force` keeps the immediate
kill-the-tree semantics.

**Monitor** — the terminal-native answers first:

```sh
theozolith status              # fleet health table; exit 0 healthy / 1 degraded / 2 unreachable
theozolith status --json       # the parsing contract (the table is for humans)
theozolith top                 # full-screen Operator TUI: fleet, Stacks & Runs, events, errors
theozolith flags               # zombie / malformed / quarantine flags
theozolith unquarantine --node box1     # human-only dispatch-quarantine release (ADR-0016)
```

`theozolith status`/`top` are pure API consumers (ADR-0038/0039) — no local systemd/docker
probing. On the Control Node they need no environment (run under `sudo` on a root install);
elsewhere set `CONTROL_NODE_URL` + `THEOZOLITH_ADMIN_TOKEN` + `THEOZOLITH_TLS_CA`. The optional
dashboard (after `origin-init`, behind the admin password) shows the same fleet — including
unregistered nodes awaiting a join paste — Run progress, `theozolith.error` summaries, secret
entry, settings (committed to `control.toml`), join tokens, and the web terminal.

**Watch Runs directly** — Runs are headless (ADR-0019): there is no session to attach to and no
mid-Run steering. Watch progress on the dashboard/TUI, read the evidence bundle afterwards, or
kill the Run (`recycle`):

```sh
docker ps --filter label=theozolith.owner        # live run containers on a node
# evidence bundles (transcripts + token usage): branch theozolith/evidence, runs/issue-<N>/
```

Interactive, human-driven agent work happens in the **Flight Deck** (container-kind Stack,
`deploy/configs-example/stacks/flightdeck.toml`), reached over one-hop tailnet SSH or the audited
web terminal. Its GitHub credential is a dedicated **no-merge** machine identity
(`flightdeck-github-token`) — never a driver or personal PAT — so the human merge gate stays human
by construction. Don't leave Flight Deck sessions running unattended.

Updating the product across the fleet (`theozolith update` / `theozolith build`), backup and
recovery, and the daemon-less dev shape are covered in [deploy/README.md](deploy/README.md).

## Development

```sh
uv sync --all-packages   # workspace venv with all components + dev tools
uv run pytest            # all tests
uv run ruff check .      # lint
uv run ruff format --check .
```
