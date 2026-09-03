Status: ACCEPTED

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
either per placement — see Amendments.)* *(Amended 2026-09-03, #120:
the tuple also declares what the worker does — kind `on`, Intake,
outputs with an Outcome Table and label groups, the prompt reference,
`rounds`, and `chain_on`; see Amendments.)* The **Agent adapter**
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

## Amendments

- **2026-08-10**: model and reasoning effort are typed, identity-bearing
  fields on the definition — the instruction hash covers the materialized
  model/effort config, so changing them DOES re-tag and rebuild the
  derived image (this ADR's "per-type variables never trigger a rebuild"
  clause now applies only to driver/workspace/secrets). See ADR-0045.
- **2026-08-17 (#60)**: workspace and secret bindings are per-Stack
  OVERRIDABLE — the type keeps the slot contract and optional defaults,
  the Stack rebinds per placement; "Tuple on the Stack" stays rejected
  for the identity tuple. See ADR-0047.
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
- **2026-08-18 (#62)**: the Knowledge Source field (git URL + pin)
  becomes an in-Config-Repo knowledge reference
  (`knowledge = "knowledge/<name>"`) with an ingest-computed per-tree
  content-hash pin; knowledge remains identity — it still enters the
  instruction hash, and only the types referencing a changed tree
  re-tag. See ADR-0048.
- **2026-08-26**: the adapter now also selects the knowledge bake
  TARGET (claude → ~/.claude, codex → ~/.codex), computed control-side
  and carried on the wire so the daemon stays adapter-blind; a
  non-default target enters the instruction hash conditionally (claude
  identities unchanged). See ADR-0052.
- **2026-09-02 (#95)**: the tuple gains an Agent Policy reference
  (`policy = "policy/<name>"`) and a CLI Pin (`cli = "<version|dist-tag>"`);
  on a driver type policy bakes and enters the instruction hash
  conditionally, on a driverless (Flight Deck) type both are declared,
  fleet-visible, and deliberately NOT identity-bearing — delivered live
  by node export and picked up on the next agent-CLI launch. See
  ADR-0055.
  - **Two fields join the tuple.** `policy = "policy/<name>"` references
    a Config Repo tree of verbatim managed-settings drop-ins;
    `cli = "<exact version | npm dist-tag>"` pins the agent CLI, resolved
    at ingest. Both are definition fields (never Stack fields — per-Stack
    policy or CLI would put placement state into a type's delivered
    behavior, the line ADR-0047 holds).
  - **Identity treatment splits on the HITL/HOTL line.** On a driver
    type the policy tree bakes into `/etc/claude-code/managed-settings.d/`
    and its content pin enters the instruction hash as a conditional key
    (existing identities unchanged); the CLI Pin is refused with a
    driver in v1. On a driverless type neither field is identity-bearing:
    the node exports, the deck mounts a stable parent, and a stable env
    selector resolves the entry — adopting or dropping a field, or
    reselecting the policy tree, recreates once; a policy content edit
    or CLI version change is live at the next agent-CLI launch (the CLI
    pin-strict: new launches refuse until the exact pinned version has
    converged on the node).
- **2026-09-03 (#120)**: the tuple gains the declarative worker fields
  (ADR-0057): `on = "issue" | "pr"` (Issue Worker / PR Worker),
  `[intake] requires / excludes / one_of / consumes`, `[output] fields`
  (allowlist over issue_body, issue_comment, issue_labels, pr_title,
  pr_body, pr_contents, pr_labels, pr_comment, pr_resume_point),
  `[output.outcome]` (the Outcome Table), `[output.pr_labels]` /
  `[output.issue_labels]` one_of groups, `[output.mirror] issue_to_pr`,
  `prompt = "prompts/<name>.md"`, `rounds` (PR Workers, required), and
  `[chain_on]`. Unknown definition keys are refused at ingest (as Stack
  keys already are). Identity treatment: the **prompt** is
  identity-bearing — its content hash enters the instruction hash, so a
  different prompt is a different candidate (ADR-0054) — even though it
  is delivered driver-side through the Config Distribution, never baked;
  the routing and behaviour fields (`on`, intake, output, `rounds`,
  `chain_on`) join the driver/workspace/secrets class: they change what
  the driver does, not the image, and trigger no rebuild. Implementer,
  Reviewer, and Initializer become shipped default definitions. Reason:
  worker behaviour moved from code (ADR-0020 subclasses) into the
  definition so new worker types are config.

## Relevant PRs

- #60 — the ADR-0047 amendment making workspace and secret bindings
  per-Stack overridable.
- #62 — the ADR-0048 amendment moving the Knowledge Source field to an
  in-Config-Repo reference with ingest-computed per-tree pins.
- #95 — the ADR-0055 amendment adding the Agent Policy reference and
  CLI Pin fields to the tuple.
