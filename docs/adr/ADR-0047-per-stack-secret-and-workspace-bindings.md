Status: ACCEPTED

Date: 2026-08-17

Provenance: issue #60.

# ADR-0047: Per-Stack secret and workspace bindings — the type declares the contract, the Stack binds the placement

## Context

Two Stacks of one worker type could not bind different credentials or
different target repositories: `[secrets]` and `workspace` were
worker-type fields (ADR-0044), so the only path to a distinct GitHub
machine account, agent-CLI account, or repo per placement was cloning
the whole worker type — duplicating base/setup/knowledge/model/adapter
just to repoint one token, and a second derived-image tag for nothing
(neither field enters the instruction hash). The cases this blocked:
implementer/reviewer Stacks per target repository each acting as its
own repo-scoped machine account (the primary motivation), per-account
usage-limit gating (#58: "if nodes use distinct accounts, gate each
independently"), and the Flight Deck enrollment-key hardening, which
could only drop `TS_AUTHKEY` for every instance of the type at once.

## Decision

The worker type keeps the **contract** — which secret slots exist,
each with an optional default binding, and an optional default
workspace. The Stack owns the **binding** — which stored secret backs
each slot and which repository this placement works. Key-wise merge at
resolution, the Stack wins. Names only, as ever: values never enter
the Config Repo (ADR-0006/0024), and the store stays name-keyed and
fleet-global — per-Stack distinctness comes from pointing at different
store names.

- **Empty-string semantics, zero new syntax.** On the type,
  `SLOT = ""` declares a required slot: every instantiating Stack must
  bind it, enforced at config load on the Control Node — never the
  silent deploy-time 404 an empty value used to produce. On the Stack,
  `SLOT = ""` unbinds an inherited default (the per-instance
  `TS_AUTHKEY` removal); `""` for a slot the type never declares is an
  error (typo catch). Empty entries never reach the resolved Stack, so
  node scoping (`secret_names_for`) and the daemon only ever see real
  names.
- **Workspace requirement relocated.** `driver ⇒ workspace` moves from
  type parse to per-Stack resolution: a workspace-less driver type is
  a legal multi-repo template, and each instantiating Stack must
  resolve a workspace (its own or the type default) or config load
  fails naming both files. Same owner/name shape check on both files.
- **Reserved slot names.** A secret slot materializes `<SLOT>_FILE`,
  which the worker's env reader takes first — so the identity names
  the worker-type-Stack `[env]` guard rejects (ADR-0045) are rejected
  as slot names too, at both declaration sites (the type-side hole
  existed before this ADR).
- **Unchanged:** Stack `[env]` (including the `THEOZOLITH_REPO` expert
  override, which still wins last); the wire format (nodes see the
  resolved mapping and env only); the node-scoping rule, which already
  unions resolved Stack bindings; the instruction hash — rebinding
  never rebuilds an image, and the `<ENV>_FILE` fingerprint already
  recycles the one driver process on a rebinding; generic Stacks'
  `[secrets]`.

## Consequences

- **Positive**: distinct identities and repos per placement with one
  worker type, no type cloning, no rebuilds; enrollment keys removable
  per instance; the empty-value deploy-time trap becomes a fail-loud
  load error.
- **Negative**: one merge rule to learn; `""` is now meaningful in a
  `[secrets]` table (any config that carried one was undeployable
  before, so nothing working changes behavior).
- **Neutral**: the node daemon is untouched; most Stacks stay three
  lines — bindings are opt-in.
- **Amends ADR-0044**: its "Tuple on the Stack — rejected" alternative
  stands for the *identity* tuple — base/setup, Knowledge Source,
  driver, adapter, model/effort never move, and Stacks never duplicate
  a definition. What moves is the per-placement *binding*: which
  stored secret backs a slot, which repo this placement works.
  Identity stays on the type; bindings are placement data — exactly
  the split ADR-0045 already drew for the image ("per-type variables
  never trigger a rebuild" applies only to driver/workspace/secrets).

## Alternatives Considered

- **Full move (secrets/workspace leave the type entirely)**: rejected —
  every Stack re-declares the complete mapping, the "which secrets does
  this worker need" contract disappears from the type, and existing
  configs break for a case the override already covers.
- **Per-Stack values under one store name**: rejected — the store stays
  name-keyed and global; distinctness by name keeps entry, rotation,
  and audit one-value-per-name.
- **A required-slot keyword (e.g. `required = true`)**: rejected — the
  empty string already says it, and a second spelling of "no default"
  invites drift.
