Status: ACCEPTED

Date: 2026-07-15

Provenance: authored in-repo under the M2 delegated-decisions mandate; pending uplift to Notion (ADR-0001).

# ADR-0012: Reviewer actor design and Run lifecycle defaults

## Context

M2 delegates the Reviewer actor's design (poll cadence, review-comment format including the resume designation, model configuration) and the Worker recycle policy defaults. Both actors ship in one image with the vendor adapter contained per image (agent swap boundary), so these choices must be pure configuration and comment-format contracts, not code coupling.

## Decision

### Reviewer actor

- **Poll cadence**: same knob as the Worker (`THEOZOLITH_POLL_SECONDS`, default 60s). Each poll pass reviews every PR labeled `pr_ready` without `needs_human`/`blocked`, sequentially. Reviewer downtime queues PRs at `pr_ready`; a restart picks up cleanly (all state on GitHub).
- **Review-comment format**: one PR comment per verdict — human-readable heading (`### Reviewer verdict: <verdict> (round N)`), evidence, and for revise verdicts the revised plan plus `Resume from commit `<sha>`` (optionally `Then cherry-pick: …`), followed by a machine block (`<!-- theozolith:verdict … -->`, JSON: verdict, round, grades, revised_plan, resume_commit, cherry_pick, bundle_url). The next Run parses the latest machine block; humans read the same comment. Comments after the latest verdict are treated as review discussion (this is how a human's decision on a `blocked` PR reaches the next Run).
- **Resume designation**: `resume_commit` defaults to the PR head at verdict time when the model gives none. The next Run hard-resets the branch to it (force-with-lease push when that rewrites history) and cherry-picks any designated commits with `-x`. An unresolvable resume commit falls back to the branch head and is noted in the Run report — a wrong designation degrades to "resume from head", never a crash.
- **Verdict application order (revise)**: comment → `attempt-N` → remove `pr_ready` → strip claim → re-queue `plan_ready`. The issue only becomes claimable after the plan that the next Run needs is already on the PR. Approve keeps `pr_ready` and adds `needs_human` + `deviation:*` + `risk:*`; escalate removes `pr_ready` and adds `blocked` + `needs_human` with the bundle link.
- **Budget enforcement**: a PR arriving for review already bearing `attempt-3` is escalated deterministically (no model call) — the budget check cannot be argued with. Under budget, the model chooses among the three verdicts.
- **Model configuration**: `THEOZOLITH_MODEL` per actor; defaults Worker `claude-sonnet-5`, Reviewer `claude-fable-5` (a stronger tier, per ADR-0008). The stronger-model requirement is a deploy-time convention (documented in `.env.example`), not runtime-enforced — the actors cannot compare arbitrary model names.
- **An unusable model reply** (no parseable verdict JSON) applies no state and is retried on the next poll: throughput degrades, correctness never.

### Run lifecycle defaults

- **Recycle policy**: the Worker exits after `THEOZOLITH_RECYCLE_RUNS` Runs (default **10**); the container restart policy brings it back fresh. Resource-threshold recycling is deferred to the Node Agent (M3+), which owns container lifecycle.
- Runs execute in `THEOZOLITH_WORKDIR/run-<run-id>` and the tree is removed unconditionally after the Run (statelessness); a crashed Run leaves no PR-side state and its stale claim waits for the M3 janitor (manual cleanup in M2).

## Consequences

- **Positive**: the Worker↔Reviewer interlock is a documented comment format on the shared PR — any future adapter or human can produce or consume it; deterministic budget escalation caps spend even with a misbehaving model; verdict ordering removes the race where a Worker claims before the revised plan exists.
- **Negative**: verdict parsing depends on an HTML-comment convention in PR comments (editing a verdict comment by hand can confuse the next Run); model strength is asserted by convention, not enforcement.
- **Neutral**: per-issue review serialization (one verdict per PR per pass) bounds Reviewer throughput; acceptable single-box V1.

## Alternatives Considered

- **Structured verdicts via a separate API/file instead of PR comments**: rejected — comments are the durable, human-legible, vendor-neutral channel ADR-0008 already prescribes; a side channel would need its own store and break "everything durable is a GitHub object".
- **Reviewer re-reviews budget-exhausted PRs on merit**: rejected — an unbounded loop with a persuasive Worker; the human is the escape hatch.
- **Enforcing the stronger-model rule in code**: rejected — requires a model-ranking table that ages badly (ADR-0002 spirit: conventions over brittle enforcement).
- **Recycle by wall-clock or memory thresholds in M2**: rejected — container resource lifecycle belongs to the Node Agent (M3); Run count is observable from inside and sufficient for cache hygiene.
