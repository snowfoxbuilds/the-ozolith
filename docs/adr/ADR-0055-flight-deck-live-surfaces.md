Status: ACCEPTED

Date: 2026-09-02

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

The second adapter's CLI ships differently — at codex-cli 0.150.0 the
platform tarballs are versions of the wrapper package itself
(`@openai/codex@<version>-linux-{x64,arm64}`, static musl binaries), each
carrying five executables under a per-triple `vendor/` prefix plus the
layout marker the binary resolves its helpers by — so the archive
contract of Decisions 3–5, previously implicit in one claude-shaped
validator, is explicit per tool (amended 2026-09-04, #132).

This decision amends ADR-0044 (the customization tuple gains `policy` and
`cli`; on a driverless type both are declared but not identity-bearing),
ADR-0045 (policy trees are validated at ingest and config load by a safe-key
allowlist strictly stronger than the managed-scope conflict scan, which keeps
its driver-image build-gate site; the "never overwrites operator policy"
clause now also reads "operator policy is a reviewed, allowlist-validated
Config Repo tree"), and ADR-0048 (the Config Repo gains `policy/`, the pinned
build gains `[cli]` pins carrying the per-platform integrity map, and the
config distribution carries the policy tree — the mechanical-pin doctrine
extends to the agent CLI).

## Decision

Two new worker-type-definition fields become **declared, fleet-visible, and
deliberately not identity-bearing on a driverless type**: the **Agent Policy**
reference and the **CLI Pin**. Both generalize the knowledge precedent; the
Node Daemon exports, the deck mounts a stable parent, a stable env selector
resolves the entry, and the next agent-CLI launch picks up the new content (amended 2026-09-04: the deck's CLI is whichever registered adapter the definition names).

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
   map**. A CLI ships one platform tarball per (OS, architecture, libc)
   tuple it serves — each with its own registry-published integrity, so
   a single recorded integrity cannot authenticate every tarball a
   heterogeneous fleet selects. The supported-tuple set is product-owned
   (the platforms the Node Daemon itself supports); which registry
   coordinate serves each tuple is the adapter's declaration under the
   **CLI archive contract** (point 4): the wrapper package the declared
   version or dist-tag resolves against, and per tuple the platform
   package plus the tarball-version rule — claude's platform packages
   are distinct packages at the same version, while codex publishes its
   platform tarballs as versions of the wrapper package itself,
   `<version>-<platform>`, and one static musl tarball serves both libc
   tuples of an architecture (amended 2026-09-04, #132). Ingest resolves
   every tuple in the set from that declaration and records, in
   `pins.toml` under `[cli]` keyed `<tool>/<declared>`, the exact version
   plus one `{package, integrity}` entry per tuple — a supported tuple
   whose tarball or integrity the registry cannot supply fails the
   ingest. Nodes select only from the pinned map (point 4a): every
   network-derived trust decision lives in the durable pinned build,
   never in registry metadata fetched node-side at download time, and
   the registry supplies nothing but bytes and their integrity — never a
   path, a mode, or an executable name. A dist-tag re-resolves on every
   ingest, exactly as a moving base tag does. Ingest and load refuse a
   resolved version below the adapter's `MIN_ENFORCING_CLI` floor — the
   product-wide statement of which CLIs it has validated. Absent field means
   today's behavior: the image's CLI, no mount.
4. **The Node Daemon installs the CLI through a fail-closed lifecycle
   held to a product-owned CLI archive contract.** The registry-published
   integrity authenticates bytes; it says nothing about whether an archive
   is safe to unpack, so verification and extraction are separate, ordered
   gates — and what an archive is *allowed to contain* is a product
   decision, never read from the archive or the registry (amended
   2026-09-04, #132). The **CLI archive contract** is a closed table in
   Node Daemon product code keyed by tool slug (`cliinstall`, one row per
   registered adapter). The daemon is stdlib-only (ADR-0010) and cannot
   import the adapters, so the resolution half of the same table —
   wrapper, per-tuple package, tarball-version rule — is declared as
   adapter constants, and a dev-only contract test holds the two halves
   equal for every tool, the way the tuple-key spelling is already held.
   Per tool the contract fixes: the wrapper package; the supported
   platform-package map (tuple → package and tarball-version rule); the
   expected archive prefix per tuple; the exact executable member; the
   published executable name; the closed set of members permitted to
   carry executable bits; and the layout markers that must be present.
   The rows, verified against the real tarballs at the adapters' floors
   (claude-code 2.1.260 and codex 0.150.0, 2026-09-04):

   | | claude | codex |
   | --- | --- | --- |
   | Wrapper | `@anthropic-ai/claude-code` | `@openai/codex` (its `bin/codex.js` is a node launcher the deck never runs) |
   | Platform tarball | `@anthropic-ai/claude-code-linux-{x64,arm64}[-musl]` at `<version>` | `@openai/codex` at `<version>-linux-{x64,arm64}`; the glibc and musl tuples of one architecture share it |
   | Prefix | `package/` | `package/vendor/<triple>/`, triple `x86_64-unknown-linux-musl` or `aarch64-unknown-linux-musl` |
   | Executable member | `package/claude` | `<prefix>bin/codex` |
   | Published name | `claude` | `codex` |
   | Permitted executables | exactly the executable member | `bin/codex`, `bin/codex-code-mode-host`, `codex-path/rg`, `codex-resources/bwrap`, `codex-resources/zsh/bin/zsh`, all under the prefix — the helpers the binary resolves relative to its own canonical path |
   | Required markers | `package/package.json` | `package/package.json` and `<prefix>codex-package.json`, the marker the binary locates its helpers by |

   A tool absent from the contract fails closed with a typed, redacted
   class before any download, its records untouched; a malformed contract
   row is a product bug that fails the same way. At reconcile the node:
   (a) resolves its platform tuple deterministically — OS, architecture,
   and libc — and selects that tuple's `{package, integrity}` entry from
   the ingest-pinned map; a tuple absent from the map fails before any
   download; (b) downloads the tarball — at the coordinate the contract
   derives from the pinned package and version — with a bounded size and
   timeout into a private staging directory on the state filesystem; (c)
   verifies the complete tarball against the selected entry's
   ingest-pinned integrity before extracting anything; (d) parses and
   validates every member before any extraction — absolute paths, `..`
   traversal, symlinks, hardlinks, devices, sockets, FIFOs, and unexpected
   entry types refuse; duplicate or conflicting paths refuse; entry-count
   and expanded-size caps apply; every member must sit under the tool's
   prefix root, every required marker and the executable member must be
   present as regular files, and any executable-mode member outside the
   contract's permitted set refuses — a claude tarball offered to the
   codex contract, or the reverse, refuses on both counts; (e) extracts
   only into staging, never following links, to paths computed from the
   validated names; (f) requires the executable member and every
   permitted executable to be a regular file and normalizes ownership
   (the service account) and modes itself — permitted executables 0755,
   everything else 0644; archive metadata is never trusted; (g)
   assembles the publish directory from the contract's published set —
   the permitted executables and required markers at their
   prefix-relative paths, nothing else from the archive — and, when the
   executable member is not at the prefix root, adds a product-created
   relative link `<published name>` → executable member inside the
   directory (codex: `codex` → `bin/codex`; the binary canonicalizes its
   own path before looking for its helpers, so the link is transparent
   and the vendored layout stays intact beside it; claude publishes
   `claude` itself), then atomically publishes the completed
   `<state-dir>/cli/<tool>/<version>/` directory (same-filesystem rename)
   and only then the worker-type export. A partially downloaded or
   extracted version is never visible at any published path, concurrent
   reconciles included. On failure or interruption: staging is cleaned,
   previously verified versions are retained for recovery, the desired pin
   stays non-converged, a redacted `theozolith.error` event reports it,
   and the next reconcile retries. Control/daemon skew is fail-closed and
   non-destructive by construction: a codex pin reaching a daemon that
   predates the contract fails at the integrity gate — it can only derive
   the claude-shaped coordinate, whose bytes do not match the pinned
   integrity — and a daemon that carries the contract refuses an unknown
   tool before download; either way nothing publishes and no verified
   entry is touched. A claude pin's wire shape is unchanged, so an older
   Control's claude pins converge on a newer daemon exactly as before.
   This is the ADR-0042 staged-verify-then-exchange doctrine applied to
   a registry artifact.
5. **The pin is an execution requirement: desired-first publication,
   converge-strict launch, no fallback.** Per worker type the export parent
   carries two published facts: a **desired record** — the exact resolved
   version, rewritten atomically the moment the applied config changes, no
   download required — and the **export entry**, re-pointed atomically only
   after that exact version has completed point 4. The deck bind-mounts the
   export parent read-only, control injects the un-overridable
   `THEOZOLITH_WORKER_TYPE`, and the start script wires the check into the
   launch path itself: the example installs a shim named for the tool's
   published executable — `claude` or `codex` — first on PATH, which
   reads `<state-dir>/cli/<tool>/by-type/<worker type>.desired`, compares
   it with the `.current` entry, and execs the version-addressed
   `<state-dir>/cli/<tool>/<desired>/<published name>` with the caller's
   argv (for codex the deck's launch also passes the well-known model
   file's value as `--model`, ADR-0052 §4; amended 2026-09-04, #132):
   **every** launch — container start or a new tmux window on a deck that
   has been up for weeks — verifies that the entry exists and identifies
   exactly the desired version before exec'ing it, and fails loudly
   otherwise. A missing entry, a stale entry, and the image's own CLI are
   all non-answers: a new launch never runs the previous export and never
   falls back to the image binary (`DISABLE_AUTOUPDATER=1` closes claude's
   self-update path; `check_for_update_on_startup = false`, seeded into
   the codex state volume's `config.toml`, closes codex's update prompt). Running sessions are undisturbed — they hold their
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
   never recreates: it is picked up on the next agent-CLI launch in any
   tmux window, while a running session keeps what it loaded. A deck whose node
   has not yet converged a declared artifact fails its container start
   loudly and the daemon's reconcile loop recreates it on a later pass,
   retrying the start (amended 2026-09-03, #118), and the point-5 check
   guards every later launch the same way — non-convergence is never a
   silent fallback.
7. **Scope.** The CLI Pin is driverless-only in v1 and refused with a driver
   (the mirror of `effort` being refused on decks until a consumer exists);
   driver types keep the base image's CLI as identity bytes. Agent Policy is
   refused on a codex-adapter type until a codex consumer and its own
   classification review exist: codex's admin tier is a single system
   `config.toml` below user config plus a constraint-typed
   `requirements.toml`, not a drop-in directory, so a codex policy tree
   would be a different shape under a different allowlist (amended
   2026-09-04). The CLI Pin is open to a
   driverless codex type (amended 2026-09-04): the codex adapter declares
   the resolution half of the CLI archive contract and the Node Daemon
   holds the tarball to the codex row of point 4, and the pin resolves,
   installs, exports, and launches pin-strict exactly as the claude pin
   does — the deck is the consumer this clause was waiting for. Telemetry: per worker type
   the heartbeat carries the desired CLI version, the applied (exported)
   version, the convergence state, and the last install failure as a
   redacted class + message — names, versions, and error classes only,
   never credentials, request URLs, or archive contents — and `theozolith
   status` renders them. Skew has no dispatch consequence (decks are never
   dispatch targets) and is not a permitted execution state either: an
   unconverged pin is enforced at launch (point 5) and reported here.
8. **Ownership.** The product owns validation (the safe-key allowlist and
   tree lint), resolution (ingest), the CLI archive contract and the
   install lifecycle and export contract (Node Daemon: the per-tool
   contract rows, staging, verification, atomic publication, desired
   records, pruning), and telemetry. The in-container wiring — the
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
- **CLI archive contract** (amended 2026-09-04, #132): success fixtures
  for both real archive shapes — the four-member claude layout and the
  eight-member vendored codex layout, per Linux tuple — install, publish
  exactly the contract's published set with normalized modes, and expose
  the published executable at `<version>/<name>` (for codex through the
  daemon-created link, resolving inside the version directory); negative
  tests refuse, each without publishing and with previously verified
  versions untouched: cross-tool archive substitution in both directions,
  a wrong executable path or name, an unexpected executable payload (an
  extra executable beside the permitted set, or a permitted name under
  another triple's prefix), a missing layout marker, a malformed contract
  row, and an unknown tool on the wire; mixed-version wire behavior is
  exercised — an older daemon receiving a codex pin fails at the integrity
  gate and publishes nothing, and a newer daemon converges an older
  Control's claude pins unchanged; the adapter-side and daemon-side halves
  of the contract are held equal by a test for every registered adapter
  that declares a CLI table; the codex launch shim passes the pin check
  and execs the published link exactly as the claude shim execs its
  binary.
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
  nodes) and, per pinned version on each node's state dir, a ~215 MB claude
  binary or ~330 MB of vendored codex executables; a
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

## Amendments

- **2026-09-03 (#118)**: the reconcile loop, not a Docker restart policy,
  retries a failed deck start (Decision 6). Daemon-managed single-image Stack
  containers carry no Docker `--restart` policy — the Node Daemon's reconcile
  loop is the sole restarter, so a deck that fails its fail-closed launch is
  recreated on a later pass (roughly the heartbeat cadence). This closes the
  boot-time race (#114) where dockerd, restarting an `unless-stopped`
  container before the daemon materialized secrets onto the freshly-wiped
  `/run` tmpfs, auto-vivified the missing bind source as a directory and
  wedged both the mount and the secret writer. See NODE-SUBSTRATE.md.
- **2026-09-04 (#132)**: the codex Flight Deck exists (ADR-0052 amended), so Decision 7's codex refusal narrows to Agent Policy alone — the CLI Pin opens to codex driverless types with adapter-declared packages and archive shape, and "next `claude` launch" reads "next agent-CLI launch" throughout. The CLI archive contract becomes explicit per tool (Decisions 3–5 and 8): a closed product-owned table in Node Daemon code, mirrored by adapter constants under a contract test, with the rows verified against the real claude 2.1.260 and codex 0.150.0 tarballs; the registry supplies bytes and integrity only, the published-executable link for a vendored layout is daemon-created and never archive-supplied, and an unknown tool or a mixed-version wire fails closed without touching verified entries.

## Relevant PRs

- #95 — grilling session (2026-09-02) that settled this decision.
- #97 — post-merge review that hardened the first draft: the safe-key allowlist closing the identity denylist gap, the pin-as-requirement fix, separating integrity from safe extraction, closing the interior-of-an-admitted-key hole, and the per-platform integrity map.
- #118 — the reconcile loop is the sole restarter of Stack containers, retiring the Docker restart policy that raced tmpfs secret materialization on boot (#114).
- #132 — the #127 grilling (2026-09-04) narrowing the codex refusal to Agent Policy and opening the CLI Pin to codex decks.
