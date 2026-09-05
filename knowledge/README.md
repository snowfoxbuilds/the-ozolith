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
over a `config.toml` layer — `developer_instructions`, `model`, `model_reasoning_effort`, and
any other `config.toml` key. `name`, `description`, and `developer_instructions` are required.
Files are carried byte-for-byte, never rewritten.

The loader validates a role against the supported codex baseline, currently **codex-cli
0.153.3**, so a malformed role fails `validate` or ingest — naming the file and the field —
instead of being skipped at deck startup. Three things are easy to conflate here, and the
contract keeps them apart:

- **Schema-valid** is what Ozolith decides. The configuration layer must use only the canonical
  keys and value shapes in codex's own `config.toml` JSON Schema at the baseline version,
  vendored verbatim in the package (`theozolith_knowledge/codex_schema/config.schema.json`) and
  pinned by digest: the top level is closed, nested tables are open or closed exactly as the
  schema says, and every value must have the type codex deserializes. The metadata follows
  codex's standalone role parser: `name` is trimmed and must be non-empty (the trimmed value is
  the role's identity and must be unique across the root), `description` and
  `developer_instructions` must be non-blank after trimming, and nickname candidates are
  trimmed, non-blank, unique, and limited to ASCII letters, digits, spaces, hyphens, and
  underscores.
- **Parser-compatible** is wider than schema-valid, and is not promised. Codex's parser also
  accepts compatibility spellings the generated schema does not list — at 0.153.3 the aliases
  `agents.max_threads` (for `agents.max_concurrent_threads_per_session`) and
  `memories.no_memories_if_mcp_or_web_search` (for `memories.disable_on_external_context`).
  Ozolith rejects those aliases on purpose; write the canonical keys.
- **Runtime-effective** is narrower than schema-valid. A schema-valid key is one codex can
  parse and the view transports, not one codex necessarily applies to the subagent. At 0.153.3
  a role layer's effective overrides are exactly `developer_instructions`, `model`,
  `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`, `personality`,
  `service_tier`, the supported feature disables (`shell_tool`, `apps`, `personality`,
  `plugins`, `memory_tool`, or `request_permissions_tool` set to `false` under `[features]`),
  and the supported skill disables (`skills.config` entries with `enabled = false`, disabled
  bundled skills, `skills.include_instructions = false`). `sandbox_mode`, `mcp_servers`, and
  every other key are schema-valid and transported but are **not** effective role overrides at
  this baseline: the subagent inherits the parent session's runtime permission authority, so
  `sandbox_mode = "read-only"` in a role is not an enforcement boundary on 0.153.3.

Validation is offline and deterministic — nothing is fetched. Ozolith adds no allowlist of its
own on top of the schema. Relative paths in a role (`skills.config[].path`,
`model_instructions_file`, …) keep codex's semantics: codex resolves them against the role
file's directory — `~/.codex/agents/` once installed — and never checks at load that they
exist, so whatever they reference must exist in the environment the view lands in.

The baseline moves only by a deliberate, versioned update; a key a newer codex adds is refused
until then. The procedure lives in `codexrole.py`: fetch the tag's
`codex-rs/core/config.schema.json`, replace the vendored file, record the tag, commit, and
digest in `CODEX_SCHEMA_BASELINE`, re-read the role parser, the rejected aliases, and the
effective override set at that tag, and run the tests — the checker refuses schema vocabulary
it does not implement, so a schema that outgrows it fails the suite rather than validating
less.

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
