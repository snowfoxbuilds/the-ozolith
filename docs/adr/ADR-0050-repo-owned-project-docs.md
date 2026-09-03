Status: ACCEPTED

Date: 2026-08-23

# ADR-0050: All project docs are repo-authored; the Notion sync is retired

## Context

ADR-0001 made Notion the source of truth for project docs, synced one-way into the repo by `scripts/sync_notion.py`. ADR-0033 carved ADRs out of that arrangement because rulings emerge from PR reviews and want to change in lockstep with the code that implements them. The same pressure has since consumed the rest of the docs: specs owe amendments whenever an ADR changes their content (ADR-0032's NODE-SUBSTRATE entry, ADR-0048's knowledge sections, ADR-0049's secrets-scoping sentence), and each such amendment queued behind a manual Notion round-trip — "Notion follow-up owed" became a standing debt category on every doc-touching PR. Meanwhile the index files that ship in the repo (AGENTS.md, CLAUDE.md) drifted: the working index was kept current per-branch while the Notion-owned copy went stale, inverting the declared ownership in practice. This decision (an operator ruling) supersedes the docs half of ADR-0001 and completes the migration ADR-0033 started, retiring ADR-0033's "Specs remain Notion-owned" clause and its Notion-side spec-amendment debt.

## Decision

- **The repo is the source of truth for all project docs.** `AGENTS.md`, `CONTEXT.md`, `docs/specs/`, and `docs/adr/` are authored and amended directly in the repo, reviewed in the PR that carries them. A PR whose ADR changes a spec's content updates the spec in the same PR — there is no separate follow-up lane.
- **The Notion→repo sync is retired.** `scripts/sync_notion.py` is deleted (with its `requests`/`python-dotenv` dev dependencies). The Notion workspace becomes a read-only mirror; nothing in the repo is ever overwritten from Notion again.
- **Notion participates through the front door.** A Notion-side agent reads the repo for documentation and, when a change originates there, contributes it via a PR — the same review chokepoint as every other change. No write path bypasses PR review.
- **ADR-0001's config half is unchanged.** Agent configs are still authored in their source repo and synced one-way into local tool config dirs by the knowledge machinery; `CLAUDE.md` remains a generated copy of `AGENTS.md` (ADR-0009), and sync targets are still never hand-edited.

## Consequences

- **Positive**: One authoring surface, one review chokepoint; spec amendments land atomically with the ADR and code that motivate them; the "Notion follow-up owed" debt category disappears; the shipped index files can no longer drift from their source of truth.
- **Negative**: Notion loses its role as the doc authoring surface; contributions drafted there take the PR round-trip.
- **Neutral**: The frozen Notion ADR mirror (ADR-0033) stays frozen; the rest of the workspace joins it as a mirror, now maintained by an agent reading the repo instead of a sync script writing it.

## Alternatives Considered

- **Status quo (Notion owns specs/glossary/index)**: the follow-up debt was structural — every doc-affecting ADR owed a second, unsynchronized edit on a surface no PR review covers — and the ownership had already inverted in practice.
- **Two-way sync**: the conflict machinery ADR-0001 was written to avoid; unchanged verdict.
- **Keep the sync script for an occasional manual pull**: a dormant write path into the repo is a standing clobber risk for exactly the files it once owned; the agent-plus-PR path replaces it with a reviewed one.
