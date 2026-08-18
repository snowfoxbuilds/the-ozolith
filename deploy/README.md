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
settings live in `control.toml` in the Config Repo (materialized into the pinned
build by `theozolith config ingest`, ADR-0048), secrets in the encrypted store,
node identity in the join-string exchange. A private Config Repo adds
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
   skipped or reconciled, operator edits in the Config Repo are preserved
   (the resume re-ingests them into the pinned build, ADR-0048), an
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
   partitioned by durability class (ADR-0024, amended by ADR-0048):
   `configs/` (the machine-owned **pinned build** — `control.toml`, `stacks/`,
   `worker-types/`, `knowledge/`, `pins.toml`, `product.toml` — committed only
   by `theozolith config ingest`), `config-src/` (the scaffolded human
   **Config Repo** the operator edits and ingests; keep it here or anywhere),
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

3. **Config Repo -> pinned build** (ADR-0006/0048): author your **Config
   Repo** anywhere — the init-scaffolded `config-src/` beside the data dir, any
   directory, or a git host — declaring Stacks, worker types, `drivers/`, and
   `knowledge/`; `deploy/configs-example/` is a complete starter. Then run

   ```sh
   sudo theozolith config ingest              # the scaffolded config-src/
   sudo theozolith config ingest <path-or-git-url>
   ```

   Ingest lints the repo with the exact fail-loud config-load checks, resolves
   the mechanical pins (tag-only `base` -> registry digest; per-knowledge-tree
   content hashes), compiles `knowledge/`, and commits the machine-owned
   **pinned build** (`configs/` in the data partition —
   `/var/lib/theozolith-control/configs` root-mediated) with the source commit
   stamped. The pinned build is the tree the service loads and distributes:
   never hand-edit it; rollback is `git revert` there. The running service
   picks a new commit up within one heartbeat; nodes converge over the hash
   ladder. The Implementer/Reviewer drivers are process-kind Stacks (the
   daemon injects the control channel — URL, per-node token, CA — into them;
   Stack env overrides win); the Flight Deck is a container Stack. Tier-2
   tunables (heartbeat interval, grace periods, sweep cadences, terminal cap,
   event budget, session length, bootstrap port) live in `control.toml`
   `[settings]` in the Config Repo and go through ingest like everything else
   (they apply on service restart); the dashboard's Settings page is
   display-only, and the control address renders read-only there.

   `sudo theozolith config ingest --dry-run [source]` is the config
   **linter**: the identical pipeline through the lint step — every refusal
   fires the same — then a report of what ingest would change (per-file
   adds/updates/deletes, worker-type re-tags, `control.toml` and
   product-version movement) with **nothing committed**, not even loose git
   objects. Uncommitted edits in a local Config Repo are previewed from the
   working tree (with the refusal a real ingest would give called out), so
   you can lint before you commit.

4. **Secrets**: enter values once on the dashboard's Secrets form, or:

   ```sh
   theozolith secret set github-implementer    # on the Control Node; no env needed
                                          # (under sudo on a root-mediated install)
   ```

   Encrypted at rest in `secrets/store.db`; pulled node-scoped (only nodes whose
   Stacks reference a name may pull it) over TLS; materialized to tmpfs
   (`/run/theozolith/secrets`, `/run/secrets/<name>` inside containers) and wired via
   `<ENV>_FILE`. Never on node disk.

   The worker type's `[secrets]` declares the **slots** (env names) with optional
   default store-names; a Stack may **rebind** any slot per placement in its own
   `[secrets]` table (ADR-0047) — how two Stacks of one type act as distinct
   identities, e.g. one GitHub machine account per target repository. On the type,
   `SLOT = ""` declares a required slot every instantiating Stack must bind
   (fail-loud at config load); on the Stack, `SLOT = ""` unbinds an inherited
   default. Values stay one-per-name in the store — distinctness comes from
   distinct names (`theozolith secret set github-impl-a`, `… github-impl-b`).

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
   replacement (in-flight Runs finish on the old value). With per-Stack bindings,
   rotating one identity touches only the Stacks bound to that name — recycle those,
   the siblings never notice:

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

## Upgrading a pre-ingest deployment (ADR-0048)

