Status: ACCEPTED — amended 2026-08-20 by ADR-0049 (a private base digest resolves at ingest via a managed `registry:<host>` pull credential; the tag-only-base / mechanical-pin doctrine is preserved — the credential is what makes it hold for a private base) (see Amendment) — amended 2026-08-26 by ADR-0051 (a source Config Repo without `product.toml` no longer deletes the pinned build's product pin: ingest carries the current pin forward — the update flow owns it unless the Config Repo declares one) (see Amendment) — amended 2026-08-26 by ADR-0052 (ingest compiles every knowledge tree once per registered compiler into `knowledge/<name>/<tool>/` with pins keyed `"<name>/<tool>"`; claude pin values are byte-stable across the layout change, legacy builds load through compat shims until the next ingest migrates them, and the node's deck export serves the claude view)

Date: 2026-08-18

Provenance: issue #62 (design discussion 2026-08-17/18).

# ADR-0048: Config ingestion — human Config Repo, machine-owned pinned build, knowledge in the Config Repo

## Context

Pins were hand-managed TOML values: the base image digest, the
knowledge git SHA, the tailscale checksum all lived as literals the
operator edited in place in the deployment config tree — the same tree
control loads and distributes. That made every promotion a manual
multi-field edit (resolve a HEAD, resolve a digest, paste, commit) and
left no structural line between what a human *decides* and what a
machine can *resolve*. Knowledge had its own seam: it lived in a
separate git repo referenced by `knowledge_source`/`knowledge_pin`,
baked into worker images at build time, while Flight Decks got it from
a shared *writable* clone per worker type per node (ADR-0043) whose
promote workflow (commit/push + re-pin + rebuild) was the only path by
which in-deck edits became durable — a second authoring surface, a
shared-mutable volume, and the crash-recovery/symlink-safety machinery
that came with it. Meanwhile ADR-0042 had already established the
cleaner pattern for behavioral content: `drivers/` lives in the config
tree, git-native, hash-pinned to nodes, web UI hands off.

## Decision

Split the config surface into two git trees with one deliberate
command between them, and move knowledge into the first.

