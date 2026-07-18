Status: ACCEPTED

Date: 2026-07-18

Provenance: authored in-repo under the M5 delegated-decisions mandate (this PR); awaiting Notion uplift (ADR-0001).

# ADR-0019: Control web hardening — attach argv, canonical origin, PTY bounds, per-Stack jobs directories

## Context

The post-merge review of PR #5 (M4: dashboard + web terminal) found security and stability gaps in the terminal command construction, the browser surface's origin story, PTY resource handling, and queue-behind's shared-jobs-dir ownership heuristic. The M5 brief settles the policy decisions (structured attach argv, server-derived Stack ownership, mandatory randomized origin, configurable base domain, private DNS + per-deployment TLS, bounded terminal resources, best-effort live-Worker evidence, per-Stack jobs directories) and delegates the concrete contracts to the implementing PR. This ADR records those contracts. The trusted-network, single-operator deployment model is unchanged.

## Decision

### Attach command structure (brief acceptances 1–3)

- A Stack's `attach` key is an **argv array of strings**; a free-form command string is a Config Repo parse error. `{host}` and `{container}` substitute only as **complete argv elements**; an element embedding a placeholder (`user@{host}`) is a parse error. No other element is ever transformed — there is no format-string surface.
- Both substituted identifiers are validated immediately before process launch against shell-inert whitelists: hostnames as dot-joined DNS labels (`[A-Za-z0-9]` first, inner hyphens, ≤253 chars), container names by the Docker name rule (`[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`). No whitespace, control characters, shell syntax, or leading `-` can pass, so a forged heartbeat value cannot alter command structure even after SSH re-parses the remote command with a shell — safety comes from the charset, not from quoting.

### Terminal target authorization (acceptances 4–5)

