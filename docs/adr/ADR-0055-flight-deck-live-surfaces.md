Status: ACCEPTED
Date: 2026-09-02

Provenance: issue #95 (grilling 2026-09-02).

# ADR-0055: Flight Deck live surfaces — Agent Policy trees and CLI Pins delivered without container recreate

## Context

A Flight Deck is a long-lived interactive workstation, yet two routine changes
cost it a container recreate that kills every live tmux and agent session: a
Claude Code CLI update (a `base` bump) and a CLI-settings change (a managed-
settings drop-in baked by a setup step, as ozolith-configs#6 ships for the
attribution policy). Both ride the derived-image identity (ADR-0045/0048),
which is the right contract for ephemeral headless Runs and the wrong one for
a deck. Knowledge already has the answer for decks (ADR-0043 as amended by
ADR-0048): content lives on the node's applied pinned tree, read-only
bind-mounted at a stable parent, the selection rides env, the content pin
stays out of the image identity, and an agent-CLI restart picks up an edit.
Two vendor facts shape the design: since the 2.1.x line the CLI is a single
native binary shipped as a platform npm package (the `@anthropic-ai/
claude-code` package is a wrapper whose postinstall copies it into place),
and the CLI merges `/etc/claude-code/managed-settings.json` first, then
`managed-settings.d/*.json` alphabetically with later files winning,
accepting symlinked entries, skipping dot-prefixed names, and treating a
missing directory as silence (read from the 2.1.257 binary; the path is
hard-wired on Linux).

## Decision

Two new worker-type-definition fields become **declared, fleet-visible, and
deliberately not identity-bearing on a driverless type**: the **Agent Policy**
reference and the **CLI Pin**. Both generalize the knowledge precedent; the
Node Daemon exports, the deck mounts a stable parent, a stable env selector
resolves the entry, and the next `claude` launch picks up the new content.

1. **Agent Policy is a Config Repo tree, referenced by name, delivered on the
   HITL/HOTL line.** The Config Repo gains `policy/<name>/` — verbatim
   managed-settings drop-ins — referenced as `policy = "policy/<name>"`. A
   policy tree is strict: top-level `*.json` regular files only; ingest
   refuses subdirectories, non-JSON files, dot-prefixed names, symlinks, and
   malformed documents. Hook scripts a drop-in references live in the image
   or a skill, never in the tree. The tree rides the existing one-hash config
   distribution beside `drivers/` and `knowledge/`. On a **driver type** it
   bakes: codegen copies it into `/etc/claude-code/managed-settings.d/`
   before the materialize step (so the ADR-0045 build-time conflict scan sees
   it), and its ingest-computed content pin joins the instruction hash as a
   conditional key (`policy`/`policy_pin`, absent when unset — every
   pre-existing identity hashes byte-identically). On a **driverless type**
   it never bakes: the node exports `<state-dir>/policy/<name>` from the
   applied config distribution by the knowledge export's atomic child
   exchange, the deck bind-mounts the export parent read-only, control
   injects an un-overridable `THEOZOLITH_POLICY_TREE`, and the deck's start
   script links `/etc/claude-code/managed-settings.d` to the mounted tree.
   Changing the *selection* recreates the deck (env delta); editing the
   *content* reaches every deck live.
2. **Agent Policy is identity-free by rule, enforced at the lint site.**
   Ingest and config load run the ADR-0045 conflict scan in its build-gate
   mode over every policy tree, refusing any identity key (`model`,
   `availableModels`, `enforceAvailableModels`, `fallbackModel`,
   `effortLevel`, `modelOverrides`, policy helpers) and any model-, effort-,
   or endpoint-steering `env` entry, naming file and key. There is no
   runtime re-check in the deck: the pinned build is trusted by construction
   and the distribution is hash-verified — the same trust already extended
   to `drivers/` code. (The deck's model rides a `--model` flag, which
   outranks a managed `model` key regardless; the full key list still
   applies because an allowlist or endpoint entry would bite.)
3. **The CLI Pin is a worker-type field resolved at ingest, the base-tag
   doctrine applied to the agent CLI.** `cli = "<exact version | npm dist-
   tag>"` on the definition; ingest resolves it against the npm registry to
   an exact version plus the platform package's registry-published
   integrity, recorded in `pins.toml` under `[cli]` keyed `<tool>/<declared>`
   with an inline `{version, integrity}` value. A dist-tag re-resolves on
   every ingest, exactly as a moving base tag does. Ingest and load refuse a
   resolved version below the adapter's `MIN_ENFORCING_CLI` floor — the
   product-wide statement of which CLIs it has validated. Absent field means
   today's behavior: the image's CLI, no mount.
4. **The Node Daemon fetches, verifies, stores, and exports the binary.** At
   reconcile the node downloads the platform package tarball for its
   architecture from the npm registry, verifies it against the pinned
   integrity (the network is never trusted alone), and extracts the binary
   into `<state-dir>/cli/<tool>/<version>/`. One export entry per worker
   type, re-pointed atomically when the pin moves; unreferenced versions are
   pruned on convergence like retired config distributions. The deck
   bind-mounts the export parent read-only, control injects an un-overridable
   `THEOZOLITH_WORKER_TYPE`, and the start script puts the entry ahead of the
   image's CLI on PATH and exports `DISABLE_AUTOUPDATER=1` — the pin is the
   only version path. A fetch failure keeps the last export and reports
   through the existing error feed.
