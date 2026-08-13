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
`CLAUDE_CODE_EFFORT_LEVEL` (which overrides every effort surface).
**Mappable means enforceable**: a value the
CLI accepts but cannot be held to (Claude's `default`, `opusplan`) is
unmappable; a driverless type's `effort` is rejected until a runtime
consumer exists (interactive scope bakes only the well-known model
file); and the build fails when the in-image CLI predates the
enforcement settings, verified by an in-image version preflight. The
enforcement behavior itself is proven against a live CLI by an opt-in
worker test suite. Evidence reports the **observed** model reconciled
from all session-stream signals, surfacing remaps, fallbacks, and
multi-model sessions instead of flattening them.

### Amendment (2026-08-13): enforcement fails closed, at two gates

The baked identity can be superseded by policy the original design never
checked: Claude Code merges `managed-settings.d/*.json` drop-ins over
the base file (arrays **concatenate** — one drop-in line widens the
allowlist), a managed `policyHelper` preempts the entire managed tier,
server-managed organization settings outrank the local managed file, an
organization effort cap clamps silently in stream-json, the CLI
substitutes an unavailable model with only a suppressed stderr warning,
and an unsupported effort silently runs at the nearest supported level.
The amendment's doctrine: **a Run never receives its real task prompt
unless the effective model and effort are proven to match the baked
identity; anything unverifiable fails closed; organization policy is
never disabled, replaced, or weakened to make a Run pass.**

- **Build gate** (conflicts knowable from the image filesystem): the
  materialize step scans the base managed file and every drop-in in
  merge order and fails the build — naming the file and key — on any
  identity-affecting key (`model`, `availableModels`,
  `enforceAvailableModels`, `fallbackModel`, `effortLevel`,
  `policyHelper`/`policyHelpers`, or a model/effort-selecting `env`
  entry), on a malformed document, and on a pre-existing managed
  `effortLevel` even when the type bakes no effort (inherited settings
  must not convert `effort = ""`, the model's own default, into an
  enforced value). Conflicting operator policy is never deleted or
  overwritten; unrelated operator settings still merge and survive.
  The CLI floor rises to **2.1.223**: the per-key managed `env` merge
  it introduced is what keeps the baked effort pin alive beside a
  server-delivered org `env` block.
- **Runtime gate** (the Run's own credential and effective policy;
  there is no machine-readable effective-settings dump, so identity is
  proven behaviorally): static re-checks of the image policy and the
  managed pin's consistency with the well-known files; a **widen
  canary** (an intruder `--model` must coerce back to the pin — catches
  widened/replaced policy from any source, server-side included); then
  a **gated task session** — stdin-driven, first turn a no-op probe,
  the real pointer prompt released into the same process only after the
  init announcement and an executed turn match the baked model (exact
  for pinned IDs, family for aliases) and, when effort is baked, the
  hook-captured *applied* effort equals it. After release the harness
  monitors the stream and kills the agent on any identity drift — a
  mid-run policy change invalidates the Run immediately. Failures carry
  `failure_class: identity` and a category
  (`policy-conflict`, `identity-inconsistent`, `pair-invalid`,
  `cli-too-old`, `unavailable`, `substituted`, `policy-widened`,
  `effort-clamped`, `unverifiable`, `preflight-timeout`); evidence
  embeds the expected-vs-effective verdict (never credentials or
  settings contents). Post-run stream reconciliation remains as
  defense-in-depth, not as the gate.
- **Pair validation** replaces the global effort allowlist: `(model,
  effort)` validate together at config load, build, and preflight — an
  effort the specific model silently clamps or ignores is rejected, as
  is any effort on a model whose capability is not positively known.
  `effort = ""` stays "model default" and pins nothing.

Wire and identity are unchanged: the nodedaemon's eight-key recipe
contract, the materialize-instruction format, the instruction hash, and
model-less worker-type hashes are byte-identical; the credential
contract (`ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`, no
model-selection env vars) is untouched. The runtime gate costs a few
hundred tokens per Run (canary + probe); model-less images skip it
entirely.

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
