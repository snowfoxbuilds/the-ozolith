Status: ACCEPTED

Date: 2026-07-16

Provenance: authored in-repo under the M2 delegated-decisions mandate; pending uplift to Notion (ADR-0001).

# ADR-0014: M2 execution contracts — job directory, harness, gate, verdicts, evidence

## Context

The regenerated M2 brief (ADR-0013 topology: node-resident drivers, ephemeral per-Run containers) delegates the concrete contracts to the implementing PR: gate step contracts and mechanical-fix policy; job-dir file formats; harness mechanics (completion hooks, timeouts, tmux layout); the evidence bundle format and git ref layout; Reviewer driver cadence, comment rendering, and model configuration; the Decisions-section format; and driver loop/backoff defaults with `--once` semantics. Repo ADR-0011/0012 died with PR #2; their surviving content is absorbed and updated here.

## Decision

### Job directory (the only driver–harness channel)

- Layout per Run under `THEOZOLITH_JOBS_DIR/<run-id>/`: `input/` (`manifest.json`, `prompt.md`, `issue.json`, `jobs/`), `output/` (`status.json`, `transcript.txt`, `hook-events.log`, `verdict.json`, `jobs/`), plus `checkout/` (run mode: the token-free clone) or `work/` (review mode: driver-seeded input files). Bind-mounted at `/job`; paths in the manifest are job-dir-relative so both sides resolve against their own mount.
- **Manifest**: `{run_id, mode: run|review, session, adapter, model, workdir, agent_timeout_seconds (3600), settle_seconds (20), startup_seconds (5), jobs_idle_timeout_seconds (600)}`.
- **Status**: `{phase: starting|agent|serving-jobs|done|failed, agent: {completed, timed_out, session_died}, error}`. Files any side polls for (status, job requests/results) are written atomically (tmp + rename) — a reader never sees partial JSON.
- **Jobs**: requests `input/jobs/NNN-<name>.json` `{name, command, timeout_seconds}`, answered as `output/jobs/NNN-<name>.json` `{name, ok, exit_code, output (last 4000 chars)}`. Driver-side sequencing = ascending file names; an empty `command` is the shutdown request. The harness answers an unreadable request file with a failed result so the queue never wedges; an idle queue past `jobs_idle_timeout_seconds` fails the harness (a dead driver must not leak containers indefinitely).
- **Decisions payload** (run mode): the agent writes `.theozolith/decisions.json` in the checkout (`decisions[{what,why}]`, `open_questions[]`, `remaining_work[]`, `dead_ends[]`); the driver reads it from the host. It and the harness's `.claude/settings.local.json` are pinned into `.git/info/exclude` (rewritten by the driver post-container) — pipeline metadata, never repo content. A Run whose agent leaves no parseable file still ships: the driver synthesizes a section saying so.
- **Verdict file** (review mode): the agent writes `verdict.json` in its working directory; the harness copies it to `output/verdict.json`. Driver-side validation is strict — object shape; `verdict ∈ {approve, revise, escalate}`; non-empty `evidence`; approve requires `deviation` and `risk` ∈ {low, medium, high}; revise requires a non-empty `revised_plan`; `resume_commit` string (empty = PR head at verdict time); `cherry_pick` a list of SHAs. **Final-round rule enforced here**: at the last budgeted round a revise verdict is invalid. Any invalid or missing file applies zero PR-side state; the round retries on the next poll (the driver stamps the round number itself; a file-supplied round is ignored).

### Harness (PID 1 of every run container)

- Flow: read manifest → tmux `new-session -d -s <session>` running the adapter's interactive command in the workdir → `pipe-pane` to `output/transcript.txt` → wait `startup_seconds` → inject the prompt by buffer paste (`load-buffer` + `paste-buffer`, then Enter; never per-key sends, never TUI scraping) → await completion → collect outputs → serve jobs (run mode) → kill session, write final status, exit. Session naming: `run-<run-id>`, `review-<pr>-round-<n>`; one session, one window — what `docker exec -it <container> tmux attach` reaches.
- **Completion detection (Claude adapter)**: the harness writes `.claude/settings.local.json` in the workdir with `Stop` and `UserPromptSubmit` hooks that append `stop` / `prompt` lines to `output/hook-events.log` (path via `THEOZOLITH_HOOK_LOG` in the session env). Complete when the last event is `stop` and the log has been quiet for `settle_seconds` — the settle window is what lets an attached human's queued input re-arm the wait instead of ending the Run under them. Backstops: the hard `agent_timeout_seconds`, and session death (agent process exited). All three outcomes are recorded in the status file; the driver ships best-effort regardless (only harness/container infrastructure failure aborts a Run with no PR).
- The interactive command is `claude --model <model> --dangerously-skip-permissions` — which is exactly why Runs execute inside a disposable container: the agent has no credentials to misuse and nothing durable to break (ADR-0013).

