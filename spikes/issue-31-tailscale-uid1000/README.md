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

- A GNU/Linux Docker-capable host on your tailnet (run the script however
  your docker socket access requires, e.g. `sudo ./run-spike.sh` — that
  sudo is host docker-socket access only and says nothing about the
  container's privilege model).
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

Auth-key hygiene, enforced by the script:

- the key is written to a **mode-0600 temp file on a verified tmpfs**
  (`/dev/shm`) and reaches the container only as a read-only mount of that
  file — `file:` form only, mirroring the production tmpfs → read-only
  `VAR_FILE` delivery;
- the value never enters argv, shell history, or the build context. A path
  argument is accepted **only** if the file already lives on a tmpfs;
  durable host files and any path inside the build context are refused;
- every exit path (success, failure, Ctrl-C) removes the temp file and
  prints the removal as proof;
- if the `spike-tailscale-state` volume already exists, the run is keyless:
  no prompt, nothing secret mounted (that *is* check 3). The retained
  volume holds machine state — node keys — not the auth key; the sweep
  below proves the auth key is not on it.

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

Note the node's 100.x address (`tailscale status` output in the log). Press
Enter to finish run 1, optionally `docker build` again (same pinned
version), then re-run `./run-spike.sh`. The volume exists, so the run is
**keyless**: expect the log's "existing state found" branch, the same 100.x
address, no new machine in the admin console, and ssh still landing.

### 4. State-volume loss permits deliberate re-enrollment

```sh
docker volume rm spike-tailscale-state
./run-spike.sh          # fresh run again — prompts for the same reusable key
```

Expect the fresh-enrollment branch to succeed and a new machine to appear;
prune the stale one in the admin console (that prune is part of the
documented operator procedure).

### 5. No secret value anywhere but the tmpfs file

Automated: after you press Enter, the script sweeps for the **exact key
value** using `grep -Ff` with the tmpfs key file as the pattern file (the
value itself never enters argv or output), across:

- image history (`docker history --no-trunc`);
- container environment + mount metadata (`docker inspect` — only the
  non-secret `/run/secrets/ts-authkey` path may appear);
- recorded process argv (`docker top`, live and pre-stop snapshots — it
  uses the host's ps, so the slim image needs no tooling);
- the full container log;
- **every file on the `spike-tailscale-state` volume**, scanned inside a
  scratch container so the pattern stays a path everywhere.

Each surface reports `ok`/`FAIL`; any `FAIL` fails the run. The script then
prints proof the temporary key file is gone. Run the sweep on both fresh
runs (checks 1 and 4) — those are exactly the runs where a key exists.

## Evidence

Each run ends with a sanitized evidence block (uid line, exact
`tailscale version`, branch taken, sweep result) to paste into #31. Include
one block per phase: fresh enrollment, keyless reuse, re-enrollment.

## Caveat worth recording

With `--tun=userspace-networking`, *inbound* Tailscale SSH works but
*outbound* dials from inside the container to the tailnet need the SOCKS5/
HTTP proxy. The Flight Deck use case is inbound-only, so this is fine —
but note it in #31 so nobody later assumes outbound works.

## Cleanup

```sh
docker rm -f ozolith-ts-spike 2>/dev/null
docker volume rm spike-tailscale-state
docker rmi ozolith-ts-spike
```

Then delete the spike machine from the tailscale admin console and **revoke
the auth key** (admin console → Settings → Keys → revoke): the spike key
was minted for this gate and its job is done. Nothing else persists — the
temp key file was already removed (every exit path prints the proof), so
after revocation no live or durable copy of the key exists anywhere.
