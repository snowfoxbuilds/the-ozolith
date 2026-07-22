# deploy

The node substrate: Control Node + Node Daemon + secrets + the M4 dashboard/terminal.
Since ADR-0017 the Control Node is load-bearing for the pipeline — it writes every
claim (no second claim path exists), so the daemon-less dev shape is `theozolith-control
serve` on the same box as the drivers, not "no Control Node". With it down, in-flight
Runs finish and publish; new claims and review rounds pause.

Deployment footprint (the deletion test, NODE-SUBSTRATE.md): **docker + the TheOzolith
package + a `.env`** — a private Config Repo adds Stacks and worker types on top, never
below.

## Full substrate

1. **Control Node** (any box with docker; the Pi in the reference deployment):

   ```sh
   cp deploy/.env.example .env         # set THEOZOLITH_NODE_TOKEN + THEOZOLITH_ADMIN_TOKEN
   # Provision BEFORE the service is healthy (build the image, then two one-shots):
   docker compose -f deploy/compose/control.yml build
   docker compose -f deploy/compose/control.yml run --rm control \
     origin-init                       # one-time public origin (mandatory, ADR-0019)
   docker compose -f deploy/compose/control.yml run --rm control \
     tls-init                          # one-time TLS provisioning (mandatory); covers
                                       # the origin's hostname; --host adds extras
   docker compose --env-file .env -f deploy/compose/control.yml up -d
   ```

   `origin-init` mints the deployment's one public origin —
   `https://<128-bit-random-slug>.theozolith.internal` by default (`--base-domain` to
   change; `--port` only when browsers dial a nonstandard *external* port) — which
   production `serve` requires: until both one-shots have run, `serve` exits and the
   container restarts (expected during provisioning). The origin is independent of the
   Uvicorn bind (`serve --host/--port`): the reference compose publishes external 443
   onto the container's 8443 bind, so the origin carries no port, and remapping the bind
   never changes the accepted browser `Host`/`Origin`. Give the origin's hostname a
   **trusted-network-only** DNS record (or hosts entries); the Control Node must have no
   public ingress path, and browsers must use exactly this origin (cookie-authenticated
   requests from any other Host/Origin are rejected). Experts may instead supply
   `THEOZOLITH_PUBLIC_ORIGIN` (format-checked only — the operator owns slug entropy;
   see `.env.example`). Copy `/data/tls/ca.pem` out of the volume — every node pins it,
   and every `CONTROL_NODE_URL` uses the origin's hostname.

2. **Nodes** (every physical box that should run Stacks):

   ```sh
   sudo THEOZOLITH_NODE_TOKEN=... deploy/install-nodedaemon.sh \
     --control-url https://<slug>.theozolith.internal --ca ca.pem
   ```

   The installer creates the `ozolith` user, a venv at `/opt/theozolith` (daemon +
   drivers + knowledge machinery — one versioned distribution, ADR-0013), the systemd
   unit (`KillMode=control-group`: every TheOzolith process on the node dies with the
   daemon), and starts heartbeating. The node registers within one heartbeat interval.

3. **Config Repo** (`~/.theozolith/configs` on the Control Node; ADR-0006): declare
   Stacks and derived images — `deploy/configs-example/` is a complete starter. Desired
   state distributes over the heartbeat channel; nodes cache it for degraded mode.
   The Implementer/Reviewer drivers are process-kind Stacks; `control` and the Flight
   Deck are container Stacks.

4. **Secrets**: enter values once —

   ```sh
   CONTROL_NODE_URL=https://<slug>.theozolith.internal THEOZOLITH_ADMIN_TOKEN=... \
     theozolith-control secret set github-worker --ca ca.pem
   ```

   Encrypted at rest on the Control Node; pulled node-scoped (only nodes whose Stacks
   reference a name may pull it) over TLS; materialized to tmpfs
   (`/run/theozolith/secrets`, `/run/secrets/<name>` inside containers) and wired via
   `<ENV>_FILE`. Never on node disk.

5. **Operate**:

   ```sh
   theozolith-control status                                  # fleet state
   theozolith-control command drain   --node box1 --target worker
   theozolith-control command recycle --node box1 --target worker   # kills the whole
       # driver tree, run containers included, and restarts it
   theozolith-control command rebuild --node box1 --target claude-dev
   theozolith-control command update  --node box1             # nudge convergence now
   theozolith-control command restart --node box1             # re-exec the daemon in place
   theozolith-control flags                                   # zombie/malformed/quarantine flags
   theozolith-control unquarantine --node box1                # human-only release (ADR-0016)
   ```

