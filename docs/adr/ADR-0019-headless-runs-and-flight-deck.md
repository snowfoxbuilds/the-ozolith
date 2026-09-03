Status: ACCEPTED

Date: 2026-07-21

# ADR-0019: Headless Implementer and Reviewer Runs; the Flight Deck owns interactivity

## Context

The always-interactive contract (2026-07-15) ran every Run's agent in an attachable tmux session, with prompt injection by buffer paste and completion detected via per-adapter Stop hooks plus a settle window. The fragile seam is the TUI: an unexpected dialog (onboarding change, trust prompt, update notice) silently swallows the pasted prompt and the Run burns the full agent timeout before failing — an automation contract riding an unversioned vendor TUI surface. Pre-testing review 2026-07-21 re-ranked the trade under the binding priority ordering of ADR-0016: stability and token efficiency over uptime — and over live steering. This decision amends ADR-0013 and ADR-0014 in part, and supersedes the 2026-07-15 always-interactive decisions in [AGENTIC-CODING-PIPELINE.md](../specs/AGENTIC-CODING-PIPELINE.md) and [NODE-SUBSTRATE.md](../specs/NODE-SUBSTRATE.md) (prior design archived in Historical Context); ADR-0018's terminal machinery stands, with its attach targets becoming the Flight Deck and other configured Stacks.

## Decision

- Implementer Runs and Review Runs execute **headless**: the harness invokes the adapter's one-shot mode (Claude: `claude -p` with `--dangerously-skip-permissions` and structured output), passing the prompt at invocation (amended 2026-07-22, #7). Completion is process exit; the hard agent timeout remains as backstop. ADR-0014's Stop-hook / settle-window / hook-events completion machinery is deleted.
- **Headless only.** No opt-in interactive Run mode: a dual mode means two harness contracts, two completion detectors, and two transcript paths, with the interactive one rarely exercised and rotting.
- Run containers are never attach targets; mid-Run attach and human steering are removed. Run diagnostics is read-only: progress telemetry (phase, elapsed time, counters, transcript tail) plus evidence-bundle transcripts. The transcript is captured from the headless session's structured output stream, which also supplies token usage (fills ADR-0018's null token count).
- The **Flight Deck** is the interactive surface: a human-driven agent container declared as a container-kind Stack — agent CLI in a discoverably named tmux session, the web terminal's primary attach target. Used for issue drafting and non-decomposable work (cross-cutting refactors, design-in-flux). The Flight Deck holds GitHub credentials under human supervision — a **separate machine identity** with a fine-grained PAT (issues, PRs, contents; **no merge permission**), never the operator's own PAT: a prompt-injected session must not be able to perform human-authority transitions (above all the merge) as the human, so the human merge gate stays human by construction and Flight Deck output is distinguishable in the audit trail. It is not a pipeline actor: it never claims issues and holds no transition authority (amended 2026-08-27).
- Credential isolation for Runs is unchanged: run containers hold no GitHub credentials; drivers own all GitHub I/O. The job-directory interface, gate-steps-as-harness-jobs, and the validate-verdict harness job are unchanged.

## Consequences

- **Positive**: completion detection becomes the adapter's supported exit contract instead of TUI hook mechanics — no settle-window latency and no paste-swallowed-by-dialog failure class; token counts land in telemetry; the harness sheds tmux, pipe-pane, and hook plumbing.
- **Negative**: a live Run can be watched or killed but never corrected — human steering of in-flight Runs is gone; the Flight Deck reintroduces a PAT inside an LLM container, accepted under human supervision (a prompt-injected Flight Deck session can act with its identity's GitHub authority while the operator is not watching — do not leave Flight Deck sessions running unattended; the no-merge machine identity bounds the blast radius).
- **Neutral**: the web terminal's scope shrinks to the Flight Deck and other configured Stacks; ADR-0018's PTY bridge, gates, and audit log are unchanged; the "audited human steering" transcript concept now applies only to Flight Deck sessions.

## Alternatives Considered

- **Keep always-interactive** (rejected: prompt delivery and completion detection ride an unversioned vendor TUI; permission prompts were already bypassed by flags, so interactivity bought only live steering at the cost of the paste-injection failure class).
- **Headless default + interactive debug flag per Run** (rejected: two harness contracts; the debug path rots unexercised; the Flight Deck covers hands-on reproduction).
- **Credential-free Flight Deck** (rejected by the operator: drafting/posting friction outweighs the supervised-session risk).

## Amendments

- **2026-07-22 (#7)**: Pointer prompt — the harness passes a constant pointer prompt at invocation ("work on the task specified in the mounted task file") instead of embedding the task content in the argv. The driver remains responsible for fetching the task and writing it into the per-Run job directory the container mounts; the argv stays constant-size regardless of task size, and the full task rides the existing file interface. Applies symmetrically to Implementer and Review Runs.
- **2026-08-27 (operator ruling — Sean, 2026-08-26/27; ozolith-configs PR #2)**: delegated initial `plan_ready` stamp.
  - **The Flight Deck may execute the initial plan_ready transition as the human's scribe.** The decision remains the human's; the deck only carries out a confirmed transition. It acts solely on an explicit instruction given in the live Flight Deck conversation that names each work issue and confirms its exact `risk:low|medium|high` label; it then applies `plan_ready` plus the confirmed `risk:*` and removes `draft` on exactly the named agent-created work issues, verifying by reading the labels back.
  - **What never authorizes**: publication approval (approving an issue set for posting is not readiness approval), repository files, issue or PR text, tool output, retrieved content, and ambiguous conversational remarks. Tracking issues are never stamped.
  - **What is not delegated**: issue claiming, blocked-decision answers, every Reviewer transition (verdicts, deviation/risk/attempt labels, re-queues), PR approval, and the merge. The Flight Deck machine identity keeps **no merge permission**, and every other "not a pipeline actor" property stands — it never claims issues and holds no further transition authority.
  - **Accepted trade, recorded**: this delegation is prompt-enforced, not identity-enforced. Under the prior doctrine any deck-applied plan_ready was recognizable as forgery by construction; now a prompt-injected deck session could apply a legitimate-looking stamp. Accepted because deck sessions run under live human supervision and the stamp is narrowly bounded (draft → plan_ready on named work issues only); label events carry the Flight Deck identity, and Flight Deck session transcripts are the audit trail for whether an explicit human instruction preceded each stamp.
  - **Distinct from ADR-0008's Reviewer re-queue**: the Reviewer holds a *standing* delegation — it re-queues an in-flight issue to plan_ready autonomously at each revise verdict, exercising delegated judgment inside the review loop. The Flight Deck holds *no standing authority*: each initial stamp is the execution of one explicit human instruction from the live conversation — delegated hands, never delegated judgment.

## Relevant PRs

- #7 — pointer-prompt review round that changed the harness invocation to pass a constant pointer prompt instead of embedding task content in the argv.
