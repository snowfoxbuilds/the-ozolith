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

### Amendment (2026-08-14): the gate runs behind a real boundary

The 2026-08-13 gate had holes an adversary (a substituted model, a
booby-trapped checkout) could walk through: the gated probe turn ran in
the task checkout with full tools and `--dangerously-skip-permissions`
(only a prompt asked it not to use them), the effort proof required a
tool execution in that same environment, project hooks/CLAUDE.md loaded
before any verification, the task file sat readable on disk throughout,
a swallowed `BrokenPipe` during release could yield a "completed" Run
that never saw its task, and the identity-key scan missed
`modelOverrides` and provider-endpoint redirects. This amendment closes
them:

- **Pre-verification boundary.** The harness removes `input/prompt.md`
  from disk (held in memory) before any verification subprocess exists,
  and every verification session — widen canary, a new **same-family
  canary** for full-ID pins (family-granular enforcement would pass a
  different-family canary and still run the wrong model; aliases skip
  it), and a **neutral identity probe** — runs in a scratch directory
  outside the job mount with `--tools ""`, `--permission-mode dontAsk`,
  `--setting-sources ""` (managed policy always applies — it is what is
  under test), and `--strict-mcp-config`. Canaries and probe alike must
  **announce and execute** the pin — a stream with no init event is
  `unverifiable`, and matching strips the CLI's context-window
  decoration (`claude-opus-5[1m]` announces what `claude-opus-5`
  executes). The same-family sibling must be **genuinely distinct**:
  never an undated prefix of the pin (the provider resolves it to the
  newest dated variant — possibly the pin itself, a vacuous canary); a
  known multi-member family with no usable sibling fails
  `unverifiable`, while single-member families (fable, mythos) have no
  sibling to ask for and skip it by documented design. The applied
  effort is read from the **`Stop` hook payload**, which reports the
  post-clamp value after a plain no-tool turn (verified live), so no
  tool ever executes for the effort proof. A **process-environment
  audit** fails the Run on any identity or provider-endpoint variable
  (`ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`/`VERTEX`,
  `ANTHROPIC_DEFAULT_*_MODEL`, …) — behind a foreign endpoint the
  stream is unfalsifiable. The identity key scan adds `modelOverrides`
  (it remaps what *serves* a model ID while the allowlist sees the
  Anthropic ID). A boundary that cannot be established at all —
  scratch, task-withholding, or hook-script I/O failure — is itself an
  identity failure (`unverifiable`, step named), never a generic
  harness crash.
- **Fresh server policy.** `materialize` now writes
  `forceRemoteSettingsRefresh: true` (documented: block startup until
  server-managed settings are freshly fetched, exit on failure), so
  canaries, probe, and task all start on fresh organization policy;
  the scan type-validates it and rejects any non-`true` value anywhere
  in the managed tier, and the preflight fails `identity-inconsistent`
  on images that predate the key (rebuild required). The
  refusal-on-fetch-failure half rests on documented behavior — it
  cannot be isolated locally, but a dead settings endpoint also kills
  the model endpoint, which fails closed regardless (no executed turn,
  no release; verified by hand).
- **The task session exists only after the proof — and loads no
  non-managed settings source itself.** It launches with
  `--setting-sources ""`: no user, project, or local settings ever
  enters the one session that will hold the task, closing every initial
  checkout/home surface (`env.ANTHROPIC_BASE_URL`, Bedrock/Vertex
  switches, `modelOverrides`, model/effort selectors, and identity
  surfaces not yet invented) at once instead of scanning for the known
  ones — verified live with a booby-trapped checkout plus a positive
  control proving the trap fires without the flag. The documented cost
  (verified live): the checkout's CLAUDE.md, skills, and slash commands
  ride the project settings source and do not load either — the
  driver-rendered task file is the session's complete assignment.
  `--settings` hooks stay active under the flag: the session is still
  stdin-gated on its own announced + executed identity, its tools are
  structurally denied by a PreToolUse hook until the harness writes a
  release marker — the denial binds under
  `--dangerously-skip-permissions` (verified live) — and a
  `ConfigChange` hook records identity-relevant mid-session settings
  changes (never blocking them — organization policy is never resisted)
  with the guard killing on any record. Capture files are same-user
  state and therefore a fail-closed channel only; the release decision
  itself rides on CLI-authored transcript events plus the neutral
  preflight.
