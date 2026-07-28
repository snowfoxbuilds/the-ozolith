Status: ACCEPTED — amended 2026-07-27 by ADR-0023 (admin credential and session superseded; see Amendments)

Date: 2026-07-17

Provenance: authored in-repo under the M4 delegated-decisions mandate (PR #5); uplifted to Notion after M4 landed (ADR-0001).

# ADR-0018: M4 dashboard, terminal, and dispatch contracts

## Context

The M4 brief delegated four decisions to the implementing PR: dashboard information architecture and refresh, admin credential and session mechanics, PTY transport and audit logging, and quarantine release. ADR-0016/0017 conformance also required dispatch, activation, telemetry, evidence, and queue-behind contracts.

## Decision

### Dashboard IA and refresh

- One page with three server-rendered fragments: Workers & Runs, Nodes, and Events. HTMX polls every 5 seconds.
- Jinja templates and vendored HTMX/xterm assets require no frontend build step.
- Build skew compares base digest and instruction hash for the same image name across nodes. Nodes become stale after 150 seconds without a heartbeat.
- Agent-authored transcript tails and unknown event payloads render through Jinja autoescape. Unknown namespaced event types render generically.
### Admin credential and session

- The admin token is the single credential for dashboard, secret entry, and terminal access.
- Login mints a stateless 12-hour session cookie signed by a per-process random key. The cookie is HttpOnly, SameSite=Strict, and Secure over TLS. Sessions die when the Control Node restarts.
- API callers may use the admin token as a bearer credential.
- The secret form uses the same encrypted store write as `PUT /api/v1/secrets/{name}` and never displays values.
### PTY bridge and audit log

- One websocket carries each terminal session. Binary frames are raw PTY bytes; text frames are resize controls. Uvicorn websocket ping provides keepalive.
- The bridge runs the Stack's control-side `attach` template with `{host}` and `{container}` substitutions in its own process group. SIGHUP followed by SIGKILL cleans up the attach tree.
- Gates are admin session, configured attach command, and a container reported live in the latest heartbeat.
- Attach and detach records append as JSON lines to `<data-dir>/terminal-audit.log`, including timestamp, actor, node, Stack, container, and command.
### Quarantine release

- `POST /api/v1/nodes/{node}/quarantine/release` and `theozolith-control unquarantine --node <node>` release quarantine.
- Queueing recycle or update also releases quarantine and resets the consecutive-failure counter. No timer releases quarantine.
### Dispatch and conformance contracts

- `POST /api/v1/dispatch` carries role, worker, node, and GitHub login. Worker dispatch returns a write-through claimed issue; Reviewer dispatch is discovery-only.
- Grants serialize on one lock. Claim order is assign login, add `in_progress`, remove `plan_ready`.
- The driver's `claimed` event activates the grant. The first Run abandons a grant if activation cannot be confirmed after three emissions; retry Runs proceed because the claim is already activated.
- Pending drain, recycle, or update commands close the node's dispatch gate. Deferred commands remain unacknowledged and are re-delivered; heartbeats report deferrals.
- Progress transcript tails are capped at 8 KiB and event payloads at 32 KiB. Progress events evict oldest-first under the configured ~10 GB budget; terminal Run and review events remain.
- Janitor evidence is landed when `swept.json` or `run.json` exists in the Run bundle. Failed pushes retain job directories for the boot sweep.
- Failure classes are `timeout`, `session-died`, `harness`, `no-changes`, and `infra`.
- Progress telemetry carries phase, elapsed time, tool calls, operator prompts, transcript byte count, and transcript tail. Token count remains null until an adapter supplies it.
## Consequences

- **Positive**: M4 ships with no frontend tooling; one credential covers the full web surface; dashboard state remains reconstructible.
- **Negative**: polling refetches unchanged fragments; Control Node restart ends sessions; the single-actor audit log cannot distinguish operators; token counts are absent.
- **Amends**: ADR-0015's control-plane API with dispatch, deferred commands, force, and auditor removal.
## Alternatives Considered

- **SSE dashboard updates**: rejected; polling is simpler and stays within the heartbeat bound.
- **Separate dashboard password**: rejected; it protects the same authority as the admin token.
- **Terminal audit in SQLite**: rejected; the control database is deletable cache state.
- **Implicit terminal template for every Stack**: rejected; no configured attach command means no terminal exposure.
- **Reject oversized progress events without truncation**: rejected; retaining the tail preserves useful advisory telemetry.
## Amendments (2026-07-27, grilling session)

- **Admin credential and session superseded by ADR-0023**: a scrypt-hashed admin password (entered at `theozolith-control init`) becomes the browser login credential; sessions move to a server-side table in the control database — random 128-bit session ID in the ADR-0022 cookie, 30-day absolute expiry, revocable (logout deletes the row; password change truncates the table); sessions survive Control Node restarts, which the stateless per-process-key design did not — operationally hostile once `os.execv` self-updates make restarts routine. The admin bearer token remains the machine credential for CLI and API, unchanged.
- **"Separate dashboard password: rejected" is reversed** by ADR-0023: the original rejection weighed authority (unchanged — the password protects the same single-operator trust domain as the admin token), not ergonomics. Init-time password entry changed the ground: humans do not memorize 128-bit bearer tokens.
