Status: DRAFT

Last updated: 2026-07-16

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
- Run outcomes (ADR-0014): a completed session with commits ships normally. A session concluding that no change is needed still ships — an empty PR (one driver-synthesized allow-empty commit) whose body carries the no-change reasoning; the Reviewer judges it like any PR, and a justified no-change earns approve (the human closes the issue). A Run ending with no commits and no reasoning, or a crashed/timed-out session, is a failed Run: the driver pushes evidence, strips the claim, re-queues plan_ready, and stamps a machine-readable run-failed marker comment on the issue. One retry; a failure with a marker already present escalates blocked + needs_human.
- Claim Protocol: the Worker self-assigns and adds the in_progress label, then re-reads to verify it is the sole assignee; backs off otherwise. When the Control Node is reachable, claim requests route through it first as a race pre-filter; the GitHub assign-and-verify step still runs and remains the only authority.
- Zombie claims: the Control Node strips in_progress and the assignee from issues whose claiming Worker has been absent from Node Daemon heartbeats past a grace period, returning them to plan_ready. Optional slow GitHub Action cron as a backstop.
- Round budget: 3 review rounds per issue, tracked as attempt-N labels on the PR, incremented by the Reviewer at each revise verdict — never on claim. Round input is the Reviewer's revised plan plus its designated resume commit. Runs that produce no PR consume no rounds — they draw on the separate failed-Run budget (one retry, then escalation; see Run outcomes); Runs lost to a dead Worker are re-queued by the zombie janitor. The Control Node cross-checks attempt labels against Run and review events and flags mismatches; it does not auto-correct.
- On round-budget exhaustion the Reviewer escalates: blocked + needs_human with the evidence bundle link. The Reviewer also escalates early when a Decisions Section surfaces a call only a human may make; the human answers with a comment and re-queues the issue to plan_ready.
### Execution model

