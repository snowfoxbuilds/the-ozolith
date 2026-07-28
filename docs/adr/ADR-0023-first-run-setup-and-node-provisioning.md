Status: ACCEPTED

Date: 2026-07-27

Provenance: designed in a chat working session 2026-07-26/27; authored directly in Notion (ADR-0001). Amends ADR-0015 (config surface, node-token model, installer flow); supersedes ADR-0018's admin-credential-and-session section (separate browser password, stateful revocable sessions — password rejection reversed); builds on ADR-0022 (public origin, session cookie, per-deployment TLS); leaves ADR-0006 unchanged (Stack/image config editing stays git-native). Storage locations named here are refined by ADR-0024 (Control Node storage partition and recovery).

# ADR-0023: First-run setup — unified init, settings surface, and join-string node provisioning

## Context

V1 setup is `.env`-driven: the operator hand-edits environment files in the repo/deploy directory before anything runs. The goals are (1) initialization on the Control Node generates everything needed for the web dashboard to be reachable, (2) all remaining settings and access tokens are entered through the dashboard, (3) dashboard access is password-protected with a session cookie, and (4) configuration lives in the config folder, never the repo directory. The governing constraints: the dashboard cannot configure what it depends on (bootstrap chicken-and-egg); config editing is git-native per ADR-0006 and the M3/M4 briefs (web config editor is post-V1); `nodedaemon/` stays stdlib-only (ADR-0010/0015); the trusted-network, single-operator model is unchanged (ADR-0022).

## Decision

### Unified Control Node init

`theozolith-control init` composes the existing pieces into one command, in order:

1. Generate the master key (unchanged from ADR-0015 first-start behavior).
2. `origin-init` — random-slug public origin, persisted as a read-only `control.toml` field (ADR-0022 as amended by ADR-0024).
3. `tls-init` — per-deployment CA and server certificate (ADR-0022). The server cert additionally carries the Control Node's IP address in its SAN, so nodes and browsers reaching it by IP present a valid certificate once the CA is trusted.
4. Prompt for the admin password (new); store only its hash.
5. Print the operator handoff: the dashboard URL, the DNS record to create (exact `/etc/hosts` line or router entry), the CA download URL, and per-OS trust instructions (macOS `security add-trusted-cert` one-liner, Firefox store note, iOS profile steps).
Init automates generation completely; the two irreducibly manual actions (DNS record, CA trust per device) get exact copy-pasteable instructions rather than prose. Re-running init requires `--force`, matching `origin-init` semantics.

### Configuration surface: four tiers

| Tier | Examples | Where it lives | Web-editable |
| --- | --- | --- | --- |
| Bootstrap-critical | admin password, origin/slug, TLS material, bind port, data dir | `~/.theozolith/secrets/` (ADR-0024), written by init; the public origin sits as a read-only field in `control.toml` | No — the dashboard depends on them existing. Init/CLI only. |
| Operational tunables | heartbeat interval, grace periods, sweep cadences, terminal session cap | `control.toml` in the Config Repo | Yes |
| Secrets / access tokens | control PAT, LLM keys | encrypted secret store (ADR-0015) | Yes — the existing M4 secret web form; write-only, never echoed |
| Stack/image topology | `stacks/*.toml`, `images/*.toml`, `product.toml` | Config Repo | No — git-native per ADR-0006; a web config editor is its own post-V1 brief |

`control.toml` lives in the Config Repo alongside `stacks/`, `images/`, and `product.toml`; it replaces the `THEOZOLITH_*` environment-variable sprawl of ADR-0015 as the durable home for tier-2 settings, and environment variables remain as overrides only (the expert escape hatch, same posture as `THEOZOLITH_PUBLIC_ORIGIN` in ADR-0022). The dashboard settings form edits it by committing to the Config Repo's working home on the Control Node — a fixed-schema, single-file write path following the `product.toml` precedent (`theozolith update` already commits pin bumps), not the post-V1 free-form config editor, which remains scoped to Stack/image topology. Every tier-2 setting ships a default, so the file is optional; the ADR-0004 deletion test is restated: with no Config Repo, the product boots from docker + the TheOzolith package + `theozolith-control init` output, all tunables at defaults. `.env` as a user-facing configuration surface is deleted and leaves the deletion-test wording; `deploy/` ships only the compose stub pointing at `~/.theozolith/`. Nothing configuration-bearing lives in the repo directory.

### Admin password and sessions

- **Two credentials, two audiences.** The password is the human/browser credential; the admin bearer token (ADR-0015) remains the machine credential for CLI and API. Neither derives from the other; they rotate independently. Init generates the token and prompts for the password.
- **Hashing**: stdlib `hashlib.scrypt` — memory-hard, no new dependency. Fernet is encryption, not password hashing, and is not used here.
- **Sessions**: server-side session table in `cache.db` (ADR-0024 — deleting it costs a re-login, nothing more). The cookie (shape fixed by ADR-0022: `__Host-ozolith_session`, Secure, HttpOnly, SameSite=Strict) carries only a random 128-bit session ID — nothing decodable client-side. Absolute expiry, default 30 days (`session_days` in `control.toml`). Logout deletes the row; a password change truncates the table. Signed stateless cookies were rejected: they save nothing at this scale and cost revocation.
- **Login hardening**: constant-time comparison and rate limiting on the login form. The 128-bit origin slug is defense in depth (ADR-0022); the password check must stand alone.
### CA role and distribution

