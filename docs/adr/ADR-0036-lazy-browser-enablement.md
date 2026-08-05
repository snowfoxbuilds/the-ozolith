Status: ACCEPTED

Date: 2026-08-04

Provenance: delegated decision from the M8 brief (Single-Node Deployment, status CLI, events read API), implementing the "Grilling 2026-08-04 (late)" ruling in NODE-SUBSTRATE.md. Amends ADR-0023 (init scope: the admin password leaves init) and ADR-0034 (the browser address becomes lazy and may again carry a hostname; the IP-origin decision otherwise stands). ADR-0022's fail-closed origin posture is preserved and moved from serve startup into the request path.

# ADR-0036: Lazy browser enablement — the init / origin-init split

## Context

ADR-0023 made `init` a unified first run ending in an admin-password prompt and a browser handoff; ADR-0034 retired the slug origin and `origin-init`, making the browser origin the persisted control IP — but kept the password prompt inside init. The 2026-08-04 grilling ruled the browser surface optional: a Single-Node Deployment bootstraps and operates entirely from the terminal (`theozolith status` now, the Operator TUI in M9), so the two browser-only credentials — the origin browsers may dial and the admin password — must not be demanded before anyone wants a browser. The web UI itself is frozen at shipped M4/M5 scope.

## Decision

### init composes the machine surface only

`theozolith init` becomes: master key → admin bearer token → control address (`control_ip`, `control_port`; ADR-0031/0034 unchanged) → CA + server certificate with **IP SANs only** — the control IP and `127.0.0.1` — → systemd unit (root-mediated, ADR-0034 unchanged) → operator handoff. No origin prompt, no password prompt, no browser instructions beyond one pointer at `origin-init`.

- The **loopback SAN is unconditional**, on every deployment shape: the local Node Daemon of a Single-Node Deployment persists a loopback dial address, and the Operator TUI is a loopback API consumer — both verify against the same server certificate. IPv6 loopback is omitted until something dials it.
- **SAN input on the machine surface is IP-literals-only**: `init --host` and `tls-init --host` validate every entry as an IP address and refuse hostnames before any state is written. A hostname enters the certificate exactly one way — the persisted browser origin, via `origin-init` — so the pre-browser machine identity is IP-only by construction.
- `--force` semantics: re-init still mints a new CA and re-mints the server certificate; if a browser origin is already persisted, its hostname is included in the new SAN set so an enabled browser surface survives re-init. The password is never prompted by init in any form.
- The handoff names the finish lines: start serving, provision nodes (`join-token create`), `theozolith status`, and — optional, later — `origin-init` for the dashboard.

### origin-init is the opt-in browser step

