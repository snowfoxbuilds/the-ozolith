Status: ACCEPTED

Date: 2026-07-21

# ADR-0021: The Initializer — draft-stage worker type (specified, deferred)

## Context

Planning is the human bottleneck; the operator's goal is to make the planning experience as smooth as possible. ADR-0020 defined the worker taxonomy with the Initializer as a built-in type; this ADR specifies its contract. Binding constraints: the queue rule (each actor watches one label condition on one object type), the transition-authority matrix, GitHub as sole coordination truth, and headless Runs (ADR-0019). This decision amends the V1-planning decision in [AGENTIC-CODING-PIPELINE.md](../specs/AGENTIC-CODING-PIPELINE.md) (Grilling 2026-07-14), adding one row to the transition-authority matrix and the `initialized` label to the vocabulary; no existing actor's contract changes.

## Decision

- **Anatomy**: an Issue Worker definition in the Config Repo (amended 2026-09-03, #120 — ADR-0057 supersedes ADR-0020): Intake `requires = ["draft"]`, `excludes = ["initialized"]`; declared outputs `issue_comment` and `issue_labels`; one outcome, adding `initialized` — run by the shared node-resident credentialed driver as ephemeral headless Initializer Runs; own GitHub identity.
- **Discovery**: Control Node dispatch, claiming like every Issue Worker — the grant adds the Initializer's login and in_progress on the draft, and the driver releases on every classified ending (amended 2026-09-03, #120; the discovery-only exception and its in-memory in-flight window are retired — one claim path for every worker). The terminal initialized label remains the durable, on-GitHub dedupe.
- **Workflow**: (1) dispatch grants a draft issue lacking initialized; (2) the Run reads the issue and the repo to understand intent; (3) the driver renders **one structured analysis comment** from the Run's output file — intent restatement, feasibility, challenges, recommended path, and grilling-style questions with recommendations — **updated in place on re-runs**, never a comment pile; (4) the driver applies initialized.
- **The issue body is never edited.** It stays human-owned: the original phrasing is the record the human rules against, and a body-writing agent would let an injected analysis rewrite the instructions a later Implementer Run executes.
- **Human loop**: the human rules on the questions (from the Flight Deck or Notion), edits the draft, and applies plan_ready — which remains human authority. Removing initialized is the human re-queue for a fresh pass.
- **Transition authority**: initialized on draft issues is the Initializer's only declared output label (amended 2026-09-03, #120); removal is human-only.
- **Timing**: deferred past the current testing scope — documented now, excluded from the next build scope, and slated as an early pipeline-built feature once the core loop works (prerequisite: the ADR-0057 declarative worker model — amended 2026-09-03, #120).

## Consequences

- **Positive**: planning throughput without ceding plan_ready; the publication contract is the Reviewer's exact shape (driver renders comment + label from an output file), so no new coordination machinery; a well-bounded early dogfood candidate for the pipeline building itself.
- **Negative**: a third GitHub identity/PAT to manage; analysis quality depends on repo comprehension inside a headless Run — a bad analysis can anchor the human wrongly (mitigated: it is advisory, and the human rules on every question); the in-flight dedupe lives in Control Node memory (accepted: worst case is one overwritten duplicate comment).
- **Neutral**: the V1-planning decision narrows from "no in-pipeline planning agent" to "no automated issue generation" — the Initializer analyzes human drafts, it never authors issues; "Planner" stays reserved for a future actor that does.

## Alternatives Considered

- **Rewrite the issue body** (rejected: destroys the ruling record; opens an injection path into Implementer instructions; comment + human paste-back achieves the clean-body outcome with a human hand on the plan text).
- **Direct GitHub polling by the Initializer driver** (rejected: forks the shared base-worker fetch-execute loop; dispatch is base infrastructure per ADR-0020).
- **Claim-write-through on drafts** (rejected 2026-07-21: label churn on drafts to prevent a non-corrupting duplicate; reversed 2026-09-03, #120 — one claim path for every Issue Worker outweighs the churn).
- **Build in the current testing scope** (rejected: expands the surface under test while the core Implementer→Reviewer→merge loop is being debugged; the Flight Deck covers the workflow manually until then).

## Amendments

- **2026-09-03 (#120, ADR-0057)**: the Initializer becomes an Issue Worker definition rather than a built-in subclass — Intake `draft` without `initialized`, outputs `issue_comment` and `issue_labels`, one outcome adding `initialized` — and it claims like every Issue Worker: the discovery-only exception (no assignee, no `in_progress`, in-memory in-flight window) is retired, and the "Claim-write-through on drafts" rejection is reversed. Body-never-edited, comment-updated-in-place, human-owned `plan_ready`, and the deferred timing stand; the prerequisite moves from the ADR-0020 inheritance refactor to the declarative worker model. Reason: one claim path for every worker kind, with no per-type dispatch exception to maintain.
