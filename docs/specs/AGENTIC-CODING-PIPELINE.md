Status: DRAFT

Last updated: 2026-07-14

# Agentic Coding Pipeline

Agent-agnostic framework for staged autonomous development: plan in GitHub issues, implement via Workers executing disposable Runs that always ship a best-effort PR, review by a dedicated Reviewer actor, merge by a human.

## Context

Models, agents, and skills improve constantly; the pipeline must allow swapping any coding agent without migration. Defect-catching lives in tests, CI, and a push gate — not in any single agent's judgment.

## Design

### Core principle

Runs are stateless, disposable executions; Workers are long-lived but hold no authoritative state. Everything durable is a file or a GitHub object. No agent memory, vendor config format, or vendor-exclusive feature is load-bearing. Canonical terms are defined in [CONTEXT.md](http://context.md/).

### Stages

| Stage | Actor | Staging ground | Output |
| --- | --- | --- | --- |
| Planning | Human-led, agent-assisted | GitHub issues | plan_ready issue with acceptance criteria and baseline risk |
| Implementation | Worker (one Run per round) | Branch + first-party Worker-side gate | Best-effort PR with a Decisions Section |
| Review | Reviewer actor + CI + human | PR | Approved (needs_human), revised (next round), or escalated PR |

- Notion anchor documents ([AGENTS.md](http://agents.md/), [CONTEXT.md](http://context.md/), Specs, ADRs) govern the project and sync one-way into the repo (ADR-0001). Anchor documents do not govern individual changes.
- All per-change planning lives in GitHub issues. Applying plan_ready is the act of issue approval and asserts the two hard artifacts: acceptance criteria and a baseline risk label. Other template fields (objective, out of scope, pointers) are issue-form prompts, never enforced — the human is the lint.
- V1 planning: the human writes issues through GitHub issue forms, optionally drafting with any agent out-of-band. No in-pipeline planning agent and no automated issue generation in V1. A future planning Worker enters through the same gate: agent-drafted issues land as draft, and plan_ready is applied by the human — or by the agent itself where the human has explicitly granted that permission.
- Integration (merge sequencing across parallel PRs) is handled by the human merge gate; parallel PRs are serialized via issue dependencies.
### Issue and PR lifecycle (labels)

```javascript
Issue: draft -> plan_ready -> in_progress -> (revise verdict: back to plan_ready) -> closed | abandoned
PR:    pr_ready               -> ready for the automated Reviewer (under round budget)
       pr_ready + needs_human -> awaiting human review/stamp
       blocked  + needs_human -> awaiting a human decision to move forward
       -> closed (merged) | abandoned
```

- plan_ready is the only claimable state — the unambiguous name for "agents may start".
- Queue rule: each actor polls one label on one object type. Workers poll issues (plan_ready); the Reviewer polls PRs (pr_ready without needs_human, under the round budget); the human polls PRs (needs_human).
- Transition authority: the human applies initial plan_ready, answers blocked decisions (comment + re-queue), and closes issues. The Worker applies in_progress + assignee at claim and pr_ready at PR push. The Reviewer owns all post-PR state — deviation:*, risk:*, attempt-N, blocked, needs_human — and, under explicitly delegated authority, the claim strip + plan_ready re-queue that starts the next round. No other transitions exist.
- Best-effort PR contract: every Run that reaches a checkout ends by pushing what it has and opening/updating the PR with a mandatory Decisions Section (decisions with rationale, open questions, remaining work, dead ends tried), then applies pr_ready. The Worker never stops mid-Run to ask; judgment calls are made, recorded, and adjudicated by the Reviewer afterward (ADR-0008). The hyphenated needs-human label is retired in favor of needs_human.
- Claim Protocol: the Worker self-assigns and adds the in_progress label, then re-reads to verify it is the sole assignee; backs off otherwise. When the Control Node is reachable, claim requests route through it first as a race pre-filter; the GitHub assign-and-verify step still runs and remains the only authority.
- Zombie claims: the Control Node strips in_progress and the assignee from issues whose Worker has missed heartbeats past a grace period, returning them to plan_ready. Optional slow GitHub Action cron as a backstop.
- Round budget: 3 review rounds per issue, tracked as attempt-N labels on the PR, incremented by the Reviewer at each revise verdict — never on claim. Round input is the Reviewer's revised plan plus its designated resume commit. Runs that produce no PR (crash, infra failure) consume no rounds; the zombie janitor re-queues them. The Control Node cross-checks attempt labels against Run and review events and flags mismatches; it does not auto-correct.
- On round-budget exhaustion the Reviewer escalates: blocked + needs_human with the evidence bundle link. The Reviewer also escalates early when a Decisions Section surfaces a call only a human may make; the human answers with a comment and re-queues the issue to plan_ready.
### Execution model

- A Worker is a long-lived container bound to one Agent config; it polls for plan_ready issues and executes Runs sequentially, one at a time. Long-lived Workers keep warm caches and suit local-server resource budgets.
- Every Run starts from a fresh clone/worktree and fresh context. No agent session state, build artifacts, or scratch files carry over between Runs; the only carryover is PR branch content at the Reviewer-designated resume commit, plus the Reviewer's revised plan (ADR-0008).
- Workers are recycled on a schedule (every N Runs or on resource thresholds) so long-lived never becomes immortal.
### Monitoring (Control Node)

- The Control Node (TheOzolith's control/ component) receives Node Agent heartbeats (60s, carrying node and stack status), Run events (worker, issue ref, attempt, phase: claimed / gate / pr-open / failed / escalated), and Reviewer review events, and renders the fleet dashboard. Heartbeat responses carry infrastructure commands (drain, recycle, update, rebuild) that the Node Agent reconciles.
- Infrastructure commands are not coordination: the Control Node is authoritative for node and docker lifecycle but never writes claim state; GitHub owns issue coordination (ADR-0002).
- Coordination invariant: a coordination action is valid only once committed to GitHub. The Control Node is advisory — claim pre-filter, zombie-claim janitor, retry auditor — and is never required for correctness (ADR-0002).
- Control Node down = degraded mode: no dashboard, slower zombie cleanup, slightly higher duplicate-claim odds. Workers keep claiming and shipping via GitHub alone.
### Quality gate

A first-party quality gate (in worker/) fronts every push: disposable worktree, test → docs → lint → push → PR → CI. It borrows the no-mistakes concepts but has no dependency on the no-mistakes binary (ADR-0008).

- Safe mechanical fixes are auto-applied; structured findings are recorded.
- The gate never blocks PR creation: unresolvable findings are recorded in the Decisions Section of the best-effort PR.
- Adversarial review is not a gate step — it belongs to the Reviewer actor (see Review loop).
- The gate + Reviewer + CI + human triage replace a separate pull-side review-agent fleet.
- Evidence bundles give per-run traceability and per-agent metrics.
### Review loop

- The Reviewer is a separate long-lived actor (own container, own GitHub identity, configured with a stronger model than the Worker adapters; no self-grading by construction). It polls PRs labeled pr_ready without needs_human and under the round budget, and owns all post-PR state.
- Verdicts:
  - Approve: add needs_human (keeping pr_ready) plus deviation:* and risk:* labels and an evidence-citing comment; the human stamps and merges.
  - Revise: increment attempt-N on the PR, remove pr_ready, comment a clear revised plan plus a resume commit ID (or cherry-picked commits), strip the issue claim, and re-queue the issue to plan_ready under explicitly delegated authority.
  - Escalate: blocked + needs_human when a human decision is required to move forward, or when the round budget is exhausted, with the evidence bundle link.
- Retries reuse the same PR and branch — never a duplicate PR. The next Run (any Worker) claims the re-queued issue, detects the existing PR, checks out the Reviewer's resume commit, and executes the revised plan from fresh context. The Reviewer, as the stronger model, holds the decision-making power in this loop; Workers execute plans.
- For blocked + needs_human PRs, the human comments the decision and re-queues the issue to plan_ready (human authority).
- Reviewer downtime degrades throughput, never correctness: PRs queue at pr_ready; all state lives on GitHub, so a restarted Reviewer picks up cleanly.
- Review quality is measured per Agent config from evidence bundles (rounds per merged PR, re-litigated decisions, repeated dead ends).
### Human-in-the-loop lanes

| Lane | Where | Purpose |
| --- | --- | --- |
| Planning | Issue approval before plan_ready | Spec conformance, acceptance criteria quality |
| Review | Merge gate, findings queue | Risk-budgeted final review |
| Ad-hoc | Dev docker with agent CLI and full access | Non-decomposable work: cross-cutting refactors, design-in-flux |

### Two-layer risk assessment

- Layer 1 — baseline, at planning: a risk:low|medium|high label applied together with plan_ready, judged on sensitive paths (auth, data model, migrations, public API), blast radius, and reversibility. The planning agent proposes; the human confirms.
- Layer 2 — review, at pr_ready: the Reviewer actor emits two PR labels plus a comment citing evidence. deviation:low|medium|high grades divergence from the plan (files outside the plan's footprint, unrequested behavior changes, new dependencies, size far beyond expectation). risk:low|medium|high is the Reviewer's own overall risk read of the change as implemented, independent of the baseline.
- The Reviewer — a separate actor, never the Worker adapter — owns both grades, so the judgment is adversarial rather than self-assessed by the implementing side. Mechanical signals (diff size, files touched, dependency changes, sensitive paths) are computed and fed to the Reviewer as evidence; they inform the grades but are not an independent grader. The Decisions Section is a first-class review input: the Reviewer judges the decisions the Worker made, not just the diff.
- The human combines the three labels (baseline risk on the issue; reviewer risk and deviation on the PR) to budget review depth (all low = skim; high on any axis = line-by-line). The final merge judgment is never automated.
### Agent swap boundary

- Contract: input = issue ref + repo checkout + repo-resident instructions ([AGENTS.md](http://agents.md/), [CONTEXT.md](http://context.md/), specs, skills). Output = branch pushed through the gate + evidence bundle. Everything between is a black box.
- One Worker image per Agent config with a common poll-claim-run entrypoint. Vendor-specific flags, permissions, and headless modes are contained in the per-image adapter.
- [AGENTS.md](http://agents.md/) is the canonical instruction file; vendor files (e.g. [CLAUDE.md](http://claude.md/)) are generated copies, never sources.
- Skills are files in the repo; the knowledge machinery (knowledge/) generates per-tool placement and format. Two scopes: global skills live in the private config repo and travel with the operator; project skills live in the target project's repo and travel with the project.
- No code-level LLM abstraction layer. The swap boundary is the process/artifact contract above.
### Upgrade path

- A new model or agent is a new adapter image. Route a fraction of plan_ready issues to the new adapter.
- Compare gate pass rate, retry count, escalation rate, and cost per merged PR from evidence bundles and GitHub data. Promote on wins.
### Deployment and substrate

The Control Node, Node Agent, Config Repo, secrets, extension points, and the deployment boundary are specified in [NODE-SUBSTRATE.md](http://node-substrate.md/). The pipeline is one consumer of that substrate; nothing pipeline-side may depend on private deployment specifics.

## Decisions

- **Three stages with GitHub issues and PRs as staging grounds**: durable, agent-agnostic, human-legible substrate with free tooling. [SETTLED]
- **Agents stateless and disposable; artifacts own all state**: enables agent swap with zero migration. [SETTLED]
- **no-mistakes as push-side gate**: collapses review into the implementation side; rejected a separate pull-side review-agent fleet as redundant and failure-correlated. [SUPERSEDED 2026-07-14: the gate is first-party (no-mistakes concepts only, no binary dependency) and adversarial review moved to the Reviewer actor. See ADR-0008.]
- **Acceptance criteria authored at planning time, in the issue**: reviewer judges against pre-existing criteria, not tests written by the implementing agent in the same run. [SETTLED]
- **3 review rounds then human escalation with evidence bundle**: attempt-N lives on the PR and is incremented by the Reviewer at each revise verdict, never on claim; no-PR Runs consume no rounds. [SETTLED, amended 2026-07-14 — see ADR-0008]
- **Rejected code-level LLM abstraction layer**: couples to every vendor's API churn; process-level boundary ages better. [SETTLED]
- **Anchor docs govern the project, issues govern changes**: no dual source of truth for plans. [SETTLED]
- **Grilling 2026-07-13**: terminology → Orchestrator = whole system; Worker = long-lived container bound to one Agent config; Run = stateless disposable attempt. Statelessness attaches to Runs, not Workers. [SETTLED]
- **Grilling 2026-07-13**: coordination → GitHub is the sole coordination source of truth; Control Node is advisory (claim pre-filter, zombie-claim janitor, retry auditor). See ADR-0002. [SETTLED]
- **Rejected standalone Dispatcher service**: coordination dissolves into the Claim Protocol on GitHub state. [SETTLED]
- **Grilling 2026-07-13**: park = WIP branch + draft PR + Handoff Doc; resume from the branch with the decision injected. [SUPERSEDED 2026-07-14: no mid-Run parking — best-effort PR contract; the carryover exception generalizes to the Reviewer-designated resume commit. See ADR-0008.]
- **Grilling 2026-07-13**: Handoff Doc over vendor session restore as the resume contract. See ADR-0003. [SETTLED, amended 2026-07-14: the schema survives as the mandatory Decisions Section on every best-effort PR; no separate artifact. See ADR-0008.]
- **Rejected Worker self-unblock**: replaced by Speculative Runs — the finding stays open; contingent work only. [SUPERSEDED 2026-07-14: the best-effort PR contract is universal decide-and-flag with the Reviewer as backstop; Speculative Runs are removed. See ADR-0008.]
- **Infrastructure commands are not coordination**: the Control Node is authoritative for node/docker lifecycle, never for claims. [SETTLED]
- **2026-07-14 spec split**: substrate decisions moved to [NODE-SUBSTRATE.md](http://node-substrate.md/). [SETTLED]
- **Grilling 2026-07-13**: label vocabulary — plan_ready is the sole claimable state; issues and PRs carry separate label sets; each actor polls exactly one label on one object type. [SETTLED, amended 2026-07-14: pr_ready = ready for the automated Reviewer; pr_ready + needs_human = awaiting human stamp; blocked + needs_human = awaiting a human decision. See ADR-0008.]
- **Grilling 2026-07-13**: two-layer risk assessment — baseline risk label at plan_ready, reviewer signals at pr_ready; the human combines them to budget review depth. [SETTLED]
- **Grilling 2026-07-14**: the adversarial Reviewer emits both review signals — deviation:* and its own overall risk:* on the PR — keeping grading adversarial, never self-assessed. Mechanical diff signals feed the Reviewer as evidence rather than acting as a separate grader. [SETTLED, amended: the Reviewer is a separate long-lived actor and applies all post-PR labels itself. See ADR-0008.]
- **Rejected mandatory issue-template enforcement**: template fields are prompts; the two machine-consumed artifacts (acceptance criteria, baseline risk label) are asserted by the act of applying plan_ready. [SETTLED]
- **Grilling 2026-07-14**: Advisor deferred past V1. [SUPERSEDED 2026-07-14: Advisor removed entirely — the Reviewer's revised-plan loop plus human decision comments replace it. See ADR-0008.]
- **Grilling 2026-07-14**: V1 planning = human-authored issues via GitHub issue forms; no in-pipeline planning agent. plan_ready authority is human by default and delegable to an agent only by explicit human permission. [SETTLED]
- **Grilling 2026-07-14 (late)**: review-loop redesign — the Worker always ships a best-effort PR with a mandatory Decisions Section (no mid-Run parking); a separate long-lived Reviewer actor (stronger model) owns all post-PR state and drives revise rounds via a revised plan plus a designated resume commit on the same PR; 3 review rounds tracked as attempt-N on the PR; the gate is first-party. See ADR-0008. [SETTLED]
