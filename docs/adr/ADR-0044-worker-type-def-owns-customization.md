Status: ACCEPTED — amended by ADR-0045 (2026-08-10): model and reasoning effort are typed, identity-bearing fields on the definition — the instruction hash covers the materialized model/effort config, so changing them DOES re-tag and rebuild the derived image (this ADR's "per-type variables never trigger a rebuild" clause now applies only to driver/workspace/secrets). Amended by ADR-0047 (2026-08-17, #60): workspace and secret bindings are per-Stack OVERRIDABLE — the type keeps the slot contract and optional defaults, the Stack rebinds per placement; "Tuple on the Stack" stays rejected for the identity tuple (see Amendments). Amended by ADR-0048 (2026-08-18, #62): the Knowledge Source field (git URL + pin) becomes an in-Config-Repo knowledge reference (`knowledge = "knowledge/<name>"`) with an ingest-computed per-tree content-hash pin; knowledge remains identity — it still enters the instruction hash, and only the types referencing a changed tree re-tag. Amended by ADR-0052 (2026-08-26): the adapter now also selects the knowledge bake TARGET (claude → ~/.claude, codex → ~/.codex), computed control-side and carried on the wire so the daemon stays adapter-blind; a non-default target enters the instruction hash conditionally (claude identities unchanged). Amended by ADR-0055 (2026-09-02, #95): the tuple gains an Agent Policy reference (`policy = "policy/<name>"`) and a CLI Pin (`cli = "<version|dist-tag>"`); on a driver type policy bakes and enters the instruction hash conditionally, on a driverless (Flight Deck) type both are declared, fleet-visible, and deliberately NOT identity-bearing — delivered live by node export and picked up on the next agent-CLI launch.
Date: 2026-08-09

# ADR-0044: Worker-type definition owns the customization tuple

## Context
Earlier phrasing defined a Stack as "driver + harness + workspace +
secrets". This overloaded Stack and treated the harness as a variable,
when the harness is the product's immutable PID-1 contract.

## Decision
The customization tuple lives on the **worker-type definition**: base
image + setup instructions, Knowledge Source (git URL + pin), driver
reference (`builtin:<name>` / `drivers/<name>`), Agent adapter,
workspace (target repo), and secret names. *(Amended by ADR-0047:
workspace and secret names are the type's defaults; a Stack may rebind
either per placement — see Amendments.)* The **Agent adapter**
(which one-shot CLI the harness invokes — Claude Code, Pi) is the
variable; the harness itself is immutable plumbing. A **Stack** stays
the thin generic unit: worker type + placement + desired state. The
Flight Deck worker type has the identical shape minus the driver.

## Consequences
- **Positive**: one place defines what a worker *is*; Stacks stay
  swappable placement records; "harness" stops leaking into config.
- **Negative**: one more named layer (worker-type definition) between
  Config Repo and Stack.
- **Neutral**: Flight Deck reuses the shape, keeping config uniform.

## Alternatives Considered
- **Tuple on the Stack**: rejected — duplicates the definition across
  every placement and conflates identity with scheduling.
- **Harness as a variable**: rejected — the harness is the product
  contract; only the adapter it invokes varies.

## Amendments (2026-08-17, ADR-0047 / #60)

- **Bindings vs identity.** The tuple's two per-placement members —
  workspace and each secret slot's binding — become Stack-overridable:
  the type declares the contract (slots, optional defaults), the Stack
  rebinds per placement, key-wise merge with the Stack winning. The
  identity members (base/setup, Knowledge Source, driver, adapter,
  model/effort) never move; "Tuple on the Stack" stays rejected for
  them, and Stacks remain thin placement records.
- **Empty-string semantics.** On the type, `SLOT = ""` declares a
  required slot every instantiating Stack must bind (fails at config
  load); on the Stack, `SLOT = ""` unbinds an inherited default; `""`
  for an undeclared slot is an error.
- **Workspace requirement relocated.** `driver ⇒ workspace` is
  enforced at per-Stack resolution, not type parse — a workspace-less
  driver type is a legal multi-repo template.

## Amendment (2026-09-02, ADR-0055 / #95 — Agent Policy and CLI Pin)

- **Two fields join the tuple.** `policy = "policy/<name>"` references a
  Config Repo tree of verbatim managed-settings drop-ins; `cli = "<exact
  version | npm dist-tag>"` pins the agent CLI, resolved at ingest. Both are
  definition fields (never Stack fields — per-Stack policy or CLI would put
  placement state into a type's delivered behavior, the line ADR-0047 holds).
- **Identity treatment splits on the HITL/HOTL line.** On a driver type the
  policy tree bakes into `/etc/claude-code/managed-settings.d/` and its
  content pin enters the instruction hash as a conditional key (existing
  identities unchanged); the CLI Pin is refused with a driver in v1. On a
  driverless type neither field is identity-bearing: the node exports, the
  deck mounts a stable parent, and a stable env selector resolves the entry
  — adopting, dropping, or reselecting recreates once; content or version
  changes are live. See ADR-0055.