The CA exists for **server authentication and channel integrity**: nodes and browsers verify they are talking to the real Control Node, which is what makes the TLS channel MITM-proof — critical because secrets ride it (ADR-0015). Client authentication is the bearer tokens and the password/cookie; the CA plays no part in it. The CA is minted once at init; its private key never leaves the Control Node — only the public certificate is distributed.

- **Bootstrap endpoint**: the Control Node serves `{CA certificate, public origin, canonical control URL}` unauthenticated over a **dedicated plaintext bootstrap listener** on its own port (default documented; nonstandard ports ride in the join-string payload) — GET-only, no auth, no state, no cookies, never mounted on the HTTPS app, so ADR-0022's fail-closed origin posture is untouched. Its route table is closed by decision, not convention: these three inert values and nothing else, ever. The channel is safe not because it is trusted but because every byte on it is public and the one value that matters — the CA certificate — is integrity-checked against the join string's pinned fingerprint. **Code never rides it**: the installer and node distribution are fetched over channels with pre-existing trust (the GitHub release over WebPKI HTTPS, or `scp` from the Control Node for air-gapped setups); a plaintext-fetched installer would be remote code execution for a LAN MITM before any verification runs. Browsers downloading the CA cert for device trust have no fingerprint to check; trust there flows from the operator having received the URL from init output over the trusted network — recorded, not fixed, matching ADR-0022's defense-in-depth posture. `provision` fetches the CA cert from this listener for fingerprint verification.
- **Escape hatch**: operators who own a public domain may substitute a publicly valid certificate (e.g. Let's Encrypt DNS-01) for the minted CA, deleting the per-device trust step. Opt-in, never default; requires no product changes beyond accepting the operator-supplied cert paths.
- IP-literal browser origins remain rejected (ADR-0022 reaffirmed): an IP origin removes only the DNS step, still requires CA trust to avoid warnings, breaks on DHCP, and forfeits the slug's entropy for no friction saved.
### Node provisioning: the join string

Provisioning a physical node is one paste:

```javascript
theozolith-nodedaemon provision ozjoin1:<base64url-payload>
```

The operator never composes this line: `theozolith join-token create` prints the complete paste — for a fresh box, prefixed with the installer invocation (see Installer consolidation).

- **Payload**: `{addr, ca_sha256, token, exp}` compact-serialized, with a version prefix (`ozjoin1`) and an integrity checksum so a truncated paste fails as "malformed join string", not a confusing network error. Roughly 120 characters. `theozolith-nodedaemon provision --inspect` pretty-prints a payload without acting on it.
- **Flow**: parse and checksum → fetch the CA certificate from the bootstrap endpoint at `addr` → hash and compare against the embedded `ca_sha256` → **abort before transmitting anything on mismatch** → open TLS verified against the CA → authenticate with the join token → receive the per-node token and canonical control URL → persist CA, control URL, and node token under the daemon state dir → install/enable the systemd unit → register and first heartbeat.
- **No manual fingerprint step, no fingerprint-less fallback.** Verification is the machine's job; trust flows from where the join string came from (an authenticated dashboard session or SSH on the Control Node). A human visual check after joining is theater and too late. `provision` requires a join string, full stop: nothing transmits until a machine-checked fingerprint passes.
- **Failure modes fail closed and loud**: bad checksum → malformed paste; fingerprint mismatch → "possible MITM **or stale join string after CA rotation**" with nothing transmitted; expired/consumed/revoked token → clean rejection after TLS with nothing persisted. Re-minting the CA invalidates every outstanding join string by construction — a feature, distinguished in the error text so legitimate re-inits don't read as attacks.
- The provision CLI ships in the stdlib-only node distribution: `ssl`, `hashlib`, `urllib`, `base64` cover the whole flow.
### Join token semantics

- Minted on the Control Node: `theozolith join-token create` (CLI or dashboard), default **1 hour TTL, single-use**, consumed on successful exchange. `--ttl` and `--uses` widen it for batch provisioning sessions. `join-token revoke` is the backstop for the oops case.
- The join token is **never the node token**. It is exchanged over verified TLS for a freshly minted per-node token; the join string is disposable, the per-node token is what persists.
- Long-lived revocable join tokens were rejected as the default: a standing "add a node anytime" credential is a second admin password with worse hygiene (shell history, scrollback).
### Per-node tokens

Provisioning mints a unique bearer token per node, recorded as `{node, token}` in `store.db` (ADR-0024). This deletes the shared-node-token weakness ADR-0015 accepted ("any node can heartbeat as any other") at zero extra cost, and makes revocation per-node. Per-node tokens never expire — nodes are long-lived by design; removing a node from the fleet is explicit revocation, not credential lapse. The admin password never touches a provisioned node.

**Provisioning is registration.** The join-token exchange is the sole way a node comes to exist on the Control Node; ADR-0015's register-on-first-heartbeat behavior is superseded. A heartbeat bearing an unknown or revoked token is rejected with 401 and never creates a node record — but the dashboard surfaces such rejections as **unregistered nodes** (self-declared name, source address, last seen). This view is advisory display built from unauthenticated input: deduplicated and size-capped in `cache.db`, never a node record, never dispatch-eligible. Its payoff is recovery from a stale backup (ADR-0024): nodes whose tokens were lost keep heartbeating, surface as unregistered, and the list is exactly the set needing one re-provision paste each.

### Installer consolidation

`provision` subsumes the manual configuration half of `deploy/install-nodedaemon.sh` (ADR-0015): the installer installs the venv and distribution, then hands off to `provision` as its final step. No hand-edited node-side configuration remains.

### CLI surface

- **Human commands live on the Control Node only.** `theozolith` is the operator CLI — an installed console-script entry point from the package, never a script run out of the repo directory: `update`, `build`, `test`, `join-token create|revoke`. `theozolith-control` is the service-admin entry point for the machine it runs on: `init`, `origin-init`, `tls-init`, `serve`, `recover` (ADR-0024).
- **Nodes have no human CLI grammar.** The node distribution ships only `theozolith-nodedaemon` (the daemon plus the `provision` subcommand); the sole node-side interaction is pasting the line `join-token create` printed. The earlier `theozolith provision` spelling is renamed accordingly — the operator-CLI name is not installed on nodes.
- **Bootstrap from source**: `theozolith build` cannot be the first command — it presupposes an installed CLI. A fresh checkout bootstraps with `python3 build.py` in the repo root: a thin shim over the same build implementation `theozolith build` wraps (one implementation, two entry paths — they cannot drift), producing the same artifacts and finishing by installing the `theozolith` and `theozolith-control` entry points. From then on, source-based updates are `theozolith build` (ADR-0015 as amended 2026-07-22: builds from the checkout, pins the committed SHA, serves the artifact for node pulls). `build.py` is the sole exception to "never a script run out of the repo directory" — it exists to end that state.
## Consequences

- **Positive**: setup collapses to init → paste join string per node → enter tokens in the dashboard; every generated secret and setting lives under the `~/.theozolith/` partition (ADR-0024), none in the repo; the provisioning handshake is MITM-proof without manual hash comparison; per-node tokens arrive for free; `.env` archaeology is gone; the CA-trust and DNS steps — the only manual residue — ship with exact instructions.
- **Negative**: two manual per-device actions (DNS record, CA trust) survive unless the operator brings a public domain; a new bootstrap endpoint and session table widen `control/` slightly; existing deployments must migrate env-var settings into `control.toml` and re-provision nodes to get per-node tokens.
- **Neutral**: ADR-0006 is untouched — Stack/image topology stays git-native and a web config editor remains post-V1; the admin bearer token and secret-store mechanics of ADR-0015 are unchanged; ADR-0022's origin/cookie/TLS decisions are consumed as-is.
## Alternatives rejected

- **IP-literal origin instead of hostname + CA**: removes only the DNS step; CA trust is still required for warning-free HTTPS, DHCP breaks a pinned-IP origin, and the slug's entropy is forfeited (ADR-0022's fail-closed origin parsing stands).
- **Plain HTTP over IP**: violates the TLS-mandatory secret channel (ADR-0015) and the Secure-cookie requirement (ADR-0022).
- **Admin password as the provisioning credential**: types the root credential on every box; a LAN MITM during a fingerprint-less bootstrap would capture it — full cluster compromise. The join token bounds the worst case to a short-lived, single-purpose credential.
- **Visual CA fingerprint check after joining**: theater (humans don't reliably compare hex) and too late (the token already crossed the wire). Machine verification before any transmission replaces it.
- **Separate CLI flags instead of an opaque join blob**: not copy-paste-atomic; invites flag reordering, partial edits, and shell-escaping mistakes. Debuggability is preserved via `--inspect`.
- **Fingerprint-less manual provision fallback** (operator types an address by hand, visually confirms the CA hash): rejected, 2026-07-27 grilling — its only safety mechanism is the visual comparison already rejected as theater; `join-token create` is available from the dashboard or any Control Node SSH session, so no scenario exists where a node shell is reachable but a join string is not (the exchange needs the Control Node live regardless); and it reintroduces exactly the TOFU window an attacker would steer a user toward.
- **Signed stateless session cookies**: no benefit at single-operator scale and revocation becomes impossible; a SQLite session table is boring and sufficient.
- **Web config editor for Stack/image topology**: reverses ADR-0006 and the M3/M4 scope decisions; dashboard-writes-git needs its own brief (git identity, conflict handling, validation UX) — deferred, not rejected on the merits.
