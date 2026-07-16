Status: DRAFT

Last updated: 2026-07-15

# Node Substrate

Cluster-management layer of TheOzolith: Control Node, Node Daemon, Config Repo, Stacks, secrets, and extension points. The coding pipeline ([AGENTIC-CODING-PIPELINE.md](http://agentic-coding-pipeline.md/)) is one consumer of this substrate.

## Context

TheOzolith is cluster management, not container management: it manages physical nodes that run declarative workloads. The substrate serves any adopter of a coding-worker fleet; operator-specific needs are expressed as configuration in a private Config Repo, never as product code.

## Design

### Node model

- The Node Daemon (renamed from "Node Agent" — Agent is reserved for LLM tool configs; ADR-0013) is the uncontainerized TheOzolith daemon installed on every physical node, registering it as a Container-Host. It runs as a systemd unit with cgroup kill semantics (KillMode=control-group): every TheOzolith process on the node is a live descendant of the Node Daemon or does not exist — no zombie processes by construction. Bootstrap = install the daemon; everything else flows from config.
- Heartbeats (60s — node, stack, and labeled run-container status) flow to the Control Node; heartbeat responses carry infrastructure commands (drain, recycle, update, rebuild) that the Node Daemon reconciles.
- Ephemeral run containers: pipeline drivers create per-Run containers, labeled (`theozolith.run-id`, `theozolith.owner`) and deterministically named. The Node Daemon observes labeled ephemerals via docker, reports them in heartbeats, kills them on drain/recycle of the owning Stack, and reaps orphans (label present, owner gone) at startup; the pipeline's zombie-claim janitor restores GitHub claim state. Creation authority is pipeline-side; lifecycle authority is substrate-side (ADR-0013).
- Infrastructure commands are not coordination: the Control Node is authoritative for node and docker lifecycle but never writes claim state; GitHub owns issue coordination (ADR-0002).
### Deployment boundary

- Dependency rule: private depends on public, never the reverse (ADR-0004). The private config repo consumes TheOzolith releases; TheOzolith never references the private repo, specific hosts, or tailnet names.
- TheOzolith publishes versioned product images (GHCR); a deployment's entire private surface is one Config Repo (digest-pinned bases, private Stacks, overlays, secret names). Product changes release in TheOzolith, then a version bump in the Config Repo; hosting changes touch the Config Repo only. Never deploy :latest.
- Substrate admission rule: a feature enters the product only if an external adopter of a coding-worker fleet would want it; operator-specific needs are implemented in the Config Repo against extension points (custom stacks, overlays, out-of-band scripts).
- Transport-agnostic protocol: nodes reach the Control Node via HTTPS with bearer-token auth at a configured URL. TLS is mandatory since secrets transit the channel; a self-signed or install-provisioned CA is fine. Tailscale is a deployment detail: hosts join the tailnet via private-side setup; product images contain no tailscaled, auth keys, or MagicDNS assumptions. Per-container tailnet identity, if wanted, is a compose overlay (tailscale sidecar) layered onto the product compose.
- Network model: the substrate assumes flat IP reachability — every node can reach the Control Node's URL, and the Control Node can reach nodes for interactive access. A single LAN satisfies this; Tailscale's only role (private-side) is extending the flat network to devices off the LAN (laptop or phone away from home). TLS is mandatory even on a LAN because secrets transit the channel.
- Deletion test: with the private config repo gone, TheOzolith must run anywhere with docker, the TheOzolith package, and a .env; driver `--once` modes are the daemon-less dev path (ADR-0013).
### Extension points (all config, no code hooks)

- Custom worker types: base image + user setup instructions + optional Knowledge Source (git URL + pin to a repo of skills, subagents, and workflows, baked in by the knowledge machinery at image build), declared in the Config Repo and compiled at config-change time into a derived image tagged deterministically (base tag + instruction hash). Setup instructions never execute at container start.
- Stacks: the Node Daemon has no hardcoded workload knowledge. Built-in (worker, reviewer, control) and user-defined (e.g. script runner) Stacks share one declarative format: name, workload, placement, desired state. Two workload kinds: container (image or compose plus overlays) and process (a native command from the product distribution, supervised as a Node Daemon child inside its cgroup — how the Worker and Reviewer drivers deploy; ADR-0013).
- Compose overlays: topology changes (sidecars such as Tailscale, extra mounts, networks) layer onto product compose files.
- Typed event API: the run-event endpoint generalizes to namespaced event types; dashboards render known types richly and unknown types generically, so custom Stacks get first-class visibility without product changes.
- Config Repo: git-backed folder (default ~/.theozolith/configs) with its working home on the Control Node; the web UI edits by committing. Desired state flows to Node Daemons over the command channel; nodes cache last-applied config for degraded mode. See ADR-0006.
- Image builds: each container-host builds derived images locally when instructed — base image pinned by digest plus setup instructions from the Config Repo. No registry or builder node for derived images; base and product images still come from GHCR. Containers exist for containment, not byte-identical reproducibility: build skew is surfaced, not prevented — build metadata (base digest, instruction hash, build timestamp) is stamped as image labels and reported in heartbeats, and rebuild is a standard command.
- Secrets: configs carry secret names; values are entered once via the web UI or the Control Node CLI (both write through the same API to the same encrypted store), stored encrypted at rest on the Control Node, and pulled by container-hosts at deploy time — pull-only, node-scoped (only secrets referenced by Stacks placed on that node), TLS mandatory, materialized to /run/secrets (tmpfs), never written to node disk. The provider interface stays pluggable (Control Node default; systemd-creds/SOPS for air-gapped deployments). All TheOzolith services accept the VAR_FILE convention.
- Channel invariant: the heartbeat/command channel carries desired state and references; the only payload it ever carries is node-scoped secret values over TLS.
- Anything these mechanisms cannot express is a product feature request subject to the admission rule — never a new hook.
### Dashboard and operator access

- Web terminal: the dashboard embeds a terminal; a Control Node PTY bridge runs a config-supplied attach command per Stack (default template: ssh {host} -t docker exec -it {container} tmux attach). Worker images run every agent process in interactive mode inside a dedicated, discoverably named tmux session — never headless — so any session is attachable at any time to monitor and interact (the always-interactive contract; see [AGENTIC-CODING-PIPELINE.md](http://agentic-coding-pipeline.md/), Agent session contract). Input typed into an attached session is permitted and audited via the session transcript in the evidence bundle. Attach targets are the live run containers reported in heartbeats — no live container, no attach affordance. No attach command configured = no terminal exposed. All terminal sessions are audit-logged (who attached, when, to which target).
- Trust model: dashboard access = cluster access. Secret entry and the terminal sit behind the same single admin credential — acceptable for a single-operator deployment where the dashboard is reachable only from the trusted network. Finer-grained roles are post-V1.
- V1 web UI scope: read-only fleet dashboard plus secret entry (web UI or CLI). Config editing stays git-native in V1 — the operator edits the Config Repo directly; the web config editor is post-V1. The terminal is a small addition (PTY bridge plus frontend) and may ship in V1 or immediately after.
## Decisions

- **Private-to-public dependency rule**: the product is self-contained; the private config repo hosts pinned releases. See ADR-0004. [SETTLED]
- **Transport-agnostic HTTP + token protocol**; Tailscale demoted to a deployment overlay. [SETTLED]
- **The product owns the node substrate** (dashboard/monitoring, heartbeat/command, docker lifecycle); private deployments extend it with stacks and overlays. See ADR-0005. [SETTLED]
- **Extension surface** = derived images compiled at config change, declarative Stacks, compose overlays, typed event API — all config, no code hooks. [SETTLED]
- **Git-backed Config Repo is the single source of truth**; the web UI commits to it; authority lives at the Control Node. See ADR-0006. [SETTLED]
- **Secrets are named references with _FILE delivery via pluggable providers**; the default provider distributes values through the Control Node — encrypted at rest, pull-only, node-scoped, TLS mandatory. Container-hosts build derived images locally; no registry for derived images. See ADR-0006 (amended). [SETTLED]
- **Cluster management, not container management**: the Node Daemon is an uncontainerized daemon on every physical node (container-host); bootstrap = install the daemon. [SETTLED]
- **Grilling 2026-07-14**: worker types may declare a Knowledge Source baked in at image build by the knowledge machinery; the same private repo syncs to laptop tool dirs. See ADR-0007. [SETTLED]
- **Grilling 2026-07-14**: network model — flat IP reachability (typically one LAN); Tailscale is a private-side bridge for off-LAN devices, never a product dependency. [SETTLED]
- **Grilling 2026-07-14**: dashboard trust model — dashboard access = cluster access (secrets and terminal behind one admin credential) for single-operator V1; terminal sessions audit-logged. Web terminal = Control Node PTY bridge running a config-supplied attach command; tmux-in-container is the worker-image convention. [SETTLED]
- **Grilling 2026-07-14**: V1 web UI = read-only dashboard + secret entry (web or CLI); config editing stays git-native. [SETTLED]
- **Grilling 2026-07-15**: always-interactive contract — agent processes run interactively in dedicated per-session tmux (never headless); drivers inject prompts, detect completion via per-adapter hooks with a timeout backstop, and read file outputs; agents hold no GitHub credentials (drivers own all GitHub I/O); attached-terminal input is permitted and audited via the session transcript in the evidence bundle. See [AGENTIC-CODING-PIPELINE.md](http://agentic-coding-pipeline.md/), Agent session contract. [SETTLED]
- **Grilling 2026-07-15 (late)**: node-resident drivers — Stacks gain a process workload kind; Worker/Reviewer drivers run as supervised Node Daemon children (systemd cgroup kill-tree: drivers die with the daemon, no zombies); Runs execute as labeled ephemeral containers, observed via docker and reaped by label when orphaned; daemon + drivers ship in one versioned distribution updated via the update command; deletion test restated (docker + package + .env); Node Agent renamed Node Daemon. See ADR-0013. [SETTLED]
