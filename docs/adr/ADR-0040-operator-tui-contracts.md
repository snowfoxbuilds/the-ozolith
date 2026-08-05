Status: ACCEPTED

Date: 2026-08-05

Provenance: delegated decisions from the M9 brief (Operator TUI) — the ADR-0015 amendment text, attach-command delivery, degraded-mode rendering, and the panel/keybinding/cadence surface (documented in `--help`, per the brief; recorded here only where something surprising emerged). Implements the "Grilling 2026-08-04" Operator-TUI rulings in NODE-SUBSTRATE.md; consumes ADR-0022 (attach hardening, 150 s threshold), ADR-0038 (events read view), ADR-0039 (state read model, server-clock rule); amends ADR-0015 (dependency exception, state-document keys).

# ADR-0040: Operator TUI contracts — read-model keys, pure-consumer enforcement, and the failure-class channel gap

## Context

`theozolith top` (M9) is a Textual application in the operator CLI: a pure API consumer over loopback with the admin bearer token, same endpoints as any remote client, reaching capability parity with the frozen web surface for routine operations. The brief fixed the surface (read panels; exactly three writes; print-only attach assistance; read-only settings) and forbade inventing endpoints beyond M8's. Implementing it surfaced four data needs the M8 read models did not carry, and one datum the channel itself does not carry.

## Decision

### The state document gains the TUI's keys — fields, not endpoints

`/api/v1/state` (already the status/TUI read model, ADR-0039) gains, additively:

- `desired_stacks[*].attach` — the Stack's attach argv, verbatim from the Config Repo. The TUI resolves the pastable attach command client-side, against the same heartbeat evidence (`stack_containers` rows) and the same refusal order the PTY bridge uses (ADR-0022), judging freshness on the server clock (`now` vs the row's `updated_at`, 150 s). **The channel invariant is untouched**: attach is consumed on the admin read model only and still never rides a heartbeat response (test-pinned).
- `desired_stacks[*].env` — the Stack's non-secret env declarations. The one consumer today is the Run timeout budget: the TUI renders "elapsed vs. budget" by resolving `THEOZOLITH_AGENT_TIMEOUT_SECONDS` from the Run's Stack exactly as the daemon injects it, falling back to the worker's shipped default (3600 s, mirror pinned by test). The read model stays on the server, the derivation in the client — ADR-0039's split.
- `repo` — the coordination target (`owner/name`, null when dispatch is off). The TUI builds issue/PR/evidence links from it exactly as the dashboard does server-side.
- `control_toml` — the read-only settings view: `control_ip`, `control_port`, `browser_origin` (null while disabled), and the **effective** tier-2 values this serve is running with (control.toml overlaid with env overrides — what is live, not what the file says). Editing stays git-native; the TUI renders and never writes.

These are additive keys on an existing endpoint under the "server documents evolve under their own contract" rule (ADR-0039) — not new endpoints, which the brief reserves for a grilling.

### Pure-consumer discipline is structural, not aspirational

The `tui` package's import closure is stdlib + Textual + its own subpackage, enforced by AST test: the store, the secret store, the web surface, `sqlite3`, `subprocess`, and `pty` are unreachable from the TUI module tree, and neither database file is even named in its sources. Constants the TUI shares with other owners (the 150 s threshold, the attach identifier whitelists and placeholders, the evidence branch and bundle path shape, the worker timeout default) are **redeclared and pinned by a mirror test** — the statuscli precedent — instead of imported, because every owning module would drag a heavy or database-capable dependency into the tree. Auth resolves through `cli._admin_env` → `statuscli.resolve_target`, the one implementation; the client is the package's only I/O and sends the bearer on every call (exercised against a real loopback socket — the SSH-forwarded shape).

### Terminal Runs: the failure class is not on the channel — rendered honestly, flagged for grilling

The ruling's run-detail list ("outcome, failure class, PR link, evidence-bundle reference once terminal") assumed the existing channel contract carries all four. It carries three. A failed/escalated `theozolith.run` event has no `failure_class` field — the classes (`infra`, `harness`, `timeout`, `session-died`, `no-changes`) exist worker-side and land only in the evidence bundle's `run.json`. M9 may not touch `worker/` (acceptance: unchanged, stdlib-only), so the TUI renders the gap honestly: terminal run detail shows outcome, PR link, and the evidence-bundle reference, and prints "(not on the channel — recorded in the evidence bundle's run.json; ADR-0040)" for the failure class. The renderer already consumes `payload.failure_class` when present, so a future one-field amendment to the worker's terminal run events lights it up with no TUI change. **Whether to make that amendment is an open grill item**, deliberately not decided here.

