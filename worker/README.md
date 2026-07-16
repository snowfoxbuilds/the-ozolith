# theozolith-worker

The coding-pipeline component: the Worker and Reviewer **drivers**, the **agent
harness** + run-container image, the Claim Protocol, the first-party quality gate, and
the repo bootstrap tool. See `docs/specs/AGENTIC-CODING-PIPELINE.md` and ADR-0013.

## Topology (M2)

Each actor is a trusted, credentialed **driver** — a node-resident host process — paired
with a credential-free **agent harness** running as PID 1 of an ephemeral per-Run
container:

- `theozolith-worker` — the Worker driver: polls `plan_ready` issues, claims via the
  Claim Protocol, prepares a per-Run job directory with a **token-free checkout**,
  launches `ozolith-run-<run-id>`, sequences gate steps as harness jobs, then pushes and
  ships the best-effort PR with its Decisions Section. All GitHub I/O happens in the
  driver; the driver never executes repository code or model output.
- `theozolith-reviewer` — the Reviewer driver (own GitHub identity, stronger model):
  polls `pr_ready` PRs, materializes review inputs as files, launches
  `ozolith-review-<pr>-round-<n>`, validates the agent's `verdict.json`, renders the
  verdict comment, and applies all post-PR state. 3 review rounds per issue; at the last
  budgeted round a revise verdict is rejected (approve or escalate only).
- `theozolith-harness` — PID 1 of the run container: starts the interactive agent
  session in tmux (`run-<run-id>` / `review-<pr>-round-<n>`), injects the prompt by
  buffer paste, captures a `pipe-pane` transcript into the evidence bundle, detects
  completion via the per-adapter hook (Claude Stop hook) plus a hard timeout, serves
  driver-sequenced jobs, writes outputs, and exits. Headless `-p` invocation is banned
  (always-interactive contract).

Driver and harness communicate only through the job directory (`input/`, `output/`,
`checkout|work/`), bind-mounted at `/job` — no network channel, no shared process tree.
Both drivers support the continuous loop and `--once` (a single poll pass, the dev mode).

Contracts and formats (job-dir schemas, gate step contracts, verdict schema, evidence
bundle layout, harness mechanics) are recorded in ADR-0014. Deployment instructions live
in `deploy/README.md`.

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

The target repo declares its gate steps in `.theozolith/gate.toml` (`[steps.test|docs|lint]`
tables with `run`, optional `fix` and `timeout`), and its CI should build the
run-container image so image rot is caught (see `.github/workflows/ci.yml` here for the
pattern).
