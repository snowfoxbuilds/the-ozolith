# TheOzolith

An open-source agent-orchestration platform that addresses the pain points of running coding agents today:

- **Containerization and security** — long-running agents run in containers with zero
  credentials, so autonomous agents can safely run with full permissions.
- **Autonomous workflows** — workers are triggered automatically by tags on GitHub issues and
  PRs. Built-in workers handle tasks such as implementation and code review on their own.
- **Agent-knowledge library** (`knowledge/`) — a tool-agnostic config format for managing
  skills, subagents, and workflows; knowledge survives container rebuilds.
- **Agent-agnostic** — models and harnesses are built to be easily swappable.
- **Benchmark integration (WIP)** — autonomous implementers can be benchmarked with
  SilverquiLLM-bench to evaluate effectiveness and cost.
- **Cluster management (WIP)** — deploy nodes across multiple machines and manage them centrally.


## Setup

One Control Node, plus a Node Daemon on every box that should run Stacks. The per-box
footprint is docker + the TheOzolith package + `theozolith init` output — there is no `.env`
(the deletion test as restated by ADR-0023/0034). Operations, backup/recovery, the
daemon-less one-box dev shape, and cleanup live in [deploy/README.md](deploy/README.md); this
is the orientation path.

> **No published releases yet — everything builds from a checkout of this repo.** Bootstrap the
> CLI with `sudo python3 build.py` — on a box already running an initialized Control Node the
> same command also publishes the build to the fleet (ADR-0051; `--no-publish` defers).
> `theozolith build` remains the publish-without-reinstall fast path from a clean source
> checkout. The release-based paths (`theozolith update`'s version pin and the fresh-box
> `curl … | sudo bash` installer) activate once releases are cut.

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

Or bare metal (root-mediated). First bootstrap the CLI from the checkout — `sudo python3
build.py` puts `theozolith` at a system path, printing a publish-skipped notice on this
not-yet-initialized box (full sequence: *Build / rebuild from the repo* in
[deploy/README.md](deploy/README.md)) — then:

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

### 2. Add nodes to the fleet (capacity) — one paste each *(experimental)*

Multi-node is still experimental. Mint a join token on the Control Node:

```sh
theozolith join-token create      # on the Control Node (sudo on a root install), or the dashboard
```

The printed fresh-box installer line (`curl … | sudo bash`) pulls from GitHub releases, which
don't exist yet — so for now, clone this repo on each node and bootstrap the CLI first
(`sudo python3 build.py`, exactly as on the Control Node; its chained publish skips itself
here — a node box has no Control Node), then run the printed
`sudo theozolith-nodedaemon provision 'ozjoin1:…'` line alone.

`provision` verifies the CA against the join string's pinned fingerprint **before transmitting
anything**, exchanges the single-use join token for the node's own non-expiring per-node token,
persists everything under `/var/lib/theozolith`, and enables the systemd unit
(`KillMode=control-group`: every TheOzolith process on the node dies with the daemon).
Provisioning **is** registration (ADR-0023): the node exists the moment the exchange succeeds and
heartbeats within the interval (60 s default). Join tokens default to 1 hour / single use;
`--ttl`/`--uses` widen them for batches, `theozolith join-token revoke <id>` is the backstop.

### 3. Add a worker to the fleet

A worker is a **worker type** placed by a thin **Stack**, declared in your **Config Repo** —
the human-authored tree `theozolith init` scaffolds at `config-src/` beside the data dir
(keep it there, move it anywhere, or host it on a git server). `deploy/configs-example/` is a
complete starter to copy in and adjust. The Config Repo is the source of truth (ADR-0006/0048);
`theozolith config ingest` turns it into the machine-owned **pinned build** (`configs/`) the
service actually loads — never hand-edit that one. For a pipeline worker, first label the
target repo once:

```sh
GITHUB_TOKEN=... theozolith-bootstrap --repo owner/name    # labels + issue forms, one-time
```

Then, in the Config Repo:

1. **Define the worker type** — `worker-types/<type>.toml`: the driver (`builtin:implementer`,
   `builtin:reviewer`, or a custom `drivers/<name>`), `adapter`, `model`, `workspace` (the
   `owner/name` repo it works), the run-image `base` (digest-pinned, or a bare tag ingest
   resolves; + optional `setup` and an in-repo knowledge reference
   `knowledge = "knowledge/<name>"`), and a `[secrets]` table mapping env → secret **names**
   (the type's defaults; `""` declares a slot every instantiating Stack must bind).
   See `deploy/configs-example/worker-types/claude-dev.toml` (Implementer) and
   `claude-review.toml` (Reviewer — its **own** GitHub identity, no self-grading).
2. **Place it with a Stack** — `stacks/<name>.toml`: `worker_type`, `node` (exact node name),
   `state`, plus optional per-placement bindings (ADR-0047): a `workspace` repointing the
   target repo, a `[secrets]` table rebinding the type's slots (distinct credentials per
   Stack — e.g. one machine account per repo), and an `[env]` of expert overrides. See
   `deploy/configs-example/stacks/implementer.toml`. The Implementer/Reviewer resolve to
   **process** Stacks; the Flight Deck resolves to a **container** Stack. (The Control Node is
   never a Stack — a `stacks/control.toml` is rejected at validation, ADR-0035.)
