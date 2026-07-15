# theozolith-knowledge

The agent-knowledge machinery of TheOzolith: a tool-agnostic on-disk format for agent
knowledge (skills, subagents, workflows), per-tool compilers, a one-way sync engine, and a
bake CLI for container images. Pure machinery — all knowledge content lives in data repos
(the operator's private config repo for global scope, the target project's repo for project
scope). Standalone: no dependency on any cluster component.

## Knowledge root format

A *knowledge root* is a directory of pure data:

```
<root>/
├── AGENTS.md            # optional: canonical instruction file (vendor files are generated from it)
├── skills/
│   └── <name>/          # one folder per skill
│       ├── SKILL.md     # required
│       └── ...          # optional scripts and reference files, copied verbatim
├── agents/
│   └── claude/          # subagent files, namespaced per tool
│       └── <name>.md
└── workflows/
    └── <name>[.md]      # one file or folder per workflow
```

Every section is optional; an empty root is rejected. See ADR-0009 for the format decision.

## Claude compiler mapping

| Source | Global scope (target = Claude config dir, default `~/.claude`) | Project scope (target = project repo root) |
| --- | --- | --- |
| `AGENTS.md` | `<target>/CLAUDE.md` (generated marker + verbatim body) | `<target>/CLAUDE.md` |
| `skills/<name>/` | `<target>/skills/<name>/` | `<target>/.claude/skills/<name>/` |
| `agents/claude/<name>.md` | `<target>/agents/<name>.md` | `<target>/.claude/agents/<name>.md` |
| `workflows/<name>` | `<target>/workflows/<name>` | `<target>/.claude/workflows/<name>` |

## Commands

```sh
theozolith-knowledge validate --source <root>
theozolith-knowledge sync --source <root> --scope global [--target ~/.claude] [--check] [--strict]
theozolith-knowledge sync --source <root> --scope project --target <project-root>
theozolith-knowledge bake --source <git-url> --pin <commit|tag> [--subdir <path>] [--target ~/.claude]
```

The sync is one-way, deterministic, and idempotent. A manifest
(`.theozolith-knowledge-manifest.json`, kept next to the synced files) records every managed
file, so re-syncs update or delete only what the machinery itself placed; foreign files in
the target (e.g. `settings.json`) are never touched. Sync targets are never hand-edited
(ADR-0001): local edits are detected, warned about, and overwritten — or rejected with
`--strict`.

`bake` runs inside a `docker build` to install a pinned Knowledge Source (git URL + pin)
into an image; nothing executes at container start. It requires `git` in the build
environment and records provenance (source, pin, resolved commit) in
`.theozolith-bake.json` next to the manifest. See `examples/bake/` for a sample Dockerfile.