**The Config Repo** (glossary ruling: this term now means the *human*
repo, keeping ADR-0006's meaning) is the source of truth. It is
authored and reviewed by humans, lives wherever the operator likes —
ingest accepts a local path or a git URL — and carries no computed
pins. It gains a `knowledge/` tree: per-worker-type knowledge roots
(ADR-0009 layout), referenced from worker-type definitions as
`knowledge = "knowledge/<name>"`. `knowledge_source`, `knowledge_pin`,
and the `KNOWLEDGE_GIT_TOKEN` slot are retired.

**The pinned build** is the machine-owned tree at the control data
dir's `configs/` — still a git repo, but one *only* `theozolith config
ingest` commits to. Hand edits are refused structurally (ownership/
permissions) and operationally (ingest fails on a dirty tree). It is
what config load reads and config distribution serves; its single
distribution hash is unchanged (whole-tree sync stays — per-path
distribution was rejected: one hash makes the node's worker-type +
drivers + knowledge set mutually consistent by construction, and the
tree is text-sized per the #51 ruling).

**`theozolith config ingest`** is the only path from one to the other:

- **Harvest** the Config Repo (path or git URL) at its current commit.
- **Lint** with exactly the fail-loud checks config load applies today
  (model/effort pairs, required slots, driver-module existence,
  driver⇒workspace, reserved names) — a config that would not load is
  never committed.
- **Resolve pins** where resolution is mechanical: each `knowledge/
  <name>/` tree gets a content-hash pin (so a worker type's
  instruction hash moves only when the tree *it references* changes —
  selective rebuild falls out of per-tree pins, not partial sync);
  base image tags resolve to digests. **Never** where the value must
  come out-of-band: the tailscale sha256 stays human-entered in the
  Config Repo, validated non-placeholder, never computed — an
  ingest-computed checksum just signs whatever the network served,
  deleting the supply-chain control while the field still looks
  intact. Model/effort are policy, not pins; ingest validates, never
  chooses.
- **Compile knowledge** (the ADR-0009 compiler runs here): the pinned
  build carries compiled, per-tool-layout output, ready for both
  delivery paths below. Compile errors surface at ingest, not at image
  build or container start.
- **Commit** to the pinned build with the source Config Repo commit
  SHA stamped in the commit — every pinned state maps to the human
  commit it was built from. Rollback is `git revert` on the pinned
  build: the resolved pins are decisions that exist nowhere else, so
  re-ingesting an old source commit is *not* a rollback (HEADs and
  registry tags re-resolve differently).
- **Reload**, not restart: control re-reads config in place; nodes
  converge through the existing hash ladder, and the brief off-hash
  dispatch-ineligible window is the ADR-0042 advisory-skew design
  working as intended.

**Everything goes through ingest** — `control.toml` and the settings
surface included. There is no second write path into the pinned build;
a quick ops tweak is a Config Repo edit plus an ingest, accepted
deliberately. `theozolith-control init` scaffolds both trees.

**Knowledge delivery is deliberately two-path**, split on the
HITL/HOTL line:

- **HOTL driver workers keep baking.** Knowledge is compiled output
  copied into the derived image at build; the image stands alone
  (benchmarks run it with no substrate), and the run-image tag remains
  a complete statement of what a Run executed with (ADR-0045 spirit).
  Editing one knowledge tree re-tags only the types referencing it.
- **HITL Flight Decks read-only bind-mount** the node's applied pinned
  knowledge tree. The writable clone, the promote workflow, and the
  `knowledge-<worker-type>` volume are retired; authoring happens in
  the Config Repo, and edits reach every deck on every node through
  the distribution the substrate already runs. The applied tree swaps
  by atomic rename (the #23 staged-repair machinery), so a running
  deck keeps its old inode until agent-CLI restart — restart = pick up
  new knowledge is the *stated* semantic, not an accident. Cross-node
  live mounts (NFS-shaped) are rejected: config distribution is the
  transport, per ADR-0043's no-sync-daemon doctrine.

**Per-Stack knowledge is explicitly rejected** (it would put per-Stack
state into image identity, breaking ADR-0047's rebinding-never-
rebuilds line): two Stacks needing different knowledge are two worker
types, and worker types are cheap.

## Consequences

- **Positive**: pin management becomes one deliberate command;
  promotion is mechanical where it can be and human where it must be;
  knowledge follows the `drivers/` precedent (one repo, one hash, one
  review surface); the private-knowledge-repo auth wart disappears;
  the shared-mutable deck volume and its recovery machinery retire;
  multi-node deck knowledge works with zero new transport.
- **Negative**: the deck edit loop lengthens (edit Config Repo →
  ingest → agent restart, versus edit-in-place) — deliberateness over
  ergonomics, accepted; ops tweaks to `control.toml` also go through
  ingest; `recover` doctrine must treat the pinned build as durable
  state (it is not derivable — see Amends ADR-0024).
- **Neutral**: worker-image knowledge baking, the distribution hash
  ladder, the tailscale double pin (install.sh and vendored-artifact
  alternatives were considered and rejected: unpinned versions,
  non-reproducible instruction hashes, gate-evidence drift), and
  Stacks are untouched.
- **Amends ADR-0006**: the Config Repo (human) remains the source of
  truth; the tree control loads is now its machine-built, pinned
  materialization rather than the same tree.
- **Amends ADR-0009**: the compiler's invocation point moves to
  ingest; compiled output is distributed, not compiled at bake.
- **Amends ADR-0024**: `configs/` changes role from human-managed
  source to machine-owned pinned build; it stays in the durable git
  class (its pins are underivable decisions), and recovery restores
  it as before.
- **Amends ADR-0043**: the shared writable clone, promote workflow,
  and knowledge volume are retired; the deck's knowledge surface is a
  read-only bind of the applied pinned tree. Git remains the only
  transport — now via config distribution rather than per-node
  clones.
- **Amends ADR-0044**: the Knowledge Source tuple field becomes an
  in-repo knowledge reference plus an ingest-computed per-tree pin;
  it remains identity (it still enters the instruction hash).

## Alternatives Considered

- **Pins in control's database, no git**: rejected — image identity
  would live in the deletable cache tier (ADR-0016/0024), with no
  diff, no revert, and no recovery; resolved pins exist nowhere else.
- **Ingest computes the tailscale checksum**: rejected — a fetched-
  and-hashed artifact is a self-signed pin; the value must come from
  the vendor's published checksums, out-of-band, or the field is
  theater.
- **Run containers mount knowledge too (no baking anywhere)**:
  rejected for now — images must work standalone (benchmarks), and
  the run-image tag stays a self-contained execution record; the
  HITL/HOTL split makes the two paths non-confusing.
- **Per-path distribution hashes for selective sync**: rejected —
  sync is cheap (#51 sizing); one tree hash buys node-side
  consistency for free, and selectivity belongs in rebuilds (per-tree
  pins), not transport.
- **Writable deck mount with promote killed**: rejected — edits would
  strand on one node's volume with no path to the source repo; worse
  than both the status quo and full retirement.
- **Cross-node shared live mount (NFS-shaped)**: rejected — adds an
  availability dependency to every deck and reintroduces the sync
  daemon ADR-0043 refused.

## Amendment (2026-08-20, ADR-0049 — authenticated base resolution)

- **The resolve step gains an authenticated path.** As written, base
  tag→digest resolution spoke only anonymous pull scope, so a **private**
  base failed ingest with a bare `403` (GHCR mints the anonymous token,
  then 403s the manifest HEAD). ADR-0049 threads a managed
  `registry:<host>` pull credential (a reserved-name secret in the
  existing Fernet store, value `<user>:<token>`) into `resolve_image_digest`:
  attempt 1 stays anonymous (public bases resolve with no credential), and
  the stored credential rides the token-realm request as HTTP Basic on the
  401 challenge. The mechanical-pin doctrine here is **preserved** — the
  human Config Repo still carries tag-only bases and no computed pins; the
  credential is simply what lets ingest resolve the digest of a private
  one. The digest pin (and the fleet-skew visibility and git-revert
  reproducibility that ride on it) is unchanged. See ADR-0049 for the flow,
  the scoping extension, and the node-side base pull.

## Amendment (2026-08-26, ADR-0051 — an undeclared product pin is preserved)

- **The commit step no longer round-trips the product pin destructively.**
  As written, ingest copied the source's config files verbatim and
  committed the whole staging tree, so a Config Repo without
  `product.toml` *deleted* the pin the update flow (`theozolith build`/
  `theozolith update`) had written into the pinned build — including for
  the Config Repo `theozolith init` scaffolds, which ships none. ADR-0051:
  when the source carries no `product.toml`, ingest carries the pinned
  build's current one forward into staging (preserve, never delete), with
  an explicit report note in both the real and dry-run paths. A source
  that **does** carry `product.toml` still wins, with the existing
  divergence note — declarative release pinning is unchanged. Absent in
  both trees stays absent. A present `product.toml` must be a REGULAR
  FILE: a directory, symlink, or other shape at that path is refused
  loudly (preservation must never commit into a `product.toml/`
  directory). Under a pending marker, the dry run reads the old pinned
  state — the preserved pin included — from a read-only snapshot of the
  committed HEAD, never from a worktree the interrupted ingest left
  behind. See ADR-0051.
