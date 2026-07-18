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
   docker compose --env-file .env -f deploy/compose/control.yml up -d --build
   docker compose -f deploy/compose/control.yml run --rm control \
     origin-init                       # one-time canonical origin (mandatory, ADR-0019)
   docker compose -f deploy/compose/control.yml run --rm control \
     tls-init                          # one-time TLS provisioning (mandatory);
                                       # covers the canonical host; --host adds extras
   ```

   `origin-init` mints the deployment's one canonical hostname —
   `<128-bit-random-slug>.theozolith.internal` by default (`--base-domain` to change) —
   which production `serve` requires. Give it a **trusted-network-only** DNS record (or
   hosts entries); the Control Node must have no public ingress path, and browsers must
   use exactly this name (cookie-authenticated requests from any other Host/Origin are
   rejected). Restart after tls-init so the server picks up the certificate. Copy
   `/data/tls/ca.pem` out of the volume — every node pins it.

2. **Nodes** (every physical box that should run Stacks):

   ```sh
   sudo THEOZOLITH_NODE_TOKEN=... deploy/install-nodedaemon.sh \
     --control-url https://<canonical-host>:8443 --ca ca.pem
   ```

   The installer creates the `ozolith` user, a venv at `/opt/theozolith` (daemon +
   drivers + knowledge machinery — one versioned distribution, ADR-0013), the systemd
   unit (`KillMode=control-group`: every TheOzolith process on the node dies with the
   daemon), and starts heartbeating. The node registers within one heartbeat interval.

3. **Config Repo** (`~/.theozolith/configs` on the Control Node; ADR-0006): declare
   Stacks and derived images — `deploy/configs-example/` is a complete starter. Desired
   state distributes over the heartbeat channel; nodes cache it for degraded mode.
   The Worker/Reviewer drivers are process-kind Stacks; `control` is a container Stack.

4. **Secrets**: enter values once —

   ```sh
   CONTROL_NODE_URL=https://<control-host>:8443 THEOZOLITH_ADMIN_TOKEN=... \
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
   theozolith-control command update  --node box1             # Config-Repo-pinned version
   theozolith-control flags                                   # zombie/malformed/quarantine flags
   theozolith-control unquarantine --node box1                # human-only release (ADR-0016)
   ```

   `recycle` and `update` received mid-Run queue behind the current Run (job-dir
   presence is the in-flight signal; the deferral shows in heartbeats and on the
   dashboard); `--force` keeps the immediate kill-the-tree semantics. The dashboard
   (same origin as the API, behind the admin credential) is the read-only fleet view
   plus secret entry and the web terminal: to expose a Stack's live run containers to
   the terminal, give it an `attach` argv array in the Config Repo (see
   `deploy/configs-example/stacks/worker.toml`; free-form command strings are rejected,
   ADR-0019).

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

## Observing and attaching

- Live containers: `docker ps --filter label=theozolith.owner` — names are
  `ozolith-run-<run-id>` and `ozolith-review-<pr>-round-<n>`; heartbeats report the same
  set to the Control Node.
- Attach to any live agent session (input is permitted and lands in the transcript):

  ```sh
  docker exec -it ozolith-run-<run-id> tmux attach
  ```

  Detach with `C-b d`. No live run container = nothing to attach to.
- Evidence bundles (incl. full session transcripts): branch `theozolith/evidence`
  in the target repo, `runs/issue-<N>/`.

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
