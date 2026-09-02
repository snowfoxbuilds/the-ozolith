Status: ACCEPTED
Date: 2026-09-02

Provenance: issue #95 (grilling 2026-09-02; hardened same day by post-merge
review of PR #97).

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

A post-merge review of this ADR's first draft surfaced three gaps, folded
into the decision below: the ADR-0045 conflict scan is an identity
denylist, not a safety boundary for a live-delivered document that could
carry `hooks` or helper commands; "a fetch failure keeps the last export"
contradicted the pin being a requirement; and an npm integrity value
authenticates bytes without making archive extraction safe for a
privileged daemon. A second review pass added two more, also folded in:
an allowlist that admits a key without closing its interior recreates
the forward-compatibility hole one level down, and the CLI ships one
platform package per (OS, architecture, libc) tuple — each with its own
tarball and integrity — so a single recorded integrity cannot
authenticate every package a heterogeneous fleet selects.

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
   malformed documents. A drop-in is declarative only — it cannot register
   hooks or reference any executable (point 2). The tree rides the existing
   one-hash config distribution beside `drivers/` and `knowledge/`. On a
   **driver type** it bakes: codegen copies it into
   `/etc/claude-code/managed-settings.d/` before the materialize step (so
   the ADR-0045 build-time conflict scan sees it), and its ingest-computed content pin joins the instruction hash as a
   conditional key (`policy`/`policy_pin`, absent when unset — every
   pre-existing identity hashes byte-identically). On a **driverless type**
   it never bakes: the node exports `<state-dir>/policy/<name>` from the
   applied config distribution by the knowledge export's atomic child
   exchange, the deck bind-mounts the export parent read-only, control
   injects an un-overridable `THEOZOLITH_POLICY_TREE`, and the deck's start
   script links `/etc/claude-code/managed-settings.d` to the mounted tree.
   Changing the *selection* recreates the deck (env delta); editing the
   *content* reaches every deck live.
2. **Agent Policy is declarative by rule: a safe-key allowlist, enforced
   identically at ingest and config load.** The ADR-0045 conflict scan is a
   denylist over identity keys and is not, alone, what makes an arbitrary
   managed-settings document safe to deliver live: the managed tier can
   also carry executable or dynamically resolved behavior — `hooks`,
   helper commands (`apiKeyHelper`, `awsAuthRefresh`,
   `awsCredentialExport`, `otelHeadersHelper`), `statusLine`, plugin and
   MCP-server registration, externally fetched configuration. One
   validator therefore runs over every policy tree at ingest **and** at
   config load (the same function at both sites, byte-identical rules),
   and it admits a document only when every top-level key is on the
   product's **safe-key allowlist**: keys positively classified as static
   and declarative — unable to name a command, script, plugin, MCP server,
   endpoint, or fetched resource, and unable to carry an identity or
   steering value. Everything else refuses the ingest or load: identity
   and steering keys (`model`, `availableModels`,
   `enforceAvailableModels`, `fallbackModel`, `effortLevel`,
   `modelOverrides`, provider/endpoint selection), the `env` block (no
   admitted variable classes yet; nothing needs one), every
   executable-reference key named above, and — the default posture — any
   key the allowlist has not classified, so a future vendor setting can
   never silently become an execution or identity surface. An admitted
   key is validated by a **recursively closed schema**, not a shape
   check: every permitted nested member is enumerated with its exact
   type, an unknown nested member refuses exactly as an unclassified
   top-level key does, and a wrong type or extra nesting depth refuses —
   confirming that an admitted key holds an object proves nothing about
   what the object carries, and an open interior would recreate the same
   forward-compatibility hole one level down. Errors fail loud and name
   the policy file and offending key path, never echoing other document
   contents. The v1 allowlist is exactly the declarative class the
   motivating drop-in needs — `attribution`, an object closed to the
   single member `sessionUrl: boolean` (ozolith-configs#6's
   `attribution.sessionUrl: false`); nothing else under it is admitted.
   The allowlist and its schemas are adapter-owned product code,
   advanced only by deliberate classification review — a review that
   classifies the full nested schema, never just the key name — when the
   adapter's validated-CLI set advances (the same review that moves
   `MIN_ENFORCING_CLI`), never by default. The build-gate conflict scan
   still runs where it always ran — over the whole baked managed-settings
   directory of a driver image — as defense in depth. There is no runtime
   content re-scan in the deck: the pinned build is validated twice
   upstream and the distribution is hash-verified — the same trust already
   extended to `drivers/` code. (The deck's model rides a `--model` flag,
   which outranks a managed `model` key regardless; the allowlist still
   matters because a hook, helper, allowlist, or endpoint key would bite.)
