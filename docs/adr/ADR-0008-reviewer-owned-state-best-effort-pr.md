Status: ACCEPTED

Date: 2026-07-14

# ADR-0008: Reviewer-owned state and the best-effort PR contract

## Context

The original pipeline parked Runs mid-execution on judgment-call findings (WIP branch + draft PR + Handoff Doc + blocked), deferred resolution to a future Advisor, and tracked retries as attempt-N labels incremented by the Worker on claim. Three structural problems surfaced while writing the V1 build briefs: infrastructure failures were charged against a budget meant to measure agent capability; increment-on-claim contradicted "parked Runs consume no retries" and double-charged crashes via the zombie-janitor path; and resume scheduling had no poll target that respected the one-label-per-actor queue rule. Since nothing merges without a human, parking was an efficiency mechanism, not a safety one.

## Decision

- **Best-effort PR contract**: every Run that reaches a checkout ends by pushing what it has and opening/updating the PR with a mandatory **Decisions Section** (decisions made with rationale, open questions, remaining work, dead ends tried — the former Handoff Doc schema, inlined). The Worker never stops mid-Run to ask; judgment calls are made, recorded, and adjudicated afterward.
- **Reviewer actor**: a separate long-lived actor (own container, own GitHub identity, configured with a stronger model than the Worker adapters) polls PRs labeled pr_ready without needs_human and owns all post-PR state. The Reviewer holds the decision-making power in the loop; Workers execute plans.
- **Verdicts**: approve (add needs_human, deviation:* and risk:*, evidence-citing comment → human stamps); revise (increment attempt-N on the PR, remove pr_ready, comment a revised plan plus a resume commit ID or cherry-picked commits, strip the issue claim, re-queue the issue to plan_ready under explicitly delegated authority); escalate (blocked + needs_human when a human decision is required or the round budget is exhausted, with the evidence bundle link).
- **Label semantics (PR)**: pr_ready alone = ready for the automated Reviewer; pr_ready + needs_human = awaiting human review/stamp; blocked + needs_human = awaiting a human decision.
- **Same PR, all rounds**: retries reuse one PR and one branch — never a duplicate PR. The next Run (any Worker) checks out the Reviewer's resume commit and executes the revised plan from fresh context.
- **Round budget**: 3 review rounds per issue, tracked as attempt-N on the PR at each revise verdict. Runs that produce no PR (crash, infra failure) consume no rounds; the zombie janitor re-queues them.
- **Advisor and Speculative Runs are removed** from the design entirely.
- **The gate is first-party and Worker-side** (no-mistakes concepts, no no-mistakes dependency): test → docs → lint → push → PR → CI, structured findings, safe mechanical auto-fixes. Adversarial review is not a gate step; the gate never blocks PR creation.
## Consequences

- **Positive**: the park/resume machinery is deleted; the park-vs-fail classification burden on the Worker dissolves into "PR produced or not"; single writer per object per phase (Worker owns pre-PR issue state, Reviewer owns everything post-PR); the queue rule holds (Workers poll plan_ready issues, Reviewer polls pr_ready PRs, human polls needs_human); the stronger model plans retries instead of the Worker re-deriving intent from objections; no-self-grading is enforced by construction (separate process and identity, not a prompt boundary); Reviewer downtime degrades throughput but never correctness — all state lives on GitHub.
- **Negative**: a wrong early decision costs a full Run plus a review round instead of one parked question; complete implementations anchor both Reviewer and human (accepted for single-operator V1 — the human merge gate is the backstop); a second long-lived component and machine-user PAT ship in V1; the Reviewer's plan_ready re-queue is an agent exercising explicitly delegated human authority and must be recorded as such.
- **Neutral**: the Handoff Doc schema survives as the Decisions Section (ADR-0003 amended); the resume-from-WIP carryover exception generalizes to the Reviewer-designated resume commit on the shared PR branch.
## Alternatives Considered

- **Keep park/resume, add an aborted class and on-fail attempt accounting**: rejected — fixes the accounting but keeps the mid-Run classification burden on the Worker and adds resume-scheduling complexity the queue rule cannot express.
- **Worker polls its own blocked draft PRs for resume**: rejected — a second poll target per actor, dies on Worker recycle, and breaks the cross-agent resume that ADR-0003 requires.
- **Advisor as finding resolver**: rejected — replaced by post-hoc review; the Reviewer adjudicates recorded decisions with the full artifact in view instead of answering questions mid-flight with less context.
