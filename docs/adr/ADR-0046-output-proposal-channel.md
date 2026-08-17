Status: ACCEPTED

Date: 2026-08-17

Provenance: grilling 2026-08-16 (#41 Problem 3 rulings), implemented via #53.

# ADR-0046: Output Proposal — a schema-validated output channel written through a format-output CLI, applied post-exit by the driver

## Context

The agent's outputs left the session as free-form artifacts: an in-worktree
`.theozolith/decisions.json` the driver excluded from commits, a hand-written
`verdict.json` the harness copied out, and prompt instructions demanding exact JSON
shapes. The commit message was driver-generated boilerplate (`Run <id> for #<N>`),
even though git history is the only context surface guaranteed to every future Run —
fresh clones carry commits, never PRs or issues. Nothing structurally prevented a
prompt from growing new output surfaces, and a completed session that garbled its
outputs had no lane other than the full local retry, which throws the finished
worktree away. The Context Tree (#52) fixed the input half; this ADR fixes the
output half.

## Decision

### One validated Output Proposal per Run, applied post-exit

- The agent proposes GitHub mutations only via a single **Output Proposal** —
  `output/proposal.json` in the job dir — written through the `format-output` CLI.
  The driver validates and applies it after the session exits, as the **sole policy
  boundary**. CLI validation is convenience, never the trust boundary: an agent
  writing the file by hand changes nothing, because the driver re-validates
  everything post-exit.
- **Allowlist by schema**: forbidden mutations (base branch, issue state, labels,
  needs_human, other PRs) are unrepresentable rather than validated away — there is
  no field that could express them, and unknown fields are rejected. An absent field
  is a no-op, never a clear.
- Schemas are per worker type, keyed off the job manifest's mode.
- **Implementer fields**: `pr-title` (the descriptive part; the driver owns the
  `#N: ` prefix), `pr-description` (the narrative zone; the driver composes the PR
  body = Closes line + narrative + Decisions Section), the Decisions-Section entries
  (`decisions` with rationale, `open-questions`, `remaining-work`, `dead-ends`,
  `process-issues`), and `commit-message` (**required** on every round). `pr-title`
  and `pr-description` are required on the round that creates the PR; on resume
  rounds they are optional and absent means keep-what-exists (the driver only
  upserts the Decisions Section).
- **Reviewer fields**: `verdict` — enumerated `approve | revise | escalate`; an
  invalid value and a final-round `revise` fail loud **at write time** (this absorbs
  ADR-0014's validate-verdict harness job) — plus the verdict content (`evidence`,
  `deviation`/`risk` for approve, `revised-plan`/`resume-commit`/`cherry-pick` for
  revise, `process-issues`). Content is audience-conditional by prompt: a non-final
  revise carries findings and the amendment plan for the next Run; an approve or any
  final round writes for the human — signals, decisions requiring adjudication,
  findings. The driver renders the published verdict comment (human text + machine
  block) and applies labels from the validated proposal, exactly as before.
- The in-worktree `.theozolith/decisions.json` and its `_exclude_metadata` fence are
  retired; the proposal lives in the job dir, outside the checkout, and never needs
  excluding from commits.

### The CLI

- `format-output <field> <value>` writes one field and echoes fill state per write
  (e.g. `pr-title: 23 chars`); multi-line fields (`commit-message`,
  `pr-description`, verdict content) take stdin or `--file`. `format-output status`
  prints the full fill-state table and exits non-zero while the proposal would fail
  driver-side validation — the prompt requires running it before exit.
  `view-output <field>` reads pending state.
- Ships in the product distribution and is baked into run images (the same wheel
  install that provides the harness). The command names deliberately do not imply
  live GitHub writes: nothing the agent runs mutates GitHub — the proposal is
  pending state the driver may apply.

### Commit message doctrine

- The commit message is rich, schema-required, and **subject to review** like any
  artifact. Doctrine: **git history is the only context surface guaranteed to every
  future Run** — fresh clones carry commits, never PRs or issues. Redundancy with
  the Decisions Section is intentional: the PR is the review-time copy, the commit
  message is the archival copy.
- Structure (prompt-enforced, review-judged): subject line, what/why, key decisions
  with rationale, dead ends tried, constraints discovered. A weak message is a
  legitimate revise finding.
- The agent still never runs `git commit`/`git push`; the driver commits with the
  proposed message and appends a provenance trailer (run id, issue, round — the
  previously generated `Run <id> for #<N> (round <r>)` message is demoted to this
  trailer). Single commit per Run in v1.
- **No fallback-generated message ever ships** — silent filler would pollute the
  durable context channel. A completed session with no valid `commit-message` is not
  committed; it takes the completion-retry lane (ADR-0016 as amended by this work).

### Schema versioning

- The proposal schema carries an integer `schema_version`. The driver stamps it into
  the job manifest; the harness asserts compatibility against the version its own
  distribution speaks **before the agent session starts** — a mismatch fails
  pre-work, classed as a pre-session infra failure (ADR-0016), with a distinct
  anchored marker so the driver never mistakes it for harness breakage. The CLI
  asserts the same compatibility at first invocation (defense in depth), and the
  proposal file records the version it was written under.
- No dispatch-eligibility coupling in v1 — add only if skew failures show up in
  evidence bundles.

### Evidence

- The raw proposal file joins every Run's evidence bundle (`proposal.json` beside
  `decisions.json`), and the boot-time sweep preserves it from orphaned job dirs.
  The Reviewer's invalid-verdict evidence preserves the offending proposal file
  where it previously preserved the offending verdict file.

## Consequences

- **Positive**: the agent's entire mutation surface is one enumerated, versioned
  schema — new output channels require a schema change, not a prompt change;
  forbidden mutations are structurally impossible rather than filtered; the
  Reviewer's in-session second chances (write-time enum and final-round
  enforcement) come free with the write path instead of a separate harness job;
  commit messages become durable context with provenance; the fill-state echo and
  `status` table give the agent deterministic completion criteria.
- **Negative**: the agent must learn a CLI instead of writing files (prompt cost per
  Run); a schema bump requires rebuilding run images in step with drivers (caught
  pre-work, but still an operational coupling); PR-body composition is now
  driver-owned, so a human hand-editing the narrative zone of an open PR will see it
  replaced on the next round that proposes a new `pr-description`.
- **Amends ADR-0013**: the job-directory channel description — `output/` now carries
  the Output Proposal (`proposal.json`) instead of a mode-specific decisions or
  verdict file.
- **Amends ADR-0014**: the decisions-payload and verdict-file contracts (including
  the validate-verdict harness job, which this ADR absorbs into write-time CLI
  enforcement plus driver re-validation) are superseded; the strict driver-side
  validation rules themselves (enum, grades, revise plan, final-round rule,
  one-strike reviewer escalation) carry forward unchanged against the proposal.
- **See ADR-0016 (as amended)** for the completion-retry class this channel
  introduces for the Implementer.

## Alternatives considered

- **Keep free-form files, validate harder**: rejected — validation cannot make
  forbidden mutations unrepresentable, and every prompt change re-negotiates the
  output surface.
- **A live GitHub-writing CLI in the container**: rejected outright — the run
  container is credential-free by construction (ADR-0013); the proposal channel
  keeps every write post-exit and driver-owned.
- **Generated fallback commit messages on a missing proposal**: rejected — silent
  filler in the one context surface every future Run inherits; the completion retry
  exists precisely so the finished worktree is not thrown away over a missing
  message.
- **Coupling dispatch eligibility to schema version**: deferred — version skew fails
  loud pre-work and is visible in evidence; the coupling buys nothing until skew
  actually shows up at fleet scale.
