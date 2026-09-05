# Team knowledge for the codex-dev worker type

The codex compiler emits this `AGENTS.md` VERBATIM (ADR-0052 §1) into the tree's
codex view, alongside `skills/` and — from an `agents/codex/` dir this example
does not ship — `prompts/`. It loads as the agent's global memory in every
codex Flight Deck of this type. Keep it small: conventions, not documentation.

Ingest compiles every knowledge tree ONCE PER TOOL (ADR-0052): the codex view is
`AGENTS.md` + `skills/` + `prompts/`, the claude view is `CLAUDE.md` + `skills/`
+ `agents/claude/` + `workflows/`. Keeping this tree separate from
`claude-dev/` is this example's convention, not a product rule — a tree is
compiled for every registered tool, so a codex deck could select `claude-dev`
just as well.