Deployments that predate config ingestion hand-edited `configs/` directly and
declared worker-type knowledge as `knowledge_source`/`knowledge_pin` (a git
URL + SHA, cloned with the now-retired `KNOWLEDGE_GIT_TOKEN`). The upgraded
code **refuses those fields loudly at config load** — and the OLD code must
never load an ingested (post-ADR-0048) build either: it would not crash, it
would silently ignore the new `knowledge` field and serve knowledge-less
worker types, re-tagging the fleet onto images with no knowledge baked in.
So the first ingest happens inside a **bounded, deliberate control-plane
stop**: prepare and review everything while the old service keeps running,
stop it, ingest, start the upgraded service.

1. **Back up first.** The standard one-folder backup (next section) covers
   everything the migration touches; at minimum copy `configs/` — it is a git
   repo, so `git -C configs bundle create ~/configs-backup.bundle --all` is a
   complete history backup.
2. **Upgrade the code, keep the service running**: `sudo python3 build.py`
   from the updated checkout installs the new CLI and packages. The running
   service process still runs the OLD code and keeps loading the old config
   — do not restart it yet. Everything through step 5 is preparation the old
   service never observes.
3. **Migrate**: `sudo theozolith config migrate` reads `configs/` (never
   modifying it — the legacy tree stays byte-for-byte and history-identical
   until ingest) and atomically publishes a human Config Repo at
   `config-src/` — config files copied, retired
   `knowledge_source`/`knowledge_pin` removed and preserved as MIGRATION
   comments, `control.toml` reduced to its operator `[settings]` surface
   (the machine `[control]` block stays in `configs/`, where ingest
   preserves it). It prints one note per follow-up. A failed migration
   leaves no partial `config-src/` and is immediately rerunnable.
4. **Place the knowledge**: for each worker type the report names, clone that
   knowledge repo's CONTENT into `config-src/knowledge/<name>/` (the ADR-0009
   source layout: `skills/`, `agents/`, `workflows/`, `AGENTS.md`) and set
   `knowledge = "knowledge/<name>"` on the type. Flight Deck types using the
   retired writable-clone pattern (`clone-init`, `knowledge-<type>` volumes)
   adopt the read-only-mount pattern from
   `deploy/configs-example/worker-types/flightdeck.toml`. The
   `KNOWLEDGE_GIT_TOKEN` secret can be deleted once no type references it.
5. **Review and commit** `config-src/`, then **preview the first ingest**:
   `sudo theozolith config ingest --dry-run` lints the migrated repo with
   the upgraded validator and prints everything the first ingest will change
   — writing nothing, so it is safe while the old service is still running.
   Fix any refusal now: it shrinks the stop window below to one
   already-validated ingest.
6. **Stop the old service**: `sudo systemctl stop theozolith-control.service`.
   This begins the bounded outage — control-plane only. Nodes ride it out in
   degraded mode: running containers and Runs keep running, daemons keep
   retrying heartbeats, and nothing new is dispatched until control returns.
7. **Ingest while stopped**: `sudo theozolith config ingest`. This commits
   the machine-owned pinned build ONTO the existing `configs/` git history —
   machine `control.toml` address, product pin, stacks, worker types, and
   secrets (which live in the encrypted store, untouched by all of this) are
   preserved. **If the ingest is refused** (a lint failure, a placeholder
   checksum, an unresolvable base tag), nothing was committed: `configs/` is
   exactly as it was, so you may either fix `config-src/` and re-run ingest,
   or start the OLD service again (reinstall the previous release first) and
   finish the preparation later.
8. **Start the upgraded service immediately**
   (`sudo systemctl start theozolith-control.service`) — ending the outage —
   **then update the nodes** (`theozolith build` / `theozolith command
   update`) so they run matching daemons: the recipe wire format changed with
   the knowledge fields, so control and nodes upgrade together, control
   first.