3. **The CLI Pin is a worker-type field resolved at ingest, the base-tag
   doctrine applied to the agent CLI.** `cli = "<exact version | npm dist-
   tag>"` on the definition; ingest resolves it against the npm registry
   to an exact version plus the **complete supported-platform integrity
   map**. The CLI ships one platform npm package per (OS, architecture,
   libc) tuple — Linux x64, Linux ARM64, and musl variants among them —
   each with its own tarball and registry-published integrity, so a
   single recorded integrity cannot authenticate every package a
   heterogeneous fleet selects. The supported-tuple set is product-owned
   (the platforms the Node Daemon itself supports); ingest resolves
   every tuple in that set and records, in `pins.toml` under `[cli]`
   keyed `<tool>/<declared>`, the exact version plus one
   `{package, integrity}` entry per tuple — a supported tuple whose
   package or integrity the registry cannot supply fails the ingest.
   Nodes select only from the pinned map (point 4a): every
   network-derived trust decision lives in the durable pinned build,
   never in registry metadata fetched node-side at download time. A
   dist-tag re-resolves on
   every ingest, exactly as a moving base tag does. Ingest and load refuse a
   resolved version below the adapter's `MIN_ENFORCING_CLI` floor — the
   product-wide statement of which CLIs it has validated. Absent field means
   today's behavior: the image's CLI, no mount.
4. **The Node Daemon installs the binary through a fail-closed lifecycle.**
   The registry-published integrity authenticates bytes; it says nothing
   about whether an archive is safe to unpack, so verification and
   extraction are separate, ordered gates. At reconcile the node: (a)
   resolves its platform tuple deterministically — OS, architecture, and
   libc — and selects that tuple's `{package, integrity}` entry from the
   ingest-pinned map; a tuple absent from the map fails before any
   download; (b) downloads the tarball with a bounded size
   and timeout into a private staging directory on the state filesystem;
   (c) verifies the complete tarball against the selected entry's
   ingest-pinned integrity before extracting anything; (d) parses and validates the archive before
   extraction — absolute paths, `..` traversal, symlinks, hardlinks,
   devices, sockets, FIFOs, and unexpected entry types refuse; duplicate or
   conflicting paths refuse; entry-count and expanded-size caps apply; the
   layout must be exactly the expected package shape with the CLI binary at
   its expected path, and any unexpected executable payload refuses; (e)
   extracts only into staging, never following links; (f) requires the
   resulting binary to be a regular file and normalizes ownership (the
   service account) and modes itself — archive metadata is never trusted;
   (g) atomically publishes the completed
   `<state-dir>/cli/<tool>/<version>/` directory (same-filesystem rename)
   and only then the worker-type export. A partially downloaded or
   extracted version is never visible at any published path, concurrent
   reconciles included. On failure or interruption: staging is cleaned,
   previously verified versions are retained for recovery, the desired pin
   stays non-converged, a redacted `theozolith.error` event reports it, and
   the next reconcile retries. This is the ADR-0042
   staged-verify-then-exchange doctrine applied to a registry artifact.
