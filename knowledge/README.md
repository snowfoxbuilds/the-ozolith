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
│   ├── claude/          # subagent files, namespaced per tool
│   │   └── <name>.md
│   └── codex/           # codex custom-prompt sources
│       └── <name>.md
└── workflows/
    └── <name>[.md]      # one file or folder per workflow
```

Every section is optional; an empty root is rejected. See ADR-0009 for the format decision.

## Compilers

One compiler per target tool, dispatched through a registry (`--tool` on the CLI; ADR-0009,
ADR-0052). Each tool has a default global-scope destination: `~/.claude` for claude,
`~/.codex` for codex.

### Claude compiler mapping (`--tool claude`, the default)

| Source | Global scope (target = Claude config dir, default `~/.claude`) | Project scope (target = project repo root) |
| --- | --- | --- |
| `AGENTS.md` | `<target>/CLAUDE.md` (generated marker + verbatim body) | `<target>/CLAUDE.md` |
| `skills/<name>/` | `<target>/skills/<name>/` | `<target>/.claude/skills/<name>/` |
| `agents/claude/<name>.md` | `<target>/agents/<name>.md` | `<target>/.claude/agents/<name>.md` |
| `workflows/<name>` | `<target>/workflows/<name>` | `<target>/.claude/workflows/<name>` |

### Codex compiler mapping (`--tool codex`)

**Global scope only** — project scope is rejected: in a project the knowledge root *is* the
repo, whose `AGENTS.md` already sits where codex reads it (ADR-0052).

| Source | Global scope (target = Codex config dir, default `~/.codex`) |
| --- | --- |
| `AGENTS.md` | `<target>/AGENTS.md` (verbatim — it IS the canonical format; no generated marker) |
| `skills/<name>/` | `<target>/skills/<name>/` (verbatim — codex consumes the same skills format) |
| `agents/codex/<name>.md` | `<target>/prompts/<name>.md` (codex custom prompts) |
| `workflows/<name>` | omitted — no codex equivalent (visible in `validate`'s per-tool counts) |

On a fleet deployment, `theozolith config ingest` runs every registered compiler over each
Config Repo knowledge tree, writing `knowledge/<name>/<tool>/` into the pinned build with one
content-hash pin per `(tree, tool)`, keyed `"<name>/<tool>"` — an edit re-tags exactly the
worker types whose tool's compiled view changed (ADR-0048/0052).

## Commands

```sh
theozolith-knowledge validate --source <root>
theozolith-knowledge sync --source <root> --scope global [--tool claude|codex] [--target DIR] [--check] [--strict]
theozolith-knowledge sync --source <root> --scope project --target <project-root>
theozolith-knowledge bake --source <git-url> --pin <commit|tag> [--subdir <path>] [--tool claude|codex] [--target DIR]
```

The sync is one-way, deterministic, and idempotent. A manifest
(`.theozolith-knowledge-manifest.json`, kept next to the synced files) records every managed
file, so re-syncs update or delete only what the machinery itself placed; foreign files in
the target (e.g. `settings.json`) are never touched. Sync targets are never hand-edited
(ADR-0001): local edits are detected, warned about, and overwritten — or rejected with
`--strict`.

`bake` runs inside a `docker build` to install a pinned Knowledge Source (git URL + pin)
into an image; nothing executes at container start. It requires `git` in the build
environment and records provenance (source, pin, resolved commit, and the `tool` the tree
was compiled for) in `.theozolith-bake.json` next to the manifest. See `examples/bake/`
for a sample Dockerfile.
