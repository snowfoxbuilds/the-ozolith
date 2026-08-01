# deploy

The node substrate: Control Node + Node Daemon + secrets + the dashboard/terminal.
Since ADR-0017 the Control Node is load-bearing for the pipeline — it writes every
claim (no second claim path exists), so the daemon-less dev shape is `theozolith
serve` on the same box as the drivers, not "no Control Node". With it down, in-flight
Runs finish and publish; new claims and review rounds pause.

Deployment footprint (the deletion test, restated 2026-07-27; ADR-0023): **docker + the
TheOzolith package + `theozolith init` output** — every tier-2 tunable at its
shipped default, environment variables as the expert override only. There is no `.env`:
settings live in `control.toml` in the Config Repo (dashboard-edited), secrets in the
encrypted store, node identity in the join-string exchange. A private Config Repo adds
Stacks and worker types on top, never below.

## Full substrate

1. **Control Node** (the Pi in the reference deployment). **Prerequisite: give
   this box a static IP or DHCP reservation** — the channel is IP-only
   (ADR-0023 as amended; ADR-0034): nodes AND browsers dial the control IP
   directly, with zero DNS dependency anywhere.

   Bare metal (root-mediated, ADR-0034):

   ```sh
   sudo theozolith init
   sudo systemctl start theozolith-control.service
   ```

   Or with docker:

   ```sh
   docker compose -f deploy/compose/control.yml build
   docker compose -f deploy/compose/control.yml run --rm control init --ip <this-box-LAN-IP>
   docker compose -f deploy/compose/control.yml up -d
   ```

   `--ip` is required in the compose flow: inside a container the auto-detected
   address would be the Docker bridge IP, unreachable from your LAN, so `init`
   refuses to guess there (ADR-0031); bare metal auto-detects. The confirmed IP is
   persisted as a read-only `control.toml` field (with `control_port`, default
   443, beside it) and is the one address every join string, the bootstrap
   listener's `/control-url`, the certificate SAN, and the browser origin carry —
   mint surfaces never re-detect it.

   `init` (ADR-0023/0034) composes the whole first run: master key → control
   address (`https://<control-ip>`; `--port` to vary) → per-deployment CA +
   server cert with the persisted IP in the SAN → admin password prompt (only
   its scrypt hash is stored) → on root bare metal, the systemd unit
   (`theozolith-control.service`: a dedicated service user binding 443 via
   `AmbientCapabilities=CAP_NET_BIND_SERVICE` — never a root serve) → the
   **operator handoff**. There is no DNS step and no required certificate
   install: the first browser visit clicks through the self-signed-certificate
   interstitial and logs in (the TrueNAS model). Trusting the per-deployment CA
   (download URL and per-OS one-liners in the handoff) is the optional
   green-lock upgrade; operators with a public domain can substitute a publicly
   valid certificate instead. Re-running `init` requires `--force`: **a new CA
   invalidates the pinned `ca.pem` on every provisioned node — the whole fleet
   fails TLS until each box gets one join-string re-paste** (outstanding join
   strings and device trust die too; the master key and stored secrets are never
   touched).

   Everything lands under `/var/lib/theozolith-control/` on a root-mediated
   bare-metal install — that exact path is the only one the root installer
   will manage or `chown` (a `THEOZOLITH_DATA_DIR` override is refused there;
   it stays honored for unprivileged and compose runs, which use
   `~/.theozolith/`) —
   partitioned by durability class (ADR-0024): `configs/` (the git-backed
   Config Repo — `control.toml`, `stacks/`, `images/`, `product.toml`),
   `secrets/` (master key, CA keypair, TLS material, admin password hash,
   `store.db` — a **sibling** of configs/, never inside any git tree), `cache/`
   (`cache.db`, deletable at any time: costs a re-login and one heartbeat
   round), `logs/` (terminal audit log). On the root-mediated install, admin
   subcommands read their credentials from there — run them under `sudo`.

   Experts may override any setting with `THEOZOLITH_*` environment variables
   (validated).