5. **The pin is an execution requirement: desired-first publication,
   converge-strict launch, no fallback.** Per worker type the export parent
   carries two published facts: a **desired record** — the exact resolved
   version, rewritten atomically the moment the applied config changes, no
   download required — and the **export entry**, re-pointed atomically only
   after that exact version has completed point 4. The deck bind-mounts the
   export parent read-only, control injects the un-overridable
   `THEOZOLITH_WORKER_TYPE`, and the start script wires the check into the
   launch path itself (the example installs a `claude` shim first on PATH):
   **every** launch — container start or a new tmux window on a deck that
   has been up for weeks — verifies that the entry exists and identifies
   exactly the desired version before exec'ing it, and fails loudly
   otherwise. A missing entry, a stale entry, and the image's own CLI are
   all non-answers: a new launch never runs the previous export and never
   falls back to the image binary (`DISABLE_AUTOUPDATER=1` closes the
   self-update path). Running sessions are undisturbed — they hold their
   binary's inode and the policy they loaded. A fetch failure therefore has
   one meaning: cached versions remain on disk for recovery and for the
   sessions running them, but the desired pin is non-converged and new
   launches fail until it converges — never a silent keep-last. Pruning
   removes only versions that are neither desired nor referenced by any
   export entry, and a pruning failure degrades to retained disk, never a
   corrupted export. On a replacement host or daemon restart the cache
   reconstructs from the durable pinned build plus the registry; the
   binary cache is never backup state (ADR-0024's durability classes
   hold).
6. **Recreate and failure semantics.** Adopting or dropping either field,
   or changing the selected policy tree, changes env or volumes and
   recreates the deck once. A policy content edit or a CLI version bump
   never recreates: it is picked up on the next `claude` launch in any tmux
   window, while a running session keeps what it loaded. A deck whose node
   has not yet converged a declared artifact fails its container start
   loudly and lets the restart policy retry, and the point-5 check guards
   every later launch the same way — non-convergence is never a silent
   fallback.
7. **Scope.** The CLI Pin is driverless-only in v1 and refused with a driver
   (the mirror of `effort` being refused on decks until a consumer exists);
   driver types keep the base image's CLI as identity bytes. Both fields are
   refused on a codex-adapter type until a consumer exists: codex has no
   managed-settings tier, its config file is theozolith-owned and baked, and
   codex decks are already refused (ADR-0052). Telemetry: per worker type
   the heartbeat carries the desired CLI version, the applied (exported)
   version, the convergence state, and the last install failure as a
   redacted class + message — names, versions, and error classes only,
   never credentials, request URLs, or archive contents — and `theozolith
   status` renders them. Skew has no dispatch consequence (decks are never
   dispatch targets) and is not a permitted execution state either: an
   unconverged pin is enforced at launch (point 5) and reported here.
8. **Ownership.** The product owns validation (the safe-key allowlist and
   tree lint), resolution (ingest), the install lifecycle and export
   contract (Node Daemon: staging, verification, atomic publication,
   desired records, pruning), and telemetry. The in-container wiring — the
   managed-settings symlink via the deck's passwordless sudo, the launch
   shim with the pin check, PATH precedence, the autoupdater switch —
   stays operator-authored in the example start script; the strict-launch
   behavior is part of the deck contract and the product's deploy tests
   exercise it. No product-owned deck helper: the Node Daemon learns no
   workload internals.

## Implementation obligations

This ADR binds the later implementation to test coverage, not just
behavior. The implementing PRs must demonstrate at minimum:

- **Policy validation**: the attribution drop-in passes; every
  identity/steering key, every executable-reference key (`hooks`, helpers,
  `statusLine`, plugin/MCP registration), an `env` block, and an
  unclassified key each fail naming file and key with values redacted;
  the recursively closed schemas hold — an unknown nested member beside
  an admitted one (e.g. a second key under `attribution`), a wrong
  nested type, extra nesting depth, and a non-object document each
  refuse; ingest and config load provably share the one validator.