5. **Recreate and failure semantics.** Adopting or dropping either field, or
   changing the selected policy tree, changes env or volumes and recreates
   the deck once. A policy edit or a CLI version bump is picked up on the
   next `claude` launch in any tmux window; a running session keeps what it
   loaded. A deck whose node has not yet converged a declared artifact fails
   its container start loudly and lets the restart policy retry — the
   knowledge semantic, never a silent fallback to the image's CLI.
6. **Scope.** The CLI Pin is driverless-only in v1 and refused with a driver
   (the mirror of `effort` being refused on decks until a consumer exists);
   driver types keep the base image's CLI as identity bytes. Both fields are
   refused on a codex-adapter type until a consumer exists: codex has no
   managed-settings tier, its config file is theozolith-owned and baked, and
   codex decks are already refused (ADR-0052). Telemetry: the heartbeat
   carries the node's applied CLI versions per worker type (names and
   versions only), `theozolith status` shows them, and an off-pin node is
   advisory skew (ADR-0042) — decks are never dispatch targets.
7. **Ownership.** The product owns validation (lint), resolution (ingest),
   fetch/verify/export (Node Daemon), and telemetry. The in-container wiring
   — the managed-settings symlink via the deck's passwordless sudo, PATH
   precedence, the autoupdater switch — stays operator-authored in the
   example start script, exercised by the product's deploy tests as today.
   No product-owned deck helper: the Node Daemon learns no workload
   internals.

## Consequences

- **Positive**: a CLI update or a policy edit reaches every deck on every
  node through machinery the substrate already runs, with no session lost;
  the fleet's CLI version and policy become definition diffs and heartbeat
  facts instead of a cached npm layer; ozolith-configs#6's heredoc bake
  retires into a reviewed tree; the identity doctrine of ADR-0045 is
  untouched — policy trees cannot select a model by construction.
- **Negative**: two more resolvers (npm registry at ingest, tarball fetch on
  nodes) and a ~215 MB binary per pinned version on each node's state dir; a
  deck's *actual* CLI can now postdate or predate the base image's, so a
  model ID the pinned CLI does not know fails at launch rather than at build
  (the floor lint bounds this; the operator pins a CLI that knows the
  definition's model). Operators wanting a pinned deck CLI and a matching
  driver CLI keep two pins in two places (`cli` and `base`) until a driver
  consumer exists.
- **Neutral**: the deck image still ships the base image's CLI, inert when
  a pin is declared; the config distribution hash keeps its protocol name
  while covering a third tree; the Candidate Bundle exporter and verifier
  gain the policy tree bytes and the conditional hash keys (ADR-0054's
  "recompute everything that can affect the image" holds); the deck's model
  is unchanged — it still rides the image via `/etc/theozolith/model`.

## Alternatives Considered

- **Policy inside the knowledge tree** (a `settings/` subdirectory the claude
  compiler emits): no new field or tree, but stretches "knowledge is pure
  data for the agent" into tool policy, forces a second bake destination on
  driver images, and couples policy edits to the knowledge pin.
- **A typed TOML policy table the adapter materializes**: cannot express
  multiple drop-ins or ordering, and turns every new CLI key into a product
  schema change.
- **Deployment-wide CLI pin in `control.toml`**: violates ADR-0044 (the
  definition owns the customization tuple) and cannot express two deck types
  on one node at different versions.
- **Control fetches the binary at ingest and serves it over the artifact
  path**: parks a 215 MB blob per version in control's cache tier for no
  gain; nodes already pull registry artifacts directly and verify against a
  pin in the pinned build.
- **Sanctioned in-container self-update on a per-deck volume**: undeclared,
  drifts per instance, invisible to the fleet — the exact drift this ADR
  removes.
- **Bake the CLI Pin on driver types too**: the bench bundle would either
  carry the binary or require a networked install at build; deferred until a
  consumer needs it.
- **A product-owned `theozolith-deck` wiring helper**: would put workload
  internals into product code the deck contract deliberately leaves to the
  operator's start script.
- **Runtime re-scan of the mounted policy at every launch**: redundant with
  the trusted, hash-verified pinned build; a settings key is a far smaller
  surface than the `drivers/` code already trusted on the same basis.

## Amends

- **ADR-0044**: the customization tuple gains `policy` and `cli`; on a
  driverless type both are declared but not identity-bearing.
- **ADR-0045**: the managed-scope conflict scan gains an ingest/config-load
  site over policy trees; the "never overwrites operator policy" clause now
  also reads "operator policy is a reviewed Config Repo tree".
- **ADR-0048**: the Config Repo gains `policy/`, the pinned build gains
  `[cli]` pins, and the config distribution carries the policy tree; the
  mechanical-pin doctrine extends to the agent CLI.
