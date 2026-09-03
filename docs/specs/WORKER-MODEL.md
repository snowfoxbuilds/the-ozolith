Status: DRAFT

Last updated: 2026-09-03

# Worker Model

Declarative worker types: every automated pipeline actor is a Worker-Type Definition of one kind — Issue Worker or PR Worker — whose intake, outputs, prompt, and round budget are Config Repo data, and whose label writes are the wiring between worker types.

## Context

Until 2026-09-03 the pipeline's actors were built-in classes — Implementer, Reviewer, Initializer — each hardcoding which labels it polled, which labels it wrote, and the prompt it ran; adding a worker (a tester that works completed PRs, a second Reviewer with a lighter pass) meant a subclass and a code release. Routing the pipeline by Baseline Risk was the change that made the pattern untenable (grilling 2026-09-03, #120): every new rule was another hardcoded label check. This page defines the model the pipeline is now a configuration of; [AGENTIC-CODING-PIPELINE.md](AGENTIC-CODING-PIPELINE.md) carries the shipped default definitions.

## Design

### Kinds

- An **Issue Worker** (`on = "issue"`) starts a Run from an issue its Intake matches; a **PR Worker** (`on = "pr"`) starts a Run from a PR its Intake matches. The matched object is the Run's trigger object. The two kinds share every other mechanism on this page; Implementer, Reviewer, and Initializer are shipped default definitions, not subclasses.
- One issue has exactly one PR — the `ozolith/issue-N` branch — and that link is the only PR → issue resolution: a PR Worker's issue is the branch's N, never a parsed body line. A PR matching an Intake whose head is not `ozolith/issue-N` is a malformed state (surfaced, never granted, never silently skipped).
- Intake reads the trigger object's labels only — never the linked object's. What must influence PR-side routing is mirrored onto the PR by an Issue Worker's declared mirror rule (below).

### Worker-Type Definition fields

The routing and behavior fields, added to the identity and image fields specified in [NODE-SUBSTRATE.md](NODE-SUBSTRATE.md). Unknown keys on a definition are refused at ingest, as on Stacks.

| Field | Kind | Meaning |
| --- | --- | --- |
| `on` | both | `"issue"` or `"pr"`; required with a driver |
| `prompt` | both | `prompts/<name>.md` — the operator's instruction template (see Prompt) |
| `[intake] requires` | both | labels that must all be present; non-empty |
| `[intake] excludes` | both | labels none of which may be present |
| `[intake] one_of` | both | list of label groups; exactly one member of each group must be present — zero or two is malformed |
| `[intake] consumes` | both | labels the Control Node removes when it writes the claim |
| `[output] fields` | both | allowlist from `issue_body`, `issue_comment`, `issue_labels`, `pr_title`, `pr_body`, `pr_contents`, `pr_labels`, `pr_comment`, `pr_resume_point` |
| `[output.outcome]` | both | the Outcome Table, at least one entry (see Outputs) |
| `[output.pr_labels]`, `[output.issue_labels]` | both | named label groups: `one_of = [...]`, optional `required_for = [<outcome>, ...]` |
| `[output.mirror] issue_to_pr` | issue | exact-name map applied by the driver at PR creation and update, e.g. `"risk:low" = "baseline:low"` |
| `rounds` | pr | the loop budget; required on every PR Worker |
| `[chain_on]` | issue | `requires`/`excludes` on the blocker PR's labels that grant the Chained Base go-ahead; absent = no chaining |

Label names everywhere are exact GitHub label names — no globs, no prefixes. Ingest validates: `requires` non-empty; no Core Label (below) in any intake or output; every non-looping outcome, applied together with `consumes`, leaves the trigger object outside the definition's own Intake; every outcome's `requires` names declared fields; `required_for` names declared outcomes; every placeholder in the prompt is in the fixed set.

### Intake and dispatch