`theozolith origin-init` (reinstated from ADR-0034's retirement, in changed form) prompts for the two browser-only credentials together:

1. **The browser origin** — default offered: `https://<control_ip>[:<control_port>]`, the ADR-0034 IP origin. An operator who runs their own DNS may enter a hostname origin instead (`https://name[:port]`); this is the sole hostname re-entry point — no slug, no `--base-domain`, no DNS machinery returns. Parsing fails closed exactly as ADR-0022 required: https only, no credentials/path/query/fragment, no wildcard, exact one origin.
2. **The admin password** — hashed (scrypt, ADR-0023) and stored; the session table is truncated.

Then origin-init persists the origin and re-mints the server certificate **via the same machinery `recover` uses** (`tls.remint_server_cert`, same CA — never a new CA): SAN = control IP + loopback + the origin hostname when it is not an IP literal. Nodes pin the CA, not the server cert, so a re-mint touches no node. Re-run requires `--force` once an origin is persisted; the non-TTY form reads origin (blank line = default) then password from stdin.

### Credentials written under sudo belong to the service

On a root-mediated install, origin-init and set-password run under `sudo` **after** init's chown handed the partition to the service user — so everything they write (the password record, the re-minted server key, the control.toml commit) is handed back: the same guarded recursive chown the installer uses, restricted to the constant system leaf, symlink-free, skipped everywhere else (the PR #12 blast-radius rule). It runs in a `finally`, so even an interrupted origin-init never strands a root-owned server key that would break the next service restart. The password record itself is written **atomically** (temp file + rename, 0600, ownership set before the record becomes visible): a crash leaves either the previous complete record or the new complete record, never a partial one. Session invalidation never root-creates the service's cache database — with no `cache.db` there are no sessions to invalidate.

### Persistence: `browser_origin`, a read-only `[control]` field

```toml
[control]
control_ip = "192.168.1.20"        # what nodes dial (ADR-0031)
control_port = 443                  # external https port (ADR-0034)
browser_origin = "https://192.168.1.20"   # written by origin-init; absent = browser disabled
```

Written only by `origin-init`, committed with the fixed machine identity (`theozolith: browser origin <origin>`), rendered read-only in the settings form and refused by its write path — the `control_ip` precedent under the ADR-0029 fixed schema. It is deliberately **not** named `public_origin`: that key belongs to the retired slug design and is still dropped on regeneration.

### Fail-closed moves from serve startup into the request path

- `serve` no longer requires an admin password to start; it still requires TLS material and a valid control address (the machine channel is not optional).
- While no `browser_origin` is persisted, the **entire web mount refuses**: every HTML/cookie route (`/login`, `/`, fragments, `/secrets`, `/settings`, `/join`, `/terminal`) answers `503` with "browser surface not enabled — run 'theozolith origin-init' (ADR-0036)"; the terminal websocket closes `4403` before accepting. This holds for bearer-bearing callers too: the HTML surface is browser enablement, and the machine surface is the JSON API. The bearer `/api/v1` routes are untouched and work over TLS against the IP SAN from the moment init finishes.
- When `browser_origin` is persisted, `BrowserGuard` arms with exactly that origin (Host/Origin exact-match, ADR-0022/0034 unchanged; bearer clients remain origin-exempt). `BrowserGuard`'s fail-open-when-unarmed shape survives only for `--insecure-dev`; a production serve is either armed or refusing.

### recover validates browser state conditionally

The browser credentials are optional-but-consistent: no `browser_origin` → a missing password hash is not a problem; `browser_origin` persisted → the password hash must exist and parse, and the re-minted certificate includes the origin hostname. `set-password` keeps working as the rotation path for an enabled deployment (and remains inert-but-harmless before enablement).

## Consequences

- **Positive**: a Single-Node Deployment (and any browserless fleet) never types a password and never sees TLS-trust prose; the two browser-only credentials are demanded exactly when a browser is wanted, together, once; the fail-closed posture is finally a per-request property instead of a startup gate, so "API up, browser off" is a first-class state.
- **Negative**: existing deployments upgrade into the disabled state — the dashboard refuses until `origin-init` is re-run once (manual migration note in the PR; no automated migration per the M8 brief). The password is re-entered at that point.
- **Neutral**: `tls-init --host` remains the additive path for extra **IP** SANs (it always preserves the control IP, loopback, and any enabled browser-origin host); ADR-0027's rate limit, ADR-0022's cookie shape, and the frozen web scope are untouched.

## Alternatives rejected

- **Keep the password in init, defer only the origin**: leaves setup demanding a browser credential no terminal-only operator uses, and splits the browser-only pair across two ceremonies — the ruling's point is that they travel together.
- **A separate `browser_enabled` boolean beside a derived origin**: two fields that can disagree; the persisted origin's presence *is* the enablement bit.
- **Refusing only cookie-authenticated requests while letting bearer reach the HTML surface**: preserves a half-alive dashboard that renders for scripts but not humans; the crisp line is HTML surface = browser enablement, JSON API = machine surface.
- **Reusing the `public_origin` key**: resurrects retired slug-era semantics and collides with the regeneration path that drops it.
- **An `origin-init` that only ever accepts the IP origin**: forfeits the one legitimate hostname case (operator-owned DNS, issue #16's future) for no simplification — the parse-and-SAN machinery is shared either way.