2. **Nodes** (every physical box that should run Stacks) — one paste each:

   ```sh
   theozolith join-token create        # on the Control Node (or the dashboard's
                                       # Join tokens page): prints the exact line
   ```

   Paste its output on the box. Fresh box: the printed `curl … | sudo bash -s --
   'ozjoin1:…'` line fetches the installer over GitHub release HTTPS (code never rides
   the plaintext listener) — it creates the `ozolith` user, the venv at
   `/opt/theozolith`, the systemd unit (`KillMode=control-group`), and hands off to
   `theozolith-nodedaemon provision` as its final step. Already-installed box: the
   printed `sudo theozolith-nodedaemon provision 'ozjoin1:…'` line alone.

   `provision` parses and checksums the join string, fetches the CA from the bootstrap
   listener, verifies it against the pinned fingerprint **before transmitting
   anything** (mismatch = possible MITM or a stale join string after CA rotation —
   abort), exchanges the short-lived single-use join token over verified TLS for this
   node's own **non-expiring per-node token**, persists everything under
   `/var/lib/theozolith`, enables the unit, and heartbeats. The persisted control URL
   is the **IP-based address the node just verified** — nothing in the deployment
   resolves a hostname or needs DNS (ADR-0034). Re-pasting a fresh join string on an
   already-provisioned node rotates its token and replaces its persisted state in
   place: that one paste per node is the whole recovery path when the Control Node's
   IP changes. Provisioning **is** registration:
   unknown/revoked tokens are rejected 401 and surface on the dashboard
   as *unregistered nodes* (advisory, never dispatch-eligible). Remove a node with
   `POST /api/v1/nodes/<node>/revoke`; `theozolith join-token revoke <id>` kills an
   outstanding join string. `theozolith-nodedaemon provision --inspect 'ozjoin1:…'`
   pretty-prints a payload without acting.

3. **Config Repo** (`~/.theozolith/configs` on the Control Node; ADR-0006): declare
   Stacks and derived images — `deploy/configs-example/` is a complete starter. Desired
   state distributes over the heartbeat channel; nodes cache it for degraded mode.
   The Implementer/Reviewer drivers are process-kind Stacks (the daemon injects the
   control channel — URL, per-node token, CA — into them; Stack env overrides win);
   `control` and the Flight Deck are container Stacks. Tier-2 tunables (heartbeat
   interval, grace periods, sweep cadences, terminal cap, event budget, session
   length, bootstrap port) live in `control.toml` and are edited on the dashboard's
   Settings page — each save is a fixed-schema commit touching only that file. The
   control address (and the browser origin derived from it) renders read-only there.

4. **Secrets**: enter values once on the dashboard's Secrets form, or:

   ```sh
   theozolith secret set github-worker    # on the Control Node; no env needed
   ```

   Encrypted at rest in `secrets/store.db`; pulled node-scoped (only nodes whose
   Stacks reference a name may pull it) over TLS; materialized to tmpfs
   (`/run/theozolith/secrets`, `/run/secrets/<name>` inside containers) and wired via
   `<ENV>_FILE`. Never on node disk.

5. **Operate**:

   ```sh
   theozolith status                                  # fleet state
   theozolith command drain   --node box1 --target worker
   theozolith command recycle --node box1 --target worker   # kills the whole
       # driver tree, run containers included, and restarts it
   theozolith command rebuild --node box1 --target claude-dev
   theozolith command update  --node box1             # nudge convergence now
   theozolith command restart --node box1             # re-exec the daemon in place
   theozolith flags                                   # zombie/malformed/quarantine flags
   theozolith unquarantine --node box1                # human-only release (ADR-0016)
   ```

   On the Control Node these need no environment (run them under `sudo` on a
   root-mediated install): the URL comes from the persisted control address, the
   admin token from `secrets/admin-token`, the CA from `secrets/tls/ca.pem` (all
   init-written). Elsewhere, set `CONTROL_NODE_URL` + `THEOZOLITH_ADMIN_TOKEN` +
   `THEOZOLITH_TLS_CA`.