### Attach delivery: print-only

The attach action shows the substituted command in a modal for the operator to copy — no OSC 52 clipboard write in v1. Terminal support for OSC 52 is inconsistent and fails silently (a "copied!" that copied nothing is worse than no copy), printing is the portable floor, and the ruling's audit posture ("pasted SSH bypasses terminal-audit.log by accepted design") is about the paste either way. Template argv elements are shell-quoted on render; the substituted identifiers pass the ADR-0022 shell-inert whitelists first, so quoting is belt over braces.

### Degraded mode: banner over stale data, never blocking

A failed refresh keeps the last documents on screen under a prominent banner naming the dial target, the error class, and the age of what is shown; polling continues on the cadence and the banner clears on the next success. A blocking modal was rejected: the operator mid-incident needs the last known fleet picture more than a confirmation prompt.

### The write flows

- **Infrastructure commands**: the TUI offers `drain`, `recycle`, `update`, `rebuild` (`restart` stays CLI-only — it is the off-pin escalation's verb, not a routine operation, and the TUI deliberately has no `--force`). Destructive verbs (`recycle`, `update`, `rebuild`) refuse until the target's name — the Stack/image, or the node for whole-node forms — is typed back exactly; the refusal happens client-side, before any HTTP write. Queue-behind deferrals reported over heartbeats surface on the command queue panel.
- **Quarantine release**: one confirmed action against the existing release endpoint.
- **Secret entry**: masked input; the value's only egress is the `PUT /api/v1/secrets/{name}` body; the confirmation names the secret, never the value (test-swept across every rendered surface).

### Events follow mode and eviction honesty

Follow mode keeps only ids newer than the newest row already held: each tick fetches the head page and walks the cursor **only across the unseen gap**, stopping on overlap or when the page ends exactly one id above the newest seen (ids are monotonic and never reused — ADR-0038); an unclosed gap after a bounded walk resyncs the panel honestly instead of pretending continuity. The eviction notice is the split contract verbatim: it keys on the response's query-relative `evicted` for the panel's own filter conjunction, accumulates while the conjunction stands (shown history can stay incomplete after one evicted answer), resets when the conjunction changes, and `any_evicted` alone never flags a panel.

### Rendering rule: agent text is never markup

Transcript tails, event payloads, error messages, and every other agent-adjacent string render through plain `Text` — no markup interpretation anywhere (the template-escaping rule of the web surface, transposed). The advisory transcript tail is labeled as such, with the shown byte count and the source transcript's total.

## Consequences

- **Positive**: the TUI needs zero privileged local shortcuts — pointed at an SSH-forwarded socket it works unmodified; the read-model additions serve any future API consumer, not just this one; the failure-class gap is visible instead of papered over; a worker-side amendment later is one field.
- **Negative**: five constants exist twice (pinned by test, but still twice); run-state reduction is capped at one max-size page of run and progress events per tick — a fleet with more than 500 tracked issues renders a truncated Runs panel (far beyond V1 scale; noted, not solved).
- **Neutral**: Textual adds a dependency to the one component that already has five; the web surface is untouched (no template, static, or cookie-route diffs).

## Alternatives rejected

- **A server-side `/api/v1/runs` (or `/api/v1/settings`) endpoint**: new read endpoints beyond M8's, which the brief reserves for a grilling; the state document and events view already carry the facts.
- **Importing `web.views`/`web.terminal`/`worker.evidence` from the TUI for shared constants**: drags the web surface (and its FastAPI import graph) or worker internals into the pure-consumer tree; mirror-pinned redeclaration keeps the boundary machine-checked from both sides.
- **Synthesizing a failure class from correlated `theozolith.error` events**: error events carry no run correlation key; substring-matching run ids out of free-text messages is guesswork presented as fact.
- **OSC 52 clipboard delivery for attach**: silent failure on unsupported terminals; print-only is the floor every terminal has.
- **Blocking (modal) unreachable handling**: hides the last known fleet state exactly when it matters most.
- **A TUI `--force` on commands**: kill-the-tree from a full-screen surface invites reflex-clicking; the CLI form remains the deliberate path.
