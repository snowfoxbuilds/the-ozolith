Status: ACCEPTED — amended 2026-08-23 by ADR-0050 (all project docs move to repo authorship; the "Specs remain Notion-owned" clause and the Notion-side spec-amendment debt are retired)

Date: 2026-07-30

Provenance: operator ruling (2026-07-30) resolving PR #10 review round 1, finding 1 — a repo-only `ACCEPTED` ADR contradicted ADR-0001's Notion ownership of docs.

# ADR-0033: ADRs are repo-authored (amends ADR-0001)

## Context

ADR-0001 made Notion the source of truth for all project docs, synced one-way into the repo. ADRs have outgrown that arrangement: rulings emerge from PR reviews and coding sessions (ADR-0030 and ADR-0031 both entered the repo through PR commits implementing same-day rulings), and the decision artifact wants to change in lockstep with the code that implements it — reviewed by the same PR review. Routing every ruling through Notion authoring before its implementing PR can merge is a manual round-trip that buys nothing, and the alternative that actually happened — an `ACCEPTED` ADR existing only repo-side under Notion ownership — is a standing contradiction (PR #10, finding 1).

## Decision

- **`docs/adr/` is repo-authored as of 2026-07-30.** New ADRs and amendments to existing ADRs are written directly in the repo and reviewed in the PR that carries them. ADR-0001's split becomes: repo owns configs **and ADRs**; Notion keeps `AGENTS.md`, `CONTEXT.md`, and `Specs/`.
- **`sync_notion.py` no longer exports ADRs.** Notion's ADR pages and index freeze as a historical mirror; an export would clobber repo-side rulings.
- **Specs remain Notion-owned.** A repo ADR that changes a spec's content still owes the Notion-side spec amendment (for ADR-0032: NODE-SUBSTRATE.md's CLI-surface entry), landing in the repo through the normal spec sync. *(Retired by ADR-0050, 2026-08-23: specs, the glossary, and the index are repo-authored; a spec amendment lands in the same PR as the ADR that motivates it.)*

## Alternatives rejected

- **Status quo (author in Notion, then sync, then merge)**: the grilling that hardens an ADR is the PR review, which lives repo-side; the Notion round-trip adds latency and a second editing surface to every ruling without adding scrutiny.
- **Two-way sync**: the conflict machinery ADR-0001 was written to avoid.
- **Move all docs to the repo**: specs and the glossary genuinely benefit from Notion as the operator's authoring surface and change on a slower cadence; ADRs are the one artifact whose lifecycle is the code's.
