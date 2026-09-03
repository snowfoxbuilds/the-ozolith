Status: ACCEPTED

Date: 2026-07-13

# ADR-0002: GitHub owns coordination state; the Control Node is advisory

## Context

Workers claiming issues concurrently need race resolution, zombie-claim cleanup, and retry accounting. A central coordinator solves these cleanly but risks becoming a second source of truth and a single point of failure on home-lab hardware.

## Decision

All coordination state (claims, attempt counts, escalations) lives in GitHub issue state: assignees, labels, comments. The Control Node participates in coordination only in advisory roles: claim pre-filter when reachable, liveness-based zombie-claim janitor, and retry-count auditor (flags mismatches, never auto-corrects). A coordination action is valid only once committed to GitHub. Workers must function with the Control Node down.

## Consequences

- **Positive**: no second source of truth; a Control Node outage degrades the system (no dashboard, slower zombie cleanup) instead of halting it; the coordination substrate stays agent-agnostic and durable.
- **Negative**: GitHub label/assign operations are not atomic, so rare duplicate claims remain possible in degraded mode (cost: one wasted duplicate PR); coordination logic is split between the Worker-side Claim Protocol and Control Node janitorial jobs rather than centralized.
- **Neutral**: monitoring and janitorial functions live in the product's own Control Node (ADR-0005; original wording archived in Historical Context).

## Alternatives Considered

- **Authoritative central dispatcher**: rejected — single point of failure, second source of truth, and a service that must stay alive for correctness.
- **Pure choreography (no control node)**: rejected — no liveness-based zombie cleanup and no fleet dashboard; a monitoring web host was wanted regardless.
