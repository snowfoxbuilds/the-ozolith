Status: ACCEPTED
Date: 2026-08-28

# ADR-0054: Candidate Bundles and the published bench contract

## Context

SilverquiLLM-bench adopted the ruling that a Benchmark Candidate IS a worker-type definition (SilverquiLLM-bench#39): benchmark evidence transfers to deployment because both consume the identical derived image. Its 2026-08-27 correction comment established that a worker-type TOML is not self-contained and promised ozolith-side tooling — a self-contained candidate export, an independently verifiable identity spec, a standalone build path, and job-dir contract stability. The operator now also wants Reviewers benchmark-able, which that comment had deferred. TheOzolith must decide whether, and in what form, to expose its internals as a public contract.

## Decision

TheOzolith publishes a versioned bench-export contract (docs/specs/BENCH-CONTRACT.md) and owns all four of its parts; the bench side owns modes, task synthesis, evaluation, results, and scheduling.

1. **Candidate Bundle, exported from any source tree — never from the Pinned Build.** `theozolith candidate export` runs the ingest resolution machinery (per-tool knowledge compile, base digest resolution, pins, instruction hash) against any config-repo-shaped directory and writes a self-contained, docker-buildable bundle: `candidate.json` manifest, the compiled knowledge tree for the candidate's adapter tool only, and a Dockerfile emitted by the Node Daemon's own codegen. Benchmark variants never enter deployment config history; benchmark↔deployment equivalence is identity-hash equality, not shared provenance. Secret slot names travel, values never.
2. **Identity is the triple (base digest, instruction hash, adapter name), kind-agnostic, always recomputed.** The production instruction-hash formula (ADR-0045/0052) is published with golden vectors; `theozolith candidate verify` recomputes identity from bundle bytes and refuses on mismatch. Implementer vs reviewer is a benchmark-mode property — the manifest's `mode` field alone selects the proposal schema, so one candidate runs either kind.
3. **The Run Contract is the production Run at full fidelity, for both kinds.** Bench replays the job-dir contract with the production prompt renderers, PR-body composition, and gate-step sequence — exposed as stable entry points in the worker package's public API, never copied. The first-party gate runs in implementer-mode bench runs. A Review Run job dir is constructible from a synthetic issue + a git branch + an implementer-contract output (no live GitHub PR), pinned at round 1 of 3. The reviewer proposal schema is unchanged — no structured findings field; evaluation parses prose bench-side.
4. **Versioning is visibility, not immutability.** The job manifest's `schema_version` is the public compatibility key for the Run Contract and exposed entry points; bundle format and identity spec carry their own versions; every breaking change bumps a version and lands a changelog entry — no silent breaks, no compatibility windows.

## Consequences

- **Positive**: benchmark evidence transfers to deployment by construction — same image, same prompts, same gate, same proposal validation. Experimental candidates stay out of the Pinned Build. Identity verification needs no trust in the exporter. Reviewer benchmarking gets a contract without a schema change. Contract drift between bench and production becomes structurally impossible where entry points are shared.
- **Negative**: the job-dir layout, proposal schemas, prompt renderers, and gate sequencing become public surface — internal churn now carries a version-bump and changelog obligation. Export re-resolves base digests, so the same source can yield different identities across time as tags move (recorded, but a footgun for naive comparisons). The Control package becomes a bench-host dependency.
- **Neutral**: the standalone build is plain `docker build` plus a thin verify-and-tag wrapper. A private base needs a credential passed to export directly. Deviating from the production prompt template remains possible, but as a recorded benchmark-mode variable.

## Alternatives Considered

- **Export from the Pinned Build only**: strongest provenance story, but forces every benchmark variant through the deployment's machine-owned config history and a control-node ingest; identity-hash equality already carries the transfer guarantee without shared provenance.
- **Bench maintains its own copies of prompts/gate/composition**: every production template improvement becomes silent, unmeasured drift between what is benchmarked and what deploys — the exact failure the candidate ruling exists to prevent.
- **Freeze the job-dir contract**: immutability is unpayable while the pipeline is young; the promise worth making is no silent breaks.
- **Structured findings field in the reviewer proposal (schema v2)**: changes the production contract for bench convenience, inverting the fidelity principle; the LLM judge must read prose regardless. Revisit only if prose judging proves unreliable.
- **Worker kind in candidate identity**: would fork every candidate into an implementer and a reviewer twin with identical images; kind is run-shape, and the manifest already selects it.
