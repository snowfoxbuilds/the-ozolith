Status: ACCEPTED

Date: 2026-07-21

# ADR-0019: Headless Implementer and Reviewer Runs; the Flight Deck owns interactivity

## Context

The always-interactive contract (2026-07-15) ran every Run's agent in an attachable tmux session, with prompt injection by buffer paste and completion detected via per-adapter Stop hooks plus a settle window. The fragile seam is the TUI: an unexpected dialog (onboarding change, trust prompt, update notice) silently swallows the pasted prompt and the Run burns the full agent timeout before failing — an automation contract riding an unversioned vendor TUI surface. Pre-testing review 2026-07-21 re-ranked the trade under the binding priority ordering of ADR-0016: stability and token efficiency over uptime — and over live steering.

## Decision

- Implementer Runs and Review Runs execute **headless**: the harness invokes the adapter's one-shot mode (Claude: `claude -p` with `--dangerously-skip-permissions` and structured output), passing the prompt at invocation. Completion is process exit; the hard agent timeout remains as backstop. ADR-0014's Stop-hook / settle-window / hook-events completion machinery is deleted.
- **Headless only.** No opt-in interactive Run mode: a dual mode means two harness contracts, two completion detectors, and two transcript paths, with the interactive one rarely exercised and rotting.
- Run containers are never attach targets; mid-Run attach and human steering are removed. Run diagnostics is read-only: progress telemetry (phase, elapsed time, counters, transcript tail) plus evidence-bundle transcripts. The transcript is captured from the headless session's structured output stream, which also supplies token usage (fills ADR-0018's null token count).
- The **Flight Deck** is the interactive surface: a human-driven agent container declared as a container-kind Stack — agent CLI in a discoverably named tmux session, the web terminal's primary attach target. Used for issue drafting and non-decomposable work (cross-cutting refactors, design-in-flux). The Flight Deck holds GitHub credentials under human supervision — a **separate machine identity** with a fine-grained PAT (issues, PRs, contents; **no merge permission**), never the operator's own PAT: a prompt-injected session must not be able to perform human-authority transitions (above all the merge) as the human, so the human merge gate stays human by construction and Flight Deck output is distinguishable in the audit trail. It is not a pipeline actor: it never claims issues and holds no transition authority.
- Credential isolation for Runs is unchanged: run containers hold no GitHub credentials; drivers own all GitHub I/O. The job-directory interface, gate-steps-as-harness-jobs, and the validate-verdict harness job are unchanged.
## Consequences

- **Positive**: completion detection becomes the adapter's supported exit contract instead of TUI hook mechanics — no settle-window latency and no paste-swallowed-by-dialog failure class; token counts land in telemetry; the harness sheds tmux, pipe-pane, and hook plumbing.
- **Negative**: a live Run can be watched or killed but never corrected — human steering of in-flight Runs is gone; the Flight Deck reintroduces a PAT inside an LLM container, accepted under human supervision (a prompt-injected Flight Deck session can act with its identity's GitHub authority while the operator is not watching — do not leave Flight Deck sessions running unattended; the no-merge machine identity bounds the blast radius).
- **Neutral**: the web terminal's scope shrinks to the Flight Deck and other configured Stacks; ADR-0018's PTY bridge, gates, and audit log are unchanged; the "audited human steering" transcript concept now applies only to Flight Deck sessions.
## Alternatives Considered

- **Keep always-interactive** (rejected: prompt delivery and completion detection ride an unversioned vendor TUI; permission prompts were already bypassed by flags, so interactivity bought only live steering at the cost of the paste-injection failure class).
- **Headless default + interactive debug flag per Run** (rejected: two harness contracts; the debug path rots unexercised; the Flight Deck covers hands-on reproduction).
- **Credential-free Flight Deck** (rejected by the operator: drafting/posting friction outweighs the supervised-session risk).
## Amends

- ADR-0013 (harness tmux mechanics and the attach-target consequence) and ADR-0014 (hook-based completion, settle window, tmux transcript capture) in part. Supersedes the 2026-07-15 always-interactive decisions in [AGENTIC-CODING-PIPELINE.md](http://agentic-coding-pipeline.md/) and [NODE-SUBSTRATE.md](http://node-substrate.md/) (prior design archived in Historical Context). ADR-0018's terminal machinery stands; its attach targets become the Flight Deck and other configured Stacks.
