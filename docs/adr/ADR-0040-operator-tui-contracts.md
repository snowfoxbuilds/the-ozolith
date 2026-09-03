Status: ACCEPTED

Date: 2026-08-05

# ADR-0040: Operator TUI contracts — read-model keys, pure-consumer enforcement, and the terminal failure-class channel

## Context

This is a set of delegated decisions from the M9 brief (Operator TUI) — the ADR-0015 amendment text, attach-command delivery, degraded-mode rendering, and the panel/keybinding/cadence surface (documented in `--help`, per the brief; recorded here only where something surprising emerged) — implementing the "Grilling 2026-08-04" Operator-TUI rulings in NODE-SUBSTRATE.md and the M9 correction-pass ruling on terminal failure classes; it amends ADR-0015 (dependency exception, state-document keys, terminal run-event schema) and consumes ADR-0022 (attach hardening, 150 s threshold), ADR-0038 (events read view), and ADR-0039 (state read model, server-clock rule).

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

### Terminal Runs: the failure class rides the channel (ruling accepted)

The ruling's run-detail list ("outcome, failure class, PR link, evidence-bundle reference once terminal") assumed the existing channel contract carries all four; it carried three — the classes (`infra`, `harness`, `timeout`, `session-died`, `no-changes`) existed worker-side and landed only in the evidence bundle's `run.json`. The grill item is now ruled and closed: **the worker event channel is amended**. Terminal failed/escalated `theozolith.run` events carry `failure_class` — the canonical class the driver already determined and wrote to `run.json`, the same value and vocabulary at both destinations, emitted at the two (and only two) terminal-failure emit sites so every exit path (timeout, session death, harness failure, setup/infra breakage, no-changes, local retry, final escalation) carries it. Nothing infers the class downstream — not the TUI, not the Control Node; only the already-classified label crosses the channel, never evidence-bundle contents. `pr-open` events carry no field at all (a successful Run has no failure class — the TUI renders "not applicable", never a channel defect), and the schema change is a narrow event amendment: `worker/` stays stdlib-only and architecturally untouched.

Backward compatibility: events written before the field existed remain readable with no migration — an absent `failure_class` on a legacy failed/escalated event renders as an explicit "(legacy event — emitted before failure_class rode the channel; recorded in the evidence bundle's run.json)" message, never a blank and never an error. Ingestion is untouched (payloads store verbatim; unknown fields were always accepted).

### Run states: a complete client-side index, honest about its telemetry

