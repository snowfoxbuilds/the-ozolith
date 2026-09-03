Status: ACCEPTED

Date: 2026-07-18

# ADR-0022: Control web hardening — attach argv, public origin, PTY bounds, per-Stack jobs directories

## Context

The post-merge review of PR #5 found security and stability gaps in terminal command construction, browser-origin isolation, PTY resource handling, and queue-behind's shared-jobs-directory ownership heuristic. Brief M5 settled the policy: structured attach argv, server-derived Stack ownership, a mandatory randomized public origin, configurable base domain, private DNS and per-deployment TLS, bounded terminal resources, best-effort live-Worker evidence, and per-Stack jobs directories. The trusted-network, single-operator deployment model is unchanged. This ADR was originally authored in-repo as ADR-0019 (PR #6) and renumbered to ADR-0022 after ADR-0019 was allocated to Headless Runs and the Flight Deck.

## Decision

### Attach command structure

- A Stack's `attach` key is an **argv array of strings**. A free-form command string is a Config Repo parse error.
- `{host}` and `{container}` substitute only as **complete argv elements**. An element embedding either placeholder, such as `user@{host}`, is a parse error. No other element is transformed.
- Both identifiers are validated immediately before process launch against shell-inert whitelists: hostnames are dot-joined DNS labels (`[A-Za-z0-9]` first, inner hyphens, at most 253 characters); container names follow `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`. Whitespace, control characters, shell syntax, and leading `-` cannot pass, including across SSH's remote-command shell boundary.

### Terminal target authorization

- The terminal URL names only `node` and `container`. The target resolves from the live container record reported by heartbeat; its owning Stack is derived server-side from `owner`. Caller-supplied Stack identity is not an authority.
- Attach is refused before websocket acceptance when the container is unknown on that node, heartbeat evidence is older than 150 seconds, the owner is not a configured Stack on that node, the owner has no attach configuration, or identifier validation fails.

### Public origin

- The canonical browser origin is one explicit, complete `https://` URL, independent of Uvicorn's bind host and port.
- `theozolith-control origin-init` provisions `https://<slug>.<base-domain>` with a 26-character lowercase-base32 slug carrying 128 bits from the OS CSPRNG. It persists the complete value at `<data-dir>/public-origin`. Default HTTPS omits `:443`; `origin-init --port` records a nonstandard external port explicitly. Re-provisioning requires `--force`.
- The base domain is configurable. The default is `theozolith.internal`; the first-party deployment uses `theozolith.com`, yielding `https://<128-bit-random-slug>.theozolith.com` without first-party coupling in the product.
- Production `serve` accepts the persisted artifact or `THEOZOLITH_PUBLIC_ORIGIN`, with the environment override winning. The override is an expert escape hatch: format is validated, but the operator is responsible for supplying a CSPRNG-generated slug because entropy cannot be inferred from text. `--insecure-dev` may run without an origin.
- Parsing fails closed on non-HTTPS schemes, credentials, any path other than empty or `/`, queries, fragments, wildcard hosts, malformed ports, and nonconforming slug labels. Changing `serve --port` changes only the bind and never the accepted browser origin.
- The hostname resolves only on the trusted network, and the Control Node has no public ingress. Random naming is defense in depth, never a replacement for the admin credential, private networking, or exact-origin enforcement.

### Browser-origin isolation

- Production uses `__Host-ozolith_session`: Secure, HttpOnly, SameSite=Strict, Path=/, and no Domain. `--insecure-dev` over plain HTTP uses the unprefixed `ozolith_session` without Secure. A deployment issues and accepts only the cookie appropriate to its mode.
- Cookie-authenticated state-changing HTTP requests and websockets must carry exactly the `Host` and `Origin` derived from the parsed public-origin URL. Missing or mismatched values fail closed, including on login. Bearer-authenticated callers are exempt; bearer authentication wins when both credentials appear.

### Per-deployment TLS

- `tls-init` extracts only the public origin's hostname, never its port, and includes it in the certificate SAN.
- Wildcard hosts are refused. Every deployment mints its own CA and key; no shared `*.theozolith.com` private key exists.

### PTY resource bounds

- PTY output buffers at most **512 KiB** per session. Reading pauses at high water so the kernel PTY buffer backpressures the attach process; reading resumes below **64 KiB**.
- A websocket send stalled for more than **30 seconds** kills only the attach process group: SIGHUP, a 10-second grace period, then SIGKILL. The Run container and its tmux session survive. Detach audit records include the reason.
- Every failed-spawn path closes the PTY master exactly once, including non-`OSError` exceptions and cancellation. Cancellation during teardown cannot skip the SIGKILL fallback.
- Resize frames clamp to **20–500 columns** and **5–300 rows**. Malformed, negative, missing, non-numeric, non-finite, deeply nested, or absurd values cannot tear down the session or reach `TIOCSWINSZ` outside those bounds.
- Concurrent terminal sessions are capped at **8** by default (`THEOZOLITH_TERMINAL_SESSION_CAP`). A slot is reserved synchronously before process launch; excess connections close with 4429. Other close codes are 4401 auth, 4403 origin, and 4404 target.

