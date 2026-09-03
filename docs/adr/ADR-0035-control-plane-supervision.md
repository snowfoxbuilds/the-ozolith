Status: ACCEPTED

Date: 2026-08-04

# ADR-0035: Control-plane supervision — the substrate never supervises its own control plane

## Context

- The node substrate spec listed control as a built-in Stack kind alongside worker and reviewer, implying the Node Daemon could supervise the control workload like any other declarative Stack — including containerized control placed on some node.
- Single-Node Deployment (grilling 2026-08-04) forced the latent ambiguity into the open. If the Config Repo placed a control Stack on the local node, drain or recycle of the only node would kill the control plane mid-command: the process orchestrating the drain and the tool needed to undrain die together.
- Update ordering already assumes control owns its own lifecycle: the Control Node updates itself last via os.execv (ADR-0015). Daemon-supervised control would create two supervisors that both believe they own the control restart.
- The diagnostic layering settled in the same grilling depends on the separation: `theozolith status` reports what the Control Node knows; systemd and docker report whether the Control Node is. Control-as-Stack blends the two layers.

## Decision

- The Control Node process always runs as its own systemd unit (`theozolith-control serve`) on its host, on every deployment shape — single-node and multi-node alike.
- The built-in control Stack kind is deleted. Built-in Stack kinds are worker and reviewer; user-defined kinds are unaffected.
- Invariant: **the substrate never supervises its own control plane.**
- Drain and recycle of any node — including the local node of a Single-Node Deployment — never touch control. The Operator TUI therefore keeps working through a drain, which is precisely when it is needed.

## Consequences

- Containerized control is off the table. Control deploys like the Node Daemon does: a systemd unit installed on whichever host serves as the Control Node. Moving control to a new host is install + restore `~/.theozolith/` + `theozolith-control recover` (ADR-0024), never a placement edit in the Config Repo.
- One supervision story per layer: systemctl owns control; the Node Daemon (cgroup kill-tree, ADR-0013) owns everything else on a node.
- The os.execv self-update keeps exactly one owner, and update fan-out ordering (nodes first, control last) is coherent by construction.
- The `init --with-local-node` scaffold places worker Stacks only; no control Stack can appear on the substrate.
- NODE-SUBSTRATE.md is updated: control struck from the built-in Stacks list; Decisions entry "Grilling 2026-08-04 (late)" records the ruling.

## Alternatives Considered

- **Control as a Stack on its own substrate** (the status-quo ambiguity): circular supervision — drain kills both the control plane and the undrain path; two owners for control restarts; recovery inverts onto a command channel that just died. Rejected.
- **Control Stack allowed only on non-control nodes** (containerized control for multi-node): preserves a second deployment story for control, doubling the TLS, storage-partition, and recovery paths (ADR-0024 assumes host-resident `~/.theozolith/`), for a need no deployment currently has. Rejected; may be re-proposed if a concrete multi-node need appears.
- **Daemon-supervised control with a drain-exemption flag**: an exemption flag inside the drain path forks the one mechanism into shape-dependent behavior — the exact special-casing rejected by the Single-Node Deployment ruling. Rejected.
