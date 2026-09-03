Status: SUPERSEDED by ADR-0057

Date: 2026-07-21

# ADR-0020: Worker as base type — Implementer, Reviewer, Initializer; Flight Deck naming

## Context

Naming grilling 2026-07-21. "Worker" named the implementation actor specifically while also flavoring generic surfaces (the worker/ component, the theozolith-worker package, "worker types" in the Config Repo, the built-in worker Stack) — an overload that worsened once ADR-0019 added the interactive container (briefly "Pilot", then "Helm") and a draft-stage agent concept arrived. A flat rename of Worker → Implementer would cascade through code surfaces shipped in M2–M5. This decision amends terminology in ADR-0013 and ADR-0017 (their "Worker" = today's Implementer).

## Decision

- **Worker is the base abstraction** for every automated pipeline actor: one long-lived, node-resident driver process (supervised Node Daemon child, process-kind Stack) bound to one Agent config, executing ephemeral headless Runs. Shared infrastructure: heartbeat, container lifecycle, and the fetch-execute loop.
- **Worker types differ only in GitHub state management and the harness/model.** Built-in types: **Implementer** (implementation stage; formerly "Worker"), **Reviewer** (review stage), **Initializer** (draft stage; detailed contract specified in a follow-up ADR once grilled).
- **The code mirrors the taxonomy with inheritance**: a base worker type implements the shared infrastructure; the three built-ins extend it; custom worker types extend the base type or one of the built-ins. This is the long-term code-level extension surface alongside Config-Repo worker-type image definitions.
- **Renames**: the implementation actor Worker → Implementer; Worker Run → Implementer Run; the interactive human-driven container is named **Flight Deck** ("Pilot" and "Helm" rejected — the name should denote the station, not the person; ADR-0019 renamed in place, same day). **"Planner" is reserved** for a future autonomous planning actor that authors plans — distinct from the Initializer, which sharpens human drafts.
- **Code surfaces keep worker-based names where they denote the base abstraction** (worker/ component, theozolith-worker package, "worker types" in the Config Repo). Identifiers that denote the Implementer specifically (the built-in worker Stack name, WORKER_MODEL, the worker field in run events) are misnamed under the taxonomy; they are renamed as part of the inheritance-refactor issue, not by docs fiat.

## Consequences

- **Positive**: the stage↔actor mapping is clean (Initializer → draft, Implementer → plan_ready, Reviewer → pr_ready); the Worker-vs-worker-types overload dissolves — worker types now literally are types of Worker; most shipped code identifiers remain correct; extensibility gets a defined shape.
- **Negative**: the inheritance refactor is real code work (worker/ is currently Implementer-specific); historical documents (ADRs through 0019 as originally worded, dated Decisions entries) use "Worker" meaning the Implementer and must be read with their dates; inheritance invites fragile-base-class rot if the hierarchy deepens — variance must stay confined to narrow overridable seams (work query, transition set, harness/model configuration).
- **Neutral**: the Flight Deck's semantics are unchanged from ADR-0019 (interactive, credentialed, human-supervised, not a pipeline actor); only the name changed.

## Alternatives Considered

- **Flat rename Worker → Implementer everywhere** (rejected: cascades through shipped code surfaces and forfeits the natural base-type reading of "worker").
- **New base term (e.g. "Actor") with Worker kept as the implementer** (rejected: Worker already reads generic; a second abstract noun adds vocabulary without adding meaning).
- **Composition over inheritance in code** (deferred, not rejected: the ruling mandates inheritance to strengthen the taxonomy; keep the hierarchy one level deep and the variance in narrow seams to retain composition's benefits).

## Amendments

- **2026-09-03 (#120, ADR-0057)**: superseded. Worker stays the base abstraction and the names Implementer, Reviewer, Initializer, and Flight Deck stand, but worker types differ by declaration, not subclass: a Worker-Type Definition declares its kind (`on = "issue" | "pr"` — Issue Worker or PR Worker), Intake, outputs with an Outcome Table, prompt, and `rounds`, and the three built-ins become the shipped default definitions. The inheritance extension surface ("custom worker types extend the base or a built-in") is retired — a new worker type is a new definition in the Config Repo. Reason: Baseline Risk routing needed reduced and thorough review as two worker types, and the operator wants new workers (a PR-triggered tester, for example) to be config, not code.
