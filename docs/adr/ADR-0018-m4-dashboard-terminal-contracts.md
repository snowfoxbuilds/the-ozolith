Status: ACCEPTED

Date: 2026-07-17

Provenance: authored in-repo under the M4 delegated-decisions mandate (this PR); awaiting Notion uplift (ADR-0001).

# ADR-0018: M4 dashboard, terminal, and dispatch contracts

## Context

The M4 brief (dashboard + web terminal, closing V1) delegates four decisions to the implementing PR: the dashboard information architecture and refresh mechanism, the admin credential and session mechanics, the PTY-bridge transport and audit-log format/location, and the quarantine-release surface. Implementing ADR-0016/0017 conformance also forced several wire- and policy-level contracts those ADRs left open. Governing constraints: ADR-0016 (evidence-first failure handling, cache-not-archive, quarantine policy), ADR-0017 (claim write-through, activation window, control PAT required), NODE-SUBSTRATE.md (trust model, attach-command terminal, channel invariant), and the M4 brief's out-of-scope list (no config editor, no roles, no dashboard write path to GitHub).

## Decision

### Dashboard IA and refresh

- One page, three server-rendered fragments — Workers & Runs (with needs-attention flags), Nodes (stacks desired-vs-actual, run containers, image build metadata), Events — each refreshed by **HTMX polling every 5s** (`hx-trigger="every 5s"`), well inside the one-heartbeat acceptance bound. Polling over SSE: one mechanism for every panel, no connection bookkeeping or reconnect logic, and cost that is trivial at control-plane cadence. Jinja templates + vendored `htmx.min.js`/xterm assets — committed static files, no build step.
- Build skew = the same image name reported by different nodes with differing (base digest, instruction hash); flagged as a banner plus per-row badges. A node is "stale" after 150s of heartbeat silence (~2.5 missed beats).
- Everything agent-authored (transcript tails, unknown event payloads) renders through Jinja autoescape and is treated as prompt-injection-shaped text; unknown namespaced event types render generically (type + escaped JSON payload) — the typed-event extension point needs no product change.

### Admin credential and session

- The **admin token is the single credential** (trust model: dashboard access = cluster access). The login form takes it directly; no second password store exists. A successful login mints a stateless session cookie — `expiry.HMAC(expiry)` under a per-process random key — HttpOnly, SameSite=Strict (the CSRF measure for the two POST forms), Secure over TLS, 12h TTL. Sessions deliberately die with a Control Node restart. API-style callers may present the same token as a bearer header on any web route.
- The secret form performs the same store write as `PUT /api/v1/secrets/{name}` (encrypt, upsert) under the same TLS-channel guard, and never echoes values — it lists names only.

### PTY bridge and audit log

- Transport: **one websocket per session**; binary frames are raw terminal bytes both ways; text frames are JSON control messages, currently only `{"resize": {"cols", "rows"}}` (TIOCSWINSZ). Keepalive is the server's websocket ping (uvicorn default). The bridge runs the Stack's `attach` template (new Config Repo key on Stacks, control-side only — it never travels to nodes) with `{host}`=node name and `{container}`=target, in its own process group, killed (SIGHUP, then SIGKILL) when either side hangs up.
- Gates, in order: admin session; the Stack declares an attach command (none = no terminal affordance and a refused socket); the container is live in the latest heartbeat. Close codes 4401 (auth) / 4404 (no target).
- Audit log: **JSON lines** appended to `<data-dir>/terminal-audit.log` — an `attach` and a `detach` record per session with UTC timestamp, actor (`admin`; the single-credential model has exactly one), node/stack/container, and the exact command. A file rather than the control database because the database is a deletable cache (ADR-0016) and an audit trail is not.

### Quarantine release surface

- `POST /api/v1/nodes/{node}/quarantine/release` + CLI verb `theozolith-control unquarantine --node <n>`. Queueing a `recycle` or `update` command for the node also releases it (ADR-0016 names these as the human releases). Release zeroes the consecutive-failure counter; the policy itself (2 consecutive failed Runs closes the gate, never a timer) is ADR-0016's.

### Dispatch and conformance contracts (finishing ADR-0016/0017)

