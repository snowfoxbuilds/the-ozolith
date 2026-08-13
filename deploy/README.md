# deploy

The node substrate: Control Node + Node Daemon + secrets + the dashboard/terminal.
Since ADR-0017 the Control Node is load-bearing for the pipeline — it writes every
claim (no second claim path exists), so the daemon-less dev shape is `theozolith
serve` on the same box as the drivers, not "no Control Node". With it down, in-flight
Runs finish and publish; new claims and review rounds pause.

Deployment footprint (the deletion test, restated 2026-07-27; ADR-0023, amended by
ADR-0034): **docker — or the systemd unit init installs on bare metal — + the
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

   Bare metal (root-mediated, ADR-0034; the `theozolith` CLI must already be
   installed at a system path — see *Build / rebuild from the repo* below):

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

   `init` (ADR-0023/0034/0036) composes the machine surface: master key → admin
   bearer token → control address (`https://<control-ip>`; `--port` to vary) →
   per-deployment CA + server cert with IP SANs (the persisted IP and loopback)
   → on root bare metal, the systemd unit (`theozolith-control.service`: a
   dedicated service user binding 443 via
   `AmbientCapabilities=CAP_NET_BIND_SERVICE` — never a root serve) → the
   **operator handoff**. No password prompt and no browser step exist: the
   bearer API serves everything, and every browser route refuses until you
   opt in with `theozolith origin-init` — it asks for the browser origin (the
   IP origin by default; a hostname if you run your own DNS) and the admin
   password together, re-mints the server cert from the same CA, and restarts
   nothing node-side. The first browser visit then clicks through the
   self-signed-certificate interstitial and logs in (the TrueNAS model);
   trusting the per-deployment CA (download URL and per-OS one-liners printed
   by `origin-init`) is the optional green-lock upgrade, and operators with a
   public domain can substitute a publicly valid certificate instead.
   Re-running `init` requires `--force`: **a new CA invalidates the pinned
   `ca.pem` on every provisioned node — the whole fleet fails TLS until each
   box gets one join-string re-paste** (outstanding join strings and device
   trust die too; the master key and stored secrets are never touched).
   `origin-init` re-runs need their own `--force`.

   **Single-Node Deployment** (ADR-0037): `sudo theozolith init
   --with-local-node` additionally installs the Node Daemon on the same box
   and runs the unmodified join flow internally — the join token is minted and
   consumed machine-to-machine (you never see a join string), the local daemon
   persists a **loopback** dial address (LAN renumbering never touches it),
   and the Config Repo is seeded with a thin Implementer Stack and the worker
   type it names (ADR-0044) staged at `state = "stopped"` plus a README naming
   the finish line. Requires docker
   and the `theozolith-nodedaemon` CLI in the same install (the bare-metal
   build installs all four distributions).

   **If the local bootstrap fails or is interrupted** (a phase timeout, a
   provisioning error, Ctrl-C): re-run the same command —
   `sudo theozolith init --with-local-node`, no `--force`. On an initialized
   box it resumes in place: the CA never rotates, completed phases are
   skipped or reconciled, operator edits in `configs/` are preserved, an
   unconsumed machine-only join token was already revoked on the way out,
   and no join string is ever shown. Reconciliation proves health rather
   than assuming it: a registered node counts as healthy only with a fresh
   heartbeat (on the server's clock) on top of a valid on-disk identity;
   a stale or silent node is restarted and must heartbeat again before the
   resume succeeds; a node whose local state is missing or corrupt fails
   with explicit restore/re-provision instructions — nothing is deleted
   automatically. `--force` remains what it always was — a full re-init
   with a NEW CA — and is never needed for a retry.

   Everything lands under `/var/lib/theozolith-control/` on a root-mediated
   bare-metal install — that exact path is the only one the root installer
   will manage or `chown` (a `THEOZOLITH_DATA_DIR` override is refused there;
   it stays honored for unprivileged and compose runs, which use
   `~/.theozolith/`) —
   partitioned by durability class (ADR-0024): `configs/` (the git-backed
   Config Repo — `control.toml`, `stacks/`, `worker-types/`, `product.toml`),
   `secrets/` (master key, CA keypair, TLS material, admin password hash,
   `store.db` — a **sibling** of configs/, never inside any git tree), `cache/`
   (`cache.db`, deletable at any time: costs a re-login and one heartbeat
   round), `logs/` (terminal audit log). On the root-mediated install, admin
   subcommands read their credentials from there — run them under `sudo`.

   Experts may override any setting with `THEOZOLITH_*` environment variables
   (validated).

2. **Nodes** (every physical box that should run Stacks) — one paste each:

   ```sh
   theozolith join-token create        # on the Control Node, under sudo on a
                                       # root-mediated install (or the dashboard's
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

3. **Config Repo** (`configs/` in the Control Node's data partition —
   `/var/lib/theozolith-control/configs` root-mediated, `~/.theozolith/configs`
   otherwise; ADR-0006): declare
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
   theozolith secret set github-implementer    # on the Control Node; no env needed
                                          # (under sudo on a root-mediated install)
   ```

   Encrypted at rest in `secrets/store.db`; pulled node-scoped (only nodes whose
   Stacks reference a name may pull it) over TLS; materialized to tmpfs
   (`/run/theozolith/secrets`, `/run/secrets/<name>` inside containers) and wired via
   `<ENV>_FILE`. Never on node disk.

   A Claude worker authenticates the model with **either** a workspace API key
   (`ANTHROPIC_API_KEY`) **or** a subscription OAuth token
   (`CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`) — map whichever you have in
   the worker type's `[secrets]`; either alone is enough, both may be set. The driver
   forwards only the resolved adapter's credential into the (otherwise credential-free)
   run container (ADR-0013).

   **Rotating a credential** (replacing an API key or OAuth token): `theozolith secret
   set <name>` stores the new value, but a running driver read its credential once at
   startup and injects it into each Run container it launches — there is **no
   hot-reload**. Recycle the affected driver Stack so its next Runs pick up the
   replacement (in-flight Runs finish on the old value):

   ```sh
   theozolith command recycle --node <node> --target <stack>
   ```

5. **Operate**:

   ```sh
   theozolith status              # fleet health: table + exit 0/1/2; --json to parse
   theozolith command drain   --node box1 --target implementer
   theozolith command recycle --node box1 --target implementer   # kills the whole
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
   sudo python3 build.py              # bootstrap ONLY: a bare checkout with nothing
       # installed — same build implementation as `theozolith build`, but the shim
       # owns the environment (ADR-0041): it creates the /opt/theozolith venv,
       # re-executes itself inside it, installs the wheels there, and links
       # `theozolith`, `theozolith-nodedaemon`, and the one-release deprecated
       # `theozolith-control` alias into /usr/local/bin (ADR-0023/0032). The
       # full bare-metal sequence: "Build / rebuild from the repo" below.
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

   The zombie-claim janitor escalates evidence-first (ADR-0016): a silent Implementer only
   flags the dashboard; once the returned driver's boot sweep pushes the Run's evidence
   bundle, the claim is released and the issue escalated `failed` + `needs_human` with
   the evidence link — never auto-re-queued. An optional slow GitHub-Action backstop
   lives in `deploy/github/zombie-janitor.yml` (copy into the target repo).

## Build / rebuild from the repo (bare metal)

The complete from-checkout sequence for a root-mediated Control Node — first
install and every source update after it.

**First install.** The executable must live at a system path the service user
can reach (the installer refuses a home venv — `/home/<you>` is not
world-traversable; ADR-0034), so the managed venv goes under `/opt` — and
`build.py` owns it (ADR-0041): it creates (or reuses) `/opt/theozolith`,
re-executes itself with that interpreter, builds and installs the wheels
there, and links the CLI into `/usr/local/bin`. Each link is validated and
published atomically — per link, not as a set: an interrupted run can
leave a valid subset of the three published, and re-running completes it.
An unrelated file, directory, or foreign symlink already sitting at a link
name is refused by name, never overwritten — resolve it and re-run (the
re-run converges). You never create, activate, or name a venv:

```sh
git clone https://github.com/snowfoxbuilds/the-ozolith && cd the-ozolith
sudo python3 build.py     # the whole bootstrap: venv, wheels, install, CLI links
```

Prerequisites, precisely: the OS packages are `python3 >= 3.11`, the
distro's `python3-venv` package (a box missing it is refused with that
exact remediation — the shim never package-manages on its own, ADR-0037
posture), and `git` (the build derives the version pin from the checkout's
committed SHA at run time). The repository must be a git clone, not a
tarball. Installing the built wheels resolves their dependency closure
from the Python package index, so the box needs network access to PyPI —
or a pip mirror/cache you configure yourself (`PIP_INDEX_URL` /
`pip.conf`); the bootstrap is not an offline installer.

Then the first run, exactly as in step 1 of Full substrate:

```sh
sudo theozolith init            # check the auto-detected IP; --ip to correct it
sudo systemctl start theozolith-control.service
```

Check the fleet with `sudo theozolith status` (browser enablement is optional
and separate: `sudo theozolith origin-init`; ADR-0036) — then pin the product
so the fleet has a recorded version:

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
node-shaped install): re-run `sudo python3 build.py` from the updated
checkout — it reuses the existing `/opt/theozolith` venv (ADR-0041; the old
`sudo /opt/theozolith/bin/python build.py` spelling still works).

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
rm -rf ~/.theozolith         # once `sudo theozolith status` answers
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
   export THEOZOLITH_REPO=owner/name IMPLEMENTER_GITHUB_TOKEN=... REVIEWER_GITHUB_TOKEN=... \
          ANTHROPIC_API_KEY=... CONTROL_NODE_URL=... THEOZOLITH_NODE_TOKEN=... \
          THEOZOLITH_RUN_IMAGE=theozolith-run-claude:local
   ```

   The Implementer and the Reviewer must be **different GitHub identities** (ADR-0008: no
   self-grading by construction). PATs live in the drivers only; no run container ever
   sees them (ADR-0013).

4. Run the drivers (don't run them as root):

   ```sh
   theozolith-driver builtin:implementer --once   # single poll-claim-run pass; or no flag for the loop
   theozolith-driver builtin:reviewer --once
   ```

   Every built-in worker type runs through the one generic launcher (`theozolith-driver
   <ref>`, ADR-0020). A reachable Control Node is **required**: new dispatch and review
   work flows through it (ADR-0017), so when it is down, in-flight work finishes and
   new work pauses until it returns. The M2 driver units (`theozolith-implementer.service`,
   `theozolith-reviewer.service` in `deploy/systemd/`) remain as conveniences — unlike
   `theozolith-control.service` there, which is the ADR-0034 production unit; from M3 on
   the deployment contract is the Node Daemon supervising the drivers as process Stacks.

## Model & effort are baked into the run image (ADR-0045)

`model` (required with a driver) and `effort` (optional) are typed fields on the
worker-type definition, validated at config load against the Agent adapter and
**baked into the derived image**: control appends one synthesized
`theozolith-adapter materialize` setup step, which writes the model into the
image's managed adapter config where nothing in a workspace checkout can
override it. The instruction hash covers that step, so changing `model`/`effort`
re-tags the image and rolls the affected workers — that is the only way a model
ever changes.

Removed with **no fallback** (a leftover export now fails the driver loudly):
`IMPLEMENTER_MODEL` / `REVIEWER_MODEL` / `THEOZOLITH_MODEL`, the Stack `[env]`
model override, and the `--model` invocation flag. Custom drivers (ADR-0042)
no longer declare a `default_model` class attribute — delete it from your
`drivers/*.py` when upgrading.

Migration rule: when you first set (or next change) `model` on a worker type,
**bump `base` to a release that ships `theozolith-adapter` in the same edit** —
an older base fails the build loudly ("command not found"), and both edits
change the tag anyway, so it costs one rebuild. Worker types with no
`model`/`effort` keep byte-identical tags across this release and rebuild
nothing.

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

## Flight Deck knowledge & state (ADR-0043)

A Flight Deck's `~/.claude` conflates two classes of content, and they are split
across named volumes with different cardinality (`<stack>` = the Flight Deck
Stack's name; `<worker-type>` = its worker-type name; the worker type declares
the volumes with a literal `{stack}` placeholder that the Control Node
substitutes at resolution time, so two same-type Flight Decks on one node get
distinct per-instance volumes):

| Volume | Mountpoint | Cardinality |
| --- | --- | --- |
| `<stack>-claude-state` | `/home/ozolith/.claude` | **one per Flight Deck** — runtime state (sessions, transcripts, `--resume`); never shared, never worker-visible |
| `knowledge-<worker-type>` | `/home/ozolith/knowledge` | **one per worker type per node** — the shared knowledge clone; siblings of the type mount the same name |
| `<stack>-logs` | `/var/log/flightdeck` | one per Flight Deck |

(One-hop remote access into a Flight Deck — and the per-instance machine-identity
volume it will add — is split out and tracked separately; see
`configs-example/README.md` for status and the ways in today.)

**Knowledge is a live symlinked clone, not a bake.** At start the Flight Deck
runs `theozolith-knowledge clone-init` to materialize the shared clone on the
`knowledge-<worker-type>` volume, then points its `~/.claude` knowledge
directories at that clone's raw working tree:

```
~/.claude/skills     -> ~/knowledge/skills
~/.claude/agents     -> ~/knowledge/agents/claude
~/.claude/workflows  -> ~/knowledge/workflows
~/.claude/CLAUDE.md  -> ~/knowledge/AGENTS.md
```

The symlinked view is byte-equivalent to a compiled/synced view (ADR-0009), so a
skill edited in one Flight Deck is **live in every sibling of the same type on
the node after an agent-CLI restart** — no sync step, no rebuild. Runtime state
stays on the never-shared per-instance volume.

**Promote** (make an edit reach the pipeline's Runs — the single review
chokepoint):

```sh
# in the Flight Deck:
cd ~/knowledge && git add -A && git commit && git push && git rev-parse HEAD
# on the Control Node: bump the worker type's knowledge_pin to that SHA
#   -> the deterministic tag changes -> nodes rebuild -> new Runs carry it.
```

**Cross-node** transport is plain git, human-driven — there is **no auto-sync
daemon, ever**. Only pushed commits travel: in the source Flight Deck, commit
and push (to `main` or an authoring branch), then attach the other node's
Flight Deck of the same type and `git pull` (or fetch/checkout the authoring
branch) there. **Uncommitted scratch stays node-local** — `git pull` cannot
carry it, and nothing else moves it for you.

**Warnings.**

- The symlink carve-out is **Flight-Deck-only**. Run containers never mount
  knowledge (it would break pin reproducibility and open a prompt-injection
  persistence channel); a cache volume aimed at a `.claude` path is refused by
  construction.
- **Never** run `theozolith-knowledge sync` against a Flight Deck's `~/.claude`.
  Sync damages the carve-out in both directions at once: it replaces direct
  symlinks (like `CLAUDE.md`) with copied files, silently unsharing the clone —
  and where a knowledge directory is a symlink, it writes *through* the link
  into the shared knowledge clone itself, overwriting content live in every
  sibling Flight Deck of that type on the node.
- `~/.claude.json` lives *outside* `~/.claude` and is not on the state volume, so
  it regenerates when the container recycles (accepted v0 gap).

## Custom drivers (ADR-0042)

A worker type can name a driver that lives in your Config Repo instead of a
built-in one — a new pipeline worker with no product fork. See
`deploy/configs-example/drivers/hello_logger.py` and its
`worker-types/hello-logger.toml` + `stacks/hello-logger.toml` wiring for a
complete, staged example.

**Authoring.** A driver named `<name>` is `drivers/<name>.py` or a package
`drivers/<name>/__init__.py` (+ siblings). `<name>` must be a valid Python
identifier (`^[a-z_][a-z0-9_]*$` — the module is imported as `drivers.<name>`,
so dashes are out). The module MUST export a top-level `Driver` class
subclassing `theozolith_worker.api.Worker`; there is no `main()`. **`api` is
the only stable import** — everything outside `theozolith_worker.api` is
internal with no stability promise, and api changes are release-note events.
Point a worker type at it with `driver = "drivers/<name>"` (defaults are
referenced, never copied). Intra-driver imports work unmodified
(`from drivers.<name>.helpers import x`).

**Delivery and convergence.** The Config Repo's `drivers/` tree is shipped to
nodes as a hash-pinned artifact the Node Daemon fetches and **verifies by
recomputing the manifest** — never trusting the archive bytes — then unpacks
atomically. The daemon injects `THEOZOLITH_DRIVERS_DIR` (the unpacked root) and
the launcher **appends** it to `sys.path`, so `drivers.<name>` resolves from
the distribution while `theozolith_worker.api` still resolves from the product
venv — one interpreter, no shadowing. Edit the driver → commit → the hash
changes → the node converges and **restarts the driver on the new code** (queued
behind any in-flight Run), with no daemon or product change.

**The dispatch gate.** A node is dispatch-eligible only once its applied
`drivers_hash` matches desired: an off-hash node is skipped. A `drivers/<name>`
Stack **refuses to start** until the distribution is verified-applied — a
`config-dist-missing` `theozolith.error` surfaces on the dashboard, and it
self-heals once convergence lands the tree.

**Advisory skew.** The artifact records the product version it was `built_against`;
the launcher logs one advisory line on a mismatch and keeps running (a driver
written against an older api works until it touches something that moved). Skew
is never fail-closed.

**Crash at start.** A broken driver (syntax error, wrong/missing `Driver`
export, stale api, missing distribution) writes a traceback to the journal,
emits a best-effort `theozolith.error` (component `driver-host`), and exits
non-zero; the supervisor relaunches on a bounded, token-free cadence. A start
crash before a claim burns nothing (quarantine stays Run-scoped, ADR-0016).

**Fork protocol (v0).** A driver forked from another records its ancestry in one
leading comment line, logged when it starts:

```
# forked-from: builtin:<name> @ <product-version>
```

Documentation only — no enforcement. Fresh-authored drivers carry no header.

**`drivers/` is git-native only.** The web UI and any config editor refuse to
touch `drivers/` — driver code is edited in git, because
**Config Repo write access now equals code execution with driver credentials on
nodes.** ADR-0042 documents this trust posture; it is not mitigated and no
sandbox is promised. Treat Config Repo write access exactly as you treat
merge access to product code.

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
# The /usr/local/bin links the bootstrap published (ADR-0041) — removed BEFORE
# the venv so no dangling links survive, and only when they still point into
# the managed venv (a foreign binary at these names was never ours to delete):
for name in theozolith theozolith-control theozolith-nodedaemon; do  # incl. the deprecated alias link
  [ "$(readlink "/usr/local/bin/$name")" = "/opt/theozolith/bin/$name" ] \
    && sudo rm -f "/usr/local/bin/$name"
done
sudo rm -rf /opt/theozolith /var/lib/theozolith /var/lib/theozolith-control ~/.theozolith
```

After this the box is clean: secrets lived only in tmpfs and the encrypted Control Node
store, both now gone (including the root-mediated partition, its unit, its service
user, the managed venv, and every `/usr/local/bin` link the bootstrap owned — a link at
those names that pointed elsewhere is left alone, exactly as the bootstrap found it).
Orphaned run containers from a mid-Run kill are reaped by the daemon on its next
start; the zombie-claim janitor restores the GitHub claim state.
