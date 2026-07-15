Status: ACCEPTED

Date: 2026-07-15

Provenance: authored in-repo under the M2 delegated-decisions mandate; pending uplift to Notion (ADR-0001).

# ADR-0011: Gate step contracts, Decisions Section format, and evidence bundle layout

## Context

M2 delegates three artifact contracts: the first-party quality gate's step contracts, findings schema, and mechanical-fix policy; the Decisions Section format in the PR description (must carry the former Handoff Doc schema, ADR-0008); and the evidence bundle format plus the exact git ref layout in the target repo (the location — a dedicated ref — was settled by the brief).

## Decision

### Gate (worker/ `gate/`)

- **Step contract**: steps are shell commands declared by the *target repo* in `.theozolith/gate.toml` — `[steps.<name>]` tables with `run` (required), `fix` (optional), `timeout` (optional, default 900s). Canonical steps `test`, `docs`, `lint` run in that order (the spec's order); unknown step names are allowed and run after the canonical ones, alphabetically. A repo with no gate config gets an `info` finding and no steps — visible, never fatal.
- **Findings schema**: `{step, severity: error|warning|info, summary, detail, fixed}`. `detail` carries the last 4000 chars of command output. Findings are recorded in the PR's Decisions Section and in the evidence bundle (`findings.json`).
- **Mechanical-fix policy**: a fix is auto-applied only when the target repo itself declares a `fix` command for the step (e.g. `ruff check --fix`); the Worker never invents fixes. After a fix the step's `run` re-executes; only a now-green step is recorded as `fixed` (severity `warning`), otherwise the step stays an `error` with the post-fix output. Fixed changes ride in the Run's single commit; the finding is the audit trail.
- **The gate never blocks**: every outcome is a finding on the best-effort PR (ADR-0008). Push, PR, and CI are the Run's phases after the gate, not gate steps.

### Decisions Section (PR description)

- Embedded between `<!-- theozolith:decisions:begin/end -->` markers: human-readable markdown (Decisions made / Open questions / Remaining work / Dead ends tried / Gate findings) rendered from a machine copy in an HTML comment (`theozolith:decisions:data`, JSON). One block, replaced in place each round; per-round history lives in the evidence bundle, and verdict comments preserve the review dialogue.
- The agent hands the Worker its half by writing `.theozolith/decisions.json` in the worktree (`decisions[{what,why}]`, `open_questions[]`, `remaining_work[]`, `dead_ends[]`). The file is excluded from the commit via `.git/info/exclude` — pipeline metadata, not repo content. A Run whose agent leaves no parseable file still ships: the Worker synthesizes a section saying so, and the Reviewer judges accordingly.

### Evidence bundles

- **Ref layout**: an orphan branch `theozolith/evidence` in the target repo. Per Run: `runs/issue-<N>/<run-id>/` containing `run.json` (run metadata: worker, model, round, phases, PR, head), `findings.json`, `decisions.json`, `transcript.txt`, `diffstat.txt`. Reviewer verdicts land beside them as `runs/issue-<N>/reviews/round-<R>-<head12>.json`.
- `run-id` = `<utc-timestamp>-<worker-id>-<seq>`. The bundle link cited in Reviewer comments is the issue directory (`…/tree/theozolith/evidence/runs/issue-<N>`), covering all rounds.
- Bundles are pushed with plain git (retry on a fresh clone for concurrent writers). Evidence is traceability, not coordination: a failed evidence push never fails the Run or a verdict.

## Consequences

- **Positive**: gate behavior is entirely target-repo-declared, so the Worker image stays project-agnostic; the fix policy is opt-in by the audited party; the Decisions Section is lossless (machine copy) yet reviewable by humans; evidence rides in the target repo with no extra storage service; per-Agent metrics (ADR-0008) can be computed from `runs/` alone.
- **Negative**: an unconfigured repo gets no test/lint verification beyond target-repo CI; a shell `run` command is arbitrary code execution — acceptable because it already runs inside the disposable Run container against a repo the operator chose to automate; PR bodies carry a JSON blob.
- **Neutral**: the evidence branch grows unboundedly; pruning is an operator concern until a janitor exists.

## Alternatives Considered

- **Auto-detected gate steps** (pyproject → pytest/ruff, package.json → npm test): rejected for V1 — wrong guesses produce noise findings the Reviewer must discount; explicit config is one small file.
- **Decisions Section as a PR comment instead of the description**: rejected — ADR-0008 fixes it as the mandatory section of the description; comments are the Reviewer's channel.
- **Evidence in refs/notes or a separate repo**: rejected — notes are invisible in the GitHub UI (the escalation flow needs a clickable link); a second repo breaks the deletion test (NODE-SUBSTRATE.md) and doubles credentials.
