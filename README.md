# TheOzolith

An open-source agent-orchestration monorepo with three separable concerns:

- **Agent-knowledge machinery** (`knowledge/`) — a tool-agnostic config format for skills,
  subagents, and workflows; per-tool compilers (`AGENTS.md` → `CLAUDE.md`, skill placement);
  a one-way sync engine into local tool config dirs; and a bake CLI for installing a pinned
  Knowledge Source into a container image at build time.
- **Agentic coding pipeline** (`worker/`) — staged autonomous development on GitHub issues
  and PRs: Worker and Reviewer drivers, per-Run containers with the agent harness as PID 1,
  the Claim Protocol, a first-party gate, best-effort PRs with Decisions Sections, and
  verdict-file review rounds (M2; ADR-0013). Plus the repo bootstrap tool from M1.
- **Cluster substrate** (`control/`, `nodedaemon/`, `deploy/`) — Control Node, Node Daemons,
  and Stacks (M3; ADR-0015). The Control Node serves the heartbeat/command channel, typed
  Run events, the advisory claim pre-filter, the zombie-claim janitor and retry auditor,
  and the encrypted node-scoped secret store. The Node Daemon registers a box as a
  Container-Host: declarative Stacks (container + supervised driver processes,
  kill-the-tree), local derived-image builds, tmpfs secrets, orphan reaping. `deploy/`
  carries the installer, the control compose file, and a starter Config Repo; the M2
  daemon-less one-box deployment remains fully supported (the pipeline never needs the
  Control Node; ADR-0002).

Every top-level component is independently installable. A laptop-only user of the knowledge
machinery never installs the cluster manager.

The project is governed by anchor docs authored in Notion and synced one-way into this repo
([AGENTS.md](AGENTS.md) index, [CONTEXT.md](CONTEXT.md) glossary, [docs/specs/](docs/specs),
[docs/adr/](docs/adr)); see ADR-0001. Synced docs are never hand-edited here.

## Quick start (knowledge machinery)

```sh
uv pip install ./knowledge          # or: pip install ./knowledge

# Sync a knowledge repo into your global Claude config dir
theozolith-knowledge sync --source /path/to/knowledge-repo --scope global

# Generate a project's CLAUDE.md (+ .claude assets) from its AGENTS.md
theozolith-knowledge sync --source . --scope project --target .

# Inside a Dockerfile: bake a pinned Knowledge Source into the image
theozolith-knowledge bake --source https://example.com/knowledge.git --pin <commit> --target /root/.claude
```

## Development

```sh
uv sync --all-packages   # workspace venv with all components + dev tools
uv run pytest            # all tests
uv run ruff check .      # lint
uv run ruff format --check .
```