**Rollback.** Before the pinned-build commit (through step 6), nothing
changed for the service: delete `config-src/` to abandon a migration, and
start the service again on whichever code is installed (reinstall the
previous release if you already upgraded). After the pinned-build commit
(step 7 onward): stop the service, `git -C configs revert HEAD` (or `git
reset --hard HEAD^` if nothing consumed the new build yet) restores the
pre-migration tree byte-for-byte, reinstall the previous release, and start
— the old code again loads exactly the config it always had. The backup from
step 1 is the belt-and-braces path: restore the folder and the deployment is
exactly where it started.

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

`model` (required with a driver) and `effort` (optional, driver types only) are
typed fields on the worker-type definition, validated at config load against
the Agent adapter and **baked into the derived image**: control appends one
synthesized `theozolith-adapter materialize` setup step to the recipe. The
identity is held **by best effort, failing loud on detection** — selection
makes the right model happen; a detected wrong identity fails the Run with
evidence; a gap that prevents detection is recorded, never silently ignored
and never blocking.

For the Claude adapter the materialize step writes
`/etc/claude-code/managed-settings.json` with:

- a managed `model` **session default** for the MAIN agent — the managed tier
  outranks the checkout's `.claude/settings.json`/`settings.local.json` and
  the user tier for the same key (verified live), and the harness passes no
  `--model`;
- for effort, the managed `env` entry `CLAUDE_CODE_EFFORT_LEVEL`, which
  overrides `/effort`, `--effort`, the process environment, and any
  settings-file `effortLevel` (verified live, including survival of the
  per-key managed env merge beside foreign drop-in env blocks).

**Enforcement is main-agent-only — deliberately.** There is NO
`availableModels` allowlist: subagents run their declared frontmatter models,
skills route freely, and the CLI's background helpers use their own small
models (all verified live as capabilities, not escapes). Every identity check
scopes to main-agent stream events. One documented edge: a skill that
switches the MAIN thread's model will fail the Run — route cheap/heavy work
through subagents instead.

**The build fails closed on conflicting policy.** Claude Code merges the base
managed file with every `/etc/claude-code/managed-settings.d/*.json` drop-in
(base first, then alphabetical; scalars override, arrays concatenate,
objects deep-merge), and a managed `policyHelper`/`policyHelpers` preempts
the entire managed tier. The materialize step inspects all of those sources
in merge order and **fails the build naming the file and key** when any
carries an identity-affecting key: `model`, `availableModels`,
`enforceAvailableModels`, `fallbackModel`, `effortLevel`, a policy helper,
`modelOverrides`, or a model/effort/endpoint-selecting `env` entry
(`ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_*_MODEL`,
`CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_EFFORT_LEVEL`,
`ANTHROPIC_BASE_URL`, `ANTHROPIC_*_BASE_URL`,
`CLAUDE_CODE_USE_BEDROCK`/`VERTEX`, `ANTHROPIC_SMALL_FAST_MODEL`).
Conflicting operator policy is never silently deleted or overwritten —
remove or relocate it, or drop the worker type's model/effort. Unrelated
managed keys survive untouched. A malformed managed document also fails the
build: unknowable policy is not policy.

The minimum in-image CLI is **Claude Code 2.1.232**: the
managed-over-checkout model-default precedence, the per-key managed `env`
merge (2.1.223), the Stop-hook applied-effort payload, and the ConfigChange
hook were all verified live there. The materialize step probes
`claude --version` in-image and fails the build below that floor; the setup
dry-run enforces the same floor at runtime.

**One setup dry-run per driver boot — never a per-Run probe.** Before a
driver takes any work it commissions one `identity-dryrun` container: the
zero-cost static checks, the CLI floor, and ONE neutral no-tool probe
session with the Run credential that must announce and execute the baked
model and (when effort is baked) report the baked level as the *applied*
effort via the Stop-hook payload. A broken image/credential/policy
combination fails loud at setup, in seconds, without burning issues or
claims — a dry-run verdict **latches** the driver: it fetches no work and
never re-spends the probe, the reason is reported to the Control Node's
error feed (`theozolith status` shows recent errors), and you retry by
restarting the driver after fixing the cause. A dry-run that delivered no
verdict (container engine down, or the session broke before answering) is
retried with backoff. Model-less
images pass trivially; the dot-prefixed dry-run job dir is invisible to the
evidence sweep and queue-behind.

