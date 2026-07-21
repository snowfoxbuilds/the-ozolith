Status: ACCEPTED

Date: 2026-07-18 (amended 2026-07-21 — PR #6 review round: explicit public origin decoupled from the bind port, operational resize bounds, evidence-parking failure ladder)

Provenance: authored in-repo under the M5 delegated-decisions mandate (this PR); awaiting Notion uplift (ADR-0001).

# ADR-0019: Control web hardening — attach argv, public origin, PTY bounds, per-Stack jobs directories

## Context

The post-merge review of PR #5 (M4: dashboard + web terminal) found security and stability gaps in the terminal command construction, the browser surface's origin story, PTY resource handling, and queue-behind's shared-jobs-dir ownership heuristic. The M5 brief settles the policy decisions (structured attach argv, server-derived Stack ownership, mandatory randomized origin, configurable base domain, private DNS + per-deployment TLS, bounded terminal resources, best-effort live-Worker evidence, per-Stack jobs directories) and delegates the concrete contracts to the implementing PR. This ADR records those contracts. The trusted-network, single-operator deployment model is unchanged.

## Decision

### Attach command structure (brief acceptances 1–3)

- A Stack's `attach` key is an **argv array of strings**; a free-form command string is a Config Repo parse error. `{host}` and `{container}` substitute only as **complete argv elements**; an element embedding a placeholder (`user@{host}`) is a parse error. No other element is ever transformed — there is no format-string surface.
- Both substituted identifiers are validated immediately before process launch against shell-inert whitelists: hostnames as dot-joined DNS labels (`[A-Za-z0-9]` first, inner hyphens, ≤253 chars), container names by the Docker name rule (`[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`). No whitespace, control characters, shell syntax, or leading `-` can pass, so a forged heartbeat value cannot alter command structure even after SSH re-parses the remote command with a shell — safety comes from the charset, not from quoting.

### Terminal target authorization (acceptances 4–5)

- The terminal URL names only `node` and `container`. The target resolves from the **live container record** the node's heartbeat reported; the owning Stack is derived server-side from the record's `owner`. Caller-supplied Stack identity is not an authority (the parameter is gone).
- Refusals, all before websocket acceptance: container unknown on that node, heartbeat evidence older than 150s (the dashboard's stale-node bound: ~2.5 missed beats), owner not a configured Stack placed on that node, owner without attach configuration, or identifier validation failure.

### Public origin (acceptances 6–8; amended 2026-07-21)

- The canonical browser origin is **one explicit public origin** — a complete `https://` URL, independent of the Uvicorn bind host and port. `theozolith-control origin-init` provisions it: `https://<slug>.<base-domain>` with a 26-character lowercase-base32 slug carrying 128 bits from the OS CSPRNG, persisted **complete** at `<data-dir>/public-origin`. Default HTTPS omits `:443`; a nonstandard *external* port (`origin-init --port`) is included explicitly and enforced exactly. Re-provisioning requires `--force` (the origin is deliberately sticky: DNS, TLS, and every `CONTROL_NODE_URL` hang off it).
- The base domain is configurable; the default is `theozolith.internal` (ICANN-reserved private namespace, so a self-contained deployment never leaks resolution attempts). The first-party deployment uses `theozolith.com` — its normal result is `https://<128-bit-random-slug>.theozolith.com`; the product carries no first-party coupling (ADR-0004/0005).
- Production `serve` accepts the origin from the persisted artifact **or** the `THEOZOLITH_PUBLIC_ORIGIN` environment override (which wins), and refuses to start without one that parses. The override is an expert escape hatch: validation is a format check — entropy cannot be inferred from text, so an operator supplying it is responsible for a CSPRNG-generated slug; the generator remains the only sanctioned source. `--insecure-dev` may run bare.
- Origin parsing fails closed on: non-`https` schemes, credentials, any path other than empty or `/`, query strings, fragments, wildcard hosts, malformed ports, and first labels that do not meet the slug format. Changing `serve --port` (the bind) never silently changes the accepted `Host` or `Origin` — only the origin does.
- The hostname resolves only on the trusted network (private DNS or hosts entries); the Control Node has no public ingress path. The random name is defense in depth for the browser surface, never a substitute for the admin credential, the private network, or exact-origin enforcement.

### Browser-origin isolation (acceptance 7)

- The session cookie is **`__Host-ozolith_session`**: Secure, HttpOnly, SameSite=Strict, Path=/, no Domain — browsers refuse it off-host, off-path, or over plaintext. Whether the cookie is issued Secure/`__Host-` is a per-deployment choice keyed on whether `serve` terminates TLS (production always does); `--insecure-dev` over plain HTTP issues the unprefixed `ozolith_session` name without Secure (a browser drops a Secure/`__Host-` cookie set over http from any non-localhost origin, which would otherwise loop the dev login). A deployment only ever issues and accepts one of the two names, so the production guarantee is never downgraded.
- With a public origin configured, **cookie-authenticated** state-changing HTTP requests and websockets must carry exactly the canonical `Host` and `Origin` (one correct spelling each, derived from the parsed public-origin URL alone — browsers omit `:443` and include any other port); missing or mismatched headers fail closed. The login form is enforced identically (it is exclusively a browser surface). **Bearer-authenticated** callers are non-browser clients and are exempt; bearer wins when both credentials appear, so a scripted client with a stray cookie jar never trips the browser contract.

### Per-deployment TLS (acceptance 9)

- `tls-init` extracts the public origin's **hostname** (never a port) and includes it in the certificate SAN automatically, and **refuses wildcard hosts** — every deployment mints its own CA and key; no `*.theozolith.com` key exists anywhere to share or steal.

### PTY resource bounds (acceptances 10–12)

- PTY output buffers at most **512 KiB** per session; past high-water the bridge stops reading the master fd, letting the kernel PTY buffer fill and block the attach process (true backpressure, bounded memory); reads resume below **64 KiB**.
- A websocket send that cannot complete within **30s** declares the client dead: the bridge kills the **attach process group only** (SIGHUP, 10s, SIGKILL) — the Run container and its tmux session survive — and the audit log's detach record carries the reason (`process-exited` | `client-closed` | `stalled` | `spawn-failed` | `error`). Server-side task cancellation (client hang-up) escalates to SIGKILL synchronously, since level-based cancellation forbids further awaits. A failed spawn (e.g. a misconfigured attach binary) closes the PTY master on **every** failure path — whatever the exception class — and detaches cleanly rather than leaking the fd (amended 2026-07-21; a descriptor-stability regression test proves it). Resize frames are clamped to **operational bounds — 20–500 columns, 5–300 rows** (amended 2026-07-21, replacing the `unsigned short` range): malformed, negative, missing, non-numeric, or absurd dimensions neither tear the session down nor reach `TIOCSWINSZ` outside those bounds.
- Concurrent terminal sessions are capped (default **8**, `THEOZOLITH_TERMINAL_SESSION_CAP`); the slot is reserved in one synchronous critical section (check-then-increment with no intervening await) so concurrent connects cannot all pass the cap, and an over-cap connect closes with 4429 before target resolution and launches no process. Close codes: 4401 auth, 4403 origin, 4404 target, 4429 capacity.

### Best-effort live-Worker evidence (acceptances 13–14)

- Evidence pushes keep their bounded retry (3 attempts per bundle). After exhaustion on a live Worker's Run: the escalation comment renders a normal evidence link only for bundles that landed; an unpushed bundle is named as text (`runs/issue-N/<run-id>`) with the reason — never a dead link. The failure is logged as a structured JSON record (`theozolith.evidence-push-failed`: run_id, issue, bundle, attempts, error). The job directory is **parked immediately** into the `<jobs>-pending` sibling for boot-sweep recovery, so retained evidence never reads as an in-flight Run. Escalation is never blocked on evidence; zombie escalation (the janitor) remains evidence-first, unchanged.
- **Parking failure ladder (amended 2026-07-21)**: if the atomic parking rename itself fails, a structured record (`theozolith.evidence-park-failed`: run_id, issue, source, destination, error) is logged and a collision-safe unique destination in the pending directory is tried; if that also fails, a second structured record (`theozolith.evidence-lost`) is logged and the completed job directory is **removed from the active jobs directory** — evidence loss in that compound case is explicitly accepted, because a completed directory left in place reads as an active Run to queue-behind (deferring targeted recycles and node-wide updates indefinitely). The escalation comment states the loss honestly (no false boot-sweep promise); escalation itself is never blocked. A collision-parked directory still publishes under its **original** run_id path (suffix stripped by the sweep), preserving ADR-0016's evidence layout for the janitor and every comment-named path. If removal itself leaves remnants, the truth is logged instead of the discard claim, and the sweep may yet recover the dir. Should the driver also die before its own escalation lands after a compound loss, no bundle will ever arrive for the janitor's evidence-first wait — resolving that zombie is the dashboard's human call (ADR-0016's stated posture).

### Per-Stack jobs directories (acceptances 15–17)

- The Node Daemon injects `THEOZOLITH_JOBS_DIR=<base>/<stack-name>` (base `/var/tmp/theozolith/jobs`) into every process Stack; an explicit env value in the Stack definition wins. The driver's own default resolves the same way (`<base>/<stack>`), so the daemon-less dev shape (no injection) keeps worker and reviewer separate too. The queue-behind in-flight signal reads the same resolution, so a targeted recycle observes only the target driver's Runs and a node-wide update waits on each live driver's active Run — dead drivers and `-pending` parking never block.
- The Config Repo rejects duplicate resolved jobs-directory paths per node, including a path landing on another Stack's `-pending` sibling (normalized comparison). The same path on different nodes is legal (different filesystems).

## Consequences

- **Positive**: heartbeat data can no longer reach a shell as structure; terminal reachability is pinned to live, fresh, config-backed ownership; a browser can only speak to the Control Node by its unguessable name and exact origin; a wedged terminal client costs a bounded buffer and one killed ssh, not a Run; escalations are honest about missing forensics; queue-behind ownership is structural instead of heuristic.
- **Negative**: `attach` strings and the shared jobs dir are breaking config changes (parse errors / new injected paths — deployments upgrading from M4 must convert `attach` to argv form and may strand old orphan dirs at the shared `<base>` root, where no per-Stack sweep looks; move them into the owning Stack's new directory once). A public origin + private DNS record is now mandatory setup. Sessions bind to the exact origin: IP-literal browsing stops working in production (bearer/CLI access is unaffected). In the compound parking-failure case a completed Run's unpushed evidence is deliberately lost (twice-logged) rather than left masquerading as an in-flight Run.
- **Neutral**: `--insecure-dev` remains the un-armed dev shape (no TLS, no canonical origin, no origin guard). The Reviewer's workspace handling and the claim/retry/quarantine machinery are untouched.

## Alternatives Considered

- **Shell-quoting the substituted identifiers** (keep string templates, escape at render): rejected — quoting must be correct per boundary (local exec vs. SSH remote string vs. docker argv), and one miss is an injection; a charset whitelist plus complete-argument substitution is correct at every boundary at once.
- **CSRF tokens instead of exact-origin enforcement**: rejected — tokens add per-form state to defend endpoints that only ever have one legitimate origin; SameSite=Strict + `__Host-` + exact Host/Origin is stateless and covers websockets, which token schemes do not.
- **A reverse proxy (or oauth2-proxy) for origin/TLS concerns**: rejected — adds a deployment dependency the product boundary (ADR-0005) doesn't want; the Control Node already terminates TLS itself.
- **Unbounded PTY queue with client-side flow control**: rejected — the server cannot trust the client to drain; the kernel PTY buffer is the natural backpressure vessel and costs nothing.
- **Persistent retry queue for failed evidence pushes**: rejected by the brief — the boot sweep already is the durable retry; adding coordination machinery for a rare failure buys nothing but state.
- **Pipeline-specific parsing of job-dir names for queue-behind ownership**: rejected by the brief — encoding ownership in directory *placement* (one dir per Stack) needs no naming convention and survives custom Stacks.
