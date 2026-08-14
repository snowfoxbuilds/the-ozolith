Status: ACCEPTED
Date: 2026-08-10 (consolidated 2026-08-14)

# ADR-0045: Model and reasoning effort are typed fields baked into the derived image, held by best effort

## Context
Agent CLIs default to invocation-time model selection (`--model` flags,
env vars). The worker-type definition owns the customization tuple
(ADR-0044), but model and reasoning effort were expressed only inside
free-form setup instructions — invisible to tooling, unvalidated at
build, and selectable at runtime.

An earlier revision of this ADR escalated, over several amendment
rounds, into a fail-closed pre-release proof system: per-Run canaries,
a stdin-gated task session with a no-op probe turn, task-file
withholding, a release-marker tool gate, and a sealed task session that
loaded no checkout settings (losing CLAUDE.md and skills). The operator
retrospective ruled that this took "no unverified model can run"
further than the product needs, at real cost to worker capability,
per-Run latency/tokens, and availability. This consolidated revision
replaces those amendments; the exploration is preserved in the PR #40
history.

## Decision

Model and reasoning effort are first-class **typed fields** on the
worker-type definition, validated at config load against the adapter's
declared capability and **materialized into the adapter's native
configuration at derived-image build time**; they are never selected at
invocation time and never delivered as env vars. The instruction hash
(derived image tag) covers the materialized config. Convention: pin the
most-dated provider model ID over floating aliases. The Flight Deck
bakes a default model; in-session switching is session state, not
definition.

The identity is held **by best effort, failing loud on detection**:

- **Selection** makes the right identity happen. The Claude adapter
  writes a managed `model` **session default** for the MAIN agent — the
  managed tier outranks the checkout's project/local settings and the
  user tier for the same key (verified live), the harness passes no
  `--model`, and a process-environment audit rejects steering variables
  (`ANTHROPIC_MODEL`, `ANTHROPIC_BASE_URL`, provider switches,
  `ANTHROPIC_DEFAULT_*_MODEL`, …). Effort is pinned with the managed-env
  `CLAUDE_CODE_EFFORT_LEVEL`, which overrides every effort surface
  (verified live, including survival of the per-key managed env merge
  beside foreign drop-in env blocks).
- **Enforcement is main-agent-only.** Deliberately NO
  `availableModels` allowlist: subagents run their declared frontmatter
  models, skills route freely, and the CLI's background helpers use
  their own small models — all verified live as capabilities, not
  escapes. Every identity check scopes to main-agent stream events
  (`parent_tool_use_id` absent); a skill that switches the MAIN
  thread's model will fail the Run — route cheap/heavy work through
  subagents.
- **Detection fails loud.** The harness runs zero-cost static checks
  before every launch (managed selection consistent with the well-known
  `/etc/theozolith/model`/`effort` files, no superseding managed
  policy, valid pair, clean environment — file reads only), then
  launches the task session **normally**: pointer prompt in the argv,
  task file on disk, checkout CLAUDE.md, skills, and settings loading
  as they always did. A monitor reads the stream and kills the session
  on a POSITIVE detection: a main-agent turn executing off the baked
  model, an off-identity init announcement, or an identity-relevant
  mid-session settings change recorded by a ConfigChange hook (which
  records, never blocks — organization policy is never resisted). After
  exit, the last applied-effort observation from a Stop-hook journal is
  checked; a detected clamp fails the Run (`effort-clamped`).
  Identity comparisons normalize the CLI's context-window decoration
  (`claude-opus-5[1m]` announces what `claude-opus-5` executes).
- **Gaps are recorded, not failed.** A missing observation — no init
  event, no main-agent turn signal, a Stop hook that produced no
  record — is a gap noted in the evidence record, never a failed or
  blocked Run. Only detected mismatches fail.
- **One setup dry-run, never a per-Run probe.** The driver runs an
  `identity-dryrun` container once per driver process per run image
  before taking any work: static checks, the CLI version floor
  (`MIN_ENFORCING_CLI`, 2.1.232 — the managed-default precedence, the
  per-key env merge, the Stop-payload effort field, and the
  ConfigChange hook are all verified live there), and ONE neutral
  no-tool probe session that must announce and execute the baked model
  and report the baked applied effort. A broken image/credential/policy
  combination fails loud at setup in seconds — a dry-run VERDICT
  **latches** the driver: no work is fetched and the probe is never
  re-spent, the reason is reported to the Control Node's error feed
  (re-sent until it lands), and the operator retries by restarting the
  driver after the fix. Only a dry-run that delivered no verdict — it
  could not execute at all, or the session broke without the anchored
  identity marker (plausibly transient either way) — is retried, with
  backoff. The dry-run is strict
  (a probe with no signal is a broken observation channel); the per-Run
  monitor is lenient (gaps recorded). Its dot-prefixed job dir is
  invisible to the evidence sweep and queue-behind.

