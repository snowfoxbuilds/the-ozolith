# configs-example — a starter Config Repo

A complete, minimal Config Repo (ADR-0006/0048): copy it anywhere you like
(or host it on a git server), edit it, commit, and run
`theozolith config ingest <path-or-url>` on the Control Node — ingest lints
it, resolves the mechanical pins, compiles `knowledge/`, and commits the
machine-owned pinned build the service loads. Never edit the pinned build
(`configs/`) itself. The example declares two pipeline workers (Implementer,
Reviewer) as process Stacks, one **Flight Deck** as an interactive container
Stack, and one **custom driver** (`hello-logger`) demonstrating ADR-0042 —
every Stack staged at `state = "stopped"`. `base` images are tag-only: this
repo carries no computed pins (ADR-0048) — ingest resolves each tag to its
digest and records it in the pinned build's pins.toml (digest-pin a base
yourself only when the digest is a human decision, e.g. no registry access
at ingest time). The four example bases are
`ghcr.io/snowfoxbuilds/theozolith-run-claude:0.3.0`, a **private** first-party
image; before the first ingest, store a GHCR pull credential so ingest can
resolve its digest (and so nodes can pull it at build time, ADR-0049):

```sh
theozolith secret set registry:ghcr.io   # value: <github-user>:<PAT with read:packages>
```

Public bases need no credential (ingest resolves them anonymously). What
stays yours to fill in before flipping a Stack to running: the tailscale
checksum (a fail-closed placeholder ingest refuses on a running Stack),
secret values, and real workspaces.

## Knowledge (`knowledge/`, ADR-0048)

`knowledge/claude-dev/` is a knowledge root (ADR-0009 layout: `AGENTS.md`,
`skills/`, optionally `agents/`, `workflows/`) referenced by the claude worker
types as `knowledge = "knowledge/claude-dev"`. Ingest compiles it and pins its
content hash (the pin covers each file's executable state too — a chmod
redistributes like any edit); driver workers bake the compiled tree into
their derived images (editing it re-tags exactly the types that reference
it). The Flight Deck's own `knowledge` field selects which node-applied tree
its read-only mount serves — the deck fails loud until the node has converged
that tree, content edits reach it on agent-CLI restart, and changing the
selected tree recreates the deck.

## Custom drivers (`drivers/`, ADR-0042)

`drivers/hello_logger.py` is a minimal custom worker type: code that lives in
this Config Repo and runs on nodes without forking the product. Its worker type
(`worker-types/hello-logger.toml`) names it with `driver = "drivers/hello_logger"`
and its Stack (`stacks/hello-logger.toml`) is staged stopped; flip it to running
to deploy. `drivers/` is **git-native only** — the web UI never touches it,
because a write here is code execution with driver credentials on every node
that runs it. See the "Custom drivers (ADR-0042)" section in `deploy/README.md`
for the full authoring, convergence, and trust model.

This directory is the sanctioned private-side surface for deployment detail the
product never carries (the guardrail test enforces that boundary): network
transports, site-specific wiring, and the Flight Deck's baked start sequence
all live here, never in product code or images.

## Flight Deck one-hop access (tailscale)

SSH/VSCode into a Flight Deck normally takes two hops: into the node's host,
then `docker exec` into the container. The Flight Deck bakes a userspace
`tailscaled` so each instance is its **own tailnet machine** — reach it in ONE
hop:

```sh
ssh ozolith@flightdeck-box1          # Tailscale SSH; lands as ozolith in the container
```

