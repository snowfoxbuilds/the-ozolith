# TheOzolith — Domain Glossary

Canonical terms for this project. Coding agents and specs use these terms exactly. Updated during grilling sessions.

## Terms

**Agent**

The entire configuration for one coding tool — Claude, Codex, or Pi. Tool-scoped: one agent config per tool.

*Avoid*: confusing with "Claude agent" (a single subagent file, a much smaller unit).

**Agent Harness**

The credential-free half of a Worker or Reviewer: PID 1 of an ephemeral run container. Starts the interactive tmux session, injects the prompt, awaits the per-adapter completion marker (timeout as backstop), writes outputs (decisions or verdict file, transcript, status) into the job directory, and exits — container lifetime = Run lifetime. Dumb plumbing by design: no GitHub knowledge, no policy, no state; it never appears in pipeline-state sentences.

*Avoid*: "driver" (the credentialed node-resident half); giving the harness any credential or decision authority.

**Claim Protocol**

How a Worker takes exclusive ownership of a plan_ready issue: the Worker requests work from the Control Node; the Control Node selects an issue, writes the claim to GitHub itself (assigns the Worker's GitHub login, adds in_progress), and returns the issue in the same response — claim-write-through, single serialized claim-writer (ADR-0017). GitHub remains the sole source of coordination truth. A granted claim that never activates (no claimed event within the activation window) is released by the Control Node. Control Node down = new claims pause; in-flight Runs are unaffected.

*Avoid*: drivers claiming directly against GitHub (retired 2026-07-17); "assign-and-verify" (deleted by ADR-0017).

**Claude agent**

A single `.md` subagent file used by Claude. One component inside the Claude agent config.

*Avoid*: "agent" (which means an entire tool config), "skill".

**Config Repo**

The single source of truth for one deployment's customizations: Stack definitions, worker types (base image + setup instructions + optional Knowledge Source), compose overlays, and secret names. A git-backed folder (default ~/.theozolith/configs) whose working home is the Control Node; the web UI is an editor that commits to it. Never contains secret values.

*Avoid*: treating the web UI as a separate authority; per-node config dirs.

**Container-Host**

The node type a physical machine becomes when the Node Daemon is installed on it: it runs Stacks (container workloads and supervised driver processes) under desired-state control and builds derived images locally when instructed. The daemon runs on the host; only agent workloads are containerized.

*Avoid*: containerizing the Node Daemon; "builder node" (removed — every container-host builds its own images).

**Control Node**

The product's central service, shipped in TheOzolith's control/ component (deployed on the Pi). Renders the fleet dashboard; receives Node Daemon heartbeats and Run events; answers heartbeats with infrastructure commands (drain, recycle, update, rebuild); dispatches claims as the single writer of claim creation on GitHub (ADR-0017); escalates zombie claims evidence-first and quarantines failing nodes at dispatch (ADR-0016). Authoritative for node and docker lifecycle and for claim dispatch; never originates other coordination — it cannot approve, revise, or advance an issue, and GitHub remains the sole source of coordination truth. Control Node down: in-flight Runs finish and publish; new claims and review rounds pause.

*Avoid*: treating its database as coordination truth (GitHub is); "advisory" for the claim path (retired 2026-07-17; ADR-0017).

**Decisions Section**

The mandatory section of every best-effort PR description. Fixed schema (inherited from the former Handoff Doc; ADR-0003 as amended by ADR-0008): decisions made with rationale, open questions, remaining work, dead ends tried. A first-class review input: the Reviewer judges the Worker's decisions, not just the diff.

*Avoid*: "Handoff Doc" (retired term — the schema now lives in the PR description); relying on agent-native session files (never load-bearing).

**Driver**

The trusted, credentialed half of a Worker or Reviewer: a node-resident process, spawned and supervised by the Node Daemon as a process-kind Stack. Polls GitHub, runs the Claim Protocol, materializes job inputs, creates per-Run containers, sequences gate steps as harness jobs, and performs every GitHub read and write. Holds the actor's PAT; never executes repo code or model output.

*Avoid*: "agent harness" (the credential-free in-container half); running driver logic inside a container or inside the Node Daemon process.

**Knowledge Source**

An optional field on a worker-type definition: a git URL + pin pointing at a repo of agent knowledge (skills, subagents, workflows) that the knowledge machinery bakes into the derived image at build time. The same content syncs to laptop tool dirs.

*Avoid*: putting machinery in the knowledge repo (it is pure data); baking at container start.

**Node Daemon**

The uncontainerized TheOzolith daemon installed on every physical node, registering it as a Container-Host. Runs as a systemd unit with cgroup kill semantics — every TheOzolith process on the node is a live descendant of the Node Daemon or does not exist. Sends heartbeats (node, stack, and run-container status) to the Control Node; reconciles infrastructure commands (drain, recycle, update, rebuild); pulls config and node-scoped secrets; builds derived images locally; supervises container and process Stacks (Worker/Reviewer drivers are its children); reaps orphaned run containers by label. The private config repo's stacks (former homeserver workloads) run through it.

*Avoid*: "Node Agent" (retired 2026-07-15 — Agent is reserved for tool configs; ADR-0013); the legacy Home Server node agent (replaced by this); running the daemon itself in a container.

**Orchestrator**

The whole agentic coding pipeline system: planning, execution, review, and monitoring together. TheOzolith as a running system, not a single component.

*Avoid*: using it for the Control Node or a Worker.

**Review Run**

The Run kind that executes one review round: a fresh container reads the PR, diff, evidence, and Decisions Section, and emits a verdict file that the reviewer driver publishes. The round (attempt-N, 3 max per PR) is the budget unit; the Review Run is its execution. One invalid verdict = immediate escalation, no retry.

*Avoid*: conflating with "review round" (the round is the budget unit; the Review Run is its execution).

**Reviewer**

A separate long-lived actor — own node-resident driver, own GitHub identity, configured with a stronger model than the Worker adapters — that discovers pr_ready PRs without needs_human through Control Node dispatch (ADR-0017) and owns all post-PR state. Verdicts: approve (needs_human + deviation/risk labels), revise (attempt-N on the PR, revised plan + resume commit, issue re-queued to plan_ready under delegated authority), escalate (blocked + needs_human). Review rounds execute as Review Runs; the verdict is emitted as a file and published by the reviewer driver. Never implements; no self-grading by construction.

*Avoid*: running review as a gate step inside the Run.

**Run**

One ephemeral container lifecycle executing one agent session: exactly one attachable run container with the agent harness as PID 1, fresh context, container lifetime = Run lifetime. Two kinds: Worker Run and Review Run. Stateless and disposable. Every Run that reaches a checkout pushes an evidence bundle — the Run is the unit of evidence.

*Avoid*: "job"; "task" (that is the issue); bare "Run" where the Worker/Review distinction matters.

**Skill**

A reusable instruction module: a folder containing `SKILL.md` plus optional scripts and reference files.

*Avoid*: "claude agent", "prompt".

**Stack**

A declarative unit of workload the Node Daemon runs: name, workload, placement, desired state. Two workload kinds: container (image or compose file plus overlays) and process (a native command run as a supervised Node Daemon child — how Worker and Reviewer drivers deploy). Built-in Stacks (worker, reviewer, control) and user-defined Stacks (e.g. a script runner) share the same format.

*Avoid*: "role" (legacy Home Server term).

**Worker**

A long-lived driver process on a container-host, bound to one Agent config (ADR-0013 — not a container). Requests work from the Control Node (Claim Protocol; ADR-0017) and executes Worker Runs sequentially, one at a time — every completed agent session ends in a best-effort PR with a Decisions Section. Holds no authoritative state and owns no post-PR labels.

*Avoid*: "runner"; "agent" (an Agent is a config, not a process); "long-lived container" (retracted 2026-07-15).

**Worker Run**

The Run kind that attempts one GitHub issue: fresh clone/worktree; the only carryover is PR branch content at the Reviewer-designated resume commit. A completed agent session ends in a best-effort PR — including a justified no-change empty PR; a failed one (timeout, session death, harness crash, or zero commits with no reasoning) ends in evidence plus one local retry or a failed + needs_human escalation, never a PR (ADR-0016).

*Avoid*: "attempt-N" (a review-round counter, not a Worker Run counter).

**Workflow**

A configuration that involves multiple agents working together.

*Avoid*: "skill", "agent".

## Relationships

- An Agent is the full config for exactly one tool (Claude, Codex, or Pi).
- A Claude agent is one subagent file belonging to the Claude agent config.
- A Skill can be reused across agents.
- A Workflow involves two or more agents.
- The Orchestrator comprises planning (GitHub issues), execution (Workers and Runs), review (Reviewer actor plus human), and monitoring (Control Node).
- A Worker is bound to exactly one Agent config and executes one Worker Run at a time.
- A Worker or Reviewer = one node-resident driver plus one ephemeral run container per Run; driver and harness communicate only through the job directory.
- A Worker Run belongs to exactly one Worker and targets exactly one GitHub issue; a Review Run belongs to the Reviewer and executes exactly one round of one PR.
- The Control Node dispatches claims and review rounds, observes Workers and Runs, and writes claim creation to GitHub; GitHub owns all coordination state (ADR-0017).
- A Decisions Section belongs to exactly one PR; all review rounds for an issue reuse that one PR and branch.
- The Reviewer owns all post-PR state and never implements; the Worker implements and owns only claim state plus pr_ready at push.
- A Node Daemon runs on exactly one box, supervises its Stacks (container workloads and driver processes in its cgroup), and heartbeats to the Control Node.
- The Config Repo declares Stacks; Node Daemons reconcile them from desired state received over the heartbeat/command channel.
- The heartbeat/command channel carries desired state, references, and advisory telemetry (typed, size-capped, never coordination authority; ADR-0016); the only secret payload it ever carries is node-scoped secret values, pull-only over mandatory TLS.
- Labels are the coordination vocabulary: plan_ready (claimable), in_progress, attempt-N (on the PR, per review round), pr_ready (ready for the Reviewer), pr_ready + needs_human (awaiting human stamp), blocked + needs_human (awaiting a human decision), failed + needs_human (on the issue: execution failure escalated with evidence; only the human removes failed, and failed overrides plan_ready at dispatch — ADR-0016). Issues and PRs carry separate label sets; each actor polls exactly one label.
- TheOzolith is one public monorepo with separable components (knowledge machinery, worker, control, nodedaemon, deploy); all private content is data in one private config repo (ADR-0007).