**Mappable means enforceable-by-selection**: a value the CLI accepts
but that names no single checkable model (`default`, `opusplan`) is
unmappable and fails config load and build. `(model, effort)` validate
together at config load, build, and runtime — an effort the specific
model silently clamps or ignores (xhigh on the 4.6 generation, anything
on haiku) is rejected, as is any effort on a model whose capability is
not positively known; `effort = ""` is the model's own default and pins
nothing. Driverless (Flight Deck) `effort` is rejected until a runtime
consumer exists; interactive scope bakes only the well-known model
file. The build fails when the in-image CLI predates the floor.

**The build never overwrites operator policy.** Managed-scope
materialization scans the base managed file and every
`managed-settings.d/*.json` drop-in in Claude Code's merge order and
fails the build — naming the file and key — on any identity-affecting
key (`model`, `availableModels`, `enforceAvailableModels`,
`fallbackModel`, `effortLevel`, `modelOverrides`, a policy helper, or a
model/effort/endpoint-selecting `env` entry) or a malformed document.
Unrelated operator settings merge and survive.

**Failures classify distinctly.** Identity failures carry
`failure_class: "identity"` with a stable category (`policy-conflict`,
`identity-inconsistent`, `pair-invalid`, `cli-too-old`, `unavailable`,
`substituted`, `effort-clamped`, `unverifiable`, `preflight-timeout`,
`config-changed`) and a redacted `identity.json` evidence record
(expected/observed model and effort, check status, violation, gap
notes — categories and names only, never credentials or settings
contents). The marker is matched **anchored** in both drivers. The
Reviewer routes identity-failed review sessions through its one-strike
evidence-first lane (blocked + needs_human, evidence retained) so the
PR leaves the reviewable pool instead of retrying every poll. (Skipping
the Implementer's local retry for identity failures is #42 — an
ADR-0016 amendment.)

Evidence reports the **observed** model reconciled from the stream's
main-agent signals, surfacing remaps and drift in `model_note` instead
of flattening them; subagent and helper models are legitimate and
excluded from identity reconciliation.

## Known, accepted gaps (the deliberate loosening)

Stated plainly, per the operator ruling:

- A checkout-committed endpoint redirect (`env.ANTHROPIC_BASE_URL` in
  project settings) could make the stream's self-reported identity
  unfalsifiable. Ruled out of the threat model: run containers are
  credential-limited (ADR-0013) and checkouts are the operator's own
  repositories.
- `--model` beats the managed default (verified live). The harness
  never passes it; anything else that does produces an off-identity
  main-agent turn the monitor kills.
- Org-policy drift between the setup dry-run and a Run is caught by the
  Run's own turn stamps (fail loud), not preflighted.
- A wrong-identity Run spends tokens until its first detected
  main-agent turn (normally the first turn).

## Consequences
- **Positive**: the image is bound to the worker definition — model
  changes are visible as definition diffs; selecting an unsupported
  model fails at build; a wrong effective identity fails loud with
  evidence instead of running silently; Runs pay no per-Run
  verification cost (the dry-run is per driver boot); workers keep
  checkout CLAUDE.md, skills, and hooks; subagent/helper model routing
  and compaction are unconstrained.
- **Negative**: sweeping N models requires N derived-image builds; the
  identity guarantee is detection, not prevention — the gaps above are
  accepted.
- **Neutral**: images built by the earlier fail-closed revision (with
  the allowlist and `forceRemoteSettingsRefresh`) stay tolerated — they
  are stricter (subagents pinned) until rebuilt; tags are unchanged
  (the instruction format never changed). Adapter version and run date
  remain uncontrollable run metadata, recorded but never
  identity-bearing.

## Alternatives Considered
- **Env var / invocation flag**: rejected — reintroduces a selection
  surface, decouples the image from the definition, and permits
  unsupported models at runtime.
- **Model named in setup-instruction prose**: rejected — unvalidated,
  invisible to tooling, unmappable across adapters.
- **Fail-closed pre-release proof (the superseded revision)**: per-Run
  canaries, a gated stdin session with a probe turn, task withholding,
  a sealed settings-source-free task session. Rejected by operator
  ruling: the marginal guarantee over detect-and-kill did not justify
  losing checkout CLAUDE.md/skills, constraining subagents and
  background helpers, per-Run token/latency cost, and a hard
  availability coupling to the settings endpoint
  (`forceRemoteSettingsRefresh`).
- **Network/credential-layer enforcement** (an egress gateway asserting
  the model per request, or provider-side key restrictions): a stronger
  guarantee than any behavioral check, but unavailable for OAuth
  subscription credentials and out of scope for the current
  single-operator deployment; revisit if benchmark-grade identity
  integrity becomes load-bearing.