and in VSCode: **Remote-SSH → Connect to Host → `ozolith@flightdeck-box1`**.
The hostname is the Stack's `FLIGHTDECK_TS_HOSTNAME` (`stacks/flightdeck.toml`),
convention `flightdeck-<name>`; MagicDNS resolves it on your tailnet. Userspace
networking needs no TUN device, no `NET_ADMIN`, and no `devices`/`cap_add`
passthrough — `tailscaled` itself runs as the unprivileged `ozolith` uid, so a
tailnet ACL mistake can never yield more than an `ozolith` session. The
uid-1000 capability-free path is gate-verified (issue #31 evidence). Note the
deliberate scope: **inbound** Tailscale SSH is the use case; outbound dials
from inside the container to the tailnet would need the userspace SOCKS5/HTTP
proxy and are not wired.

### Pin a supported binary

`worker-types/flightdeck.toml` pins the static binaries twice: `TS_VERSION`
(shipped at **1.102.2** — the exact release the #31 gate evidence was produced
with, via the `spikes/issue-31-tailscale-uid1000/` harness) and a SHA-256 that
ships as a FAIL-CLOSED placeholder. Before the first build:

- confirm the pinned release is still **currently supported** by Tailscale and
  pick current stable if it is not; never select a release below **1.98.9**
  (security floor). If you move off the pinned version, the gate evidence no
  longer covers the binary you ship — re-run the spike harness on it;
- paste the official SHA-256 for your arch/version from
  <https://pkgs.tailscale.com/stable/> over the placeholder. The build stops
  until you do; an unverified binary is never installed.

### Mint the enrollment key

In the Tailscale admin console, mint **one** auth key that is:

- **reusable** — the same key re-enrolls any Flight Deck of this type, and
  re-enrolls one whose state volume was wiped;
- **tagged** `tag:flightdeck` — identity comes from the tag, not a user;
- **ACL-bounded** — the tag's ACLs are the whole blast radius.

Store it once as the named secret the worker type references:

```sh
theozolith secret set flightdeck-tailscale-authkey   # paste the tskey-auth… value
```

It is delivered by the standard machinery only: encrypted at rest → node-scoped
TLS pull → host tmpfs → read-only mount at `/run/secrets/…`, referenced as
`TS_AUTHKEY_FILE`. It is consumed **only while the enrollment completion
marker is absent** from the tailscale-state volume (first enrollment, a retry
after a failed or interrupted attempt, or after deliberate state loss);
thereafter identity lives on `{stack}-tailscale-state` and survives image
rebuilds. There is no durable copy of the key anywhere but the encrypted store
and that tmpfs, which evaporates with the daemon/reboot. **Hardening (per
instance, ADR-0047):** as each Flight Deck **enrolls successfully** (its
container reached the running session at least once), unbind the key for that
instance with `TS_AUTHKEY = ""` in its Stack's `[secrets]` — siblings still
enrolling keep the type's default binding. Once every instance has enrolled,
remove the `TS_AUTHKEY` line from the worker type's `[secrets]` (and the
per-Stack unbinds with it).

### Enrollment completion is an explicit marker, not the state file

A non-empty `tailscaled.state` is *not* proof of successful enrollment:
`tailscaled` writes its machine key before auth-key registration completes, so
a **rejected** enrollment (bad or expired key, rejected flags) leaves a
non-empty state file behind. `flightdeck-start` therefore decides enrollment
from an Ozolith-owned completion marker
(`/var/lib/tailscale/.theozolith-enrolled-v1`, on the same state volume):

- **marker present** → reuse the existing identity, no `TS_AUTHKEY_FILE`
  needed (the remove-the-mapping hardening keeps working);
- **marker absent** → require a readable `TS_AUTHKEY_FILE` and run the fresh
  enrollment path — *even over a non-empty `tailscaled.state`* left by a prior
  failed attempt, so Docker's restart policy (or a corrected key) retries
  enrollment instead of dead-ending on keyless reuse. The state file itself is
  never deleted or rewritten automatically.

The marker is promoted **atomically** (temp file + same-volume rename), and
only after `tailscale up` returns success — a crash mid-write can never leave
a false success marker. If the container is interrupted *after* successful
enrollment but *before* promotion, the next start simply re-enrolls: the key
is reusable, so this is safe (at worst the admin console shows a machine
re-registering). State-volume loss removes state and marker together, which
correctly returns the instance to deliberate enrollment.

### Tailnet ACLs

Two independent layers must BOTH allow the connection on a deny-by-default
tailnet: a **network-layer grant** (may TCP/22 packets reach the machine at
all?) and an **SSH authorization rule** (who may the session become?). A
policy carrying only the `ssh` rule silently fails once the default
allow-all grant is removed. In your tailnet policy file:

