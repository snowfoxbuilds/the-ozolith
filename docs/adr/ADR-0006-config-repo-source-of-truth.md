Status: ACCEPTED — amended by ADR-0048 (2026-08-18, #62): the human-authored Config Repo remains the source of truth, but the tree control loads and distributes is now its machine-built PINNED BUILD, produced only by `theozolith config ingest` (lint, mechanical pin resolution, knowledge compile, provenance-stamped commit).

Date: 2026-07-13

# ADR-0006: Git-backed Config Repo as the single source of truth for deployments

## Context

TheOzolith should be user-friendly: customizations (worker types, extra stacks, overlays) managed through a web UI. But the deployment must stay reproducible across a fleet, and a UI that owns its own state drifts from any repo backup. One authority is required.

## Decision

All deployment customization lives in one git-backed Config Repo (default ~/.theozolith/configs): Stack definitions, worker types as base-image-plus-setup-instructions, compose overlays, and secret names. The web UI is an editor over this repo — every save is a commit. The repo's working home is the Control Node; desired state distributes to Node Agents over the heartbeat/command channel, and nodes cache last-applied config for degraded operation.

Worker-type setup instructions are built into derived images locally by each container-host when instructed (base image pinned by digest; build metadata stamped as image labels and reported in heartbeats; rebuild is a standard command). No registry or builder node for derived images — containers exist for containment, not byte-identical reproducibility. Secret values are excluded from the repo: configs carry names; values are stored encrypted at rest on the Control Node and pulled node-scoped over mandatory TLS at deploy time, delivered via the _FILE convention (node-local providers remain for air-gapped deployments).

Channel invariant (amended 2026-07-13): the command channel carries desired state and references; the only payload it ever carries is node-scoped secret values over TLS.

## Consequences

- **Positive**: every change is auditable, diffable, and revertable; the deletion test holds (repo plus secret keys reconstructs a deployment); the private deployment's entire consumption of the product reduces to one private Config Repo; UI convenience and GitOps reproducibility stop being a trade-off.
- **Negative**: the UI must serialize all state to files — no hidden database; concurrent edits (UI plus direct git) can conflict and need surfacing; per-node builds can skew across the fleet (mitigated by digest-pinned bases, build metadata in heartbeats, and the rebuild command); the Control Node becomes the default secret store (encrypted at rest, TLS mandatory).
- **Neutral**: the Control Node becomes the config authority while GitHub keeps issue coordination (ADR-0002 unchanged); degraded mode is preserved via node-cached config.
## Alternatives Considered

- **UI-owned database with repo export**: rejected — export drift creates two sources of truth; backups become stale snapshots.
- **Per-node config directories**: rejected — fleet drift; the exact failure desired-state reconciliation exists to prevent.
- **Customizations inside the public product repo**: rejected — private deployment specifics do not belong in the open-sourceable repo (ADR-0004).
- **Registry plus builder node for derived images**: rejected on same-day amendment — bootstrap catch-22 (the builder needs a deployment scheme before one exists) and a second cluster-management system just for builds. Build skew is surfaced via metadata and erased via the rebuild command instead of being prevented.