- **Dispatch wire**: `POST /api/v1/dispatch` (node token) with `{role: worker|reviewer, worker, node, login}` — the request doubles as driver registration (the ADR-0017 prerequisite), recorded in a drivers registry. Worker answer: `{issue: {number,title,body,labels} | null, reason?}`; reviewer answer: `{prs: [...]}` (discovery-only). Grants serialize on one lock. Claim write order: assign login → add `in_progress` → remove `plan_ready` (the Control Node removes plan_ready as part of claim creation, as the old claim protocol's winner did).
- **Activation handshake**: the driver's `claimed` event is the grant ack. Emission is retried ×3; if the first Run's claimed event cannot land, the driver abandons the grant (running anyway could fork ownership once the Control Node releases it after the 60s window). The retry Run proceeds regardless — its claim is already activated.
- **Dispatch gate for pending lifecycle commands**: a node with a pending drain/recycle/update gets no new grants, which bounds a queued-behind command by the current Run (the NODE-SUBSTRATE "bounded by the agent-timeout budget" clause). Queue-behind itself is daemon-side: a deferred command is simply never acked, so the Control Node re-delivers it every heartbeat and the daemon re-checks the job-dir in-flight signal; the heartbeat reports `deferred_commands` for dashboard visibility.
- **Ingestion caps** (ADR-0016 "size caps enforced at ingestion"): progress transcript tails truncated to their last 8 KiB; any event payload over 32 KiB refused with 413. Progress events evict oldest-first once their stored payload bytes exceed the ~10GB budget (`THEOZOLITH_TAIL_BUDGET_BYTES`); terminal Run/review events are never evicted.
- **Evidence-landed test** (janitor phase 2): the Run's bundle directory containing `swept.json` (boot sweep) **or** `run.json` (a live push that beat the driver's death) — both are complete forensics. Job directories are deleted only after a confirmed evidence push, live Runs included; a failed push leaves the dir for the sweep to retry. Swept bundles for job dirs with no readable issue metadata (e.g. review workspaces) land under `sweeps/<dir-name>/` instead of a run path.
- **Failure classes** (uniform budget, stamped in `run.json` and the escalation comment): `timeout`, `session-died`, `harness`, `no-changes`, `infra`.
- **Telemetry counters**: `theozolith.run.progress` carries phase, elapsed seconds, tool-call and operator-prompt counters (from a new PostToolUse hook line in the Claude adapter's hook log), transcript byte count and tail. `tokens` is present but null — the Claude adapter has no cheap token feed yet; wiring one is recorded as remaining work, not faked.

## Consequences

- **Positive**: the whole M4 surface ships with zero frontend tooling; every dashboard fact is reconstructible (store stays a cache); one credential and one session mechanism cover page, form, and socket; dispatch, queue-behind, and quarantine compose through one gate.
- **Negative**: 5s polling refetches unchanged fragments (irrelevant at this scale, revisit post-V1); a Control Node restart logs the operator out; the single-actor audit log cannot distinguish operators — acceptable until multi-user auth (post-V1); token counters are absent from telemetry until the adapter grows a feed.
- **Amends**: ADR-0015 (control-plane API: claim-intents endpoint replaced by dispatch; heartbeat request gains `deferred_commands`; commands gain `force`; the auditor's settings/endpoints removed).

## Alternatives Considered

- **SSE / websocket push for the dashboard**: rejected — a second live-connection mechanism (beyond the terminal's) for a single-operator page polled at control-plane cadence buys latency nobody asked for.
- **A separate dashboard password**: rejected — a second credential to store and rotate, guarding the same total authority the admin token already carries.
- **Terminal audit in the control database**: rejected — the database is deletable by policy (ADR-0016); an audit trail must survive cache eviction.
- **Auto-attach terminals to every Stack with a default template**: rejected — NODE-SUBSTRATE is explicit that no configured attach command means no terminal; an implicit default would silently expose sessions.
- **Rejecting oversized progress events outright (no tail truncation)**: rejected — a driver with a chatty transcript would lose all telemetry; truncating the tail keeps the advisory channel alive within the cap.
