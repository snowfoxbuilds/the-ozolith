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
`ghcr.io/snowfoxbuilds/theozolith-run-claude:main`, a **private** first-party
image CI republishes on every merge to main that touches `worker/` or
`knowledge/` (ADR-0051) — so each ingest re-resolves the tag to the
then-current digest, and your fleet moves bases exactly when you ingest,
never before. Immutable `…:sha-<sha>` tags are pushed alongside for hand
digest-pinning and rollback. Before the first ingest, store a GHCR pull
credential so ingest can resolve the digest (and so nodes can pull the
image at build time, ADR-0049):

```sh
theozolith secret set registry:ghcr.io   # value: <github-user>:<PAT with read:packages>
```

Public bases need no credential (ingest resolves them anonymously). What
stays yours to fill in before flipping a Stack to running: the tailscale
checksum (a fail-closed placeholder ingest refuses on a running Stack),
secret values, and real workspaces.

## The product pin (`product.toml`)

This example deliberately ships **no** `product.toml` — the ownership mode
where the update flow owns the pin: `theozolith build` (a clean checkout's
git SHA) and `theozolith update` (a published release) write the pin into
the pinned build, and ingest carries it forward untouched (ADR-0051; a
fresh install with no pin resolves the latest release at Control Node
startup). Declare one only when you want the Config Repo to own the fleet's
product version — declarative release pinning:

```toml
[product]
version = "0.4.0"
```

A declared pin wins on every ingest, and the report calls out any
divergence from a pin the update flow wrote since the last one.

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

## Flight Deck GitHub identity & workspace

The deck mirrors a snow-maker dev container (`dev-dockers/setup.sh`): a
GitHub-authenticated session with the target repo checked out and every
shell opening inside it. Two bindings make that happen, both consumed by
`flightdeck-start` on **every container start**:

1. **The machine identity.** Create a dedicated machine account, mint a
   fine-grained PAT scoped to issues, PRs, and contents — **no merge
   permission** (never a driver PAT, never a personal token) — and store it
   once:

   ```sh
   theozolith secret set flightdeck-github-token   # paste the PAT
   ```

   At start, `gh auth login` consumes the delivered `GITHUB_TOKEN_FILE`,
   `gh auth setup-git` makes gh the git credential helper, and the git
   commit identity (user.name/user.email) is **derived from the token's
   account** — deck commits are machine-account-authored by doctrine.
   Unlike snow-maker, no human git identity is ever copied in. gh keeps
   the token in `~/.config/gh/hosts.yml` (0600) in the **container layer**
   — rewritten each start, gone on recreate, never on a volume; the
   durable copies remain the encrypted store and the tmpfs leaf only.

2. **The workspace.** `workspace = "owner/name"` on the deck's Stack
   (per-placement, ADR-0047) is cloned **on first start** to
   `/workspace/<name>` on the per-instance `{stack}-workspace` volume — the
   cluster analogue of snow-maker's host bind mount. Branches and
   uncommitted work survive container recreation and image rebuilds; later
   starts leave the checkout alone (never an automatic fetch — the session
   owns the working tree), and anything at the target that is not a git
   checkout fails the start loudly. ssh/mosh login shells and the tmux
   session open inside the checkout (`~/.flightdeck-env`, rewritten each
   start, carries the path to login shells).

Failure is loud, per the start-lifecycle doctrine below: a bound workspace
without the token secret, a bad token, or a failed clone each fail the
container before `tailscaled` launches. A deck with **neither** binding
starts bare with a logged note — `gh` stays unauthenticated and you clone
by hand. Rebinding `workspace` recreates the deck (changed env); the old
checkout stays on the volume beside the new one until you remove it.
Treat `{stack}-workspace` as durable working state: it can hold unpushed
commits, so delete it only when decommissioning the deck.

The session is also **privileged inside the container** (snow-maker
parity): `ozolith` has passwordless sudo, so installing whatever the work
needs is `sudo apt-get install …` away — heavier toolchains like
`build-essential` (the gcc/g++/make/libc-dev metapackage for compiling
native pip/npm dependencies) are installed on demand instead of baked.
sudo grants **no kernel capability**: the container stays capability-free
(no `NET_ADMIN`, no TUN — `iptables` mutation is unavailable by
construction), the tailnet daemon still runs unprivileged (never run it
under sudo), and the read-only knowledge mount cannot be remounted from
inside. Remember the flip side: a prompt-injected session is root in the
container too — the container boundary and the no-merge identity are the
walls, which is why deck sessions stay attended.

## Flight Deck one-hop access (tailscale)

SSH/VSCode into a Flight Deck normally takes two hops: into the node's host,
then `docker exec` into the container. The Flight Deck bakes a userspace
`tailscaled` so each instance is its **own tailnet machine** — reach it in ONE
hop:

```sh
ssh ozolith@flightdeck-box1          # Tailscale SSH; lands as ozolith in the container
ssh flightdeck-box1                  # bare form — needs FLIGHTDECK_SSH_USER (below)
mosh flightdeck-box1                 # same session, roaming/lag-tolerant (UDP grant below)
```

and in VSCode: **Remote-SSH → Connect to Host → `ozolith@flightdeck-box1`**.

