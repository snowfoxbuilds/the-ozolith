# TheOzolith — Domain Glossary

Canonical terms for this project. Coding agents and specs use these terms exactly. Updated during grilling sessions.

## Terms

**Agent**

The entire configuration for one coding tool — Claude, Codex, or Pi. Tool-scoped: one agent config per tool.

*Avoid*: confusing with "Claude agent" (a single subagent file, a much smaller unit).

**Claim Protocol**

How a Worker takes exclusive ownership of a plan_ready issue: self-assign plus the in_progress label on GitHub, then re-read to verify sole assignee (back off otherwise). Claims may route through the Control Node as a race pre-filter when it is reachable; the GitHub assign-and-verify step remains the only authority.

*Avoid*: treating the Control Node as claim arbiter.

**Claude agent**

A single `.md` subagent file used by Claude. One component inside the Claude agent config.

*Avoid*: "agent" (which means an entire tool config), "skill".

**Config Repo**

The single source of truth for one deployment's customizations: Stack definitions, worker types (base image + setup instructions + optional Knowledge Source), compose overlays, and secret names. A git-backed folder (default ~/.theozolith/configs) whose working home is the Control Node; the web UI is an editor that commits to it. Never contains secret values.

*Avoid*: treating the web UI as a separate authority; per-node config dirs.

**Container-Host**

The node type a physical machine becomes when the Node Agent is installed on it: it runs Stacks as containers under desired-state control and builds derived images locally when instructed. The daemon runs on the host; only workloads are containerized.

*Avoid*: containerizing the Node Agent; "builder node" (removed — every container-host builds its own images).

**Control Node**

The product's central service, shipped in TheOzolith's control/ component (deployed on the Pi). Renders the fleet dashboard; receives Node Agent heartbeats and Run events; answers heartbeats with infrastructure commands (drain, recycle, update); sweeps zombie claims and audits retry counts. Authoritative for node and docker lifecycle; advisory only in issue coordination — the pipeline ships PRs without it.

*Avoid*: "dispatcher"; treating it as claim or coordination authority.

**Decisions Section**

The mandatory section of every best-effort PR description. Fixed schema (inherited from the former Handoff Doc; ADR-0003 as amended by ADR-0008): decisions made with rationale, open questions, remaining work, dead ends tried. A first-class review input: the Reviewer judges the Worker's decisions, not just the diff.

*Avoid*: "Handoff Doc" (retired term — the schema now lives in the PR description); relying on agent-native session files (never load-bearing).

**Knowledge Source**

An optional field on a worker-type definition: a git URL + pin pointing at a repo of agent knowledge (skills, subagents, workflows) that the knowledge machinery bakes into the derived image at build time. The same content syncs to laptop tool dirs.

*Avoid*: putting machinery in the knowledge repo (it is pure data); baking at container start.

**Node Agent**

The uncontainerized TheOzolith daemon installed on every physical node, registering it as a Container-Host. Sends heartbeats (node and stack status) to the Control Node; reconciles infrastructure commands (drain, recycle, update, rebuild); pulls config and node-scoped secrets; builds derived images locally and manages docker stack lifecycle on its box. The private config repo's stacks (former homeserver workloads) run through it.

*Avoid*: "agent" (an Agent is a tool config); the legacy Home Server node agent (replaced by this); running the daemon itself in a container.

**Orchestrator**

The whole agentic coding pipeline system: planning, execution, review, and monitoring together. TheOzolith as a running system, not a single component.

*Avoid*: using it for the Control Node or a Worker.

**Reviewer**

A separate long-lived actor — own container, own GitHub identity, configured with a stronger model than the Worker adapters — that polls PRs labeled pr_ready without needs_human and owns all post-PR state. Verdicts: approve (needs_human + deviation/risk labels), revise (attempt-N on the PR, revised plan + resume commit, issue re-queued to plan_ready under delegated authority), escalate (blocked + needs_human). Never implements; no self-grading by construction.

*Avoid*: running review as a gate step inside the Run.

**Run**

One attempt at one GitHub issue by a Worker, always ending in a best-effort PR when a checkout is reached. Stateless and disposable: fresh clone/worktree and fresh context; the only carryover is PR branch content at the Reviewer-designated resume commit. The unit of review rounds (3 max) and evidence bundles.

*Avoid*: "job"; "task" (that is the issue).

**Skill**

A reusable instruction module: a folder containing `SKILL.md` plus optional scripts and reference files.

*Avoid*: "claude agent", "prompt".

**Stack**

A declarative unit of workload the Node Agent runs: name, image or compose file (plus overlays), placement, desired state. Built-in Stacks (worker, reviewer, control) and user-defined Stacks (e.g. a script runner) share the same format.

*Avoid*: "role" (legacy Home Server term).

**Worker**

A long-lived container bound to one Agent config. Polls GitHub for plan_ready issues, claims via the Claim Protocol, and executes Runs sequentially, one at a time — each Run ends in a best-effort PR with a Decisions Section. Recycled on a schedule; holds no authoritative state and owns no post-PR labels.

*Avoid*: "runner"; "agent" (an Agent is a config, not a process).

**Workflow**

A configuration that involves multiple agents working together.

*Avoid*: "skill", "agent".

## Relationships

- An Agent is the full config for exactly one tool (Claude, Codex, or Pi).
- A Claude agent is one subagent file belonging to the Claude agent config.
- A Skill can be reused across agents.
- A Workflow involves two or more agents.
- The Orchestrator comprises planning (GitHub issues), execution (Workers and Runs), review (Reviewer actor plus human), and monitoring (Control Node).
- A Worker is bound to exactly one Agent config and executes one Run at a time.
- A Run belongs to exactly one Worker and targets exactly one GitHub issue.
- The Control Node observes Workers and Runs; GitHub owns all coordination state.
- A Decisions Section belongs to exactly one PR; all review rounds for an issue reuse that one PR and branch.
- The Reviewer owns all post-PR state and never implements; the Worker implements and owns only claim state plus pr_ready at push.
- A Node Agent runs on exactly one box, manages its docker stacks, and heartbeats to the Control Node.
- The Config Repo declares Stacks; Node Agents reconcile them from desired state received over the heartbeat/command channel.
- The command channel carries desired state and references; the only payload it ever carries is node-scoped secret values, pull-only over mandatory TLS.
- Labels are the coordination vocabulary: plan_ready (claimable), in_progress, attempt-N (on the PR, per review round), pr_ready (ready for the Reviewer), pr_ready + needs_human (awaiting human stamp), blocked + needs_human (awaiting a human decision). Issues and PRs carry separate label sets; each actor polls exactly one label.
- TheOzolith is one public monorepo with separable components (knowledge machinery, worker, control, nodeagent, deploy); all private content is data in one private config repo (ADR-0007).
