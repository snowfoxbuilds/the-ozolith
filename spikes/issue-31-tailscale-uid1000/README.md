# Spike: uid-1000, capability-free tailscaled (issue #31 gate)

Runnable harness for the Step-0 gate of
[#31](https://github.com/snowfoxbuilds/the-ozolith/issues/31): verify that
`tailscaled` running as **unprivileged uid-1000 `ozolith`** — no TUN device,
no `NET_ADMIN`, no added capabilities, `--tun=userspace-networking` — still
delivers working Tailscale SSH one-hop access, a state-volume-stable
identity, and file-form auth-key hygiene.

This is spike scaffolding, not product code, and it must not grow into the
production `flightdeck-start` lifecycle (that lands via #31 with its own
seven-point lifecycle and executable tests). It lives in the repo so the
gate procedure is reviewed and versioned; the *results* are recorded as a
sanitized comment on #31. The dev/CI container cannot run it (no Docker
engine; seccomp denies namespace creation even to root), so it runs
out-of-band on a Docker-capable host that is on the tailnet.

## The binary under test is the binary that ships

The Dockerfile installs the **exact** static archive the production example
pins (`deploy/configs-example/worker-types/flightdeck.toml`, PR #35): same
version, same URL, verified against the official SHA-256 published at
<https://pkgs.tailscale.com/stable/>. Gate evidence for one release says
nothing about another, so:

- do **not** change `TS_VERSION` between the gate's phases — initial
  enrollment, retained-volume replacement, and state-loss re-enrollment must
  all run the same binary (identity survival across the *same* binary is
  part of what the gate proves);
- if the production pin moves, update the Dockerfile's version + checksum in
  lockstep and re-run all five checks (a test on the #35 branch enforces
  that the two pins match);
- the entrypoint prints `tailscale version` so the exact release lands in
  the recorded evidence.

## Prerequisites

- A GNU/Linux host with an **ordinary root-daemon Docker**, on your tailnet.
  Run the script however your docker socket access requires —
  `sudo ./run-spike.sh` is the expected shape and fully supported (that sudo
  is host docker-socket access only and says nothing about the container's
  privilege model). **Rootless Docker and userns-remap daemons are
  unsupported**: there the container's uid 1000 is not host uid 1000, so the
  key delivery below cannot mean what the gate needs it to mean. The script
  **positively detects both** (`docker info` SecurityOptions reporting
  `name=rootless` / `name=userns`) and refuses to run — before any key is
  read. The uid-1000 readability preflight below stays as a second,
  independent check of the actual mount; it is not the daemon-mode detector.
- `flock` from util-linux (present on any normal GNU/Linux) — the run lock
  below refuses to run without it.
- A second tailnet machine to ssh from.
- A **reusable** auth key (`tskey-auth-...`). A plain reusable key is fine
  for the spike; the production key policy (tagged `tag:flightdeck`,
  ACL-bounded) is a separate concern. Plan to **revoke it after the gate**
  (see Cleanup).
- Tailnet policy allowing SSH to the spike node as user `ozolith`: both a
  network-layer grant for `tcp:22` **and** an `ssh` rule. Issue #31
  specifies `action: accept`. Note what that means: `check` would ask
  nothing of the (headless) spike container — it re-authenticates the
  **initiating** user in a browser on the machine you ssh *from* when a
  connection is new or check-expired. `accept` is the deliberately weaker
  policy the production docs adopt (no interactive check-ins on reconnect,
  no re-verification of the human behind the key), so the gate runs with
  the same policy the Flight Deck will use.

## Run

```sh
./run-spike.sh          # fresh run: prompts for the key, input hidden
```

or pipe the key from a secret manager (never from a durable plaintext
file): `pass show tailnet/spike | ./run-spike.sh`.

### One run at a time: the run lock and the debris audit

Before anything else — before the image build, the volume probe, and any
key intake — the script takes a **non-blocking exclusive `flock(2)` on the
`/dev/shm` directory inode itself**, opened read-only on fd 9, and holds
that fd until cleanup completes. No lock *file* is ever created, opened, or
chmodded: the legacy `/dev/shm/ozolith-ts-spike.lock` pathname is dead —
anything planted there (a symlink included) is never touched, and a
leftover from an older revision may simply be deleted. A second invocation
while a run is live fails immediately with a value-free message: it reads
no key, runs no container, and cannot remove the live run's containers or
tmpfs directories.

The lock is kernel-owned and dies **with the harness process alone**: every
external command the script spawns after acquisition runs with fd 9 closed
(one `ext` helper routes them all; the backgrounded log follower
additionally drops the fd on the fork itself), so only the harness pins the
lock. A run killed with `kill -9` therefore releases it the moment the
process dies — even while children it started are still alive — and what
protects the *next* run from that run's leftovers is the debris audit.

With the lock held (still before any key intake), the script **audits for
debris and refuses to proceed** — nothing is ever removed automatically —
if any of these exist:

- a container whose name starts with `ozolith-ts-spike` — that covers the
  **legacy fixed main name** (`ozolith-ts-spike`, used by older harness
  revisions) and every per-run `ozolith-ts-spike-main-*` /
  `-preflight-*` / `-keyscan-*` name;
- a stale key directory (`/dev/shm/ozolith-ts-spike-key.*`);
- a stale evidence directory (`/dev/shm/ozolith-ts-evidence.*`).

Any of those means a prior run died before its provable cleanup finished —
a surviving container may still hold a **prior** auth key readable through
its bind mount, and a stale directory may hold one in plaintext. The
guidance is always the same: inspect and remove the debris by hand
(`docker rm -f <id>` / `rm -rf <dir>`), **revoke the key that run used**,
then re-run. A failing audit query (e.g. `docker ps` erroring) also refuses
the run: an unauditable field proves nothing.

### Fresh vs reuse is decided from actual state

The script probes the `spike-tailscale-state` volume **content**, not its
mere existence, before deciding anything (and before reading a key):

- **volume absent** → fresh enrollment (a new volume is created);
- **volume present with a non-empty `tailscaled.state`** → keyless reuse
  (check 3): no prompt, nothing secret mounted;
- **volume present without a usable state file** (missing or zero bytes —
  what a failed or interrupted first attempt leaves behind) → **fresh
  enrollment through the existing volume**. Nothing is deleted: recovery
  from a failed initial attempt is just running the script again with the
  same key; the entrypoint applies the same non-empty-state rule inside the
  container, so the branches cannot disagree;
- **probe error** (docker unreachable, volume inspect or the in-container
  state check failing) → the run stops before any key is read; the script
  never guesses fresh-vs-reuse.

A **corrupt but non-empty** state file is deliberately NOT recovered
automatically: the reuse branch runs and `tailscale up` fails visibly —
destroying possibly-good identity state is an operator decision
(`docker volume rm spike-tailscale-state`), never a script default.

Auth-key hygiene, enforced by the script:

- the key lives in a **mode-0700 directory on a verified tmpfs**
  (`/dev/shm`) — the directory is the barrier against other host users —
  with a single leaf file the deliberately selected container can read as
  uid 1000 (cross-UID delivery, so `sudo` invocations work):
  - invoked as root (sudo): leaf `chown 1000:1000`, mode `0400`;
  - invoked as uid 1000: leaf mode `0400` (same uid as the container);
  - invoked as any other uid: leaf mode `0444`, readable only through the
    0700 directory (bind mounts expose the leaf *inode*, so inside the
    container only the leaf's own owner/mode decide access; on the host the
    directory decides);
- a **preflight container proves the mounted leaf is readable as uid 1000**
  — the exact mount enrollment will use, shared with the state-volume
  scanner — without printing a byte of the value; if it fails (e.g. on an
  unsupported rootless/userns-remap daemon), the run stops before any
  enrollment is attempted;
- **every container that receives the key mount is tracked, and its removal
  is proven** — and *all* of them run under **per-run, collision-safe
  names** (`ozolith-ts-spike-main-<pid>-<rand>`, `-preflight-…`,
  `-keyscan-…`): there is no fixed container name anywhere, so cleanup can
  only ever remove names recorded for *this* run and can never consume
  another run's container. Each name is recorded *before* its `docker run`
  is invoked, so even a partially created container or a client-side docker
  failure stays cleanup-visible. The scratch runs' `--rm` is convenience
  cleanup, not proof — an interruption or a daemon/client failure can leave
  the container behind, and a surviving container keeps the key readable
  through its bind mount even after the host copy is deleted. Cleanup
  queries, removes when present, and re-queries each of the three names on
  every exit path; a scratch container already gone through its normal
  `--rm` counts as clean. Stale containers from a previous interrupted run
  are **rejected before any key intake** by the debris audit above —
  nothing is deleted automatically; removing them, and revoking the key
  that run used, is an operator action;
- **no log-derived byte reaches output before it is scanned**: the
  container log is followed into the tmpfs evidence directory only — never
  streamed or `tee`'d through the terminal. On a fresh run the ready-time
  excerpt is a snapshot that is exact-key-scanned first and displayed only
  on a clean miss (the scanned bytes and the displayed bytes are the same
  bytes); if the container dies early, its partial log is captured and
  scanned the same way before any diagnostic is shown. A hit, a scanner
  error, or an uncapturable log **withholds the content**, fails the run
  with a value-free message, and demands immediate key revocation. Reuse
  runs use the same capture-only path (they are keyless, so their log is
  shown without a scan — no key entered the run to leak);
- the key reaches the container only as a read-only mount of that leaf —
  `file:` form only, mirroring the production tmpfs → read-only `VAR_FILE`
  delivery; the value never enters argv, shell history, environment values,
  or the build context. A path argument is accepted **only** if the file
  already lives on a tmpfs; durable host files and any path inside the
  build context are refused;
- every exit path (success, failure, SIGINT, SIGTERM) removes the **entire
  key directory** and prints the removal as proof (see Cleanup for the
  failure semantics: an unprovable teardown fails the run);
- if the `spike-tailscale-state` volume holds a non-empty
  `tailscaled.state`, the run is keyless: no prompt, nothing secret mounted
  (that *is* check 3). The retained volume holds machine state — node
  keys — not the auth key; the sweep below proves the auth key is not on
  it.

The container starts with **no** `--cap-add`, **no** `--device`, **no**
`--privileged` — that absence is the experiment. If the gate fails, capture
the exact error — the failure mode is the finding. Do **not** retry with
`--cap-add`/`--device`: per #31, the privileged fallback is a human/ADR
decision, not a spike workaround.

## The five checks (record each, sanitized, in #31)

### 1. Enrollment via file-form key, uid-1000, no caps

Fresh run (`./run-spike.sh`, no volume yet). The log shows `uid 1000`, the
`tailscale version` block, the fresh-enrollment branch, then `up; status:`
with the node listed. No sudo exists in the image; docker run adds no
capabilities.

### 2. One-hop SSH lands as ozolith in the container fs

While the script waits at its prompt, from another tailnet machine:

```sh
ssh ozolith@spike whoami              # expect: ozolith
ssh ozolith@spike cat /spike-marker   # expect: spike-container-fs
```

### 3. Identity survives container/image replacement

Note the node's 100.x address (`tailscale status` output in the ready-time
log excerpt, which on a fresh run prints only after its exact-key scan came
back clean). Press Enter to finish run 1, optionally `docker build` again
(same pinned version), then re-run `./run-spike.sh`. The volume now holds a non-empty
`tailscaled.state`, so the run is **keyless**: expect the log's "existing
state found" branch, the same 100.x address, no new machine in the admin
console, and ssh still landing.

