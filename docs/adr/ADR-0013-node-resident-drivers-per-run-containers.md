Status: ACCEPTED — amended in part by ADR-0019 (2026-07-21): interactive tmux harness mechanics retired; Runs execute headless and are not attach targets (interactivity moves to the Pilot).

Date: 2026-07-15

# ADR-0013: Node-resident drivers, agent harness, and per-Run containers

## Context

The always-interactive contract ([AGENTIC-CODING-PIPELINE.md](http://agentic-coding-pipeline.md/), 2026-07-15) requires every agent process to run in an attachable interactive tmux session with hard credential isolation. A single-container Worker cannot deliver that isolation: an agent with a shell can read a co-resident driver's environment via /proc, so env scrubbing is theater. Splitting each actor into a trusted driver and a credential-free harness forced the topology question; M2's implementation (PR #2) — long-lived Worker containers running headless agent subprocesses — is retracted by this decision.

## Decision

1. **Two roles per actor.** The **driver** is the trusted, credentialed brain: polls GitHub, runs the Claim Protocol, materializes job inputs, creates run containers, sequences gate steps, performs every GitHub read and write, and owns every pipeline decision. The **agent harness** is credential-free plumbing: PID 1 of a run container; it starts the tmux session, injects the prompt, awaits the per-adapter completion marker (timeout backstop), writes outputs, and exits. The harness never appears in pipeline-state sentences.
2. **Drivers are node-resident processes, not containers.** The long-lived half of each actor runs on the physical node as a supervised child of the Node Daemon, declared as a process-kind Stack. "Workers are long-lived containers" is retracted; Workers remain long-lived actors.
3. **Runs execute as ephemeral containers.** The driver creates one container per Run (and per review round) from the Agent config's image; container lifetime = Run lifetime. Containers carry labels (`theozolith.run-id`, `theozolith.owner=<stack>`) and deterministic names (`ozolith-run-<run-id>`, `ozolith-review-<pr>-round-<n>`). Warm caches move to named volumes mounted into run containers.
4. **Gate steps are harness jobs.** Gate steps execute agent-authored code, so they run on the credential-free side through the same job mechanism — never inside the credentialed driver.
5. **The driver–harness interface is a per-Run job directory.** input/ (prompt, checkout, review input files) and output/ (decisions or verdict file, transcript, status). No network channel and no in-container reporter: container liveness is observed by the Node Daemon via docker; semantic Run events are emitted by the driver; session state reaches the driver as files.
6. **Node Agent is renamed Node Daemon** ("Agent" is reserved for LLM tool configs). The component directory nodeagent/ becomes nodedaemon/.
7. **Kill-the-tree.** The Node Daemon runs as a systemd unit with KillMode=control-group: every TheOzolith process on a node is a live descendant of the Node Daemon or does not exist. Two orphan classes, two reapers: host processes die with the cgroup; orphaned run containers (label present, owner gone) are reaped at daemon/driver startup, and the zombie-claim janitor restores GitHub claim state. A daemon crash costs at most the in-flight Runs; no-PR Runs consume no round budget.
8. **Versioning.** The Node Daemon and drivers ship in one versioned product distribution; the Config Repo pins the product version; updates ride the existing update infrastructure command. Digest-pinned images remain the pinning story for all containers.
9. **Deletion test restated.** "Runs anywhere with docker compose plus a .env" becomes "runs anywhere with docker, the TheOzolith package, and a .env"; driver `--once` modes are the daemon-less dev path.
## Consequences

- Credential isolation is enforced by a container boundary: no LLM ever shares a box with a PAT; the transition-authority matrix holds by construction. Web-terminal attach targets are live run containers only (reported via heartbeats); ssh-to-node + docker exec needs no in-container daemon.
- Per-Run containers guarantee container-level freshness and make per-Run adapter image selection a driver config line (upgrade path).
- The substrate gains one generic capability — supervise a native process workload — admitted under the admission rule; the daemon still contains zero pipeline knowledge.
- Residuals: the model API key lives in run containers and is leakable by a hostile session (a rotatable spend credential, not GitHub authority); isolation protects authority, not output integrity — the human merge gate absorbs bad verdicts and diffs.
- M2 is regenerated against this design; repo ADR-0011/ADR-0012 die with PR #2, their surviving content absorbed here and in the rewritten Brief M2. Amends ADR-0008's execution phrasing only (the Reviewer's "own container" becomes its own driver plus ephemeral review containers); ADR-0008's state model is unchanged.
## Alternatives rejected

- Same-container env scrubbing (isolation theater — /proc recovers the PAT).
- One container, two unix users (fiddly ownership, weak boundary, poor attach UX).
- Long-lived driver+agent container pair with a file-watching supervisor (more machinery than a one-shot harness; no container-level freshness).
- Containerized drivers with a mounted docker socket (sibling-container path translation; containers the daemon doesn't manage).
- Pipeline code inside the Node Daemon process (kills the substrate/pipeline split; merges failure domains; heartbeat cadence hostage to GitHub latency; restart granularity lost under kill-the-tree).
- In-container heartbeat or event reporters (puts Control Node credentials inside the LLM's box).