3. **Enter its secrets** — each name the type references, once:

   ```sh
   theozolith secret set github-implementer     # on the Control Node, or the dashboard
   ```

   Encrypted at rest on the Control Node, pulled node-scoped over TLS (only nodes whose Stacks
   reference a name may pull it), materialized to tmpfs — never on node disk.
4. **Commit the Config Repo, then ingest** — every Config Repo commit lands via

   ```sh
   sudo theozolith config ingest              # or: theozolith config ingest <path-or-git-url>
   ```

   Ingest lints the repo with the exact fail-loud config-load checks, resolves the mechanical
   pins (tag-only `base` → registry digest; per-knowledge-tree content hashes), compiles
   `knowledge/`, and commits the pinned build (rollback = `git revert` there). Desired state
   then distributes over the heartbeat channel; the node builds the derived image locally,
   pulls its secrets, and starts the worker. Nodes cache the config for degraded mode.

The Implementer/Reviewer split is the pipeline in one node: dispatch → Run → best-effort PR →
review rounds → human merge. For interactive (human-driven) agent work, the **Flight Deck** is a
driverless worker type — see step 4.

### 4. Manage knowledge & custom workers

**Knowledge on a laptop** — the knowledge machinery is standalone (no cluster required):
`pip install ./knowledge`, then `theozolith-knowledge sync` a knowledge repo into your
`~/.claude`, or `bake` a pinned Knowledge Source into a container image at build time. See
[knowledge/README.md](knowledge/README.md).

**Knowledge on the fleet (ADR-0048)** — deployment knowledge lives IN the Config Repo: a
`knowledge/<name>/` directory holds one knowledge root (`skills/`, `agents/`, `workflows/`,
`AGENTS.md`), referenced from worker types as `knowledge = "knowledge/<name>"`. Ingest compiles
it and pins its content hash. Driver workers **bake** the compiled tree into their derived
images (an edit re-tags exactly the types that reference the tree → nodes rebuild → new Runs
carry it). The **Flight Deck** (a driverless worker type,
`deploy/configs-example/worker-types/flightdeck.toml`) never bakes: its `knowledge` field
selects which node-applied tree its read-only `/var/lib/theozolith/knowledge` mount serves,
and the deck fails loud until the node has converged that tree. One edit → commit → ingest
reaches every deck on every node; a running session keeps what it loaded and picks up the new
trees on agent-CLI restart — no rebuild, no recreate, no sync step. Changing which tree a deck
*selects* recreates it.

> **Never** run `theozolith-knowledge sync` against a Flight Deck's `~/.claude` — it replaces
> the symlinks into the applied tree with detached copies (the mount itself is read-only, so
> nothing can write through the links). There is no writable clone and no promote workflow
> anymore; the Config Repo commit + ingest IS the promotion.

**Custom workers (ADR-0042)** — a worker type can name a driver that lives in your Config Repo
instead of a built-in one: a new pipeline worker with no product fork. Author `drivers/<name>.py`
exporting a top-level `Driver` class subclassing `theozolith_worker.api.Worker` (`api` is the one
stable import), point a worker type at it with `driver = "drivers/<name>"`, and place it with a
Stack exactly like a built-in. The `drivers/` tree ships to nodes as a hash-pinned artifact the
daemon verifies and unpacks; editing the driver, committing, and ingesting changes the hash and
the node restarts the driver on the new code (queued behind any in-flight Run). See
`deploy/configs-example/drivers/hello_logger.py` and its `worker-types/`/`stacks/` wiring for a
complete staged example.

> **`drivers/` is git-native only** and the web UI refuses to touch it: **Config Repo write
> access equals code execution with driver credentials on nodes** (ADR-0042). Treat it exactly as
> you treat merge access to product code.

Full depth — ingest/rebuild mechanics, volume cardinality, custom-driver dispatch gate and
skew, and the upgrade path for pre-ingest deployments (the retired
`knowledge_source`/`knowledge_pin` era — `theozolith config migrate`) — is in
[deploy/README.md](deploy/README.md).

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

Updating the product across the fleet (with no releases yet, `sudo python3 build.py` — one
command to update this box's CLI and publish, ADR-0051 — or `theozolith build` to publish
without reinstalling; `theozolith update`'s release-pin path activates once releases exist),
backup and recovery, and the daemon-less dev shape are covered in
[deploy/README.md](deploy/README.md).

## Further Reading

- [AGENTS.md](AGENTS.md) — project index, conventions, and the spec/ADR map.
- [CONTEXT.md](CONTEXT.md) — domain glossary; every spec, driver, and agent instruction uses
  these terms exactly.
- Component READMEs — [knowledge/](knowledge/README.md) (knowledge machinery),
  [worker/](worker/README.md) (the coding pipeline and drivers),
  [control/](control/README.md) (Control Node), [nodedaemon/](nodedaemon/README.md) (Node
  Daemon), and [deploy/](deploy/README.md) (substrate operations: install, backup/recovery,
  the daemon-less dev shape, and cleanup).
- Specs — [ARCHITECTURE.md](docs/specs/ARCHITECTURE.md),
  [AGENTIC-CODING-PIPELINE.md](docs/specs/AGENTIC-CODING-PIPELINE.md), and
  [NODE-SUBSTRATE.md](docs/specs/NODE-SUBSTRATE.md).
- [Architecture Decision Records](docs/adr/) — the numbered ADRs referenced throughout this
  document.

## Development

```sh
uv sync --all-packages   # workspace venv with all components + dev tools
uv run pytest            # all tests
uv run ruff check .      # lint
uv run ruff format --check .
```