### 4. State-volume loss permits deliberate re-enrollment

```sh
docker volume rm spike-tailscale-state
./run-spike.sh          # fresh run again — prompts for the same reusable key
```

Expect the fresh-enrollment branch to succeed and a new machine to appear;
prune the stale one in the admin console (that prune is part of the
documented operator procedure).

### 5. No secret value anywhere but the tmpfs leaf

Automated: after you press Enter, the script sweeps for the **exact key
value** using `grep -Ff` with the tmpfs key file as the pattern file (the
value itself never enters argv or output), across:

- image history (`docker history --no-trunc`);
- container environment + mount metadata (`docker inspect` — only the
  non-secret `/run/secrets/ts-authkey` path may appear);
- recorded process argv (`docker top`, captured **twice into separate
  files** — live after ready, and again pre-stop — it uses the host's ps,
  so the slim image needs no tooling);
- the full container log;
- **the `spike-tailscale-state` volume, on every naming surface**: file
  contents, pathnames, directory names, and symlink targets, all swept in
  one pass by archiving the volume into an **ephemeral tar representation**
  inside a scratch container (as uid 1000, through the same leaf mount, so
  the pattern stays a path everywhere) and grepping that archive — tar
  headers carry every name and link target, its data blocks every content
  byte. The scanner **emits nothing**: its stdout and stderr are discarded
  unread (a matching filename or a traversal error message could itself
  carry the key), and only a fixed clean/hit/error status comes back — a
  hit, or an archive/traversal/grep error, is reported without a byte of
  scanner output. This scratch container is key-bearing in its own right,
  so it runs under a tracked per-run name and its removal is proven in
  cleanup like the rest.