The Runs panel is the client-side twin of `store.run_states()` and must be equivalent to it: the latest `theozolith.run` event per **retained issue** — never a one-page snapshot (the server's page bound is on events, not issues; a single 500-row page silently lost any issue whose latest event predated it). The TUI keeps a latest-per-issue index over the existing events endpoint: bootstrapped once by a cursor walk across the full retained run history (run events are durable cache records, never evicted — the walk sees everything), then advanced incrementally from new head events each poll, with cursor overlap handled by id filtering and an unclosable advance gap answered by a fresh bootstrap — never a per-tick rescan of unbounded history. A defensive page bound remains on any single walk; crossing it sets a visible incomplete-data notice on the panel ("Runs beyond the walked window are missing") — it never silently removes a Run, and it is a bound on event pages walked, not a count of tracked issues.

Progress joins the index under ADR-0016's honesty split: run events are durable, progress is evictable advisory telemetry. The latest available progress per live run_id is found across page boundaries; a live Run whose telemetry was evicted renders "telemetry unavailable" and a terminal Run needs none — absence of progress never removes a row, because the durable run event IS the row's existence.

### Stacks: the desired/actual union, drift never hidden

Stack rows come from the union of `(node, stack)` keys in `desired_stacks` and heartbeat-reported `stacks` — the frozen web surface's drift behavior, transposed. Desired-only rows render "not reported"; actual-only rows (reported by a node after their desired definition was deleted or never placed) render desired `(unplaced)` with the reported kind and detail preserved, and are never converged — an actual-only running Stack is off desired by definition, not silently fine. The rows disappear only when the node reconciles and stops reporting them.

### Attach delivery: print-only

The attach action shows the substituted command in a modal for the operator to copy — no OSC 52 clipboard write in v1. Terminal support for OSC 52 is inconsistent and fails silently (a "copied!" that copied nothing is worse than no copy), printing is the portable floor, and the ruling's audit posture ("pasted SSH bypasses terminal-audit.log by accepted design") is about the paste either way. Template argv elements are shell-quoted on render; the substituted identifiers pass the ADR-0022 shell-inert whitelists first, so quoting is belt over braces.

### Degraded mode: banner over stale data, never blocking — and attach fails closed

A failed refresh keeps the last documents on screen under a prominent banner naming the dial target, the error class, and the age of what is shown; polling continues on the cadence and the banner clears on the next success. A blocking modal was rejected: the operator mid-incident needs the last known fleet picture more than a confirmation prompt.

Attach assistance is the exception to render-and-continue, because it presents heartbeat-derived evidence as CURRENT: a retained snapshot's `now` is frozen at the last successful refresh, so its heartbeat rows would pass the 150 s check forever. From the first failed refresh until the next successful one, attach refuses outright, stating that current server-clock freshness cannot be established — it never substitutes the local wall clock to manufacture a heartbeat age (the no-local-time rule, ADR-0039). A successful refresh clears the condition and restores the ordinary 150 s evaluation against the NEW server clock. The three write flows stay available while degraded (they attempt the authenticated call and report its failure); no read-side action presents stale heartbeat evidence as live.

### The write flows

- **Infrastructure commands**: the TUI offers `drain`, `recycle`, `update`, `rebuild` (`restart` stays CLI-only — it is the off-pin escalation's verb, not a routine operation, and the TUI deliberately has no `--force`). Destructive verbs (`recycle`, `update`, `rebuild`) refuse until the target's name — the Stack/image, or the node for whole-node forms — is typed back exactly; the refusal happens client-side, before any HTTP write. Queue-behind deferrals reported over heartbeats surface on the command queue panel.
- **Quarantine release**: one confirmed action against the existing release endpoint.
- **Secret entry**: masked input; the value's only egress is the `PUT /api/v1/secrets/{name}` body; the confirmation names the secret, never the value (test-swept across every rendered surface).

### Events follow mode and eviction honesty — including client-side gaps

Follow mode keeps only ids newer than the newest row already held: each tick fetches the head page and walks the cursor **only across the unseen gap**, stopping on overlap or when the page ends exactly one id above the newest seen (ids are monotonic and never reused — ADR-0038). An unclosed gap after the bounded walk resyncs the panel from the newest rows **and records the skip as per-panel continuity state**: the intermediate matching events were dropped client-side, so the panel shows a "history incomplete" warning naming the cause as a follow overflow — explicitly not server eviction; the two facts are tracked separately (per panel: Events and Errors independently) and only their presentation combines. The warning is cleared exclusively by a change of that panel's filter conjunction — a later head poll that overlaps the already-resynced head proves nothing about the skipped events (history is never re-fetched), so it never clears the warning.

The eviction notice is the split contract verbatim: it keys on the response's query-relative `evicted` for the panel's own filter conjunction, accumulates while the conjunction stands (shown history can stay incomplete after one evicted answer), resets when the conjunction changes, and `any_evicted` alone never flags a panel.

### Rendering rule: agent text is never markup

Transcript tails, event payloads, error messages, and every other agent-adjacent string render through plain `Text` — no markup interpretation anywhere (the template-escaping rule of the web surface, transposed). The advisory transcript tail is labeled as such, with the shown byte count and the source transcript's total.

## Consequences

- **Positive**: the TUI needs zero privileged local shortcuts — pointed at an SSH-forwarded socket it works unmodified; the read-model additions serve any future API consumer, not just this one; terminal failure classes are visible without evidence-bundle inspection, and legacy events stay readable; every incompleteness the panel can suffer (server eviction, follow overflow, a truncated index walk) is disclosed on the affected panel.
- **Negative**: five constants exist twice (pinned by test, but still twice); the run index costs a one-time bootstrap walk over the retained run history (bounded, disclosed if truncated) and holds one event per retained issue client-side; the worker event schema gained one field, so event consumers now see two generations of terminal events (handled by the explicit legacy rendering).
- **Neutral**: Textual adds a dependency to the one component that already has five; the web surface is untouched (no template, static, or cookie-route diffs).

## Alternatives Considered

- **A server-side `/api/v1/runs` (or `/api/v1/settings`) endpoint**: new read endpoints beyond M8's, which the brief reserves for a grilling; the state document and events view already carry the facts.
- **Importing `web.views`/`web.terminal`/`worker.evidence` from the TUI for shared constants**: drags the web surface (and its FastAPI import graph) or worker internals into the pure-consumer tree; mirror-pinned redeclaration keeps the boundary machine-checked from both sides.
- **Synthesizing a failure class from correlated `theozolith.error` events**: error events carry no run correlation key; substring-matching run ids out of free-text messages is guesswork presented as fact.
- **OSC 52 clipboard delivery for attach**: silent failure on unsupported terminals; print-only is the floor every terminal has.
- **Blocking (modal) unreachable handling**: hides the last known fleet state exactly when it matters most.
- **A TUI `--force` on commands**: kill-the-tree from a full-screen surface invites reflex-clicking; the CLI form remains the deliberate path.

## Amendments

- **2026-08-05 (#18)**: same-day M9 correction pass, folded into the decision above rather than left as later revisions — the terminal failure-class ruling accepted (failed/escalated `theozolith.run` events carry `failure_class`), degraded-mode attach fails closed instead of presenting a frozen heartbeat snapshot as live, the Runs index made complete across event pages rather than a single-page snapshot, Stacks render the desired/actual union so drift is never hidden, and client-side follow-gap skips disclosed on the affected panel.

## Relevant PRs

- #18 — implementation PR; the M9 Operator TUI (`theozolith top`), carrying the same-day correction pass folded into this ADR.