- The terminal URL names only `node` and `container`. The target resolves from the **live container record** the node's heartbeat reported; the owning Stack is derived server-side from the record's `owner`. Caller-supplied Stack identity is not an authority (the parameter is gone).
- Refusals, all before websocket acceptance: container unknown on that node, heartbeat evidence older than 150s (the dashboard's stale-node bound: ~2.5 missed beats), owner not a configured Stack placed on that node, owner without attach configuration, or identifier validation failure.

### Canonical origin (acceptances 6–8)

- `theozolith-control origin-init` provisions **one persistent canonical hostname**: `<slug>.<base-domain>` with a 26-character lowercase-base32 slug carrying 128 bits from the OS CSPRNG, persisted at `<data-dir>/canonical-host`. Re-provisioning requires `--force` (the origin is deliberately sticky: DNS, TLS, and every `CONTROL_NODE_URL` hang off it).
- The base domain is configurable; the default is `theozolith.internal` (ICANN-reserved private namespace, so a self-contained deployment never leaks resolution attempts). The first-party deployment uses `theozolith.com`; the product carries no first-party coupling (ADR-0004/0005).
- Production `serve` refuses to start without a persisted host whose slug meets the entropy format; `--insecure-dev` may run bare. Validation is a format check — the generator is the only sanctioned slug source.
- The hostname resolves only on the trusted network (private DNS or hosts entries); the Control Node has no public ingress path. The random name is defense in depth for the browser surface, never a substitute for the admin credential, the private network, or exact-origin enforcement.

### Browser-origin isolation (acceptance 7)

- The session cookie is **`__Host-ozolith_session`**: Secure, HttpOnly, SameSite=Strict, Path=/, no Domain — browsers refuse it off-host, off-path, or over plaintext.
- With a canonical host configured, **cookie-authenticated** state-changing HTTP requests and websockets must carry exactly the canonical `Host` and `Origin` (one correct spelling each, computed from host + public port); missing or mismatched headers fail closed. The login form is enforced identically (it is exclusively a browser surface). **Bearer-authenticated** callers are non-browser clients and are exempt; bearer wins when both credentials appear, so a scripted client with a stray cookie jar never trips the browser contract.

### Per-deployment TLS (acceptance 9)

- `tls-init` includes the canonical host in the certificate automatically and **refuses wildcard hosts** — every deployment mints its own CA and key; no `*.theozolith.com` key exists anywhere to share or steal.

### PTY resource bounds (acceptances 10–12)

- PTY output buffers at most **512 KiB** per session; past high-water the bridge stops reading the master fd, letting the kernel PTY buffer fill and block the attach process (true backpressure, bounded memory); reads resume below **64 KiB**.
- A websocket send that cannot complete within **30s** declares the client dead: the bridge kills the **attach process group only** (SIGHUP, 10s, SIGKILL) — the Run container and its tmux session survive — and the audit log's detach record carries the reason (`process-exited` | `client-closed` | `stalled` | `error`). Server-side task cancellation (client hang-up) escalates to SIGKILL synchronously, since level-based cancellation forbids further awaits.
- Concurrent terminal sessions are capped (default **8**, `THEOZOLITH_TERMINAL_SESSION_CAP`); an over-cap connect closes with 4429 before target resolution and launches no process. Close codes: 4401 auth, 4403 origin, 4404 target, 4429 capacity.

### Best-effort live-Worker evidence (acceptances 13–14)

- Evidence pushes keep their bounded retry (3 attempts per bundle). After exhaustion on a live Worker's Run: the escalation comment renders a normal evidence link only for bundles that landed; an unpushed bundle is named as text (`runs/issue-N/<run-id>`) with the reason — never a dead link. The failure is logged as a structured JSON record (`theozolith.evidence-push-failed`: run_id, issue, bundle, attempts, error). The job directory is **parked immediately** into the `<jobs>-pending` sibling for boot-sweep recovery, so retained evidence never reads as an in-flight Run. Escalation is never blocked on evidence; zombie escalation (the janitor) remains evidence-first, unchanged.

### Per-Stack jobs directories (acceptances 15–17)

- The Node Daemon injects `THEOZOLITH_JOBS_DIR=<base>/<stack-name>` (base `/var/tmp/theozolith/jobs`) into every process Stack; an explicit env value in the Stack definition wins. The queue-behind in-flight signal reads the same resolution, so a targeted recycle observes only the target driver's Runs and a node-wide update waits on each live driver's active Run — dead drivers and `-pending` parking never block.
- The Config Repo rejects duplicate resolved jobs-directory paths per node, including a path landing on another Stack's `-pending` sibling (normalized comparison). The same path on different nodes is legal (different filesystems).

## Consequences

- **Positive**: heartbeat data can no longer reach a shell as structure; terminal reachability is pinned to live, fresh, config-backed ownership; a browser can only speak to the Control Node by its unguessable name and exact origin; a wedged terminal client costs a bounded buffer and one killed ssh, not a Run; escalations are honest about missing forensics; queue-behind ownership is structural instead of heuristic.
- **Negative**: `attach` strings and the shared jobs dir are breaking config changes (parse errors / new injected paths — deployments upgrading from M4 must convert `attach` to argv form and may strand old orphan dirs at the shared `<base>` root, where no per-Stack sweep looks; move them into the owning Stack's new directory once). A canonical origin + private DNS record is now mandatory setup. Sessions bind to the exact hostname: IP-literal browsing stops working in production (bearer/CLI access is unaffected).
- **Neutral**: `--insecure-dev` remains the un-armed dev shape (no TLS, no canonical origin, no origin guard). The Reviewer's workspace handling and the claim/retry/quarantine machinery are untouched.

## Alternatives Considered

- **Shell-quoting the substituted identifiers** (keep string templates, escape at render): rejected — quoting must be correct per boundary (local exec vs. SSH remote string vs. docker argv), and one miss is an injection; a charset whitelist plus complete-argument substitution is correct at every boundary at once.
- **CSRF tokens instead of exact-origin enforcement**: rejected — tokens add per-form state to defend endpoints that only ever have one legitimate origin; SameSite=Strict + `__Host-` + exact Host/Origin is stateless and covers websockets, which token schemes do not.
- **A reverse proxy (or oauth2-proxy) for origin/TLS concerns**: rejected — adds a deployment dependency the product boundary (ADR-0005) doesn't want; the Control Node already terminates TLS itself.
- **Unbounded PTY queue with client-side flow control**: rejected — the server cannot trust the client to drain; the kernel PTY buffer is the natural backpressure vessel and costs nothing.
- **Persistent retry queue for failed evidence pushes**: rejected by the brief — the boot sweep already is the durable retry; adding coordination machinery for a rare failure buys nothing but state.
- **Pipeline-specific parsing of job-dir names for queue-behind ownership**: rejected by the brief — encoding ownership in directory *placement* (one dir per Stack) needs no naming convention and survives custom Stacks.
