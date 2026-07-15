# theozolith-worker

The coding-pipeline component: Worker + Reviewer actors, Dockerfiles, the poll-claim-run
entrypoint, per-Agent adapters, and the first-party gate. See
`docs/specs/AGENTIC-CODING-PIPELINE.md`.

**M1 ships only the repo bootstrap tool**; the actors land in later milestones.

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
