Status: ACCEPTED — docs half superseded 2026-08-23 by ADR-0050 (all project docs are repo-authored; the Notion sync is retired); the config half stands

Date: 2026-06-08

# ADR-0001: Repo owns configs, Notion owns docs (one-way sync)

## Context

TheOzolith holds two kinds of content: agent configs (skills, subagents, workflows) and project docs (glossary, specs, ADRs). Both are synced to other locations, so the authoritative side for each must be fixed to avoid editing the wrong copy.

## Decision

Configs are authored in a source repo and synced one-way into local tool config dirs (e.g. `~/.claude`); as of ADR-0007 the source for personal configs is the private config repo, with the machinery in TheOzolith's knowledge/ component. Project docs are authored in Notion and synced one-way into the repo. Neither sync target is ever hand-edited. *(Amended by ADR-0033, 2026-07-30: ADRs move to repo authorship — `docs/adr/` is written and amended directly in the repo, and the Notion ADR mirror is frozen; Notion keeps AGENTS.md, CONTEXT.md, and Specs.)* *(Superseded in part by ADR-0050, 2026-08-23: the remaining docs move to repo authorship too and the Notion sync is retired; the config half of this decision — one-way sync from a source repo into tool config dirs, sync targets never hand-edited — stands.)*

## Consequences

- **Positive**: No ambiguity about where to edit; no merge conflicts from bidirectional sync; local config dirs and in-repo docs are reproducible from their source.
- **Negative**: Editing a doc means going to Notion; changing a deployed config means editing the repo and re-running the sync.
- **Neutral**: Two separate sync scripts running in opposite directions.
## Alternatives Considered

- **Two-way sync**: rejected — invites conflicts and ambiguity about which side is authoritative.
- **Repo as source of truth for everything (docs included)**: rejected — Notion is the preferred authoring surface for docs (glossary, specs, and ADRs are produced via grilling).
