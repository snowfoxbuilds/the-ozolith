Status: ACCEPTED

Date: 2026-07-13

# ADR-0003: Handoff Doc over vendor session restore for parked Runs

## Context

Parked Runs must survive Worker recycling and may be resumed under a different Agent than the one that parked them. Vendor CLIs (e.g. Claude) offer native session restore, which is convenient but vendor-specific.

## Decision

The resume contract is an externalized Handoff Doc committed to the WIP branch, with a fixed schema: objective, decisions made with rationale, the finding verbatim with context, remaining work, dead ends tried (amended 2026-07-14, ADR-0008 — see Amendments). The resuming Run rebuilds context from repo + issue + Handoff Doc. Vendor session state may be stashed as an opportunistic same-agent fast path but is never required for a correct resume.

## Consequences

- **Positive**: resume works across Agent swaps and Worker recycling; forcing the Worker to articulate its state at park time surfaces confusion early; the Handoff Doc doubles as evidence and triage context in the draft PR.
- **Negative**: lossy compared to full session restore; writing the Handoff Doc costs tokens at park time.
- **Neutral**: resume quality is measurable (resume success rate per Agent config from evidence bundles), so the schema can be tuned by eval rather than intuition.

## Alternatives Considered

- **Vendor session restore as the contract**: rejected — makes vendor memory load-bearing, violating the core principle, and breaks cross-agent resume.
- **Full transcript replay**: rejected — token bloat; raw transcripts are mostly noise compared to a distilled handoff.

## Amendments

- **2026-07-14 (ADR-0008)**: parking is removed; the Handoff Doc schema survives as the mandatory Decisions Section of every best-effort PR description (decisions with rationale, open questions, remaining work, dead ends tried). The externalized-contract principle is unchanged: the next Run rebuilds context from repo + issue + PR (Decisions Section, review comments, Reviewer-designated resume commit); vendor session state is never load-bearing.
