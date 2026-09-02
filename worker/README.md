# theozolith-worker

The coding-pipeline component: the Implementer and Reviewer **drivers**, the **agent
harness** + run-container image, the first-party quality gate, and the repo bootstrap
tool. See `docs/specs/AGENTIC-CODING-PIPELINE.md` and ADR-0013/0016/0017/0019.

## Topology

Each actor is a trusted, credentialed **driver** — a node-resident host process — paired
with a credential-free **agent harness** running as PID 1 of an ephemeral per-Run
container:

- **Implementer** (`theozolith-driver builtin:implementer`) — the Implementer driver:
  requests work from the Control Node's
  dispatch endpoint (ADR-0017 — the Control Node writes the claim on GitHub before the
  driver ever sees the issue), prepares a per-Run job directory with a **token-free
  checkout**, launches `ozolith-run-<run-id>`, sequences gate steps as harness jobs,
  then applies the session's **Output Proposal** post-exit (ADR-0046): commits with the
  proposed commit message plus a provenance trailer, composes the PR (`#N: ` title
  prefix; Closes line + narrative + Decisions Section body), pushes, and ships the
  best-effort PR. A non-completed Run keeps the claim and retries locally exactly once;
  a completed session whose proposal fails validation gets one **completion retry**
  (worktree + pending proposal preserved, error appendix on the prompt); a second
  non-completion (or completion-retry miss) releases
  the claim and escalates `failed` + `needs_human` with every evidence bundle
  (ADR-0016 as amended). At boot (and idle passes) it sweeps orphaned job dirs to the evidence
  branch (`swept: true`, delete only after a confirmed push). All non-claim GitHub I/O
  happens in the driver; the driver never executes repository code or model output.
- **Reviewer** (`theozolith-driver builtin:reviewer`) — the Reviewer driver (own GitHub
  identity, stronger model):
  discovers `pr_ready` PRs through dispatch, materializes review inputs as files, launches
  `ozolith-review-<pr>-round-<n>`, validates the round's Output Proposal (the verdict and
  its content, ADR-0046), renders the verdict comment, and applies all post-PR state.
  3 review rounds per issue; at the last budgeted round a revise verdict is rejected
  (approve or escalate only), and the in-session CLI already refuses it at write time.
- `theozolith-harness` — PID 1 of the run container: invokes the agent **headless**
  (ADR-0019 as amended) — the adapter's one-shot command (Claude: `claude -p` with
  structured output) carrying a constant-size **pointer prompt** at the mounted task
  file (`input/prompt.md`, driver-rendered; the argv never carries task content, so
  the invocation cannot outgrow ARG_MAX) — captures the structured output stream as
  the evidence-bundle transcript (it also supplies token usage), treats process exit
  as completion with the hard agent timeout as backstop, serves driver-sequenced
  jobs, writes outputs, and exits. Run containers are never attach targets;
  interactivity lives only in the Flight Deck Stack.
- `format-output` / `view-output` — the in-session Output Proposal CLI (ADR-0046),
  baked into run images: the agent writes every proposed mutation (PR title/narrative,
  Decisions-Section entries, the required rich commit message; the Reviewer's verdict
  and content) as pending state the driver validates and applies post-exit. Enumerated
  fields fail loud at write time; `format-output status` runs the driver's exact
  validation; nothing the agent runs touches GitHub.

Driver and harness communicate only through the job directory (`input/`, `output/`,
`checkout|work/`), bind-mounted at `/job` — no network channel, no shared process tree.
Both drivers support the continuous loop and `--once` (a single poll pass, the dev mode).
A hand-run Driver (the daemon-less dev shape) defaults its `stack` to the role, but every
dispatch request names its Stack and repo, verified against the Control Node's Pinned Build
(ADR-0055) — so against a Control Node WITH a Pinned Build, export `THEOZOLITH_STACK`
naming a real Stack placed on this node for this repo (a wrong or missing Stack is refused
403; in production the Node Daemon injects it). A Control Node with no Config Repo at all
(the ADR-0004 deletion-test boot) skips the verification — the fail-open dev door.

Contracts and formats (job-dir schemas, gate step contracts, evidence bundle layout,
harness mechanics) are recorded in ADR-0014; the Output Proposal schema, the
format-output CLI, and the commit-message doctrine in ADR-0046. Deployment
instructions live in `deploy/README.md`.

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