- **An unambiguous probe/task boundary.** A `Stop` hook is registered
  for **every** gated task session (effort baked or not); it fires once
  per completed turn in stream-json input mode (verified live) and
  appends one value-redacted record — the applied effort and nothing
  else — to a turn-boundary journal. Release additionally requires the
  probe's record: the probe must have **completely stopped**, never
  merely emitted its first matching assistant event. Turn counting
  rides the journal, attributed by release state at the moment each
  record lands, so a residual probe assistant event the transcript pump
  delivers late can never increment `task_turns`; when effort is baked,
  every record's applied effort is checked (the pre-release proof and
  the post-release drift monitor in one channel), and a completed turn
  with no effort observation is `unverifiable`.
- **Atomic, observable release.** Release = open the tool gate, write
  the task file back, deliver the pointer, close stdin — in that order;
  `released` is recorded only after delivery succeeds, a broken pipe is
  `task-delivery`, a failing close is `stdin-close`, and any exit (even
  0) whose journal shows no completed post-release turn fails as
  `task-unprocessed` — "task processed" means a turn attributable to
  the delivered task, never residual probe output. New categories:
  `config-changed`, `task-delivery`, `stdin-close`, `task-unprocessed`;
  `GuardDecision` carries the category end to end (identity.json,
  status, evidence), and **every** identity failure — including a
  corrupt/half-declared identity (`identity-inconsistent`) and boundary
  setup failures (`unverifiable`) — writes the redacted identity.json
  record, not only failures that reached a `PreflightReport`.
- **One end-to-end deadline.** The budget starts before the preflight:
  the CLI version probe, every canary, the identity probe, the gated
  release wait, and the task itself all run under what remains of
  `agent_timeout_seconds` (each verification step further capped by a
  per-session safety maximum). Verification never silently extends the
  task budget; exhaustion before release is `preflight-timeout` (or the
  release-wait timeout) with the task withheld; and the driver's
  container wait (agent timeout + grace) can never expire before the
  harness's own verdict is written.
- **Reviewer identity failures terminate visibly.** The Reviewer
  catches the identity-marked session failure (the marker is matched
  **anchored** — it must begin the status error — in both drivers),
  publishes the harness's identity.json and the available transcript to
  the evidence bundle with `failure_class: identity`, and routes the PR
  through the existing one-strike lane: `blocked` + `needs_human`,
  `pr_ready` removed — the PR leaves the reviewable pool instead of
  relaunching an identical doomed review every poll. Non-identity
  session breakage and invalid verdicts keep their existing behavior.
- **CLI floor 2.1.232** (was 2.1.223): the gate now also relies on the
  Stop-payload effort field, the per-turn Stop firing in stream-json
  input mode, the ConfigChange hook, the isolation flags (including
  `--setting-sources ""` suppressing project settings/CLAUDE.md/skills
  on the task session while `--settings` hooks stay active), dontAsk,
  the PreToolUse-under-skip-permissions denial, and the freshness key —
  all verified live on 2.1.232; an older CLI ignoring the freshness key
  would silently void a guarantee, so the floor turns that into
  `cli-too-old`.

What a passing gate does NOT prove, stated plainly: the canaries do not
prove the absence of conditional fallback or of server-side overrides
that trigger only later (the in-session monitor is the answer there),
and post-release capture-file monitoring is best-effort against checkout
code that tampers with same-user files (the pre-release proof and the
transcript-based kill remain intact). Wire, hashes, and the credential
contract remain unchanged; derived images must be rebuilt once so their
artifact carries the freshness key.

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