### Best-effort live-Worker evidence

- Evidence pushes retain the bounded three-attempt budget. After exhaustion, escalation continues: landed bundles receive links, unavailable bundles are named without dead links, and `theozolith.evidence-push-failed` is logged structurally.
- The completed job directory is normally moved atomically to the `<jobs>-pending` sibling for boot-sweep recovery, so it never reads as an active Run.
- If the normal park fails, log `theozolith.evidence-park-failed` and try a collision-safe unique destination. Collision-parked bundles publish under the **original ****`run_id`**** path**; the sweep strips the parking suffix so janitor lookups and comment paths remain stable.
- If the second park also fails, log `theozolith.evidence-lost` and remove the completed directory from the active jobs directory. Evidence loss in this compound case is accepted; escalation never blocks.
- If removal leaves undeletable remnants, rename the directory in place to `.evidence-lost-<run-id>-<random>`. Dot-prefixed tombstones are never live Runs: queue-behind and the sweep ignore them. Only an unwritable jobs directory can defeat parking, removal, and tombstoning; that residual stays under its Run name, remains sweep-recoverable, logs `theozolith.evidence-remnants`, claims no loss, and may defer lifecycle work until an operator clears it.
- Zombie escalation remains evidence-first. If evidence is permanently lost and the driver dies before escalation lands, resolution remains a dashboard-visible human call.

### Per-Stack jobs directories

- The Node Daemon injects `THEOZOLITH_JOBS_DIR=/var/tmp/theozolith/jobs/<stack-name>` into every process Stack; an explicit Stack environment value wins. The driver's daemon-less default resolves identically.
- The Config Repo rejects duplicate normalized jobs paths per node, including collisions with another Stack's `-pending` sibling. The same path on different nodes is legal.
- Targeted recycle observes only the target Stack's active Runs. Node-wide update waits for every live process Stack's active Run. Dead-driver directories, pending evidence, collision-parked evidence, and dot-prefixed tombstones do not block.

## Consequences

- **Positive**: heartbeat data cannot alter shell structure; terminal reachability is pinned to fresh, config-backed ownership; browsers must use one randomized exact origin; stalled clients consume bounded resources; evidence loss is explicit; queue-behind ownership is structural.
- **Negative**: M4 attach strings become parse errors; existing deployments must convert them to argv. Per-Stack jobs paths can strand old orphan directories at the former shared root. A public origin and trusted-network DNS record are mandatory. IP-literal production browsing stops working. Compound parking failure can deliberately lose unpushed evidence.
- **Neutral**: `--insecure-dev` remains the unarmed local-development shape. Reviewer workspace handling and claim, retry, quarantine, and zombie policies are otherwise unchanged.

## Alternatives Considered

- **Shell-quote substituted identifiers**: rejected because quoting must be correct across local exec, SSH's remote shell, and docker argv; complete-argument substitution plus a shell-inert charset is correct at every boundary.
- **CSRF tokens instead of exact origin**: rejected because one legitimate origin exists, exact-origin enforcement is stateless, and it also covers websockets.
- **Reverse proxy for origin/TLS**: rejected as an unnecessary deployment dependency; the Control Node already terminates TLS.
- **Unbounded server queue with client flow control**: rejected because the client cannot be trusted to drain; kernel PTY backpressure is the natural bound.
- **Persistent evidence retry queue**: rejected; the boot sweep is already the durable retry and evidence does not become coordination state.
- **Pipeline-specific parsing for queue ownership**: rejected; one directory per Stack encodes ownership structurally and supports custom Stacks.

## Amendments

- **2026-07-27 (grilling session)**: public-origin storage relocated by ADR-0024 — the persisted origin moves from the `<data-dir>/public-origin` flat file into `control.toml` in the Config Repo as a tier-1 field — written by `origin-init`/`init`, rendered read-only in the dashboard settings form, `THEOZOLITH_PUBLIC_ORIGIN` override unchanged. Origin semantics, fail-closed parsing, and exact-Host/Origin enforcement are untouched; only the storage location changes. The origin is deployment customization, not machine state, and restoring the Config Repo must restore it (ADR-0024 recovery).

## Relevant PRs

- #5 — the PR whose post-merge review found the security and stability gaps this ADR addresses.
- #6 — original authoring PR (this ADR was drafted as ADR-0019 before renumbering to ADR-0022).