Every promised capture is itself **mandatory**: a failed `docker top`,
`inspect`, `logs`, or `history` — or a capture that comes back unreadable
or empty — fails the run before scanning starts; nothing is swallowed with
`|| true`. Every sweep is **tri-state and fails closed**: grep rc 0 is a
secret hit (FAIL), rc 1 is a clean miss (the only pass), and anything
else — a grep error, a docker failure, or a missing/unreadable/empty
evidence capture — is a scanner failure and FAILs the gate too, because an
unscannable surface proves nothing. Any `FAIL` fails the run — and it also
**withholds the sanitized evidence block**, which quotes container-log
lines: if any surface held the key (or could not be proven clean), no
log-derived line is re-emitted, the failure stays value-free, and the
script demands immediate key revocation. The script then prints proof the
key directory is gone. Run the sweep on both fresh runs (checks 1 and 4) —
those are exactly the runs where a key exists.

## Self-test (no Docker, tailnet, or key needed)

```sh
./test-run-spike.sh
```

Shell-level regression coverage with stubbed `docker`, `tailscale`, and
`tailscaled` binaries — CI runs it on every PR (the `spike-harness` job in
`.github/workflows/ci.yml`), so the green check covers the harness itself:

- **one run at a time**: the directory-inode flock run lock is taken before
  anything else and excludes a second run entirely (no key intake, no
  docker calls, no removal of the first run's containers); released on
  success, ordinary failure, SIGINT, and SIGTERM; only the harness process
  holds fd 9, so SIGKILLing *just the harness* while a spawned child is
  still sleeping releases the lock promptly — the surviving child provably
  has no fd 9, and the next run acquires the lock and is stopped only by
  the debris audit; a symlink planted at the legacy lock-file path is
  provably never opened, changed, or chmodded;
- **the debris audit fails closed before any key intake**: stale containers
  of every name generation (the legacy fixed main name and per-run
  main/preflight/keyscan names), stale key directories, stale evidence
  directories, and a failing audit query all refuse the run — and none of
  the debris is removed automatically;
- **mode selection from actual state**: absent volume → fresh; non-empty
  `tailscaled.state` → keyless reuse; existing volume without usable
  state → fresh through the same volume (no deletion); probe errors fail
  closed before any key intake;
- **daemon-mode rejection**: rootless, userns-remap, and an unreachable
  daemon all refuse to run before a key is read; the uid-1000 readability
  preflight fails closed;
- **entrypoint failure propagation**: `tailscale version`, LocalAPI
  readiness (socket alone is not ready), `up` (fresh and reuse), and
  post-up `status` failures all exit non-zero and never print ready;
  corrupt-but-non-empty state fails visibly without being deleted;
  post-enrollment daemon death stays non-zero;
- **required evidence captures**: a failure of either `docker top` snapshot
  (live or pre-stop) fails the run;
- both sweeps' **tri-state** behavior (grep rc 0 = hit fails, rc 1 = the
  only pass, rc ≥ 2 and docker-level failures fail closed;
  missing/unreadable/empty evidence fails closed); the state-volume
  scanner's tar payload is exercised for real on every naming surface —
  content hit, filename-only hit, directory-name hit, symlink-target hit,
  clean volume, and traversal/archive/grep errors — asserting the fixed
  statuses, zero payload output, and that no raw scanner byte (which could
  itself carry the key) ever surfaces;
