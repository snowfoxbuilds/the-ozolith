Status: ACCEPTED — amended 2026-07-27 by ADR-0024 (cache/store split; see Amendments); amended 2026-08-17 with ADR-0046 (#53): the completion-retry class joins the full local retry (see Amendments)

Date: 2026-07-17

# ADR-0016: Failure handling — local retry, failed label, evidence-first escalation

## Context

ADR-0014's failed-Run path (release the claim, re-queue plan_ready, track the retry budget in a machine-readable run-failed marker comment) put distributed state in prose comments: marker parsing, a sanitizer, and a budget check at claim time, with a restore-then-comment ordering race. The zombie janitor's automatic re-queue discarded forensics and could re-burn tokens on undiagnosed failures. Heartbeats were too strict a channel to carry agent progress. Grilling 2026-07-17 (with the 2026-07-16/17 design discussion) replaced the whole lane. Operator priority ordering, stated and binding: stability and token efficiency over uptime.

## Decision

- **Local retry**: on a failed Run the driver keeps the claim and re-launches once, locally. The retry is a full second Run — new run_id, fresh clone/worktree, fresh container, its own evidence bundle. Uniform budget: any non-completed Run burns the single retry regardless of class (timeout, session death, harness crash, zero commits without reasoning, pre-session infra failure). On a second non-completion the driver releases the claim and escalates, linking both evidence bundles and stating each failure's class. The run-failed marker machinery (marker comment, sanitizer, budget-check-at-claim) is deleted.
- **failed label**: escalations from execution failures apply failed + needs_human on the issue (driver: second local failure; janitor: zombie). Review-lane escalations keep blocked + needs_human on the PR — two queryable flavors: failed = execution broke, autopsy the evidence; blocked = a decision is owed. Only the human removes failed, as part of re-queueing. failed overrides plan_ready: dispatch refuses to grant an issue carrying failed and surfaces it on the dashboard as a malformed state.
- **Control-side node quarantine**: after 2 consecutive failed Runs on a node, the Control Node stops granting work to it (it already receives the failed-phase run events and holds the grant gate per ADR-0017). The dashboard shows the quarantine and its reason. Release is human action only — recycle/update or explicit unquarantine — never a timer. A consecutive completed Run resets the counter.
- **Progress telemetry**: drivers emit typed [theozolith.run](http://theozolith.run/).progress events — phase, elapsed time, token/tool-call counters, a size-capped transcript tail. The channel invariant is restated, not gutted: the channel never carries secrets (beyond node-scoped pulls over TLS) or coordination authority; telemetry payloads are advisory and size-capped, enforced at ingestion. Agent-authored text is treated as untrusted (prompt-injection-shaped) wherever displayed.
- **Cache, never archive**: the Control Node database is a cache; the evidence bundle is the sole durable audit trail, so nothing in the control database may ever be the only copy of anything and everything in it is deletable by policy. Transcript tails live under a ~10GB disk budget with oldest-first eviction; terminal events (claimed / pr-open / failed / escalated) are kept — they are tiny and are the metrics substrate.
- **Boot-time evidence sweep**: at startup the driver (the PAT holder — the daemon never receives a credential) sweeps orphaned job directories and pushes them to the evidence branch. Normally, a job dir is deleted only after the push is confirmed on the remote; on push failure it is parked outside the active jobs directory, logged, and retried at the next startup or poll cycle. ADR-0022 adds one bounded exception: if the normal park and a collision-safe fallback both fail, the completed directory may be discarded rather than left where queue-behind would mistake it for an active Run. Undeletable remnants are renamed to a dot-prefixed tombstone ignored by queue-behind and the sweep; an unwritable jobs directory remains a logged, human-cleared residual. Collision-parked bundles still publish under the original `run_id` path—the sweep strips the parking suffix—and carry `swept: true` plus the sweep timestamp, preserving janitor lookups and distinguishing recovered evidence from live-pushed evidence.
- **Two-phase zombie escalation, evidence first**: the janitor detects a claim whose Worker has been silent past the grace period and surfaces it on the dashboard, but does not touch GitHub yet. Only when the swept evidence bundle lands (driver returned and swept) does it release the claim and apply failed + needs_human with the real evidence link. There is no automatic re-queue and no escalate-before-evidence: a node that never returns is a human call made from the dashboard, where the stuck zombie is visible.
## Consequences

- **Positive**: no distributed state in comments — marker parsing, sanitizer, and claim-time budget checks are deleted; every escalation carries complete forensics; the queue is protected from sick nodes at the grant gate; retry cost is bounded and local.
- **Negative**: a down node stalls its zombie issues until it returns or a human intervenes (accepted — uptime ranks below stability and token efficiency); a local retry can re-hit node-local causes (accepted — quarantine catches the pattern); the control database stores agent-authored text, an untrusted display surface.
- **Amends**: ADR-0014 (failed-Run marker/re-queue path replaced by local retry); ADR-0015 (janitor escalates instead of re-queueing; the heartbeat/event channel carries telemetry under the restated invariant); ADR-0002 phrasing (janitorial liveness corrections are the enumerated exception to never-originates-coordination — see also ADR-0017).
## Alternatives Considered

- **Marker-comment retry ledger (ADR-0014 status quo)**: rejected — distributed state in prose comments, with parsing and ordering races the fast-follow audit surfaced.
- **Re-queue to plan_ready on failure**: rejected — burns tokens repeating undiagnosed failures and hands a broken issue to another node without forensics.
- **Escalate zombies before evidence, link the expected path**: rejected by the operator — faster issue turnaround is not worth escalations without complete forensics.
- **Driver-side circuit breaker for sick nodes**: rejected — the Control Node holds the grant gate (ADR-0017) and the fleet view, so it can distinguish a failing node from a failing issue; a driver cannot.
- **Auto-strip failed at dispatch when plan_ready is present**: rejected — launders a forgotten label into silence; a visibly stalled grant is preferable to an unnoticed failure loop.
## Amendments (2026-08-17, ADR-0046 / #53)

- **Completion retry — a second, narrower retry class beside the full local retry.**
  A COMPLETED session whose Output Proposal fails validation (missing or invalid
  required fields — most commonly `commit-message`) gets exactly one **completion
  retry**: a new container, a new run_id, and its own evidence bundle, but with the
  **worktree and the partially-filled proposal preserved** — the finished work is not
  thrown away over a missing field. The relaunch prompt is the main prompt plus a
  machine-generated error appendix (e.g. `the current work is unfinished, missing:
  commit-message, pr-description`) with a fill-only instruction; enforcement of
  fill-only is soft — any churn the retry session does make is reviewable.
- This is an enumerated exception to the no-carryover rule (ADR-0008/ADR-0014
  statelessness), and it is **Implementer-only**. Agent sessions are never preserved
  (vendor `--resume` would be a load-bearing vendor feature — a swap-boundary and
  ADR-0043 violation); only the worktree and the pending proposal carry over. The
  Reviewer's work product *is* the proposal, so a missing or invalid verdict
  escalates immediately (ADR-0014's one-strike rule stands — no driver-side retry
  for the Reviewer).
- **Capped at one per claim, and terminal**: the completion retry does not burn the
  full local retry, but its outcome is final — it ships, or the claim escalates
  `failed` + `needs_human` with every Run's evidence linked. A second invalid
  proposal, or a completion retry that fails for any other reason, escalates; it
  never re-enters the local-retry lane.
- Evidence: the completed-but-invalid Run carries the distinct failure class
  `completion` in its bundle and run events, alongside the existing uniform-budget
  classes.
- The uniform budget is otherwise unchanged: any non-completed Run still burns the
  single full local retry regardless of class, and a schema-version mismatch
  detected pre-work (ADR-0046) lands in the existing pre-session infra class.

## Amendments (2026-07-27, grilling session)

- **Cache, never archive — made true by construction by ADR-0024.** The single control database silently mixed durability classes: the encrypted secret store (ADR-0015) and per-node tokens (ADR-0023) lived in the same "deletable" file as heartbeat state and the event cache, so deleting the cache destroyed secrets and fleet enrollment. ADR-0024 splits it into `cache.db` (node/stack state, events, janitor findings, sessions, join tokens — always safe to delete) and `store.db` (encrypted secrets, per-node tokens — the backup set). This ADR's rule stands; its storage now obeys it.