```jsonc
{
  "tagOwners": {
    "tag:flightdeck": ["autogroup:admin"]   // who may mint tag:flightdeck keys
  },
  // Layer 1 — network access: least-privilege grant, the SSH port only.
  "grants": [
    {
      "src": ["autogroup:member"],          // tighten: a dedicated operator group
      "dst": ["tag:flightdeck"],
      "ip":  ["tcp:22"]
    }
  ],
  // Layer 2 — SSH authorization: who lands, and as which user.
  "ssh": [
    {
      "action": "accept",                    // the deliberate tradeoff — see below
      "src":    ["autogroup:member"],        // keep in lockstep with the grant
      "dst":    ["tag:flightdeck"],
      "users":  ["ozolith"]                  // sessions land as the container's ozolith
    }
  ]
}
```

Narrow `src` in BOTH rules to the smallest set of identities that actually
operate Flight Decks (a dedicated `group:flightdeck-operators` beats
`autogroup:member`), and add `sshTests` for those identities so a policy edit
that would revoke — or broaden — access fails at policy-check time instead of
in production.

On `action`: `check` asks nothing of the destination — it re-authenticates
the **initiating** user (a browser prompt on the machine you SSH *from*) when
a connection is new or its `checkPeriod` has lapsed, so a headless Flight
Deck is fully compatible with it. `accept` is used here as a deliberate,
weaker-verification choice: it avoids those interactive check-ins on
new/check-expired connections (IDE remoting reconnects often), at the cost of
never re-verifying the human behind the initiating device. If periodic
re-verification matters more to you than uninterrupted reconnects, use
`check` with a `checkPeriod` you can live with — either way, keep the blast
radius small with a tight `src` and the `tag:flightdeck` ACLs.

MagicDNS must be enabled for `ssh ozolith@flightdeck-<name>` to resolve; without
it, use the tailnet IP.

### Start lifecycle: every failure is a failed container

`flightdeck-start` (baked by the worker type's setup) is deliberately
fail-fast — a Flight Deck that cannot bring up its tailnet access exits
non-zero immediately, and Docker's restart policy owns any retry. (Knowledge
never blocks a start: the read-only mount's symlinks may dangle until the
node converges a distribution, ADR-0048.) There is no in-container retry loop
and no "running but unreachable" state:

- enrollment vs. reuse is decided from the completion marker (see above)
  **before** `tailscaled` launches — the two branches cannot be misrouted by a
  startup race, and a failed prior enrollment cannot be mistaken for a
  reusable identity;
- a fresh enrollment with the auth-key secret missing fails fast with its own
  message (see re-enrollment below);
- the daemon gets a bounded readiness wait; a daemon that dies while starting
  is detected immediately;
- `tailscale up` gets one **bounded** attempt — bounded by the CLI's native
  `--timeout=30s`, because its default is to wait for Running state forever:
  an invalid or expired key fails immediately, and a tailnet that never
  reaches Running fails at the 30-second bound instead of hanging the start;
- after startup, a supervisor watches both `tailscaled` and the tmux session:
  if the daemon dies, the container fails (a restart restores one-hop access);
  if the session ends, the container stops cleanly.

### State-volume loss ⇒ new machine identity

`{stack}-tailscale-state` (`/var/lib/tailscale`) IS the tailnet machine
identity. Delete or recreate it and the Flight Deck re-enrolls with the stored
reusable key on next start as a **new** tailnet machine — the old one lingers in
the admin console as a stale, offline entry. Prune it there (Machines → the old
`flightdeck-<name>` → Delete). This is expected after a deliberate state reset;
it is not an error.

If you applied the hardening (unbound or removed the `TS_AUTHKEY` binding)
**and** the state volume is gone, the fresh start fails fast with a distinct
message: restore the binding — drop the Stack's `TS_AUTHKEY = ""` unbind, or
re-add the line to the worker type's `[secrets]` — restart the Stack to
re-enroll, then re-apply the hardening.
