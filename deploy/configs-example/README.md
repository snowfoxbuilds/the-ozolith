# configs-example — a starter Config Repo

A complete, minimal Config Repo (ADR-0006): copy it to your Control Node's
`configs/` and edit in place. It declares two pipeline workers (Implementer,
Reviewer) as process Stacks and one **Flight Deck** as an interactive container
Stack. Everything with a placeholder — image digests, the knowledge URL, the
tailscale checksum, secret values — is yours to fill in.

This is the sanctioned private-side surface for deployment detail the product
never carries: the Flight Deck's tailnet wiring lives here (in
`worker-types/flightdeck.toml`'s baked `flightdeck-start`), never in product
code or images (the guardrail test enforces that boundary).

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
passthrough — it runs as the unprivileged `ozolith` uid.

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
`TS_AUTHKEY_FILE`. It is consumed **only when the tailscale-state volume is
empty** (first enrollment, or after deliberate state loss); thereafter identity
lives on `{stack}-tailscale-state` and survives image rebuilds. There is no
durable copy of the key anywhere but the encrypted store and that tmpfs, which
evaporates with the daemon/reboot — so the snow-maker "shred the key" step has
nothing to shred. **Hardening:** once every Flight Deck of the type has
enrolled, remove the `TS_AUTHKEY` line from the worker type's `[secrets]`.

### Tailnet ACLs

In your tailnet policy file:

```jsonc
{
  "tagOwners": {
    "tag:flightdeck": ["autogroup:admin"]   // who may mint tag:flightdeck keys
  },
  "ssh": [
    {
      "action": "accept",                    // accept, NOT check — see below
      "src":    ["autogroup:member"],        // who may SSH in (tighten to taste)
      "dst":    ["tag:flightdeck"],
      "users":  ["ozolith"]                  // sessions land as the container's ozolith
    }
  ]
}
```

Use `action: "accept"`, not `"check"`: `check` forces a periodic re-auth
check-in, which a headless Flight Deck (no human at a browser) cannot satisfy,
so an idle session would be dropped mid-flight. The tradeoff is deliberate —
`accept` trades interactive re-verification for uninterrupted headless access;
keep the blast radius small with a tight `src` and the `tag:flightdeck` ACLs.

MagicDNS must be enabled for `ssh ozolith@flightdeck-<name>` to resolve; without
it, use the tailnet IP.

### State-volume loss ⇒ new machine identity

`{stack}-tailscale-state` (`/var/lib/tailscale`) IS the tailnet machine
identity. Delete or recreate it and the Flight Deck re-enrolls with the stored
reusable key on next start as a **new** tailnet machine — the old one lingers in
the admin console as a stale, offline entry. Prune it there (Machines → the old
`flightdeck-<name>` → Delete). This is expected after a deliberate state reset;
it is not an error.