- **CLI resolution and installation**: an exact version and a dist-tag
  both resolve to exact version + the full supported-platform integrity
  map, and ingest fails when the registry cannot supply a supported
  tuple's package or integrity; a version below
  `MIN_ENFORCING_CLI` fails; a node tuple absent from the pinned map
  fails before download, and each platform package verifies against its
  own map entry; integrity mismatch, truncated download, timeout, missing
  binary, wrong layout, traversal, absolute paths, symlinks, hardlinks,
  devices, duplicate paths, oversized archives, and interrupted extraction
  all fail without publishing anything; success publishes atomically with
  normalized ownership and modes; concurrent reconciliation exposes no
  partial state; pruning cannot delete a desired or exported version.
- **Lifecycle**: adopting or dropping the CLI Pin recreates once; a
  version change does not recreate; an existing session survives a pin
  change; a pre-convergence launch fails loudly; a post-convergence launch
  runs exactly the desired version; neither the previous export nor the
  image CLI is ever used as a fallback; restart and replacement-host
  recovery reconstruct exports from pinned-build state; telemetry reports
  desired/applied/non-converged truthfully; a policy selection change
  recreates once while a policy content edit lands on the next launch.

## Consequences

- **Positive**: a CLI update or a policy edit reaches every deck on every
  node through machinery the substrate already runs, with no session lost;
  the fleet's CLI version and policy become definition diffs and heartbeat
  facts instead of a cached npm layer; ozolith-configs#6's heredoc bake
  retires into a reviewed tree; the identity doctrine of ADR-0045 is
  untouched — policy trees are declarative by construction: they cannot
  select a model, register a hook, or name anything executable or fetched.
- **Negative**: two more resolvers (npm registry at ingest, tarball fetch on
  nodes) and a ~215 MB binary per pinned version on each node's state dir; a
  deck's *actual* CLI can now postdate or predate the base image's, so a
  model ID the pinned CLI does not know fails at launch rather than at build
  (the floor lint bounds this; the operator pins a CLI that knows the
  definition's model). Operators wanting a pinned deck CLI and a matching
  driver CLI keep two pins in two places (`cli` and `base`) until a driver
  consumer exists. A pin bump on a node that cannot reach the registry
  means new launches fail until the fetch succeeds — the deliberate price
  of pin-as-requirement (running sessions ride through). A new vendor
  settings key is unusable in policy until deliberately classified into
  the allowlist.
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
  surface than the `drivers/` code already trusted on the same basis. (The
  point-5 launch check is convergence verification, not content
  re-validation.)
- **Keep-last-export on fetch failure** (this ADR's first draft): rejected —
  it lets a stale version silently satisfy a new pin, demoting the pin from
  execution requirement to suggestion; a deck could run an old CLI
  indefinitely without anyone having decided that.
- **Trusting the npm integrity to make extraction safe**: rejected — the
  integrity authenticates bytes, not archive semantics; unpacking runs the
  full staged validation of Decision 4.
- **A denylist (the conflict scan alone) over policy documents**: rejected —
  vendor settings grow, and an unclassified key must fail closed or a
  future key becomes a silent execution or identity surface; hence the
  allowlist.

## Amends

- **ADR-0044**: the customization tuple gains `policy` and `cli`; on a
  driverless type both are declared but not identity-bearing.
- **ADR-0045**: policy trees are validated at ingest and config load by a
  safe-key allowlist strictly stronger than the managed-scope conflict scan
  (which keeps its driver-image build-gate site); the "never overwrites
  operator policy" clause now also reads "operator policy is a reviewed,
  allowlist-validated Config Repo tree".
- **ADR-0048**: the Config Repo gains `policy/`, the pinned build gains
  `[cli]` pins carrying the per-platform integrity map, and the config
  distribution carries the policy tree; the mechanical-pin doctrine
  extends to the agent CLI.
