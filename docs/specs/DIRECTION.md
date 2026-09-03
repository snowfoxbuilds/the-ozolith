Status: SETTLED

Last updated: 2026-08-09

# TheOzolith — Motivation & Direction

Why the system exists, what it optimizes for, and what it deliberately is not. Companion to [CONTEXT.md](../../CONTEXT.md) (canonical terms) and the decision records in `docs/adr/` (individual decisions). This page governs direction; when a proposed change conflicts with a thesis here, the conflict must be resolved explicitly — by amending this page or rejecting the change — never silently.

## Why TheOzolith exists

- A delivery pipeline for agent-written code on owned hardware: planning stays human, implementation and review are autonomous, and every state transition is auditable.
- A judgment artifact: the ADR corpus, glossary, and design invariants demonstrate system-level architecture for agent infrastructure. The system is evidence, not a product.

## Core theses

### 1. Manage terminals, not agents (thin adapters)

Orchestrate agent CLIs as processes: invocation, verbatim native config passthrough, artifact collection. Never wrap the agent loop in a protocol or UI layer.

- Any wrapper protocol (e.g. ACP) is a lossy common denominator: capabilities exist only once they enter the protocol schema and both ends implement them. Frontier CLIs ship model-level knobs, subagents, skills, and hooks monthly; protocol surfaces structurally lag. Example: model reasoning effort is a one-line native config value, but was unavailable through OpenHands' wrapper (observed 2026-08).
- The lag is structural, not temporal: CLI vendors are vertically integrated and have no incentive to expose full capability through a neutral protocol that commoditizes them into interchangeable backends.
- With thin adapters the capability gap is zero by construction: new CLI features work the day they ship, via the Agent config.
- Accepted cost: no mid-run steering or in-band observability. Runs are transactions, not sessions — evidence bundles, transcripts, and exit status are the observation surface. In-flight policy, where ever needed, uses the agent's native extension points (e.g. hooks), never a wrapper around the loop.
- Adapter thickness, not agent-agnosticism, is the real variable: some per-CLI knowledge (flags, config format, output conventions) is unavoidable. Keep adapters thin and lossless.

### 2. GitHub is the only coordination truth

All pipeline state lives in labels, issues, and PRs. No second source of truth to drift; state is legible in the tool humans already use; Control Node death pauses new claims but never corrupts state.

### 3. No autonomous model ever holds credentials

Drivers hold credentials and perform all GitHub I/O; harnesses are credential-free PID 1 of ephemeral containers. Prompt injection in repo content cannot exfiltrate a token or forge a GitHub write by construction. This is a boundary no sandboxing model provides — sandboxes protect the host; the credential split protects the org.

Scope: the invariant covers autonomous pipeline actors — Workers and their Runs. The Flight Deck is the deliberate exception: a human-supervised interactive session holding its own machine identity (fine-grained PAT: issues, PRs, contents; no merge permission), never the operator's PAT, and never a pipeline actor. The boundary is autonomy, not model access — a credentialed model session requires a human at the helm.

### 4. Review is adversarial and independent

A separate Reviewer actor — own identity, stronger model, round budgets, no self-grading by construction — owns all post-PR state. Self-review by the implementing agent is a control failure, not a feature.

### 5. Substrate invariants over convenience

Owned hardware, cgroup kill semantics, IP-only control channel, local image builds, machine-consumed provisioning. The substrate reimplements a slice of generic orchestrators deliberately: the invariants are the point, and nothing downstream depends on a cloud.

## What TheOzolith is not

- **Not a product.** No roadmap promises, no support obligations, no generalization beyond our own deployments. The category evidence (Vibe Kanban: 27k stars, funded, shut down 2026-04 for lack of a business model) says adoption without a structurally paid layer is worthless; the paid layers (hosted compute, enterprise governance, support) are exactly the obligations to avoid. Reassess only on organic traction, with evidence.
- **Not a session manager.** Interactive, human-steered agent work lives only in the Flight Deck. The pipeline never hosts a conversation.
- **Not competing with hosted agents.** Devin, Copilot coding agent, and Agent HQ serve teams without sovereignty, auditability, or credential-isolation constraints. TheOzolith exists for the constraints they cannot serve.

## Positioning

- One-liner: an open-source, self-hosted alternative to GitHub's Copilot coding agent and Agent HQ — with a credential-isolated execution model, an independent AI review stage, and support for any headless agent CLI (including local-model harnesses, once an adapter ships; do not claim local-model support before one exists and is benchmarked).
- Landscape (as of 2026-08): OpenHands is the only scope-comparable open-source platform; the rest of the OSS field is single-machine session management. The credential split and the independent review stage are unclaimed differentiators across the entire field, hosted or open.
- The primary external output is written design rationale derived from these theses (thin adapters, GitHub-as-truth, credential split, adversarial review), backed by the repo and its decision records.

## Relevant ADRs

| ADR | Decision |
| --- | --- |
| ADR-0017-control-node-claim-dispatch | GitHub owns coordination state; the Control Node is the single write-through claim writer, never a second source of truth |
| ADR-0019-headless-runs-and-flight-deck | Runs execute headless (transactions, not sessions); interactivity lives only in the Flight Deck |
