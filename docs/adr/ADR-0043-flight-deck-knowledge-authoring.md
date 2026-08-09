Status: ACCEPTED
Date: 2026-08-09

# ADR-0043: Flight Deck knowledge authoring and sharing

## Context
~/.claude conflates two classes of content: runtime state (sessions,
transcripts, --resume) and knowledge (skills, subagents, workflows).
Workers bake knowledge at pin time (ADR-0009), but the Flight Deck is
where humans author knowledge, and multiple Flight Decks of the same
worker type should not hold divergent copies.

## Decision
Split ~/.claude by class:
- **Runtime state**: per-Flight-Deck named state volume. Never shared,
  never reaches workers.
- **Knowledge**: workers continue to bake at pin (ADR-0009 unchanged);
  run containers never mount knowledge. The Flight Deck mounts one
  **shared writable knowledge clone per worker type per node** (named
  volume) and symlinks its ~/.claude knowledge directories into it.
  Editing skills in one Flight Deck updates all Flight Decks of that
  type on the node after an agent-CLI restart. The symlink carve-out is
  Flight-Deck-only (human-supervised, credentialed); forbidden
  everywhere else.
- **Promote flow**: commit/push the knowledge repo, re-pin, rebuild
  derived images.
- **Cross-node transport is git**: deliberate human push from one
  node's shared clone, pull in the other's. No auto-sync daemon.

## Consequences
- **Positive**: edit once per type per node; workers stay reproducible;
  the prompt-injection persistence channel into Runs stays closed.
- **Negative**: uncommitted scratch can diverge across nodes until
  pushed; operators must remember the promote flow.
- **Neutral**: sessions and transcripts remain per-instance. A future
  `knowledge sync` convenience command is the upgrade path — never a
  daemon.

## Alternatives Considered
- **Per-Flight-Deck private clones**: rejected — skill edits in one
  repo's Flight Deck should transfer to another's.
- **Mounting knowledge into worker Runs**: rejected — breaks Run
  reproducibility and opens a prompt-injection persistence channel.
- **Auto-sync daemon**: rejected — new machinery, conflict surface,
  silent propagation of uncommitted scratch.