- **no log-derived output before a clean scan**: a key planted in the
  container log is caught at the ready-time display gate (nothing shown,
  value-free failure, revoke-now warning); a key that arrives *after* the
  ready snapshot is caught by the final sweep, which withholds the evidence
  block and every line it would have quoted; early-failure diagnostics are
  displayed only after a clean scan, and a log that leaks or cannot be
  captured/scanned stays withheld with the revoke-now warning;
- **cleanup**: every step attempted regardless of earlier failures; absence
  proven for **every container that received the key mount** — the main
  container plus the preflight and state-volume-scanner scratch containers,
  all tracked by per-run name recorded before their `docker run`
  (auto-removed scratch containers count as already clean; lingering ones
  are removed with proof), and **only current-run names are ever removed**;
  per-container `docker rm` failures and unprovable removal turn even a
  passing run non-zero (with the value-free revoke-now warning), with later
  cleanup steps still executing; an original non-zero status is preserved;
  exercised on normal exit, ordinary failure, and SIGINT/SIGTERM parked
  inside each key-bearing scratch operation;
- the key value never appears in the harness's output, in any argv, or in
  any container name.

Run it after any edit to `run-spike.sh` or `entrypoint.sh`; it exits
non-zero on any regression.

## Evidence

Each run ends with a sanitized evidence block (harness commit, image id,
run mode, uid line, exact `tailscale version`, branch taken, sweep result)
to paste into #31. The corrected-evidence comment on #31 must contain, in
full:

