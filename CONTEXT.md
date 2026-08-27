# TheOzolith — Domain Glossary

Canonical terms for this project. Coding agents and specs use these terms exactly. Updated during grilling sessions.

## Terms

**Agent**

The entire configuration for one coding tool — Claude, Codex, or Pi. Tool-scoped: one agent config per tool. A concept, not an artifact: it is realized by a worker-type definition's setup instructions, Knowledge Source, adapter selection, and model/effort fields — there is no standalone agent-config file in the Config Repo (grilling 2026-08-10).

*Avoid*: confusing with "Claude agent" (a single subagent file, a much smaller unit); treating it as a Config Repo artifact.

**Agent Adapter**

The per-tool invocation layer the Agent Harness calls to run one headless session (Claude Code, Codex; Pi reserved). Ships in the product distribution; the worker-type definition selects which adapter a worker uses (grilling 2026-08-09 — the "default harnesses" of early design talk are really default adapters). Each adapter declares the models and reasoning-effort values it can map; the derived-image build fails on an undeclared value (grilling 2026-08-10).

*Avoid*: "harness" (immutable product plumbing, never a per-type variable); "Agent" (an Agent is the whole tool config the adapter invokes).

**Agent Harness**

The credential-free half of any worker (Implementer, Reviewer, Initializer): PID 1 of an ephemeral run container. Invokes the agent headless (the adapter's one-shot mode), passes the prompt at invocation, treats process exit as completion (timeout as backstop; ADR-0019), writes outputs (decisions or verdict file, transcript, status) into the job directory, and exits — container lifetime = Run lifetime. Dumb plumbing by design: no GitHub knowledge, no policy, no state; it never appears in pipeline-state sentences.

*Avoid*: "driver" (the credentialed node-resident half); giving the harness any credential or decision authority; interactive tmux sessions (retired 2026-07-21 — interactivity lives only in the Flight Deck; ADR-0019).

**Chained Base**

The early-start mechanism for dependent work (grilling 2026-08-27; ADR-0053): when every open blocker of a plan_ready issue has an approved-and-awaiting-merge PR (`pr_ready` + `needs_human`, no `blocked`) and those PRs form a single chain by base_ref linkage, dispatch grants the dependent and the driver bases its checkout and PR on the chain's tip branch — recorded in the driver-owned "Based on #N at `<sha>`" PR-body zone and re-resolved fresh at checkout. Requires a merge-commit workspace with delete-branch-on-merge (checked mechanically from repo settings; otherwise dependents wait for full merge); GitHub retargets the dependent to main when the blocker merges. Nothing auto-merges or auto-closes — the go-ahead only overlaps the human merge stamp.

*Avoid*: "stacked base" / "stacking" (Stack is the workload unit); "based-on" as the mechanism's name (that is the PR-body zone); chaining on unreviewed pr_ready (rejected for now); fan-in chaining (parallel open blocker lines wait).

**Claim Protocol**

How an Implementer takes exclusive ownership of a plan_ready issue: the Implementer requests work from the Control Node; the Control Node selects an issue, writes the claim to GitHub itself (assigns the Implementer's GitHub login, adds in_progress), and returns the issue in the same response — claim-write-through, single serialized claim-writer (ADR-0017). GitHub remains the sole source of coordination truth. A granted claim that never activates (no claimed event within the activation window) is released by the Control Node. The grant carries claim authority only, never context: each Run — including the local retry — re-reads the full issue and PR context fresh at checkout (grilling 2026-08-16, amending ADR-0017's no-re-read clause). Control Node down = new claims pause; in-flight Runs are unaffected.

*Avoid*: drivers claiming directly against GitHub (retired 2026-07-17); "assign-and-verify" (deleted by ADR-0017); "the grant is the issue" (retired 2026-08-16 — the grant is the claim; context is read at checkout).

**Claude agent**

A single `.md` subagent file used by Claude. One component inside the Claude agent config.

*Avoid*: "agent" (which means an entire tool config), "skill".

**Completion Retry**

The cheap retry class for contract failures (grilling 2026-08-16): a session that completed cleanly but whose Output Proposal fails validation — missing or invalid required fields, e.g. commit-message — relaunches once with the working tree and partially-filled proposal preserved, a new container, run_id, and evidence bundle, and the main prompt plus a machine-generated error appendix naming the missing fields (fill-only; code churn there is a reviewable finding). Capped at one — a second miss escalates failed + needs_human. Implementer-only: it exists to protect work products that survive the session (worktree, pending proposal); the Reviewer's work product is the proposal itself, so a missing verdict escalates immediately (ADR-0014 stands). Distinct from the full local retry (crash, timeout, zero commits), which discards everything.

*Avoid*: preserving agent sessions (vendor resume is never load-bearing; every retry is a fresh context window); chaining completion retries; extending it to the Reviewer.

**Config Distribution**

The hash-pinned artifact the Control Node packages from the Pinned Build's `drivers/` and compiled `knowledge/` trees on every config change (ADR-0042/0048 — one hash covers both, kept under the original drivers-hash name for protocol stability), served to nodes over the same artifact-pull path as `theozolith build` output. The heartbeat channel carries only the hash reference; the Node Daemon reconciles it exactly like the product pin, and an off-hash node is dispatch-ineligible (ADR-0042). Stamped with the product version it was built against; skew is advisory.

*Avoid*: confusing with the product distribution (daemon, built-in drivers, harness); code riding the command channel as config payload.

**Config Repo**

The single human-authored source of truth for one deployment's customizations (glossary ruling, ADR-0048: this term means the *human* repo): Stack definitions, worker types (base image + setup instructions + optional Knowledge Source + driver reference), agent knowledge (`knowledge/` — per-worker-type knowledge roots in the ADR-0009 layout; ADR-0048), custom driver code (`drivers/`, delivered as the Config Distribution; ADR-0042), compose overlays, secret names, and control-plane settings (`control.toml`; ADR-0023). A git repo living wherever the operator likes — ingest accepts a local path or a git URL — carrying no computed pins; `theozolith config ingest` is the only path from it to the Pinned Build that control actually loads and distributes (ADR-0048). Never contains secret values.

*Avoid*: treating the web UI as a write authority (the settings form is display-only since ADR-0048); per-node config dirs; hand-managed pins (digests, knowledge hashes — ingest resolves them; the tailscale sha256 is the deliberate human-entered exception); confusing it with the Pinned Build; "pure data" (narrowed by ADR-0042 — driver code is operator content).

**Container-Host**

The node type a physical machine becomes when the Node Daemon is installed on it: it runs Stacks (container workloads and supervised driver processes) under desired-state control and builds derived images locally when instructed. The daemon runs on the host; only agent workloads are containerized.

*Avoid*: containerizing the Node Daemon; "builder node" (removed — every container-host builds its own images).

**Context Tree**

The per-Run file tree (input/) carrying the complete structured snapshot of the claimed issue and its PR: bodies, every comment surface (issue comments, PR conversation comments, review comments, reviews), timeline, commits, and checks, split into per-item files with per-surface index files. Serialized deterministically by the driver at checkout; the agent discovers content progressively and judges relevance itself. The driver never relevance-filters, summarizes, or truncates (grilling 2026-08-16).

*Avoid*: injecting thread content into the prompt (the prompt carries only the rules, the issue body, the resume-round revised plan, and the navigation guide); driver-side "relevant comment" heuristics (retired 2026-08-16).

**Control Node**

The product's central service, shipped in TheOzolith's control/ component (deployed on the Pi). Renders the fleet dashboard; receives Node Daemon heartbeats and Run events; answers heartbeats with infrastructure commands (drain, recycle, update, rebuild); dispatches claims as the single writer of claim creation on GitHub (ADR-0017); escalates zombie claims evidence-first and quarantines failing nodes at dispatch (ADR-0016). Authoritative for node and docker lifecycle and for claim dispatch; never originates other coordination — it cannot approve, revise, or advance an issue, and GitHub remains the sole source of coordination truth. Control Node down: in-flight Runs finish and publish; new claims and review rounds pause.

*Avoid*: treating its database as coordination truth (GitHub is); "advisory" for the claim path (retired 2026-07-17; ADR-0017).

**Decisions Section**

The mandatory section of every best-effort PR description. Fixed schema (inherited from the former Handoff Doc; ADR-0003 as amended by ADR-0008): decisions made with rationale, open questions, remaining work, dead ends tried, and optional process issues (pipeline friction + suggested fix — advisory, human-harvested; never review findings). A first-class review input: the Reviewer judges the Implementer's decisions, not just the diff. Entries are proposed through the format-output CLI into the worker's Output Proposal; the separate hand-written decisions file is retired (grilling 2026-08-16).

*Avoid*: "Handoff Doc" (retired term — the schema now lives in the PR description); relying on agent-native session files (never load-bearing).

**Dependency Edge**

The GitHub-native `blocked by` relation between two issues in the workspace repo — the machine-readable source of ordering truth (grilling 2026-08-27). Declared at planning time, hand-editable in the UI, rebuildable from GitHub alone. Carries ordering only, and only true build-on dependencies; sub-issue links carry grouping/progress and never ordering. Dispatch consumes edges for claim eligibility — a blocker satisfies its edge when closed as completed, or through the Chained Base go-ahead; the Context Tree walks the transitive closure into `input/deps/`. A cross-repo edge on a claimable work issue is a malformed state; cross-repo work enters ordering only through a locally created stand-in sub-issue in the workspace repo.

*Avoid*: the `blocked` label (a human decision is owed — unrelated to ordering); prose "depends on #N" as a source (body prose mirrors the edges); over-chaining (an edge that is not a true build-on dependency serializes parallelizable work).

**Driver**

The trusted, credentialed half of any worker: a node-resident process, spawned and supervised by the Node Daemon as a process-kind Stack. Polls GitHub, runs the Claim Protocol, materializes job inputs, creates per-Run containers, sequences gate steps as harness jobs, and performs every GitHub read and write. Holds the actor's PAT; never executes repo code or model output. Referenced by the worker-type definition as `builtin:<name>` (product distribution) or `drivers/<name>` (Config Distribution; ADR-0042).

*Avoid*: "agent harness" (the credential-free in-container half); running driver logic inside a container or inside the Node Daemon process.

**Flight Deck**

The interactive, human-driven agent container (named 2026-07-21 — the station, not the person): a container-kind Stack running an agent CLI in an attachable tmux session — the web terminal's primary target and the only place the interactive-session convention survives (ADR-0019). Used for issue drafting and non-decomposable work (cross-cutting refactors, design-in-flux). Knowledge reaches it as a read-only bind-mount of the node's applied pinned knowledge tree — authoring happens in the Config Repo, and an agent restart picks up a newly applied tree (ADR-0048; the shared writable clone and promote workflow of grilling 2026-08-08 are retired). Holds GitHub credentials under human supervision — its own machine identity (fine-grained PAT: issues, PRs, contents; no merge permission), never the operator's PAT; not a pipeline actor — it never claims issues, and its sole delegated transition is executing the human's initial plan_ready stamp on an explicit in-conversation instruction naming each work issue and its confirmed risk label (ADR-0019 as amended 2026-08-27); every other transition authority is withheld.

*Avoid*: "Pilot", "Helm" (rejected names); "ad-hoc container" (retired name); "Planner" (reserved for a future autonomous planning actor); confusing with workers (autonomous, headless, credential-free sessions).

**Implementer**

The implementation-stage worker type (renamed from "Worker" 2026-07-21; ADR-0020): a long-lived driver process bound to one Agent config that requests work from the Control Node (Claim Protocol; ADR-0017) and executes Implementer Runs sequentially, one at a time — every completed agent session ends in a best-effort PR with a Decisions Section. Holds no authoritative state and owns no post-PR labels.

*Avoid*: "Worker" for this actor specifically (Worker is the base type); "runner".

**Implementer Run**

The Run kind that attempts one GitHub issue (renamed from "Worker Run" 2026-07-21): fresh clone/worktree; the only carryover is PR branch content at the Reviewer-designated resume commit. A completed agent session ends in a best-effort PR — including a justified no-change empty PR; a failed one (timeout, session death, harness crash, or zero commits with no reasoning) ends in evidence plus one full local retry or a failed + needs_human escalation, never a PR (ADR-0016); a completed session with an unfinished Output Proposal gets one Completion Retry instead (grilling 2026-08-16).

*Avoid*: "attempt-N" (a review-round counter, not an Implementer Run counter); "Worker Run" (pre-taxonomy name).

**Initializer**

The draft-stage worker type (ADR-0021; specified 2026-07-21, deferred past the current testing scope): discovers draft issues lacking the initialized label through Control Node dispatch (discovery-only — no claim write), reads the issue and the repo to understand intent, and publishes one structured analysis comment — intent restatement, feasibility, challenges, recommended path, and grilling-style questions with recommendations — updated in place on re-runs, then applies the initialized label. The issue body stays human-owned; removing initialized is the human re-queue. Exists to make human planning fast; plan_ready authority stays human.

*Avoid*: "Planner" (reserved for a future autonomous planning actor); editing the issue body (forbidden); confusing with the Flight Deck (human-driven, interactive).

**Join String**

The single paste that provisions a physical node: a versioned, checksummed blob (`ozjoin1:` prefix) carrying the Control Node address, the CA certificate fingerprint, and a short-lived single-use join token. The provision CLI verifies the fetched CA against the fingerprint before transmitting anything, then exchanges the join token over verified TLS for a non-expiring per-node token. Disposable by design.

*Avoid*: treating it as a password (it expires and is consumed); confusing the join token with the per-node token (which persists) or the admin token.

**Knowledge Source**

An optional field on a worker-type definition: a `knowledge = "knowledge/<name>"` reference into the Config Repo's knowledge tree (skills, subagents, workflows in the ADR-0009 layout), pinned by an ingest-computed per-tree content hash and compiled at ingest — so a worker type's instruction hash moves only when the tree it references changes (ADR-0048; the former git-URL + pin form, `knowledge_source`/`knowledge_pin`, and the KNOWLEDGE_GIT_TOKEN slot are retired). Delivery splits on the HITL/HOTL line: HOTL workers consume it baked into the derived image at build (the image stands alone); HITL Flight Decks read-only bind-mount the applied compiled tree.

*Avoid*: putting machinery in the knowledge tree (it is pure data); baking at container start; live-mounting knowledge into worker images (workers consume it baked at pin only); per-Stack knowledge (rejected — two Stacks needing different knowledge are two worker types).

**Node Daemon**

The uncontainerized TheOzolith daemon installed on every physical node, registering it as a Container-Host. Runs as a systemd unit with cgroup kill semantics — every TheOzolith process on the node is a live descendant of the Node Daemon or does not exist. Sends heartbeats (node, stack, and run-container status) to the Control Node; reconciles infrastructure commands (drain, recycle, update, rebuild); pulls config and node-scoped secrets; builds derived images locally; supervises container and process Stacks (worker drivers are its children); reaps orphaned run containers by label. The private config repo's stacks (former homeserver workloads) run through it.

*Avoid*: "Node Agent" (retired 2026-07-15 — Agent is reserved for tool configs; ADR-0013); the legacy Home Server node agent (replaced by this); running the daemon itself in a container.

**Operator TUI**

The terminal-based fleet surface (`theozolith top`, plus the one-shot `theozolith status`): run on the Control Node over SSH, it is the primary routine-operations surface while the web dashboard is frozen. A pure API consumer over loopback — same bearer auth and endpoints as any client, no direct database or secret-store reads, and no embedded terminal: attach assistance prints a pastable command resolved from live heartbeat state.

*Avoid*: "dashboard" (the frozen web surface); "web terminal" (the browser PTY bridge); nesting a PTY inside the TUI.

**Orchestrator**

The whole agentic coding pipeline system: planning, execution, review, and monitoring together. TheOzolith as a running system, not a single component.

*Avoid*: using it for the Control Node or a Worker.

**Output Proposal**

The structured set of GitHub mutations a worker's agent proposes during a Run, written into the job dir through the format-output CLI (view-output reads pending state back; a bare status call prints the full fill-state table). The schema is per worker type, selected by the job manifest — Implementer: pr-title (descriptive part; the driver owns the #N: prefix), pr-description (narrative zone; the driver composes the PR body from the Closes line, the narrative, and the Decisions Section), Decisions-Section entries, and a required rich commit-message (subject plus what/why, key decisions, dead ends — reviewed like any artifact; git history is the only context surface guaranteed to every future Run, so redundancy with the Decisions Section is intentional; the driver commits with it and appends a provenance trailer, and no fallback-generated message ever ships); Reviewer: the Verdict and its content. The proposal lives in the job dir, not the worktree (the in-worktree decisions file and its _exclude_metadata fence are retired), and is version-checked: the driver stamps an integer schema_version into the job manifest and the CLI asserts compatibility at first invocation — skew fails pre-work as an infra-class failure, never post-exit (grilling 2026-08-16). Allowlist by schema: forbidden mutations (base branch, issue state, labels, needs_human, other PRs) are unrepresentable, not validated away. Absent field = no-op, never clear. The CLI validates in-session, fail-loud; the driver re-validates and applies post-exit — the sole trust boundary (grilling 2026-08-16).

*Avoid*: treating CLI validation as enforcement (the driver is the policy boundary); names implying live GitHub writes (application happens after process exit, by the driver); raw PR-body access (zone composition, never full replace).

**Pinned Build**

The machine-owned git tree at the control data dir's `configs/` that only `theozolith config ingest` commits to (ADR-0048): the lint-checked, pin-resolved, knowledge-compiled materialization of one Config Repo commit, stamped with the source commit SHA. It is what config load reads and what the Config Distribution serves; hand edits are refused structurally and operationally. Rollback is `git revert` on the pinned build — the resolved pins (base digests, knowledge content hashes) are decisions that exist nowhere else, so re-ingesting an old source commit is not a rollback. Durable git-class state (ADR-0024): recovery restores it, never rebuilds it.

*Avoid*: calling it the Config Repo (that term means the human repo); a second write path into it (settings included — everything goes through ingest); treating it as derivable cache (its pins are underivable).

**Review Run**

The Run kind that executes one review round, with full workspace parity (grilling 2026-08-27; ADR-0053): a fresh container holding a sanitized PR-branch checkout with history (base ref fetched) plus the same Context Tree as an Implementer Run (`input/issue/`, `input/pr/`, `input/deps/`). The driver supplies the PR's base commit and the cumulative changed-file list (driver-side git); the judging agent runs `git diff` itself and may build or run tests at its discretion — inside the container it can do everything an Implementer Run can; the asymmetry is the Output Proposal schema (a verdict, never applied code) and the driver boundary. Emits a verdict the reviewer driver publishes. The round (attempt-N, 3 max per PR) is the budget unit; the Review Run is its execution. One invalid verdict = immediate escalation, no retry.

*Avoid*: conflating with "review round" (the round is the budget unit; the Review Run is its execution); the retired curated inputs (truncated diff blob, driver-picked file contents — retired 2026-08-27).

**Reviewer**

The review-stage worker type: a separate long-lived actor — own node-resident driver, own GitHub identity, configured with a stronger model than the Implementer adapters — that discovers pr_ready PRs without needs_human through Control Node dispatch (ADR-0017) and owns all post-PR state. Verdicts: approve (needs_human + deviation/risk labels), revise (attempt-N on the PR, revised plan + resume commit, issue re-queued to plan_ready under delegated authority), escalate (blocked + needs_human). Review rounds execute as Review Runs; the verdict is emitted as a file and published by the reviewer driver. Never implements; no self-grading by construction.

*Avoid*: running review as a gate step inside the Run.

**Run**

One ephemeral container lifecycle executing one agent session: exactly one headless run container with the agent harness as PID 1, fresh context, container lifetime = Run lifetime; never attachable or human-steered (ADR-0019). Kinds: Implementer Run and Review Run. Stateless and disposable. Every Run that reaches a checkout pushes an evidence bundle — the Run is the unit of evidence.

*Avoid*: "job"; "task" (that is the issue); bare "Run" where the kind matters.

**Skill**

A reusable instruction module: a folder containing `SKILL.md` plus optional scripts and reference files.

*Avoid*: "claude agent", "prompt".

**Single-Node Deployment**

A deployment shape where the Control Node and one Container-Host share a physical machine, bootstrapped by `sudo theozolith init --with-local-node` (standard init plus an internally executed, resumable join flow; ADR-0032 retired the `theozolith-control` command spelling). Uses the same provisioning, Stack, and update mechanisms as any fleet — nothing downstream knows it is single-node.

*Avoid*: "single-node mode" (implies a separate code path or product mode); skipping the join mechanism (it runs, machine-consumed).

**Stack**

A declarative unit of workload the Node Daemon runs: name, workload, placement, desired state, plus optional per-placement bindings — env, workspace, and secret-slot rebindings (ADR-0047). Two workload kinds: container (image or compose file plus overlays) and process (a native command from the product or config distribution, run as a supervised Node Daemon child — how worker drivers deploy; ADR-0042). Built-in Stacks (worker, reviewer) and user-defined Stacks (e.g. a script runner) share the same format. The Control Node is never a Stack — it always runs as its own systemd unit on every deployment shape (2026-08-04).

*Avoid*: "role" (legacy Home Server term); a control Stack kind (deleted 2026-08-04 — the substrate never supervises its own control plane).

**Verdict**

The Reviewer's enumerated ruling on one review round: approve | revise | escalate, proposed through format-output and validated in-session — an invalid value or a final-round revise fails at write time, absorbing ADR-0014's post-exit validate-verdict job into the CLI (grilling 2026-08-16). Content is audience-conditional: a non-final revise carries findings plus the amendment prompt (the revised plan the next Implementer Run executes; ADR-0008); approve and every final-round verdict carry human-facing content — signals, decisions required, findings. The reviewer driver renders the published comment and labels from it. No Completion Retry: a completed review session that never proposed a verdict escalates immediately — the judgment died with the session (ADR-0014 stands; grilling 2026-08-16).

*Avoid*: "decision" (a Decisions-Section entry — an Implementer artifact); "require_changes" and GitHub's REQUEST_CHANGES (the Reviewer never files GitHub reviews).

**Worker**

The base abstraction for every automated pipeline actor (redefined 2026-07-21; ADR-0020): a long-lived, node-resident driver process on a container-host, bound to one Agent config (ADR-0013 — not a container), executing ephemeral headless Runs. All worker types share the same infrastructure — heartbeat, container lifecycle, and the fetch-execute loop — and differ only in GitHub state management and the Agent adapter/model. Built-in types: Implementer, Reviewer, Initializer. The code mirrors the taxonomy with inheritance: custom worker types extend the base Worker or one of the built-in types.

*Avoid*: "Worker" meaning the implementation actor specifically (that is the Implementer since 2026-07-21); "runner"; "agent" (an Agent is a config, not a process); "long-lived container" (retracted 2026-07-15).

**Worker-Type Definition**

The complete customization unit for one worker, declared in the Config Repo (grilling 2026-08-09): base image + setup instructions, optional Knowledge Source (`knowledge = "knowledge/<name>"`; ADR-0048), driver reference (`builtin:<name>` or `drivers/<name>`; ADR-0042), Agent adapter, model + reasoning effort (typed fields; grilling 2026-08-10), workspace (target repo), and secret names. Compiled into a derived image at config change — the compiler materializes model and reasoning effort into the tool's native config at build (never hand-written in setup instructions, never selected at invocation), and the build fails on a model the adapter cannot map. Instantiated by a thin worker Stack (worker type + placement + desired state, plus optional per-placement bindings).

*Avoid*: loading these fields onto the Stack format (Stacks stay generic; the Node Daemon never special-cases workers) — per-Stack workspace/secret bindings are the enumerated exception (ADR-0047), identity fields never move; "harness" as a field (the adapter is the variable; the harness is product plumbing); setting the model via setup instructions, env vars, or invocation flags (it is a typed field, baked at build).

**Workflow**

A configuration that involves multiple agents working together.

*Avoid*: "skill", "agent".

## Relationships

- An Agent is the full config for exactly one tool (Claude, Codex, or Pi).
- A Claude agent is one subagent file belonging to the Claude agent config.
- A Skill can be reused across agents.
- A Workflow involves two or more agents.
- The Orchestrator comprises planning (GitHub issues), execution (workers and Runs), review (Reviewer actor plus human), and monitoring (Control Node).
- Worker is the base type: Implementer, Reviewer, and Initializer are worker types sharing driver infrastructure (heartbeat, container lifecycle, fetch-execute loop) and differing only in GitHub state management and the Agent adapter/model; custom worker types extend the base or a built-in type (ADR-0020).
- A worker is bound to exactly one Agent config; the Implementer executes one Implementer Run at a time.
- A worker = one node-resident driver plus one ephemeral run container per Run; driver and harness communicate only through the job directory.
- A Run's agent reads context from the Context Tree and proposes GitHub mutations only through its Output Proposal; the driver validates and applies both sides of the channel (grilling 2026-08-16).
- Every Run's checkout is a self-contained reference clone (git clone --reference --dissociate) from a driver-owned node-local mirror that is never mounted into containers (grilling 2026-08-16).
- An Implementer Run belongs to exactly one Implementer and targets exactly one GitHub issue; a Review Run belongs to the Reviewer and executes exactly one round of one PR.
- The Control Node dispatches claims and review rounds, observes workers and Runs, and writes claim creation to GitHub; GitHub owns all coordination state (ADR-0017).
- A Decisions Section belongs to exactly one PR; all review rounds for an issue reuse that one PR and branch.
- The Reviewer owns all post-PR state and never implements; the Implementer implements and owns only claim state plus pr_ready at push.
- A Node Daemon runs on exactly one box, supervises its Stacks (container workloads and driver processes in its cgroup), and heartbeats to the Control Node.
- The Flight Deck is a human-driven, credentialed, interactive agent container Stack; it never claims issues and holds no transition authority beyond executing the human's explicitly instructed initial plan_ready stamp (ADR-0019 as amended 2026-08-27).
- The Config Repo declares Stacks; Node Daemons reconcile them from desired state received over the heartbeat/command channel.
- `theozolith config ingest` is the only path from the Config Repo (human) to the Pinned Build (machine-owned); control loads and the Config Distribution serves only the Pinned Build (ADR-0048).
- A worker-type definition names exactly one driver: `builtin:<name>` from the product distribution or `drivers/<name>` from the Config Distribution (ADR-0042).
- A worker-type definition is the complete customization unit — base image + setup instructions, Knowledge Source, driver reference, Agent adapter, model + reasoning effort, workspace, secret names; a worker Stack instantiates exactly one (grilling 2026-08-09; model/effort fields added 2026-08-10); workspace and secret bindings are per-Stack overridable (ADR-0047).
- The heartbeat/command channel carries desired state, references, and advisory telemetry (typed, size-capped, never coordination authority; ADR-0016); the only secret payload it ever carries is node-scoped secret values, pull-only over mandatory TLS.
- Dependency Edges (GitHub `blocked by` relations) are the machine-readable ordering truth consumed by dispatch; sub-issue links group issues under a parent and never order them; issues and PRs are never auto-closed by the pipeline (grilling 2026-08-27).
- A Chained-Base Run's PR targets its blocker's branch, carries the driver-owned "Based on #N at `<sha>`" zone, and retargets to main when the blocker merges and its branch is deleted (ADR-0053).
- Labels are the coordination vocabulary: plan_ready (claimable), in_progress, attempt-N (on the PR, per review round), pr_ready (ready for the Reviewer), pr_ready + needs_human (awaiting human stamp), blocked + needs_human (awaiting a human decision), failed + needs_human (on the issue: execution failure escalated with evidence; only the human removes failed, and failed overrides plan_ready at dispatch — ADR-0016). Issues and PRs carry separate label sets; each actor polls exactly one label.
- TheOzolith is one public monorepo with separable components (knowledge machinery, worker, control, nodedaemon, deploy); all private content lives in one private config repo (ADR-0007) — declarations and knowledge as data, plus custom driver code under `drivers/` (ADR-0042).