**Runs are watched, never gated.** The task session launches exactly as an
unbaked image would — pointer prompt in the argv, task file on disk, and the
checkout's CLAUDE.md, skills, slash commands, settings, and hooks loading
normally (they belong to the work). Two observation hooks ride `--settings`:
a Stop hook journaling one value-redacted applied-effort record per
completed turn, and a ConfigChange hook recording identity-relevant
mid-session settings changes (it never *blocks* a change — organization
policy is never resisted). The harness reads the stream as it grows and
**kills the session on a positive detection only**: a main-agent turn
executing off the baked model, an off-identity init announcement, or a
recorded ConfigChange. After exit the last applied-effort observation is
checked — a detected clamp fails the Run as `effort-clamped`. Missing
observations (no init, no turn signal, no Stop record) are **gaps recorded
in evidence, not failures**. Identity comparisons strip the CLI's
context-window decoration (`claude-opus-5[1m]` announces the same model
`claude-opus-5` executes).

Identity failures carry a distinct `failure_class: identity` (the local-retry
budget still applies uniformly for now — the carve-out is #42) and a
diagnostic naming the expected model/effort and the category
(`policy-conflict`, `identity-inconsistent`, `pair-invalid`, `cli-too-old`,
`unavailable`, `substituted`, `effort-clamped`, `unverifiable`,
`preflight-timeout`, `config-changed`). Every identity failure writes the
redacted `output/identity.json` record — corrupt or half-declared identity
declarations are `identity-inconsistent`; an unwritable hook scratch is
`unverifiable` — and evidence embeds it as an `identity` object (expected vs
observed model/effort, check status, violation, gap notes): categories and
names only, never credentials or settings contents.

**Reviewer identity failures terminate visibly.** A review session killed by
the identity monitor takes the Reviewer's one-strike lane: identity.json and
the available transcript are published to the evidence bundle with
`failure_class: identity`, then the PR turns `blocked` + `needs_human`
(losing `pr_ready`) in the same pass — it leaves the reviewable pool instead
of relaunching an identical doomed review every poll. The identity marker is
matched **anchored** in both drivers; other session breakage keeps its
existing behavior.

**Known, accepted gaps** (ADR-0045, stated plainly): a checkout-committed
`env.ANTHROPIC_BASE_URL` could make the stream's self-reported identity
unfalsifiable (ruled out of the threat model — checkouts are your own
repositories and run containers are credential-limited, ADR-0013);
`--model` beats the managed default (the harness never passes it; anything
else that does is killed at its first main-agent turn); org-policy drift
between dry-run and Run is caught at the Run's own turn stamps; a
wrong-identity Run spends tokens until its first detected turn.

**Model/effort pairs validate together.** An effort is accepted only when the
*specific* model provably honors it: Claude Code silently runs an unsupported
level as the highest supported level at or below it (`xhigh` runs as `high`
on the 4.6 generation) and silently ignores effort on models without the
setting (haiku-family, sonnet-4-5 and older), so those pairs are refused at
config load, at build, and at the static checks. An unknown future model
paired with an effort is refused too — bake the model alone (`effort = ""`,
the model's own default) or upgrade to a release that knows it. An
organization effort cap that would clamp the baked value fails the dry-run
(and, if it appears later, the post-exit effort check) — never an accepted
downgrade.

Family aliases (`sonnet`, `opus`, `haiku`, `fable`) load with a
pin-the-dated-ID warning and resolve the default to the newest model of that
family; at the identity checks an alias accepts any executed model of
exactly that family, while a pinned/full ID requires an exact resolved match
(an undated pin the provider resolves to a dated ID fails — pin the dated
ID). `default` and `opusplan` are **refused at config load**: neither names
a single checkable model (`default` floats with the account tier;
`opusplan` is a two-model mode). `effort` on a driverless (Flight Deck)
type is also refused — interactive scope bakes only `/etc/theozolith/model`,
and no Flight Deck runtime consumes a baked effort yet.

Run evidence reports the **observed** model reconciled from the session
stream's MAIN-agent signals (init announcement, executed main-agent turns,
usage records), with any drift in the bundle's `model_note`; subagent and
helper models are legitimate and excluded from identity reconciliation.

The identity behavior is verified live against **Claude Code 2.1.232** by
the worker package's opt-in suite (`THEOZOLITH_LIVE_CLAUDE=1
uv run pytest worker/tests/test_live_enforcement.py`), including: the
managed default binding the main session and outranking checkout
project/local settings; `--model` escaping by design (the documented gap);
subagent frontmatter running its own model while the main agent holds the
default; every effort surface losing to the managed env pin; the Stop-hook
applied/clamped-effort payloads; the dry-run passing on a healthy image and
failing loud on a bogus model; and the monitored `run_harness` end to end —
checkout CLAUDE.md reaching the task session, checkout hooks firing, and a
clean identity record with the journaled applied effort. The suite installs
and removes real `/etc/claude-code` (and `/etc/theozolith`) policy — **run
it only in an isolated Linux container**. One case stays outside it: a real
organization effort cap is server-side Enterprise policy no local fixture
can create (`test_org_effort_cap_fails_the_dry_run`, opt-in via
`THEOZOLITH_LIVE_CLAUDE_ORG_CAP=1` against a capped credential; the clamp
observation it relies on is proven live by the Stop-hook clamp test and in
units by the journal tests).

Removed with **no fallback** (a leftover export now fails the driver loudly):
`IMPLEMENTER_MODEL` / `REVIEWER_MODEL` / `THEOZOLITH_MODEL`, the Stack `[env]`
model override, and the `--model` invocation flag. Custom drivers (ADR-0042)
no longer declare a `default_model` class attribute — delete it from your
`drivers/*.py` when upgrading.

Migration notes: when you first set (or next change) `model` on a worker type,
**bump `base` to a release that ships `theozolith-adapter` in the same edit** —
an older base fails the build loudly ("command not found", or the CLI-version
preflight above), and both edits change the tag anyway, so it costs one
rebuild. Worker types with no `model`/`effort` keep byte-identical tags across
this release and rebuild nothing. Images built by the earlier fail-closed
revision (with the `availableModels` allowlist and
`forceRemoteSettingsRefresh`) keep working — they are tolerated as *stricter*
(subagents stay pinned to the model there) until their next rebuild picks up
the default-only artifact.

## Job-dir ownership

Run containers write into the bind-mounted job directory (`THEOZOLITH_JOBS_DIR`). Keep
the files owned by the driver user either by building the image with
`--build-arg OZOLITH_UID=$(id -u)` (or matching uid in the image recipe) or by setting
`THEOZOLITH_CONTAINER_USER=$(id -u):$(id -g)`.

## Repo mirror cache

Each Run's checkout is a reference clone (`git clone --reference <mirror> --dissociate`)
off a node-local bare mirror per repo (`THEOZOLITH_MIRRORS_DIR`, default
`/var/tmp/theozolith/mirrors`), so the per-Run download is a ref advertisement instead
of the whole history (#51). The mirror is driver-owned and never mounted into any
container; it is created lazily on the first claim per repo, refreshed under a per-repo
file lock before each checkout, and crash-cleaned (partial mirrors, stale locks) by the
boot sweep. Unlike jobs dirs the mirror root is deliberately node-shared across Stacks —
the lock makes concurrent drivers safe, and every driver on the node reuses one download.
Mirror creation or refresh failures fail the Run as a pre-session infra failure under the
normal retry budget (ADR-0016) — a stale mirror is never silently used.

**Trust boundary.** The cache is a persistent, driver-owned resource on a multi-user
box, so the drivers treat nothing about it as given. The installer provisions
`/var/tmp/theozolith` and `/var/tmp/theozolith/mirrors` before the service starts,
owned `ozolith:ozolith` with no group/world write (`0750`). At every use the driver
re-validates fail-closed (with `lstat` — symlinks are rejected, never followed): the
root and its containing directory must be real directories the driver (or, for the
parent, root) owns, with no group/world write access — a world-writable sticky parent
like `/var/tmp` itself is acceptable; a custom `THEOZOLITH_MIRRORS_DIR` is held to
exactly the same rules, so put it somewhere whose parent only root or the service user
can write. Mirror, staging, and lock entries must be real, driver-owned
files/directories; anything else fails the Run closed (`infra` lane) without deleting
or running git inside the suspect path. Persistent git config inside a mirror is never
trusted either: before any authenticated operation the driver rewrites the mirror's
config to a known-minimal form (exact configured clone URL, mirror refspec, hooks
disarmed, alternates dropped) and the update fetch passes the URL and refspec on the
command line — a pre-created or tampered repo cannot redirect credentials.

**Timeouts.** `THEOZOLITH_GIT_TIMEOUT_SECONDS` (default `600`, must be positive)
bounds each mirror operation separately: the per-repo lock wait, mirror creation, the
update fetch, and the reference clone. On expiry the git subprocess is killed, partial
state is cleaned (a timed-out update deletes the mirror — it re-clones on the next
claim), the lock is released, and the Run fails pre-session into the normal `infra`
retry/evidence lane — a stuck holder or waiter never wedges the driver and never
launches a Run container. Size it to a cold full clone of your largest repo over your
slowest link.

**A cache, not a backup.** Mirrors are never backed up and never restored (ADR-0024's
local-copy doctrine does not extend here); deleting the mirror root is always safe and
is the standard remedy for any validation refusal. Checkouts are self-contained
(`--dissociate`), and the next claim re-creates the mirror at the cost of one full
download. `/var/tmp` aging (systemd-tmpfiles) may delete it on its own; the drivers
lazily recreate it with the same ownership rules. Note the cache holds full repository
history and may include private refs of the target repo — treat its confidentiality
like the repo's, and remove it on uninstall (see the cleanup procedure below; a custom
`THEOZOLITH_MIRRORS_DIR` location must be removed separately — the cleanup script only
knows the default root).

**Upgrading from the initial #51 revision.** Roots the earlier revision created
(`0755`, service-user owned) already satisfy the ownership/mode rules and are kept;
each existing mirror is adopted only after entry validation, gets its config rewritten
to the known-minimal form before the next authenticated fetch, and anything failing
validation must be removed by hand (`rm -rf` of the root is always valid) — the driver
refuses to delete what it cannot prove is its own.

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
- **Flight Deck default model** (ADR-0045 §4): set `model` on the deck's worker type
  and the derived image carries the validated ID at the well-known file
  `/etc/theozolith/model` — materialized atomically as a root-owned, non-user-writable
  regular file (0644; symlinked or irregular destinations fail the build), never
  anything under `~/.claude`, which the claude-state volume shadows (ADR-0043). The baked start script launches the session as
  `claude --model "$(cat /etc/theozolith/model)"` (bare `claude` when no model is
  baked), so every container start deterministically begins at the definition's
  default, while `/model` stays free within a session: the CLI persists a `/model`
  choice to `~/.claude/settings.json` on the state volume, and the flag outranks it
  at the next start without rewriting it (`--resume` honors the flag the same way;
  verified on Claude Code 2.1.232, evidence on issue #39). Restart = reset to
  definition; switch = session state. `effort` stays rejected on driverless types
  (see the worker-types section above).

## Flight Deck knowledge & state (ADR-0043 as amended by ADR-0048)

A Flight Deck's `~/.claude` conflates two classes of content, and they are
split across mounts with different cardinality (`<stack>` = the Flight Deck
Stack's name; the worker type declares the volumes with a literal `{stack}`
placeholder that the Control Node substitutes at resolution time, so two
same-type Flight Decks on one node get distinct per-instance volumes):

| Mount | Mountpoint | Cardinality |
| --- | --- | --- |
| `<stack>-claude-state` (named volume) | `/home/ozolith/.claude` | **one per Flight Deck** — runtime state (sessions, transcripts, `--resume`); never shared, never worker-visible |
| `<state-dir>/knowledge` (read-only bind) | `/var/lib/theozolith/knowledge` | **one per node** — the applied pinned knowledge trees the Node Daemon exports; every deck on the node reads the same content |
| `<stack>-logs` (named volume) | `/var/log/flightdeck` | one per Flight Deck |

(One-hop remote access into a Flight Deck — and the per-instance machine-identity
volume it will add — is split out and tracked separately; see
`configs-example/README.md` for status and the ways in today.)

**Knowledge is the applied pinned tree, mounted read-only — not a clone, not a
bake** (ADR-0048; the ADR-0043 writable clone and its promote workflow are
retired). Knowledge is authored in the Config Repo's `knowledge/<name>/` trees,
compiled by `theozolith config ingest`, and distributed to nodes with the rest
of the config. The daemon maintains a stable export at
`/var/lib/theozolith/knowledge/<name>/`; when the desired distribution becomes
empty (the Config Repo dropped its last `knowledge/` tree and `drivers/`), the
export retires with it — a deck never keeps mounting deleted knowledge. The
deck's worker type SELECTS its tree with `knowledge = "knowledge/<name>"`
(validated at config load exactly like a driver type's reference: ingested
pin joined, compiled tree present — a dangling reference refuses the load;
per-Stack overrides are rejected). Control injects the selection as
`THEOZOLITH_KNOWLEDGE_TREE`, and `flightdeck-start` points the `~/.claude`
knowledge directories at that compiled tree — **failing loud when the node has
not converged it yet** (docker restart policy retries until it has; a deck
never silently runs without its knowledge):

```
~/.claude/skills     -> /var/lib/theozolith/knowledge/<name>/skills
~/.claude/agents     -> /var/lib/theozolith/knowledge/<name>/agents
~/.claude/workflows  -> /var/lib/theozolith/knowledge/<name>/workflows
~/.claude/CLAUDE.md  -> /var/lib/theozolith/knowledge/<name>/CLAUDE.md
```

An edit lands by editing the Config Repo, committing, and running
`theozolith config ingest`; it reaches **every deck on every node** through
ordinary config distribution and is picked up on **agent-CLI restart** (the
export swaps whole trees by atomic rename beneath the stable mount, so a
running session keeps what it loaded and no container is ever rebuilt or
recreated by a knowledge-CONTENT change — the pin stays out of the deck's
image identity by construction). Changing the SELECTED tree is different: it
changes the container spec (the injected env), so the deck is recreated on the
new tree. The same content edit re-tags exactly the driver worker types whose
definitions reference the tree — driver workers keep **baking** knowledge into
their derived images, so Run images stay standalone. A chmod is a content
change too: the distribution hash and per-tree pins cover each file's
normalized executable state, so flipping a skill script's exec bit
redistributes and re-tags like any edit.

**Cross-node** transport is config distribution — there is **no auto-sync
daemon and no shared network filesystem, ever**.

**Warnings.**

- The symlink carve-out is **Flight-Deck-only**. Run containers never mount
  knowledge (it would break pin reproducibility and open a prompt-injection
  persistence channel); a cache volume aimed at a `.claude` path is refused by
  construction.
- **Never** run `theozolith-knowledge sync` against a Flight Deck's `~/.claude`:
  it replaces the symlinks with copied files, silently detaching the deck from
  the applied tree (the mount itself is read-only, so nothing can write
  through the links).
- `~/.claude.json` lives *outside* `~/.claude` and is not on the state volume, so
  it regenerates when the container recycles (accepted v0 gap). The deck's model
  preference is NOT part of this gap: a `/model` choice persists to
  `~/.claude/settings.json` *on* the state volume and survives recycles — it is
  the baked `--model` flag (see the Flight Deck section) that resets every
  container start to the definition's default.

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
# The node-shared scratch root: job dirs plus the repo mirror cache (#51).
# Removed AFTER the daemon and drivers are down (the disable above killed the
# whole cgroup). The mirror cache holds full repository history and may
# include private refs — deleting it is part of data removal, not just tidy-up.
# A custom THEOZOLITH_MIRRORS_DIR (or THEOZOLITH_JOBS_DIR) location is NOT
# covered by this line — remove it separately.
sudo rm -rf /var/tmp/theozolith
```

After this the box is clean: secrets lived only in tmpfs and the encrypted Control Node
store, both now gone (including the root-mediated partition, its unit, its service
user, the managed venv, and every `/usr/local/bin` link the bootstrap owned — a link at
those names that pointed elsewhere is left alone, exactly as the bootstrap found it).
Orphaned run containers from a mid-Run kill are reaped by the daemon on its next
start; the zombie-claim janitor restores the GitHub claim state.
