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
workspace (target repo), and secret names. The **Agent adapter**
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