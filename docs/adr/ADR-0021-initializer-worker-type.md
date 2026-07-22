Status: ACCEPTED

Date: 2026-07-21

# ADR-0021: The Initializer — draft-stage worker type (specified, deferred)

## Context

Planning is the human bottleneck; the operator's goal is to make the planning experience as smooth as possible. ADR-0020 defined the worker taxonomy with the Initializer as a built-in type; this ADR specifies its contract. Binding constraints: the queue rule (each actor watches one label condition on one object type), the transition-authority matrix, GitHub as sole coordination truth, and headless Runs (ADR-0019).

## Decision

- **Anatomy**: a standard worker (ADR-0020) — node-resident credentialed driver plus ephemeral headless Initializer Runs; own GitHub identity.
- **Discovery**: Control Node dispatch, **discovery-only — no claim write** (no assignee, no in_progress on drafts). Duplicate analysis is waste, not corruption, so drafts need no claim-write-through: the Control Node serializes grants and holds the in-flight window in memory; the terminal initialized label is the durable, on-GitHub dedupe.
- **Workflow**: (1) dispatch grants a draft issue lacking initialized; (2) the Run reads the issue and the repo to understand intent; (3) the driver renders **one structured analysis comment** from the Run's output file — intent restatement, feasibility, challenges, recommended path, and grilling-style questions with recommendations — **updated in place on re-runs**, never a comment pile; (4) the driver applies initialized.
- **The issue body is never edited.** It stays human-owned: the original phrasing is the record the human rules against, and a body-writing agent would let an injected analysis rewrite the instructions a later Implementer Run executes.
- **Human loop**: the human rules on the questions (from the Flight Deck or Notion), edits the draft, and applies plan_ready — which remains human authority. Removing initialized is the human re-queue for a fresh pass.
- **Transition authority**: initialized on draft issues is the Initializer's only label write; removal is human-only.
- **Timing**: deferred past the current testing scope — documented now, excluded from the next build scope, and slated as an early pipeline-built feature once the core loop works (prerequisite issue: the ADR-0020 base-worker inheritance refactor).
## Consequences

- **Positive**: planning throughput without ceding plan_ready; the publication contract is the Reviewer's exact shape (driver renders comment + label from an output file), so no new coordination machinery; a well-bounded early dogfood candidate for the pipeline building itself.
- **Negative**: a third GitHub identity/PAT to manage; analysis quality depends on repo comprehension inside a headless Run — a bad analysis can anchor the human wrongly (mitigated: it is advisory, and the human rules on every question); the in-flight dedupe lives in Control Node memory (accepted: worst case is one overwritten duplicate comment).
- **Neutral**: the V1-planning decision narrows from "no in-pipeline planning agent" to "no automated issue generation" — the Initializer analyzes human drafts, it never authors issues; "Planner" stays reserved for a future actor that does.
## Alternatives Considered

- **Rewrite the issue body** (rejected: destroys the ruling record; opens an injection path into Implementer instructions; comment + human paste-back achieves the clean-body outcome with a human hand on the plan text).
- **Direct GitHub polling by the Initializer driver** (rejected: forks the shared base-worker fetch-execute loop; dispatch is base infrastructure per ADR-0020).
- **Claim-write-through on drafts** (rejected: label churn on drafts to prevent a non-corrupting duplicate).
- **Build in the current testing scope** (rejected: expands the surface under test while the core Implementer→Reviewer→merge loop is being debugged; the Flight Deck covers the workflow manually until then).
## Amends

- The V1-planning decision in [AGENTIC-CODING-PIPELINE.md](http://agentic-coding-pipeline.md/) (Grilling 2026-07-14) as described above. Adds one row to the transition-authority matrix and the initialized label to the vocabulary. No existing actor's contract changes.