### Driver-side git handling (token-free checkouts)

- The checkout's remote is the tokenless HTTPS URL. Drivers authenticate clone/fetch/push through environment-level git config (`GIT_CONFIG_*` injecting a credential helper that reads the PAT from a separate env var): no token in `.git/config`, argv, or the worktree, so the mounted checkout is credential-free by construction and the PAT exists only in the driver's process env.
- After the container exits, the checkout's git metadata is hostile input: the driver rewrites `.git/config` to a known-good minimum (tokenless origin, `core.hooksPath` → an empty directory) before running any further git command there — neutralizing agent-written hooks, `core.fsmonitor`, credential helpers, URL rewrites (`insteadOf`), and filter drivers. The same sanitization runs right after clone as a clean baseline. Commits are made with per-command `-c user.name/email`; pushes use `--force-with-lease` only when the Reviewer's resume designation rewrote history (or when overwriting a never-designated branch from a crashed Run).

### Gate (first-party, driver-sequenced, harness-executed)

- Step contract unchanged from PR #2: `[steps.<name>]` tables in the target repo's `.theozolith/gate.toml` with `run` (required), `fix` (optional), `timeout` (optional, default 900s); canonical order test → docs → lint, unknown steps after, alphabetically; no config → one `info` finding, nothing runs; unreadable config → an `error` finding. The gate never blocks PR creation.
- Execution moves per ADR-0013: the driver reads the declarations and submits each command as a harness job inside the run container; agent-authored code never runs in the driver. Findings schema `{step, severity: error|warning|info, summary, detail, fixed}`. Mechanical-fix policy: only a repo-declared `fix` is applied, then the step's `run` re-executes; only a now-green step is recorded `fixed` (warning). A container that dies mid-gate becomes a single gate `error` finding and the Run still ships (best-effort).
- Push → PR → CI are the driver's side effects after the gate, not gate steps. Adversarial review belongs to the Reviewer, never the gate.

### Decisions Section (PR description)

- Unchanged from PR #2 (ADR-0008 schema): one block between `<!-- theozolith:decisions:begin/end -->` markers — human-readable markdown (Decisions made / Open questions / Remaining work / Dead ends tried / Gate findings) rendered from a machine JSON copy in an HTML comment. Replaced in place each round; per-round history lives in the evidence bundle.

### Reviewer driver

