Status: ACCEPTED
Date: 2026-08-10

# ADR-0045: Model and reasoning effort are typed fields baked into the derived image

## Context
Agent CLIs default to invocation-time model selection (`--model` flags,
env vars). The worker-type definition owns the customization tuple
(ADR-0044), but model and reasoning effort were expressed only inside
free-form setup instructions — invisible to tooling, unvalidated at
build, and selectable at runtime.

## Decision
Model and reasoning effort are first-class **typed fields** on the
worker-type definition. The compiler **materializes them into the
adapter's native configuration at derived-image build time**; they are
never selected at invocation time and never delivered as env vars.
Adapters declare which models and effort values they can map; a build
with an unmappable value **fails**. The instruction hash (derived image
tag) covers the materialized model/effort config. Convention: pin the
most-dated provider model ID over floating aliases. The Flight Deck
bakes a default model; in-session switching is session state, not
definition.

## Consequences
- **Positive**: the image is bound to the worker definition — for
  benchmarking, candidate identity ≈ image identity; selecting an
  unsupported model is impossible by construction; model changes are
  visible as definition diffs.
- **Negative**: sweeping N models requires N derived-image builds.
- **Neutral**: adapter version and run date remain uncontrollable run
  metadata, recorded but never identity-bearing.

## Alternatives Considered
- **Env var / invocation flag**: rejected — reintroduces a selection
  surface, decouples the image from the definition, and permits
  unsupported models at runtime.
- **Model named in setup-instruction prose**: rejected — unvalidated,
  invisible to tooling, unmappable across adapters.