The bare `ssh <hostname>` form sends your LAPTOP username, and the non-root
`tailscaled` can only start sessions whose /etc/passwd entry has its own uid —
so the worker type's setup carries an operator-edited `FLIGHTDECK_SSH_USER`
variable that bakes your username as a second passwd entry at the ozolith uid
(same home, same `~/.claude`). Set it, re-ingest, and the changed instruction
hash rebuilds the image; also add the name to the ssh ACL's `users` list
(below). This is a build-time choice by design — the container runs
unprivileged, so no Stack `[env]` could create a user at start time.

mosh bootstraps over that same Tailscale SSH connection, then switches to
direct UDP (ports 60000–61000), which needs its own network-layer grant in
the ACL (below). Inside, tmux ships the snow-maker `tmux.conf` verbatim:
wheel scrolls history, drag-select copies to your local clipboard via OSC 52
(mosh ≥ 1.4 on your machine, and a terminal that permits OSC 52). Hold Shift
while selecting to bypass tmux and use the terminal's native copy. Note the
ssh path is gate-verified (issue #31); mosh's inbound UDP rides the userspace
daemon's netstack forwarding and has not been through a gate — verify it once
on your own tailnet (checklist below); if it fails, plain `ssh` plus the same
tmux clipboard settings is the supported fallback.
The hostname is the Stack's `FLIGHTDECK_TS_HOSTNAME` (`stacks/flightdeck.toml`),
convention `flightdeck-<name>`; MagicDNS resolves it on your tailnet. Userspace
networking needs no TUN device, no `NET_ADMIN`, and no `devices`/`cap_add`
passthrough — `tailscaled` itself runs as the unprivileged `ozolith` uid. Note
what a session is worth: `ozolith` carries passwordless sudo (see the identity
& workspace section), so a tailnet ACL mistake yields root **inside the
container namespace** — still capability-free, with the container boundary,
the read-only knowledge mount, and the no-merge GitHub identity as the
blast-radius walls — which is exactly why the `src` lists below should stay
tight. The
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
  // Using mosh? Add "udp:60000-61000" — mosh bootstraps over tcp:22, then
  // switches to direct UDP in that range.
  "grants": [
    {
      "src": ["autogroup:member"],          // tighten: a dedicated operator group
      "dst": ["tag:flightdeck"],
      "ip":  ["tcp:22"]
    }
  ],
  // Layer 2 — SSH authorization: who lands, and as which user. If you set
  // FLIGHTDECK_SSH_USER in the worker type, list that name here too (or
  // instead) — both names are the same uid-1000 account in the container,
  // and this list, not /etc/passwd, is the authorization boundary.
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

### Verify the interactive surface (operator checklist)

The ssh path is gate-verified (issue #31); mosh over the userspace daemon is
**not** — unit tests and source inspection say it should work, only this
checklist run on a real tailnet says it does. From a laptop on the tailnet,
against a running deck:

1. `ssh <your-name>@flightdeck-<name>` — and the bare `ssh flightdeck-<name>`
   form once `FLIGHTDECK_SSH_USER` is baked and the name is in the ssh ACL's
   `users` list;
2. inside the deck: `command -v mosh-server` prints a path, and
   `locale -a | grep -i en_US` lists the generated UTF-8 locale
   (`en_US.utf8`) — mosh-server refuses to start without it;
3. `mosh flightdeck-<name>` reaches a prompt. The bootstrap itself rides
   tcp:22, so a hang at "waiting for a reply" after a successful bootstrap
   means the UDP session never established — check the `udp:60000-61000`
   grant; a working prompt proves the session runs through the userspace
   netstack;
4. tmux is up (the `flightdeck` session), the mouse wheel scrolls history,
   and a drag-select lands in your LOCAL clipboard (OSC 52 — your terminal
   must permit it);
5. change the client's network (toggle Wi-Fi, switch networks): the mosh
   session freezes and resumes without a reconnect;
6. fallback: with UDP unavailable, plain `ssh flightdeck-<name>` still works
   with the same tmux scroll/clipboard behavior.

Until this passes on your tailnet, treat mosh as expected-to-work and ssh as
the verified path.

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

### The claude-state volume carries a login credential

`{stack}-claude-state` (`/home/ozolith/.claude`) holds the agent CLI's own
config, `.claude.json` — the `/login` credential and the onboarding flag —
because the image bakes `CLAUDE_CONFIG_DIR=/home/ozolith/.claude`. A `/login`
therefore survives container and image recreation for as long as the volume is
retained. `flightdeck-start` seeds a **fresh** volume with only the onboarding
flag (`0600`, written via same-directory temp + atomic rename); an existing
file — zero-byte included — is never rewritten, and an unexpected symlink or
directory at that path fails the start loudly rather than being followed.

Treat the volume as **secret-bearing** durable state: protect any backup or
export of it like a credential store, restore with `ozolith` ownership and
the restrictive file modes intact, never copy its contents into images, logs,
or ordinary config repositories, and delete the volume when permanently
decommissioning the deck — or to deliberately revoke everything it retains.

**Upgrading a deck that predates this layout:** the old credential lived
container-local at `~/.claude.json`, outside every volume, and is gone with
the replaced container — it cannot be migrated. Run one deliberate `/login`
in the first session on the new layout; every recreation after that keeps it.