- A Worker is a long-lived **driver process** on a container-host, bound to one Agent config: a supervised Node Daemon child declared as a process-kind Stack (see [NODE-SUBSTRATE.md](http://node-substrate.md/)). It polls for plan_ready issues and executes Runs sequentially, one at a time. Workers are not containers — the long-lived-container design is retracted (ADR-0013).
- The driver is the trusted, credentialed half: it runs the Claim Protocol, materializes job inputs, sequences gate steps, creates run containers, and performs every GitHub read and write. It never executes repo code or model output.
- Each Run executes as an **ephemeral run container** created by the driver from the Agent config's image; container lifetime = Run lifetime. PID 1 is the **agent harness** — credential-free plumbing that starts the tmux session, injects the prompt, awaits the completion marker or timeout, writes outputs, and exits. Driver and harness communicate only through the per-Run job directory (inputs, outputs, transcript, status) — no network channel (ADR-0013).
- Gate steps run agent-authored code, so they execute as harness jobs on the credential-free side — never inside the credentialed driver.
- Every Run starts from a fresh clone/worktree, fresh container, and fresh context. No agent session state, build artifacts, or scratch files carry over between Runs; the only carryover is PR branch content at the Reviewer-designated resume commit, plus the Reviewer's revised plan (ADR-0008). Warm caches are named volumes mounted into run containers.
- The Reviewer has the same shape: a node-resident reviewer driver executing review rounds as ephemeral containers.
### Monitoring (Control Node)

- The Control Node (TheOzolith's control/ component) receives Node Daemon heartbeats (60s, carrying node, stack, and labeled run-container status), Run events (worker, issue ref, attempt, phase: claimed / gate / pr-open / failed / escalated), and Reviewer review events, and renders the fleet dashboard. Heartbeat responses carry infrastructure commands (drain, recycle, update, rebuild) that the Node Daemon reconciles.
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

- The Reviewer is a separate long-lived actor (own node-resident driver, own GitHub identity, configured with a stronger model than the Worker adapters; no self-grading by construction). It polls PRs labeled pr_ready without needs_human and under the round budget, and owns all post-PR state.
- Verdicts:
  - Approve: add needs_human (keeping pr_ready) plus deviation:* and risk:* labels and an evidence-citing comment; the human stamps and merges. Approve means no revisions are needed at all — approve-with-nits is forbidden; a justified no-change (empty) PR earns approve.
  - Revise: increment attempt-N on the PR, remove pr_ready, comment a clear revised plan plus a resume commit ID (or cherry-picked commits), strip the issue claim, and re-queue the issue to plan_ready under explicitly delegated authority. Revise is unavailable on the final budgeted round — a final-round revise is an invalid verdict.
  - Escalate: blocked + needs_human when a human decision is required to move forward, or when the round budget is exhausted, with the evidence bundle link.
- Verdict emission: the Reviewer's agent writes its verdict as a file (verdict.json) in its session working directory; the Reviewer driver renders the published verdict comment (human-readable plus machine block) and applies all labels under its own identity (see Agent session contract). Any invalid verdict — missing, unparseable, failing validation, or a revise on the final round — escalates immediately: blocked + needs_human with a comment carrying the raw validation error and the evidence-bundle link. There is no driver-side retry; the judging agent's second chances live inside its own session via a validate-verdict harness job (ADR-0014).
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
- One run-container image per Agent config, with the agent harness as its entrypoint. Vendor-specific flags, permissions, and session-driving details are contained in the per-image adapter; headless one-shot modes are banned by the Agent session contract.
- [AGENTS.md](http://agents.md/) is the canonical instruction file; vendor files (e.g. [CLAUDE.md](http://claude.md/)) are generated copies, never sources.
- Skills are files in the repo; the knowledge machinery (knowledge/) generates per-tool placement and format. Two scopes: global skills live in the private config repo and travel with the operator; project skills live in the target project's repo and travel with the project.
- No code-level LLM abstraction layer. The swap boundary is the process/artifact contract above.
### Agent session contract (always interactive)

- Every agent process — a Run's implementing agent and the Reviewer's judging agent — runs in interactive mode inside a dedicated tmux session; headless one-shot invocation is banned. Sessions follow a naming convention (`run-<run-id>` for Runs, `review-<pr>-round-<n>` for reviews) so the dashboard terminal can discover and attach to any live session at any time to monitor and interact.
- The **agent harness** — PID 1 of the run container, credential-free plumbing — starts the session, injects the prompt, and detects completion mechanically (a per-adapter completion hook plus a timeout backstop, never parsing of terminal output), then writes outputs and exits. The **driver** — the trusted node-resident process — commissions jobs and reads results only through the per-Run job directory (ADR-0013).
- All agent I/O is files. Inputs are materialized into the session's working directory (the repo checkout for Runs; issue, diff, Decisions Section, and mechanical signals as files for reviews). Outputs are files the driver reads: the decisions file (.theozolith/decisions.json) for Runs, the verdict file (verdict.json) for reviews. Drivers render every published artifact (PR body, verdict comment, labels) from these files; no pipeline state is ever parsed out of model prose.
- Credential isolation: agent processes hold no GitHub credentials — no tokens in their environment, no tokened remotes in their worktrees. Drivers hold the PATs and perform all GitHub reads and writes, so the transition-authority matrix is enforced by construction: a compromised or prompt-injected agent session can corrupt only its own output files, never write GitHub state.
- Human input into an attached session is permitted mid-Run and mid-review; the session transcript is captured into the evidence bundle and is the audit trail for any human steering. Residual risk: isolation protects authority, not output integrity — a bad session can still emit a bad diff or verdict; the human merge gate absorbs this.
### Upgrade path

- A new model or agent is a new adapter image. Route a fraction of plan_ready issues to the new adapter.
- Compare gate pass rate, retry count, escalation rate, and cost per merged PR from evidence bundles and GitHub data. Promote on wins.
### Deployment and substrate

The Control Node, Node Daemon, Config Repo, secrets, extension points, and the deployment boundary are specified in [NODE-SUBSTRATE.md](http://node-substrate.md/). The pipeline is one consumer of that substrate; nothing pipeline-side may depend on private deployment specifics.

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
- **Grilling 2026-07-15**: always-interactive contract — every agent process (Run implementer, Reviewer judgment) runs in an interactive, attachable tmux session, never headless one-shot; all agent I/O is files (decisions file, verdict file, materialized review inputs) with drivers rendering all published artifacts; agents hold no GitHub credentials, so the transition-authority matrix is enforced by construction; human input into attached sessions is permitted and audited via evidence-bundle transcripts. [SETTLED]
- **Grilling 2026-07-15 (late)**: execution topology — Workers-as-long-lived-containers retracted. Each actor = a trusted node-resident driver (Node Daemon child via a process-kind Stack; owns all GitHub I/O and pipeline decisions) plus a credential-free agent harness (PID 1 of an ephemeral per-Run container; interactive tmux session; job-directory file interface). Gate steps execute as harness jobs; warm caches move to named volumes. See ADR-0013. [SETTLED]
- **Grilling 2026-07-16**: verdict and run-outcome contracts — any invalid verdict (malformed, failing validation, or a revise on the final round) escalates immediately to blocked + needs_human with no driver-side retry, guarded by an in-session validate-verdict harness job; approve means no revisions needed at all; a concluded no-change Run ships as an empty PR with reasoning and is judged like any PR; a Run producing no PR is a failed Run with a one-retry budget tracked by a run-failed marker comment, then escalation; every Run that reaches a checkout pushes an evidence bundle, including failed Runs. See ADR-0014. [SETTLED]