6. **Update the product** (ADR-0015 as amended — two paths, one machinery):

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
   python3 build.py                   # bootstrap ONLY: a bare checkout with nothing
       # installed — same build implementation as `theozolith build`, finishing by
       # installing the `theozolith` entry point (ADR-0023/0032; the deprecated
       # `theozolith-control` alias comes along for one release)
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
   off-pin after 3 consecutive heartbeats (`offpin_beats` in control.toml) gets a
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
   (same origin as the API, behind the admin password; ADR-0023) is the read-only fleet
   view plus secret entry, settings, join tokens, the errors panel
   (`theozolith.error` summaries with node/component filters — depth stays in each
   node's journal and the evidence bundles), and the web terminal. Terminal targets are
   container-kind Stacks with an `attach` argv array in the Config Repo — the Flight
   Deck first among them (see `deploy/configs-example/stacks/flightdeck.toml`;
   free-form command strings are rejected, ADR-0022). Run containers are headless and
   never attach targets (ADR-0019).

   The zombie-claim janitor escalates evidence-first (ADR-0016): a silent Worker only
   flags the dashboard; once the returned driver's boot sweep pushes the Run's evidence
   bundle, the claim is released and the issue escalated `failed` + `needs_human` with
   the evidence link — never auto-re-queued. An optional slow GitHub-Action backstop
   lives in `deploy/github/zombie-janitor.yml` (copy into the target repo).

## Build / rebuild from the repo (bare metal)

The complete from-checkout sequence for a root-mediated Control Node — first
install and every source update after it.

**First install.** The executable must live at a system path the service user
can reach (the installer refuses a home venv — `/home/<you>` is not
world-traversable; ADR-0034), so the bootstrap venv goes under `/opt`:

```sh
git clone https://github.com/snowfoxbuilds/the-ozolith && cd the-ozolith
sudo python3 -m venv /opt/theozolith
sudo /opt/theozolith/bin/python build.py     # builds the wheels, installs them there
sudo ln -sf /opt/theozolith/bin/theozolith /usr/local/bin/theozolith
```

Then the first run, exactly as in step 1 of Full substrate:

```sh
sudo theozolith init            # check the auto-detected IP; --ip to correct it
sudo systemctl start theozolith-control.service
```

Browse to `https://<control-ip>`, click through the certificate interstitial,
log in — then pin the product so the fleet has a recorded version:

```sh
cd the-ozolith && sudo theozolith build      # uploads the wheels, commits the pin
```

If a shell had a previous checkout venv active, `deactivate` and confirm
`which theozolith` answers `/usr/local/bin/theozolith` — the old entry point
shadows the system one until it does.

**Rebuild after source changes.** `build.py` was bootstrap only; from here the
loop is:

```sh
cd the-ozolith
git pull                        # a dirty tree is refused — every pin names a committed SHA
theozolith test                 # iterate here (checkout venv), never by deploying
sudo theozolith build           # build, upload, pin; nodes converge on their heartbeats
```

`theozolith build` serves the wheels from the Control Node and bumps the pin,
so every node — the Control Node's own host queued last — self-updates; no
node ever pulls source or builds. The `/opt/theozolith` venv itself only needs
touching when you want the *CLI on this box* updated too (it is just another
node-shaped install): re-run `sudo /opt/theozolith/bin/python build.py` from
the updated checkout.

## Backup and recovery (ADR-0024)

Backup is **one folder, one copy command**: the data partition minus `cache/`
(optionally minus `logs/`) to another trusted device. Root-mediated bare metal —
the primary shape — keeps it at `/var/lib/theozolith-control/`, owned by the
service user, so only the local *read* is privileged: root creates a tar stream
and the network leg runs as you, with your own SSH identity (root has no keys
for `backup-host`, and preserving the service user's ownership on a remote
account would fail anyway — `recover` repairs ownership on restore, so none
needs preserving):

```sh
set -o pipefail   # a tar failure must fail the pipeline, not ship a truncated archive
sudo tar -C /var/lib/theozolith-control --exclude='./cache' -cf - . \
  | ssh backup-host 'umask 077; cat > theozolith-backup.tar.tmp' \
  && ssh backup-host 'mv theozolith-backup.tar.tmp theozolith-backup.tar'
```

The remote `umask 077` matters: the archive carries the master key, CA private
key, admin token, and secret store, and the remote account's default umask
would land it world-readable. The temp-file-then-promote keeps the last good
archive intact until the new one has fully arrived.

Unprivileged and compose homes keep `~/.theozolith/`:

```sh
rsync -a --exclude cache/ ~/.theozolith/ backup-host:theozolith-backup/
```

Back up the partition your deployment actually uses — after `sudo theozolith
init`, an `~/.theozolith/` copy is empty or stale and omits the CA, master key,
tokens, and secret store. Re-copy after enrolling nodes or adding secrets; that
is the whole cadence rule.
Secret material never leaves trusted devices: **GitHub is never a full backup** — a
Config Repo clone looks like a deployment but cannot resurrect one (`secrets/` is a
sibling of `configs/` by decision, never a git-ignored child: `git clean -x` must not
be able to delete the master key, and a clone must not look complete while missing it).

Recovery:

1. Install the TheOzolith package on the replacement box; restore the copy to
   the data dir (`/var/lib/theozolith-control/` root-mediated, `~/.theozolith/`
   otherwise). Root-mediated, mirroring the backup command — the network leg as
   you, only the local extract as root (ownership comes back in step 2):

   ```sh
   sudo mkdir -p /var/lib/theozolith-control
   ssh backup-host 'cat theozolith-backup.tar' \
     | sudo tar -C /var/lib/theozolith-control --no-same-owner -xf -
   ```

   `--no-same-owner` is deliberate: root tar would otherwise restore the
   archived numeric UID/GID, which on a replacement box may belong to an
   unrelated account — files stay root-owned until step 2's controlled
   ownership repair.
2. `theozolith recover` (under `sudo` on bare metal) — validates the restore
   **loudly and completely** (every missing or corrupt artifact enumerated in one
   pass, nonzero exit), re-mints the server certificate from the restored CA
   (never a new CA), and — run as root on a systemd host — repairs the service
   too: service user, partition ownership, unit, enable. It uses the restored
   `control_ip` by default; pass `--ip` if the replacement box's address differs
   (it is then persisted for every future mint).
3. Start serving (`sudo systemctl start theozolith-control.service`, or the
   compose flow). Browsers dial the control IP directly — no DNS to update.

**Same IP** (give the Control Node a static IP or DHCP reservation so this is the
normal case): zero node touches — nodes dial the persisted IP directly, pin the CA
(not the server cert), and hold non-expiring tokens restored with `store.db`, so
they reconnect on their capped backoff, untouched.

**Changed IP**: every provisioned node still dials the old address — the recovery
path is **one join-string re-paste per node** (`theozolith join-token create`; the
paste rotates that node's token in place). Note the asymmetry `recover` also prints:
these nodes will **NOT** appear in the unregistered-nodes view, because their
heartbeats go to the dead address and never arrive — work from your node inventory,
not the dashboard.

Sessions and cached state died with `cache/` — a re-login and one heartbeat round
recover them. Nodes enrolled *after* the backup DO show up in the unregistered-nodes
view (their heartbeats arrive with unknown tokens): that list is the re-provision
worklist for the stale-backup case. Deleting `cache/cache.db` on a live system is
always safe and is the documented recovery move for cache corruption.

### Migrating a pre-ADR-0034 home-directory install

The same recover machinery relocates an existing `~/.theozolith/` deployment onto
the root-mediated shape — no re-init, no CA rotation, zero node touches (same IP):

```sh
# prerequisite: theozolith installed at a system path the service user can
# reach (e.g. a venv under /opt/theozolith) — the installer refuses a home venv
# 1. stop the old hand-run serve (Ctrl-C / kill), then:
sudo mkdir -p /var/lib/theozolith-control
sudo rsync -a ~/.theozolith/ /var/lib/theozolith-control/   # cache/ optional
sudo theozolith recover      # validates, re-mints the server cert from the SAME
                             # CA, creates the service user, repairs ownership,
                             # installs and enables the unit
sudo systemctl start theozolith-control.service
rm -rf ~/.theozolith         # once the dashboard answers
```

A deployment with no provisioned nodes and no stored secrets can skip all of
this and simply re-run `sudo theozolith init` (recover validates the full
partition, including `store.db`, which only exists once a node or secret does).

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

3. Install and export the driver configuration (every variable honors the VAR_FILE
   convention; the daemonful path injects these from the Config Repo + secret store —
   exporting them by hand is the dev-only surface):

   ```sh
   pip install ./knowledge ./worker
   export THEOZOLITH_REPO=owner/name WORKER_GITHUB_TOKEN=... REVIEWER_GITHUB_TOKEN=... \
          ANTHROPIC_API_KEY=... CONTROL_NODE_URL=... THEOZOLITH_NODE_TOKEN=... \
          THEOZOLITH_RUN_IMAGE=theozolith-run-claude:local
   ```

   The Worker and the Reviewer must be **different GitHub identities** (ADR-0008: no
   self-grading by construction). PATs live in the drivers only; no run container ever
   sees them (ADR-0013).

4. Run the drivers (don't run them as root):

   ```sh
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
# The root-mediated Control Node (ADR-0034; the guards make this safe on
# boxes that never had one):
sudo systemctl disable --now theozolith-control.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/theozolith-control.service
sudo systemctl daemon-reload
sudo userdel ozolith-control 2>/dev/null || true    # drops its group with it
docker ps -aq --filter label=theozolith.owner | xargs -r docker rm -f
docker compose -f deploy/compose/control.yml down
docker volume rm theozolith-cache
sudo rm -rf /opt/theozolith /var/lib/theozolith /var/lib/theozolith-control ~/.theozolith
```

After this the box is clean: secrets lived only in tmpfs and the encrypted Control Node
store, both now gone (including the root-mediated partition, its unit, and its service
user). Orphaned run containers from a mid-Run kill are reaped by the daemon on its next
start; the zombie-claim janitor restores the GitHub claim state.