- the **harness commit** the runs used (printed in every block — all runs
  must use the same commit) and the **image id** (also printed in every
  block; a mandatory capture, so a run cannot end without recording it);
- **one block per phase** — fresh enrollment (check 1), keyless reuse
  (check 3), state-loss re-enrollment (check 4) — showing each run's
  lifecycle: mode line, uid line, version, branch taken, and the proven
  cleanup;
- the outputs of the two **manual SSH checks** (`whoami`, `/spike-marker`
  — check 2) from another tailnet machine;
- your **identity observations** (same 100.x address and no new machine
  for check 3; a new machine plus the stale prune for check 4);
- the **exact-key sweep line from both fresh runs** (check 5 — printed in
  those blocks as `key-absence sweep: passed on all six surfaces …`);
- a closing confirmation that the **auth key was revoked** in the admin
  console after the Cleanup steps below.

## Caveat worth recording

With `--tun=userspace-networking`, *inbound* Tailscale SSH works but
*outbound* dials from inside the container to the tailnet need the SOCKS5/
HTTP proxy. The Flight Deck use case is inbound-only, so this is fine —
but note it in #31 so nobody later assumes outbound works.

## Cleanup

Per-run cleanup is automatic, **failure-aware, provable, and
ownership-safe**: every exit path (success, failure, SIGINT, SIGTERM)
attempts every teardown step — log follower, **every container that
received the key bind mount** (the main container plus the preflight and
state-volume-scanner scratch containers, all tracked under per-run names
recorded before their `docker run`), evidence directory, key directory —
regardless of earlier failures, then verifies each container is absent
(query → remove when present → re-query via `docker ps -a`; a scratch
container already gone through its normal `--rm` counts as clean) and the
key directory no longer exists. Cleanup removes **only the names recorded
for the current run** — it never deletes a container it did not create,
and there is no unconditional `docker rm` of any fixed name. Any step that
fails, or any absence the script cannot prove, makes the run exit
**non-zero even if the gate checks themselves passed** — a run that cannot
prove its own teardown is a failed run. If the removal of *any* key-bearing
container cannot be proven on a run that mounted a key, the script prints a
prominent (value-free) warning: a bind mount can keep the key readable
inside a surviving container even after the host copy is deleted, so
**revoke the key immediately** in that case instead of waiting for the
end-of-gate cleanup below. The run lock is held until this cleanup
completes, then released with the process.

Because every container is removed (with proof) by the run that created it,
end-of-gate cleanup is only the volume and the image:

```sh
docker volume rm spike-tailscale-state
docker rmi ozolith-ts-spike
```

If a container named `ozolith-ts-spike*` still exists at this point, it is
debris a run could not clean up (the next run would refuse it): inspect and
remove it by hand — `docker rm -f <id>` — and treat its key as burned. A
plain `ozolith-ts-spike` container can only come from a **pre-lock harness
revision** that used that fixed name; it bind-mounted a key too, so the
same rule applies.

Then delete the spike machine from the tailscale admin console and **revoke
the auth key** (admin console → Settings → Keys → revoke): the spike key
was minted for this gate and its job is done. Nothing else persists — the
tmpfs key directory was already removed (every exit path prints the proof),
so after revocation no live or durable copy of the key exists anywhere.
(The current harness creates no lock file — the run lock is an `flock(2)`
on the `/dev/shm` directory inode, gone with the process. An empty
`/dev/shm/ozolith-ts-spike.lock` left behind by an older revision holds no
data, is never touched again, and may be deleted or left to vanish on
reboot with the tmpfs.)
