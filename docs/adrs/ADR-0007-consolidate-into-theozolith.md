Status: ACCEPTED

Date: 2026-07-14

# ADR-0007: Consolidate into TheOzolith — one public monorepo, one private config repo

## Context

Three codebases were converging on the same substrate: snow-maker (coding pipeline + cluster substrate, open-sourceable), the original agent-config sync tooling (skills, subagents, workflows — snow-maker's founding scope), and homeserver (private infrastructure already slated to migrate onto the product Node Agent). Personal configs sitting inside an open-sourceable repo forced a split regardless, and maintaining two public products with a thin seam between them would buy branding overhead without architectural benefit.

## Decision

One public monorepo, TheOzolith, with separable, independently installable components: knowledge/ (agent-knowledge machinery: config format, per-tool compilers such as [AGENTS.md-to-CLAUDE.md](http://agents.md-to-claude.md/) generation and skill placement, sync engine), worker/, control/, nodeagent/, deploy/. snow-maker is renamed and absorbed. homeserver sunsets by reduction: once its workloads migrate onto the Node Agent, what remains is data in the private config repo.

All private content — deployment declarations (Stacks, worker types, overlays, secret names) and agent knowledge (skills, subagents, workflows) — lives in one private config repo of pure data, no machinery. Worker types reference it as a Knowledge Source (git URL + pin) baked into derived images at build time; the same content syncs to laptop tool dirs.

Naming note: ADR-0001 through ADR-0006 were authored against the names "snow-maker" and "homeserver" and have been re-termed in place; original phrasing is archived in Historical Context (Notion-only). Any surviving "snow-maker" reference means TheOzolith; "homeserver" means the private config repo/deployment.

## Consequences

- **Positive**: one brand, one repo, one release process; the open-source collision (personal skills in a public repo) is resolved; laptop-only knowledge users and cluster adopters share one project; homeserver's sunset is a defined end state instead of an open question.
- **Negative**: the monorepo spans three concerns and needs discipline (separable installables, substrate admission rule) to avoid becoming a grab-bag; a rename this late touches every doc.
- **Neutral**: ADR-0004's private-to-public dependency rule is unchanged; the specs split ([AGENTIC-CODING-PIPELINE.md](http://agentic-coding-pipeline.md/), [NODE-SUBSTRATE.md](http://node-substrate.md/)) to keep each focused and under the size limit.
## Alternatives Considered

- **Two public repos (snow-maker + TheOzolith as separate products)**: rejected — the seam between knowledge machinery and worker images is one build-time invocation; two brands and release processes for that is pure overhead.
- **Config Repo absorbs personal agent knowledge (no public knowledge component)**: rejected — the knowledge machinery is useful with zero cluster (laptop-only) and belongs public-side.
- **Keep personal configs in the public repo as curated examples**: rejected — fails the moment a skill references private infrastructure.
