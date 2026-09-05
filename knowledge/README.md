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
│   └── codex/           # codex custom agent roles (native) and custom prompts (deprecated)
│       ├── <name>.toml
│       └── <name>.md
├── hooks/               # codex hooks: hooks.json plus the scripts it references
│   └── hooks.json
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

`agents/codex/` and `hooks/` are codex-only sections: the claude view is byte-identical with
or without them.

### Codex compiler mapping (`--tool codex`)

**Global scope only** — project scope is rejected: in a project the knowledge root *is* the
repo, whose `AGENTS.md` already sits where codex reads it (ADR-0052).

| Source | Global scope (target = Codex config dir, default `~/.codex`) |
| --- | --- |
| `AGENTS.md` | `<target>/AGENTS.md` (verbatim — it IS the canonical format; no generated marker) |
| `skills/<name>/` | `<target>/skills/<name>/` (verbatim — codex consumes the same skills format) |
| `agents/codex/<name>.toml` | `<target>/agents/<name>.toml` (codex custom agent roles — native subagent definitions, verbatim; validated at load against the supported codex baseline, see below) |
| `agents/codex/<name>.md` | `<target>/prompts/<name>.md` (codex custom prompts — deprecated upstream, kept so existing views stay byte-stable) |
| `hooks/` | `<target>/hooks/` (verbatim; `hooks.json` is required and must be a JSON object, its scripts travel with it) |
| `workflows/<name>` | omitted — no codex equivalent (visible in `validate`'s per-tool counts) |

#### Codex agent roles

A role file is what codex discovers under `$CODEX_HOME/agents/`: a native subagent definition,
not a prompt. Its role metadata (`name`, `description`, `nickname_candidates`) sits flattened
over a complete `config.toml` layer — `developer_instructions`, `model`,
`model_reasoning_effort`, `sandbox_mode`, `mcp_servers`, `skills.config`, and any other
`config.toml` key — that codex applies to the subagent it spawns with the role. `name`,
`description`, and `developer_instructions` are required.

The loader accepts what codex-cli at the supported baseline (currently **0.153.3**) accepts and
refuses what codex would warn-and-skip, so a malformed role fails `validate` or ingest instead
of silently shrinking a deck's roster:

- the metadata follows codex's standalone role parser: `name` is trimmed and must be non-empty
  (the trimmed value is the role's identity and must be unique across the root), `description`
  and `developer_instructions` must be non-blank after trimming, and nickname candidates are
  trimmed, non-blank, unique, and limited to ASCII letters, digits, spaces, hyphens, and
  underscores;
- the configuration layer is validated against codex's own `config.toml` JSON Schema at the
  baseline version, vendored verbatim in the package
  (`theozolith_knowledge/codex_schema/config.schema.json`) and pinned by digest: the top level
  is closed, nested tables are open or closed exactly as the schema says, and every value must
  have the type codex deserializes.

Validation is offline and deterministic — nothing is fetched. Files are carried byte-for-byte,
never rewritten. Ozolith adds no policy of its own: which keys codex applies to the spawned
subagent, and how they rank against the parent session's permissions, are codex's runtime
concerns. Relative paths in a role (`skills.config[].path`, `model_instructions_file`, …) keep
codex's semantics: codex resolves them against the role file's directory — `~/.codex/agents/`
once installed — and never checks at load that they exist, so whatever they reference must
exist in the environment the view lands in.

The baseline moves only by a deliberate, versioned update; a key a newer codex adds is refused
until then. The procedure lives in `codexrole.py`: fetch the tag's
`codex-rs/core/config.schema.json`, replace the vendored file, record the tag, commit, and
digest in `CODEX_SCHEMA_BASELINE`, re-read the role parser at that tag, and run the tests — the
checker refuses schema vocabulary it does not implement, so a schema that outgrows it fails the
suite rather than validating less.

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
