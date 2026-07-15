# theozolith-worker

The coding-pipeline component: the Worker and Reviewer actors, the Claude adapter image,
the first-party quality gate, and the repo bootstrap tool. See
`docs/specs/AGENTIC-CODING-PIPELINE.md` and ADR-0008/0011/0012.

## The actors (M2)

**`theozolith-worker`** — a long-lived poll-claim-run loop bound to one Agent config:

- Polls the target repo for `plan_ready` issues and claims via the Claim Protocol
  (self-assign + `in_progress`, re-read to verify sole assignee, back off otherwise;
  optional Control Node pre-filter, cleanly skipped when unreachable).
- Executes stateless, disposable Runs: fresh clone and fresh agent context per Run; the
  only carryover is PR branch content at the Reviewer-designated resume commit plus the
  revised plan from the verdict comment.
- Runs the first-party gate (`test → docs → lint` from the target repo's
  `.theozolith/gate.toml`; structured findings; declared mechanical fixes auto-applied;
  never blocks — ADR-0011).
- Always ships a best-effort PR with a mandatory **Decisions Section** (decisions with
  rationale, open questions, remaining work, dead ends tried), applies `pr_ready`, and
  pushes an evidence bundle to the `theozolith/evidence` branch.
- Exits after `THEOZOLITH_RECYCLE_RUNS` Runs (default 10) so the container restarts fresh.

**`theozolith-reviewer`** — a separate long-lived actor with its own GitHub identity and a
stronger model; owns all post-PR state (ADR-0008):

- Polls PRs labeled `pr_ready` without `needs_human`, under the 3-round budget.
- Judges the diff against the issue's intent, acceptance criteria, and the Decisions
  Section; mechanical diff signals (size, files, dependency changes, sensitive paths) are
  computed and fed in as evidence.
- Verdicts: **approve** (`needs_human` + `deviation:*` + `risk:*` + evidence-citing
  comment), **revise** (`attempt-N`, revised plan + resume commit comment, claim strip,
  `plan_ready` re-queue), **escalate** (`blocked` + `needs_human` + evidence bundle link;
  deterministic on budget exhaustion).

Configuration is environment-only (see `deploy/.env.example`); every variable honors the
`<NAME>_FILE` convention. Both actors are stdlib-only Python (ADR-0010).

## The Claude adapter image

`docker/Dockerfile.claude` builds the Worker image: Claude Code CLI, git, tmux (the agent
process runs inside tmux so a terminal can attach: `docker exec -it <c> tmux attach`), and
an optional Knowledge Source baked at build time by the M1 bake CLI. The same image runs
the Reviewer with a different command and model (`deploy/docker-compose.yml`).

## Repo bootstrap

`theozolith-bootstrap` applies the pipeline substrate to a target GitHub repo, idempotently:

- The verbatim label vocabulary: `draft`, `plan_ready`, `in_progress`, `pr_ready`,
  `blocked`, `needs_human`, `risk:low|medium|high`, `deviation:low|medium|high`, and
  `attempt-1`..`attempt-3` (the 3-round review budget, ADR-0008). Missing labels are
  created, drifted ones corrected, unrelated labels left alone.
- A GitHub issue form (`.github/ISSUE_TEMPLATE/task.yml`) prompting objective, acceptance
  criteria, out of scope, and pointers. The two hard artifacts — acceptance criteria and
  baseline risk — are the only required fields; the rest are prompts, never enforced
  ("the human is the lint").

```sh
export GITHUB_TOKEN=...   # or GH_TOKEN; needs repo scope on the target
theozolith-bootstrap --repo owner/name          # apply
theozolith-bootstrap --repo owner/name --check  # report drift, change nothing; exit 1 if drift
```

Re-running against an already-bootstrapped repo is a no-op.
