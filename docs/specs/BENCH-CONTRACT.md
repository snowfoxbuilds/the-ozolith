Status: DRAFT

Last updated: 2026-08-28

# Bench Contract

TheOzolith's published export surface for benchmarking worker types off-fleet: the Candidate Bundle format, the candidate identity spec, the standalone build path, and the Run Contract an external bench harness replays for implementer and review modes.

## Context

SilverquiLLM-bench adopts the ruling that a Benchmark Candidate IS a worker-type definition (SilverquiLLM-bench#39): benchmarking and deployment consume the same derived image, so benchmark evidence transfers to deployment by construction. That guarantee only holds if TheOzolith publishes — and holds stable — the surfaces the bench harness consumes. This spec is that contract (grilling 2026-08-28; ADR-0054).

## Design

### Contract surface and versioning

The contract has four parts: the Candidate Bundle format, the identity spec (with verifier and golden vectors), the standalone build path, and the Run Contract (job directory plus exposed driver behaviors) for both run kinds. TheOzolith owns all four; the bench side owns benchmark modes, task synthesis, evaluation, results, and scheduling — nothing bench-side is ever load-bearing for the pipeline (grilling 2026-08-28).

Versioning promise (grilling 2026-08-28): visibility, not immutability. No part of this contract ever changes behavior silently — every breaking change bumps the relevant version and lands a changelog entry in this spec.

- The job manifest's integer `schema_version` (currently 1) is the public compatibility key for the whole Run Contract: job-dir layout, Output Proposal schemas, and the exposed entry points below. The format-output CLI already asserts it on every invocation; bench records it per run.
- The bundle format and the identity-hash spec carry their own independent versions, stamped in `candidate.json`.
- There are no compatibility windows: a consumer pins a version and re-syncs when it upgrades.

### Candidate Bundle

The Candidate Bundle is the self-contained export of one worker-type definition — the only form in which a candidate leaves a deployment context (see the glossary term; ADR-0054).

- Producer: `theozolith candidate export --source <path-or-git-url> --type <worker-type> --out <dir>`. The source is any config-repo-shaped tree (`worker-types/` + `knowledge/`); export runs the same resolution machinery as `theozolith config ingest` — per-tool knowledge compilation, base tag→digest resolution, pin computation, instruction hash — but writes a bundle and touches no Pinned Build (grilling 2026-08-28). Benchmark variants never enter deployment config history; equivalence with a deployed image is proven by identity-hash equality, not shared provenance. A private base image resolves with a credential passed by flag or env (the Fernet store is absent off the Control Node); the public first-party base resolves anonymously.
- Contents:
  - `candidate.json` — the machine manifest: worker-type name, adapter, base ref + resolved digest, setup instructions, model, effort, knowledge ref + pin + target, the computed instruction hash, and non-identity metadata (driver reference, secret slot names, product version, bundle-format and identity-spec versions, export timestamp).
  - The compiled knowledge tree for the candidate's adapter tool only — byte-identical to what the derived image `COPY`s into the agent home, verifiable against the knowledge pin with the published tree-hash function. The raw knowledge source is never vendored (grilling 2026-08-28).
  - A generated `Dockerfile`, emitted by the same codegen the Node Daemon uses — never a reimplementation.
- Secret slot names travel so a consumer knows what to bind; secret values never do.

### Candidate identity

Candidate identity is the triple (base image digest, instruction hash, adapter name) (SilverquiLLM-bench#39; adapter name ruled sufficient — adapter/CLI version is run metadata, grilling 2026-08-28). The instruction hash is the production formula: sha256 over the canonical JSON of base, materialized setup (operator setup plus the synthesized model/effort materialize instruction), knowledge ref, knowledge pin, and — only when non-default — knowledge target (ADR-0045/0052). Excluded from identity: driver reference, workspace, secret slots, worker-type name.

- A candidate is kind-agnostic: nothing in the bundle or its identity says implementer or reviewer. The run kind is a benchmark-mode property, mechanically real because the job manifest's `mode` field alone selects the Output Proposal schema — the same derived image executes either kind (grilling 2026-08-28).
- The identity spec is published with golden vectors (promoted from the existing control-side golden-hash tests) so any consumer can implement verification independently.
- `theozolith candidate verify <bundle>` recomputes the triple from bundle bytes — re-hash the knowledge tree against the pin, recompute the materialized setup and instruction hash — and refuses on any mismatch. A recorded identity is never trusted; it is a convenience the verifier checks (SilverquiLLM-bench#39 ruling, upheld).

### Standalone build

The bundle is a plain docker build context: `docker build` on the bundle directory is the standalone build, with a thin wrapper that first re-verifies the knowledge tree hash against the pin (the same gate the Node Daemon applies before a fleet build) and applies the deterministic tag (grilling 2026-08-28). No deployment machinery — Control Node, Node Daemon, heartbeats — participates. Fidelity comes from shared Dockerfile codegen, not from a second builder.

### Run Contract — implementer mode

A bench implementer run is a production Implementer Run with the driver replaced by the bench harness. The bench driver materializes the same job directory the production driver would — manifest (`mode: run`, stamped `schema_version`), `input/prompt.md`, `input/issue.json`, the Context Tree for a synthetic GitHub-style issue — launches the container with the in-image agent harness as PID 1, and applies the Output Proposal after process exit exactly as the production driver would (SilverquiLLM-bench#39: full-fidelity imitation).

- The scaffolding prompt defaults to the production renderer, called through the exposed entry point below — a deviating prompt template is a benchmark-mode variable, recorded on the mode, never on the candidate (grilling 2026-08-28). The task itself arrives as the synthetic issue body plus Context Tree, exactly as production workers receive work.
- The first-party gate runs (grilling 2026-08-28): a bench run without test→docs→lint measures a different thing than what deploys. The bench driver replays the production gate-step sequence over the jobs channel (`input/jobs/` ↔ `output/jobs/`); consequence for benchmark authoring, not for this contract — task repos must be gate-runnable.
- Output: the implementer Output Proposal (pr-title, pr-description, decisions, commit-message, and the rest of the mode's fields), validated by the same rules the production driver applies.

### Run Contract — review mode

A bench review run is a production Review Run under the workspace-parity shape: sanitized PR-branch checkout with history, Context Tree parity (`input/issue/`, `input/pr/` including `base.md`, `changed-files.md`, `signals.md`), the judging agent running `git diff` itself.

- Constructibility requirement (grilling 2026-08-28): a Review Run job directory must be fully constructible from a synthetic issue, a git branch, and an implementer-contract output — no live GitHub PR. The bench driver composes `input/pr/body.md` from an implementer proposal exactly as the production driver composes a PR body (Closes line, narrative, Decisions Section), computes `base.md`/`changed-files.md`/`signals.md` from git, and leaves conversation surfaces empty for a first-round review. This one shape serves both bench styles — fixtures with planted known deficiencies, and replay-review of an implementer bench run's output.
- Round pinning (grilling 2026-08-28): bench review runs use `round: 1, round_budget: 3` — production first-round conditions. On the final round `revise` is forbidden at write time, which would distort a benchmarked reviewer's verdict distribution.
- Output: the reviewer Output Proposal unchanged — verdict enum, evidence prose, deviation and risk grades, revised-plan/resume-commit on revise. No structured findings field is added for benchmarking (grilling 2026-08-28): the evaluation side reads prose regardless, and the production contract is measured as deployed. Note the vocabulary: mechanical signals are driver-computed *inputs* to the judge, never reviewer output.
- Evaluation — whether the review found the known deficiencies, judged by another LLM over the proposal — is bench-side and out of this contract's scope (grilling 2026-08-28).

### Exposed entry points

Driver behaviors the bench harness must reproduce byte-comparably are exposed as stable entry points in the worker package's public API rather than copied (grilling 2026-08-28) — hand-rolled copies would drift silently as production templates evolve:

- Prompt renderers for both run kinds: issue fields + round + flags in, `input/prompt.md` bytes out.
- PR-body composition: implementer proposal in, the PR body the production driver would publish out.
- The gate-step sequence the production driver runs.
- `theozolith candidate export` / `verify` (Control-package CLI — the resolution machinery lives there; the monorepo is public and pip-installable).

All are covered by the `schema_version` promise above.

## Decision history

Settled rulings are integrated into the sections above; decision records live in docs/adr/ (ADR-0054 for this contract), and the bench-side consumption plan is SilverquiLLM-bench#39. Implementation is tracked in the-ozolith#88.
