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

Materialization must write **enforcement, not defaults**: a config key
the agent CLI treats as a starting selection (Claude's managed `model` /
`effortLevel`) does not bind the identity — the session can steer away
from it. The Claude adapter therefore pins the model with a single-entry
`availableModels` allowlist plus `enforceAvailableModels` (constraining
every selection surface: flags, env vars, settings files, in-session
switching, subagent frontmatter) and pins effort with the managed-env
`CLAUDE_CODE_EFFORT_LEVEL` (which overrides every effort surface),
deep-merging into any operator-written managed settings with the
identity keys authoritative. **Mappable means enforceable**: a value the
CLI accepts but cannot be held to (Claude's `default`, `opusplan`) is
unmappable; a driverless type's `effort` is rejected until a runtime
consumer exists (interactive scope bakes only the well-known model
file); and the build fails when the in-image CLI predates the
enforcement settings, verified by an in-image version preflight. The
enforcement behavior itself is proven against a live CLI by an opt-in
worker test suite. Evidence reports the **observed** model reconciled
from all session-stream signals, surfacing remaps, fallbacks, and
multi-model sessions instead of flattening them.

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
