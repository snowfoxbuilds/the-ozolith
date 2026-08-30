Status: DRAFT

Last updated: 2026-08-28

# Bench Contract

TheOzolith's published export surface for benchmarking worker types off-fleet: the Candidate Bundle format, the candidate identity spec, the standalone build path, and the Run Contract an external bench harness replays for implementer and review modes.

## Context

SilverquiLLM-bench adopts the ruling that a Benchmark Candidate IS a worker-type definition (SilverquiLLM-bench#39): benchmarking and deployment consume the same derived image, so benchmark evidence transfers to deployment by construction. That guarantee only holds if TheOzolith publishes — and holds stable — the surfaces the bench harness consumes. This spec is that contract (grilling 2026-08-28; ADR-0054).

## Design

### Contract surface and versioning

The contract has four parts: the Candidate Bundle format, the identity spec (with verifier and golden vectors), the standalone build path, and the Run Contract (job directory plus exposed driver behaviors) for both run kinds. TheOzolith owns all four; the bench side owns benchmark modes, task synthesis, evaluation, results, and scheduling — nothing bench-side is ever load-bearing for the pipeline (grilling 2026-08-28).

Versioning promise (grilling 2026-08-28): visibility, not immutability. No part of this contract ever changes behavior silently — every breaking change bumps the compatibility key that owns the changed surface and lands an entry in the Changelog below. There are no compatibility windows: a consumer pins a version and re-syncs when it upgrades.

Three compatibility keys own the entire contract; every public surface maps to exactly one of them (review 2026-08-28):

- **`schema_version`** (currently 1, stamped in the job manifest) owns the Run Contract: job-dir layout, the Output Proposal schemas for both modes, the production prompt-renderer entry points, the PR-body-composition entry point, the gate entry points, proposal validation, and every other exposed Run behavior. The format-output CLI already asserts it on every invocation; bench records it per run.
- **`bundle_format_version`** (currently 1, stamped in `candidate.json`) owns the Candidate Bundle: the `candidate.json` schema, the required-and-allowed set of build-context entries, export output, bundle-structure verification, and verified standalone-build behavior.
- **`identity_spec_version`** (currently 1, stamped in `candidate.json`) owns candidate identity: the canonical serialization, the materialized-setup inputs, the conditional `knowledge_target` key, the knowledge-tree-hash function, and the identity-triple computation.

CLI invocation syntax (`theozolith candidate …` commands, flags, arguments) is governed by the product's public CLI and version policy and its changelog, not by these keys: a CLI-only syntax change bumps no contract version, while any change to the emitted bundle or to identity semantics bumps `bundle_format_version` or `identity_spec_version` respectively.

### Candidate Bundle

The Candidate Bundle is the self-contained export of one worker-type definition — the only form in which a candidate leaves a deployment context (see the glossary term; ADR-0054).

- Producer: `theozolith candidate export --source <dir> --type <worker-type> --out <dir>`. The source is an already-materialized **local** config-repo-shaped directory (`worker-types/` + `knowledge/`) — v1 accepts nothing else (review 2026-08-28): URLs and non-directory sources are rejected with a clear error, and remote Git fetching, Git authentication, ref resolution, submodules, and LFS are explicit non-goals. Callers resolve and clone repositories themselves — preferably at an immutable commit — and pass the resulting tree. Export runs the same resolution machinery as `theozolith config ingest` — per-tool knowledge compilation, base tag→digest resolution, pin computation, instruction hash — but writes a bundle and touches no Pinned Build (grilling 2026-08-28). Benchmark variants never enter deployment config history; equivalence with a deployed image is proven by identity-hash equality, not shared provenance.
- Private-base credentials ride the ADR-0049 Docker-compatible model, never argv (review 2026-08-28): export discovers the pull credential from a caller-supplied `DOCKER_CONFIG` directory — static auths or a credential helper — since the Fernet store is absent off the Control Node; the public first-party base resolves through the anonymous fast path with no credential at all. The credential serves exactly two operations — base digest resolution at export and the private-base pull at build — and is never copied into `candidate.json`, the bundle, a temporary build context, image layers, logs, diagnostics, or evidence. A credential failure names the registry host and the remediation, never usernames, tokens, authorization headers, Docker config contents, or credential-helper output. Temporary credential material a caller stages is the caller's to create with owner-only permissions and to remove afterwards — there is no second credential store.
- Contents:
  - `candidate.json` — the machine manifest: worker-type name, adapter, base ref + resolved digest, setup instructions, model, effort, knowledge ref + pin + target, the computed instruction hash, and non-identity metadata (driver reference, secret slot names, product version, bundle-format and identity-spec versions, export timestamp).
  - The compiled knowledge tree for the candidate's adapter tool only — byte-identical to what the derived image `COPY`s into the agent home, verifiable against the knowledge pin with the published tree-hash function. The raw knowledge source is never vendored (grilling 2026-08-28).
  - A generated `Dockerfile`, emitted by the same codegen the Node Daemon uses — never a reimplementation.
- Secret slot names travel so a consumer knows what to bind; secret values never do.

### Candidate identity

Candidate identity is the triple (base image digest, instruction hash, adapter name) (SilverquiLLM-bench#39; adapter name ruled sufficient — adapter/CLI version is run metadata, grilling 2026-08-28). The instruction hash is the production formula: sha256 over the canonical JSON of base, materialized setup (operator setup plus the synthesized model/effort materialize instruction), knowledge ref, knowledge pin, and — only when non-default — knowledge target (ADR-0045/0052). Excluded from identity: driver reference, workspace, secret slots, worker-type name.

- A candidate is kind-agnostic: nothing in the bundle or its identity says implementer or reviewer. The run kind is a benchmark-mode property, mechanically real because the job manifest's `mode` field alone selects the Output Proposal schema — the same derived image executes either kind (grilling 2026-08-28).
- The identity spec is published with golden vectors (promoted from the existing control-side golden-hash tests) so any consumer can implement verification independently: [bench-identity-vectors.json](bench-identity-vectors.json), the machine-readable vectors file carrying the canonical serialization rules and, per vector, the input fields with every expected value (canonical identity string, instruction hash, identity triple, deterministic tag). The conformance tests recompute each vector through the production formula, so the file cannot drift from the code it documents.
- A recorded identity is never trusted; it is a convenience the verifier checks (SilverquiLLM-bench#39 ruling, upheld). Verification is the full build-context authentication below, not an identity-field comparison.

### Bundle verification

`theozolith candidate verify <bundle>` authenticates the complete executable build context (review 2026-08-28): every bundle byte capable of affecting the built image is either recomputed from verified inputs or refused. Verification, in order:

1. Strictly parse `candidate.json`: missing, malformed, or unknown fields are refused per the stamped `bundle_format_version`; unsupported bundle or identity versions are refused outright.
2. Recompute the compiled knowledge tree's pin from bundle bytes with the published tree-hash function (an empty knowledge ref means no knowledge tree may be present).
3. Recompute the materialized setup, the instruction hash, base-digest field consistency, adapter identity, and the candidate identity triple.
4. Reconstruct the production wire recipe from the verified manifest.
5. Regenerate the complete Dockerfile through the same shared codegen the Node Daemon uses and require an exact byte match with the bundled Dockerfile.
6. Validate the bundle layout against the format's exact allowlist of entries and file types: unexpected entries, symlinks, path traversal, special files — anything that could alter or escape the build context — are refused.

A modified Dockerfile, changed `FROM`, added `RUN`, missing setup instruction, changed knowledge target, modified knowledge byte, unexpected file, or malformed manifest fails verification before Docker is ever invoked.

### Standalone build

The supported standalone build is the verify-and-build wrapper, and it operates on a private staged snapshot (review 2026-08-28): copy the bundle safely into private staging, run the full verification above on that snapshot, `docker build` that same snapshot, apply the deterministic tag only after verification succeeds, and clean the snapshot up on success, failure, and interruption alike. The snapshot closes the verify-then-mutate race — nothing can change between what was verified and what is built.

Raw `docker build` on the bundle directory remains mechanically possible — the bundle is a plain build context — but it is not an identity-verified build and must never produce trusted benchmark evidence; the wrapper is the supported path. No deployment machinery — Control Node, Node Daemon, heartbeats — participates. Fidelity comes from shared Dockerfile codegen plus byte-match verification, not from a second builder.

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
- `theozolith candidate export` / `verify` / the build wrapper (Control-package CLI — the resolution machinery lives there; the monorepo is public and pip-installable).

Every entry point maps to exactly one compatibility key (review 2026-08-28): the prompt renderers, PR-body composition, gate sequence, and proposal validation are `schema_version` surface; `candidate export`, the bundle it writes, verification of bundle structure, and the verified build wrapper are `bundle_format_version` surface; identity recomputation inside `verify` is `identity_spec_version` surface. The CLI syntax itself follows the product CLI policy (see Contract surface and versioning).

### Conformance obligations

The implementation (the-ozolith#88) must land these test classes with the code — they pin the behaviors a consumer may rely on (review 2026-08-28):

- **Golden identity vectors**: no knowledge; default knowledge target omitted from the canonical identity; non-default target included; model/effort materialization; at least the claude and codex adapters — without hardcoding the allowed adapter set into the format.
- **Negative verification**: independent tampering with every identity-bearing manifest field; compiled knowledge bytes; the recorded knowledge pin; Dockerfile `FROM`, `RUN`, `COPY`, labels, and user; unexpected files, symlinks, traversal, and special files; missing, duplicate, malformed, and unknown manifest fields; unsupported bundle/identity versions — each must fail verification.
- **Build lifecycle**: invalid bundles never invoke Docker; the wrapper builds the same private snapshot it verified; mutating the caller's original directory after staging cannot affect the build; cleanup after success, failure, timeout, and interruption; repeat builds keep the deterministic identity and tag; re-export after a moved base tag resolves a new digest and identity; a damaged archived-and-restored bundle fails until replaced or re-exported.
- **Credentials**: public resolution is anonymous; private resolution and pull work through `DOCKER_CONFIG`; missing or refused credentials fail clearly with the host named; no secret appears in argv, manifest, bundle, logs, errors, image layers, or evidence.
- **Sources**: local directories accepted; URLs, plain files, absent paths, and unsafe trees rejected.
- **Run Contract**: byte-stable prompt rendering for both modes; PR-body composition; gate ordering; proposal validation; synthetic round-one review construction; schema-version mismatch refusal.

## Changelog

Every breaking change to a contract surface lands an entry here naming the bumped key (see Contract surface and versioning).

- **2026-08-28** — initial published contract (ADR-0054): `schema_version` 1 (Run Contract), `bundle_format_version` 1 (Candidate Bundle and verified build), `identity_spec_version` 1 (candidate identity).

## Decision history

Settled rulings are integrated into the sections above; decision records live in docs/adr/ (ADR-0054 for this contract), and the bench-side consumption plan is SilverquiLLM-bench#39. Implementation is tracked in the-ozolith#88.