6. **Update the product** (ADR-0015 as amended — two paths, one machinery;
   both need `CONTROL_NODE_URL` + `THEOZOLITH_ADMIN_TOKEN`):

   ```sh
   theozolith update                  # user path: pin the latest published release
   theozolith update --version 0.4.0  # …or an explicit one; rollback = re-pin
   theozolith build                   # developer path, from a CLEAN source checkout:
       # builds the distribution, pins the checkout's git SHA, and uploads the
       # wheels — the Control Node serves them, so nodes never pull source and
       # never build. A dirty tree is refused: every pin names a committed SHA.
   theozolith test                    # the local-development signal: run the
       # checkout's test and lint suite (iterate here, never by deploying
       # uncommitted state)
   ```

   Both paths commit the pin bump to `product.toml` in the Config Repo. **The
   pin is desired state**: every Node Daemon compares its running product
   version against the pin on each heartbeat and self-updates on mismatch
   (drain-aware queue-behind as above; startup is just the first pass), so a
   failed install retries automatically — the fanned-out update command is an
   immediate nudge, never the mechanism of record. The node hosting the
   `control` Stack is queued last: the Control Node applies its own update only
   after the fan-out is queued.

   Dispatch follows convergence: the Control Node grants work only to nodes
   whose heartbeat-reported version equals the pin, so issuing an update pauses
   new dispatch fleet-wide and capacity returns node by node. A node still
   off-pin after 3 consecutive heartbeats (`THEOZOLITH_OFFPIN_BEATS`) gets a
   queued `restart`; still off-pin after that, a `theozolith.error` lands on
   the dashboard and the node stays ineligible until you intervene. The
   dashboard surfaces version skew against the recorded pin; polling backs off
   exponentially (capped at 5 minutes) while the Control Node is unreachable.
   A fresh install with no `product.toml` pin resolves the latest release and
   writes the pin at Control Node startup — a running fleet always has a
   recorded version.

   `recycle` and `update` received mid-Run queue behind the current Run (job-dir
   presence is the in-flight signal; the deferral shows in heartbeats and on the
   dashboard); `--force` keeps the immediate kill-the-tree semantics. The dashboard
   (same origin as the API, behind the admin credential) is the read-only fleet view
   plus secret entry, the errors panel (`theozolith.error` summaries with
   node/component filters — depth stays in each node's journal and the evidence
   bundles), and the web terminal. Terminal targets are container-kind Stacks with an
   `attach` argv array in the Config Repo — the Flight Deck first among them (see
   `deploy/configs-example/stacks/flightdeck.toml`; free-form command strings are
   rejected, ADR-0022). Run containers are headless and never attach targets
   (ADR-0019).

   The zombie-claim janitor escalates evidence-first (ADR-0016): a silent Worker only
   flags the dashboard; once the returned driver's boot sweep pushes the Run's evidence
   bundle, the claim is released and the issue escalated `failed` + `needs_human` with
   the evidence link — never auto-re-queued. An optional slow GitHub-Action backstop
   lives in `deploy/github/zombie-janitor.yml` (copy into the target repo).

## Daemon-less dev (the M2 shape)

1. Bootstrap the target repo (labels + issue forms):

   ```sh
   GITHUB_TOKEN=... theozolith-bootstrap --repo owner/name
   ```

2. Build the run-container image (from the repo root):

   ```sh
   docker build -f worker/docker/Dockerfile.claude -t theozolith-run-claude:local .
   # optional: --build-arg KNOWLEDGE_SOURCE=... --build-arg KNOWLEDGE_PIN=...
   # optional: --build-arg OZOLITH_UID=$(id -u)   # match the driver user
   ```

3. Install and configure (every variable honors the VAR_FILE convention; one `.env`
   serves both drivers via the role-prefixed names):

   ```sh
   pip install ./knowledge ./worker
   cp deploy/.env.example .env    # fill in both PATs and the model API key
   ```

4. Run the drivers (don't run them as root):

   ```sh
   set -a; . ./.env; set +a
   theozolith-worker --once       # single poll-claim-run pass; or no flag for the loop
   theozolith-reviewer --once
   ```

   With `CONTROL_NODE_URL` unset, the claim pre-filter and event emission are skipped
   cleanly. The M2 systemd units in `deploy/systemd/` remain as conveniences; from M3 on
   the deployment contract is the Node Daemon supervising the drivers as process Stacks.

## Job-dir ownership

Run containers write into the bind-mounted job directory (`THEOZOLITH_JOBS_DIR`). Keep
the files owned by the driver user either by building the image with
`--build-arg OZOLITH_UID=$(id -u)` (or matching uid in the image recipe) or by setting
`THEOZOLITH_CONTAINER_USER=$(id -u):$(id -g)`.

## Observing Runs, and the Flight Deck

- Live run containers: `docker ps --filter label=theozolith.owner` — names are
  `ozolith-run-<run-id>` and `ozolith-review-<pr>-round-<n>`; heartbeats report the same
  set to the Control Node. Runs are **headless** (ADR-0019): there is no session to
  attach to and no mid-Run steering — watch progress telemetry on the dashboard, read
  the evidence bundle afterwards, or kill the Run (`recycle`).
- Evidence bundles (incl. the structured-output session transcripts and token usage):
  branch `theozolith/evidence` in the target repo, `runs/issue-<N>/`.
- **The Flight Deck** is where interactive agent work happens: a container-kind Stack
  running the agent CLI in a named tmux session (`deploy/configs-example/stacks/
  flightdeck.toml`), attached from the web terminal (attach/detach audit-logged; the
  session transcript captures typed input). Its GitHub credential is a **dedicated
  machine identity** — a fine-grained PAT scoped to issues, PRs, and contents with
  **no merge permission**, stored under the dedicated secret name
  `flightdeck-github-token`. Never reuse a driver PAT and never use a personal token
  here; the human merge gate stays human by construction. Do not leave Flight Deck
  sessions running unattended.

## Cleanup / deletion test

```sh
sudo systemctl disable --now theozolith-nodedaemon    # drivers die with it (cgroup)
docker ps -aq --filter label=theozolith.owner | xargs -r docker rm -f
docker compose -f deploy/compose/control.yml down -v
docker volume rm theozolith-cache
sudo rm -rf /opt/theozolith /etc/theozolith /var/lib/theozolith
```

After this the box is clean: secrets lived only in tmpfs and the encrypted Control Node
store, both now gone. Orphaned run containers from a mid-Run kill are reaped by the
daemon on its next start; the zombie-claim janitor restores the GitHub claim state.