- Poll cadence: `THEOZOLITH_POLL_SECONDS` (default 60s), same knob as the Worker; each pass reviews every `pr_ready` PR without `needs_human`/`blocked`, sequentially. Reviewer downtime queues PRs; restart picks up cleanly (all state on GitHub).
- Review inputs are files in the container workspace: `issue.md`, `diff.patch` (truncated at 200k chars), `decisions.md`, `signals.md` (mechanical diff signals — evidence, not a grader). The judging agent writes `verdict.json`; the driver renders the published comment from the validated file — human-readable heading, evidence, revised plan + `Resume from commit`, plus the `<!-- theozolith:verdict -->` machine block the next Run parses. Comments after the latest verdict are review discussion (how a human's decision on a blocked PR reaches the next Run).
- Verdict application order (revise): comment → `attempt-N` → remove `pr_ready` → strip claim → re-queue `plan_ready` — the issue only becomes claimable after the plan the next Run needs is on the PR. Approve keeps `pr_ready`, adds `needs_human` + `deviation:*` + `risk:*`. Escalate removes `pr_ready`, adds `blocked` + `needs_human` with the bundle link.
- Budget: a PR already bearing `attempt-3` escalates deterministically (no model call). Under budget, the final-round rule is enforced twice: in the prompt (approve or escalate only) and in validation (a final-round revise is rejected, applying no state).
- Model configuration: `WORKER_MODEL` / `REVIEWER_MODEL` (or `THEOZOLITH_MODEL` per process); defaults `claude-sonnet-5` / `claude-fable-5`. The stronger-model requirement stays a deploy-time convention — the drivers cannot rank arbitrary model names.

### Evidence bundles

- Orphan branch `theozolith/evidence` in the target repo. Per Run: `runs/issue-<N>/<run-id>/` with `run.json` (worker, stack, model, container name, round, phases, PR, head, agent outcome, notes), `findings.json`, `decisions.json`, **`transcript.txt` (the full tmux pipe-pane capture — the audit trail for any human interaction mid-Run)**, `diffstat.txt`. Per review round: `runs/issue-<N>/reviews/round-<R>-<head12>.json` plus `-transcript.txt`. The bundle link cited in comments is the issue directory (`…/tree/theozolith/evidence/runs/issue-<N>`).
- `run-id` = `<utc-timestamp>-<worker-id>-<seq>`. Pushed with plain git, fresh-clone retry ×3 for concurrent writers. Evidence is traceability, not coordination: a failed push never fails a Run or a verdict.

### Container conventions and driver loop

- Names `ozolith-run-<run-id>` / `ozolith-review-<pr>-round-<n>`; labels `theozolith.run-id`, `theozolith.owner=<stack>`; launched `--detach --rm --init`; warm caches as named volumes (`THEOZOLITH_CACHE_VOLUMES`, default `theozolith-cache:/home/ozolith/.cache`). Secret env values pass to `docker run` as bare `-e NAME` (value from the CLI's environment), keeping them out of argv. A retried round removes its leftover same-name container before launching. Job-dir file ownership: build the image with `OZOLITH_UID` matching the driver user, or set `THEOZOLITH_CONTAINER_USER` (the image home is `a+rwX` for this).
- Driver loop: poll every `THEOZOLITH_POLL_SECONDS` (60s); a failed poll pass or crashed Run is logged and never kills the driver (GitHub calls already do exponential backoff + rate-limit handling in the client). `--once` = exactly one poll pass — for the Worker, at most one claim and one Run — then exit; the daemon-less dev mode (ADR-0013). Orphan reaping and claim janitoring stay manual until M3.

## Consequences

- **Positive**: every driver–harness exchange is an inspectable file with a strict schema — a wedged Run can be diagnosed from the job dir alone; the credential boundary is enforced by construction and auditable (scan env, config, argv); gate behavior remains entirely target-repo-declared; a hostile agent session can corrupt only its own outputs — its git-metadata attack surface toward the driver is sanitized away; the Worker↔Reviewer interlock stays a documented comment format any adapter or human can produce.
- **Negative**: completion detection depends on per-adapter hook mechanics (a Claude hooks-format change breaks it — caught by the CI image build + e2e walk, fixed in one adapter file); the settle window adds ~20s latency per session end; polling files at 0.5–1s granularity is crude next to inotify (acceptable at M2 scale); PR bodies and comments carry JSON blobs.
- **Neutral**: the evidence branch grows unboundedly (operator concern until a janitor exists); job dirs are removed after every Run, so mid-Run debugging means attaching, not post-mortems — the evidence bundle is the post-mortem.

## Alternatives considered

- **Completion by TUI scraping or process-exit** (rejected: scraping is banned by the brief and brittle; the interactive CLI never exits on its own).
- **First Stop event = done, no settle window** (rejected: a human's queued mid-Run input would be cut off; the settle window makes attached-operator input safe by construction).
- **A network/socket channel between driver and harness** (rejected by ADR-0013: files only; no in-container reporter, nothing to authenticate).
- **Committing inside the container instead of driver-side sanitize + commit** (viable, but pushes git identity and sequencing into the harness — the dumb-plumbing rule loses; sanitization keeps all git policy in one trusted place).
- **Tokened clone URL with post-clone scrubbing** (rejected: the token transits `.git/config`; env-level config never writes it anywhere).
- **Auto-detected gate steps; Decisions Section as a comment; evidence in refs/notes or a separate repo** (all rejected for the ADR-0011 reasons, which stand).
