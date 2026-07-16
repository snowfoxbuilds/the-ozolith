Status: ACCEPTED

Date: 2026-07-14

Provenance: authored in-repo under the M1 delegated-decisions mandate (PR #1); uplifted to Notion 2026-07-16 (ADR-0001).

# ADR-0009: Knowledge repo format, Claude compiler mapping, and sync overwrite semantics

## Context

M1 delegates three decisions to the implementing PR: the on-disk layout and file format for knowledge repos (skills, subagents, workflows), the Claude compiler's mapping rules, and the sync engine's behavior when a sync target has been hand-edited. The constraints come from the specs: knowledge repos are pure data with no machinery (ADR-0007), `AGENTS.md` is the canonical instruction file and vendor files are generated copies ([AGENTIC-CODING-PIPELINE.md](http://agentic-coding-pipeline.md/)), two scopes exist (global skills in the private config repo, project skills in the target project's repo), and sync targets are never hand-edited (ADR-0001).

## Decision

**Knowledge root layout.** A *knowledge root* is any directory with this shape, every section optional (an empty root is rejected):

```javascript
<root>/
├── AGENTS.md            # canonical instruction file
├── skills/<name>/       # one folder per skill, SKILL.md required inside
├── agents/<tool>/*.md   # subagent files, namespaced per tool ("claude" in V1)
└── workflows/<name>     # one file or folder per workflow
```

Names must match `[A-Za-z0-9][A-Za-z0-9._-]*` (they become path components in sync targets). Unknown tool namespaces under `agents/` are tolerated, so one data repo can serve compilers this version does not ship. The same shape serves both scopes: the private config repo holds a knowledge root for global scope; a project repo's root is itself the knowledge root for project scope (its `AGENTS.md` already lives there).

**Claude compiler mapping.** `AGENTS.md` → `CLAUDE.md` is a generated-marker comment plus the verbatim body; no content transformation in V1. Skills, subagents, and workflows are placed into the Claude config dir — the target itself in global scope (default `~/.claude`), `<project>/.claude/` in project scope — as `skills/<name>/` (recursive, executable bits preserved), `agents/<name>.md`, and `workflows/<name>`. `CLAUDE.md` sits inside the config dir in global scope and at the project root in project scope, matching where Claude reads each.

**Sync overwrite semantics.** The sync is a manifest-based mirror: a manifest (`.theozolith-knowledge-manifest.json`, kept in the Claude config dir) records the hash of every file the machinery placed. Re-syncs update or delete only manifest-tracked files and prune emptied directories; foreign files in the target (e.g. `settings.json`) are never touched. When a managed file differs from what the last sync wrote — or a symlink sits where a managed file belongs — the divergence is **detected, warned about, and overwritten: the source wins** (ADR-0001 already forbids hand-editing targets, so divergence is operator error, not data to preserve). `--strict` turns the warning into a failure before anything is written; `--check` is a no-write dry run. Symlinks are replaced, never written through. Re-running on an unchanged source performs zero writes.

**Bake.** `bake` is the same compile-and-sync run against a git clone of a Knowledge Source at a **required** pin, executed inside `docker build`; it stamps a provenance receipt (`.theozolith-bake.json`: source, pin, resolved commit) next to the manifest. Nothing executes at container start ([NODE-SUBSTRATE.md](http://node-substrate.md/)).

## Consequences

- **Positive**: one uniform root shape for both scopes and for bake; deletions propagate (a skill removed from the source disappears from targets); operator files coexist safely with managed files; hand-edit detection makes ADR-0001 violations visible instead of silently absorbed; per-tool namespacing gives Codex/Pi compilers a home without a format migration.
- **Negative**: a bookkeeping file lives in every sync target; verbatim `AGENTS.md` → `CLAUDE.md` copies any vendor-specific phrasing as-is (tool-specific sections would need a future transform); the manifest does not record file modes, so a chmod-only drift is repaired without a hand-edit warning.
- **Neutral**: the generated marker in `CLAUDE.md` is informational; divergence detection relies on the manifest hashes, not the marker.
## Alternatives Considered

- **Tool-specific source layout** (author directly in `.claude/` shape): rejected — couples the data repo to one vendor and breaks the agent swap boundary.
- **Silent overwrite on hand-edits**: rejected — hides operator error; the warning is cheap and teaches the ADR-0001 rule.
- **Fail-by-default on hand-edits (strict as default)**: rejected — the common case is unattended sync (image builds, worker provisioning); a stale hand-edit must not brick automation. Strict is opt-in for humans.
- **Whole-directory ownership instead of a manifest**: rejected — `~/.claude` legitimately contains operator content (settings, sessions) the machinery must never delete.
- **Marker-comment tracking instead of a manifest**: rejected — cannot track deletions, binary files, or non-commentable formats.