- Intake is evaluated by the Control Node at grant time only, from the Pinned Build's definition of the requesting Stack (the dispatch request already names the Stack; no wire change). A label change after the grant never aborts a Run — the grant is claim authority.
- Product-fixed checks run alongside every Intake and no definition can remove them: the object is not spoken for (`in_progress` on the pair's issue); neither object of the pair carries `failed`; for Issue Workers every Dependency Edge is satisfied (closed-as-completed blocker or the `chain_on` go-ahead); node gates (pin convergence, config-distribution hash, quarantine, pending lifecycle commands); oldest-first ordering by creation.
- Two advisory lanes, rebuilt every dispatch pass against every worker Stack bound to the repo, self-clearing, shown in the flags API, the web Needs-attention list, and `theozolith flags`, never written to GitHub: **malformed** (a `one_of` group with zero or two members, a `pr`-kind match on a non-pipeline branch, dependency cycles and cross-repo edges as before) and **unrouted** (passes every product-fixed check, matches no bound worker Stack's Intake; issues with a human assignee omitted — a human has it).

### Claims

- Both kinds claim the issue:PR set, so exactly one worker of either kind works a pair at a time. The Control Node is the single serialized claim writer: it adds the worker's GitHub login as an assignee and `in_progress` on the pair's issue and removes the definition's `consumes` labels from the trigger object, atomically, then answers the grant — every 200 echoing the verified (repo, stack) as before.
- Spoken for means `in_progress`. Human assignees never block a claim; the worker's login is added alongside them, and release removes only the worker's own login plus `in_progress`.
- Every worker releases its own claim on every classified ending — success, failure, empty result. A grant with no claimed event inside the activation window is released by the Control Node, which restores the consumed labels. The claim leaves a claim-scoped authorization lease, renewed from GitHub truth while the repo stays bound, for both kinds alike.
- Taking work away from the pipeline is done with labels (remove the intake label, add an exclusion), never by assignment.

### Outputs and the Outcome Table

- A Run's only write channel is its Output Proposal, whose schema the driver derives from the definition's declared fields and stamps into the job manifest. Text fields (`issue_body`, `pr_title`, `pr_body`, `pr_comment`) are pre-populated with the current agent-owned zone: edit = overwrite, absent = unchanged. Driver-owned zones — the Closes line, the Based-on zone, the Resume-at zone, and the Decisions Section framing — sit outside every field and are never agent-writable. `pr_contents` is the worktree; declaring it makes the rich commit message and the Decisions Section mandatory.
- The **Outcome Table** is the definition's enumerated endings. The agent picks exactly one (required); the definition maps it to label writes on the PR and the issue, to companion fields it `requires`, and to whether it `loops`. A group with `required_for = ["approve"]` must be filled when that outcome is chosen. Core Labels can appear in no group and no outcome. Example (a tester that works PRs the Implementer tagged `needs_test`):

```toml
on = "pr"
prompt = "prompts/tester.md"
rounds = 2

[intake]
requires = ["needs_test"]
excludes = ["needs_human"]
consumes = ["needs_test"]

[output]
fields = ["pr_labels", "pr_comment", "issue_labels"]

[output.outcome]
passed = { pr_labels = ["tested"] }
failed_tests = { issue_labels = ["plan_ready"], requires = ["pr_comment"], loops = true }
```

- `pr_resume_point` (a commit, or cherry-picked commits, the next Issue Worker Run checks out) is a typed output the driver stores in the driver-owned Resume-at PR-body zone, overwritten each loop — never a comment machine block, and never parsed from prose.
- Output labels are applied only when the Run succeeds — post-exit, after driver validation, together with the outcome, in one labels write per object. A Run that ends without a valid outcome writes none of them (see Failure lanes).

### Prompt and Contract Appendix

- The instruction prompt is a Config Repo file, `prompts/<name>.md`, referenced by the definition, content-hashed and pinned at ingest, carried to nodes in the Config Distribution, and rendered per Run by the driver — never baked into the derived image, yet identity-bearing: its content hash joins the definition's instruction hash, so a prompt edit re-tags the derived image (layers cached, bytes unchanged) and benchmark-to-deployment equivalence keeps meaning "same behavior" ([BENCH-CONTRACT.md](BENCH-CONTRACT.md)).
- Placeholders are a fixed set validated at ingest — `{issue.number}`, `{issue.title}`, `{issue.body}`, `{pr.number}`, `{pr.title}`, `{pr.head}`, `{pr.base}`, `{round}`, `{rounds}`, `{loop.comment}` (the looping outcome's `pr_comment` from the previous round) — and an unknown placeholder refuses the definition. A prompt that hard-codes read-surface paths (`input/…`) is refused too: the read surface is the product's.
- The product appends the **Contract Appendix** to every rendered prompt, and no definition can remove it: the guide to the Run's read surface (the Context Tree today; whatever the product ships later), format-output usage for exactly the declared fields and outcomes, and the round rule for PR Workers. The operator writes instructions; the product writes the contract.

### Rounds and loop safety

- `attempt-N` on the PR counts loops, not Runs: it increments only when a PR Worker takes a `loops = true` outcome, is shared per PR across every PR Worker, and is compared against each definition's `rounds`. A PR Worker whose outcomes never loop never touches it.
- On the final budgeted round a looping outcome is refused at write time in-session and again by the driver post-exit — the final-round rule of the shipped Reviewer, generalized. When `attempt-N` already equals a definition's `rounds`, the product escalates without a Run: `blocked` + `needs_human` on the PR, a decision owed by a human.
- Ingest refuses a definition that could loop forever without a human: a non-looping outcome must leave the trigger object outside the definition's own Intake once `consumes` is applied.

### Failure lanes

- Retry classes are kind behavior. Issue Workers keep the local retry (crash, timeout, zero commits) and the completion retry (a clean session whose proposal fails validation) because their work product survives the session; PR Workers get neither — the judgment died with the session.
- Every ending without a valid outcome — retries exhausted, or a PR Worker's first miss — lands in one lane: `failed` + `needs_human` on the trigger object, with the evidence-bundle link and the raw validation error, and the claim released. `failed` is a product-fixed dispatch block for every worker on either object of the pair; only a human removes it, and the consumed intake label stays consumed, so the human re-queue is "remove `failed`, re-add the intake label".

### Core Labels and product-written vocabulary

- **Core Labels** are written only by fixed mechanics and can appear in no Intake, group, or outcome: `in_progress` (Control at claim, driver at release), `failed` (failure lane), `attempt-N` (loop counter). Every other label — `plan_ready`, `pr_ready`, `needs_human`, `blocked`, `draft`, `initialized`, `risk:*`, `deviation:*`, `baseline:*`, operator labels — is vocabulary that definitions wire, touchable only through a declared group or outcome.
- Fixed lanes still write vocabulary: the failure lane adds `needs_human`; round exhaustion and the janitor's base-drift lane write `blocked` + `needs_human`; the zombie janitor writes `failed` + `needs_human`. No definition can suppress these.

### Dependencies: `chain_on`

An Issue Worker's `[chain_on]` names the blocker-PR label state that grants the Chained Base go-ahead — the same `requires`/`excludes` grammar, evaluated on each open blocker's PR. Absent, dependents wait for full merge. The shipped Implementer declares `requires = ["pr_ready", "needs_human"], excludes = ["blocked"]` — the state its shipped Reviewer's approve outcome produces. Every other Chained Base mechanic (single chain by base_ref, repo-settings preconditions, Based-on zone, retarget and ship-time reconciliation) is unchanged.

### Label bootstrap

`theozolith-bootstrap` derives a repository's label set from the Pinned Build: the Core Labels plus every label named in any bound definition's Intake or outputs, colors and descriptions from an optional Config Repo `[labels]` table, product defaults otherwise. It creates missing labels, corrects drifted color or description, and never deletes a label it did not declare. `--check` reports drift.

## Relevant ADRs

| ADR | Decision |
| --- | --- |
| ADR-0013-node-resident-drivers-per-run-containers | Actors split into node-resident drivers and credential-free agent harnesses; Runs execute in ephemeral, per-Run containers |
| ADR-0016-failure-handling | Failure handling: local retry budget, node quarantine, progress telemetry, and evidence-first zombie escalation |
| ADR-0017-control-node-claim-dispatch | The Control Node is the single writer of claim creation on GitHub (write-through dispatch); GitHub stays the source of truth |
| ADR-0042-custom-driver-code-in-config-repo | Operator code and data ride the Config Distribution, hash-pinned |
| ADR-0044-worker-type-def-owns-customization | The Worker-Type Definition is the complete customization unit for one worker |
| ADR-0045-model-effort-baked-into-derived-image | Model and reasoning effort are typed definition fields baked at build, never selected at run time |
| ADR-0046-output-proposal-channel | The Output Proposal is the agent's sole mutation surface; the driver validates and applies it post-exit |
| ADR-0048-config-ingestion-pinned-build | `theozolith config ingest` is the only path from the Config Repo to the Pinned Build control loads |
| ADR-0053-dependency-edges-and-chained-base-runs | Dependency Edges are the machine-readable ordering truth; dependency-aware dispatch and Chained-Base Runs |
| ADR-0054-candidate-bundle-bench-contract | Worker types export as Candidate Bundles and execute under the published bench contract |
| ADR-0056-multi-repo-coordination | The Claim Protocol is keyed by repository; one Control Node coordinates every Bound Workspace |
| ADR-0057-declarative-worker-types | Worker types are declarations — kind, intake, outputs, prompt — not subclasses; the pipeline's actors are shipped default definitions |
