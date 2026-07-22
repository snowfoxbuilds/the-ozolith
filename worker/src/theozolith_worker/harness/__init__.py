"""The agent harness: credential-free plumbing, PID 1 of a run container.

It invokes the agent headless — the adapter's one-shot command with the
prompt passed at invocation (ADR-0019) — captures the structured output
stream as the transcript, treats process exit as completion (hard timeout
as backstop), serves driver-sequenced jobs (gate steps), writes outputs
into the job directory, and exits — container lifetime = Run lifetime
(ADR-0013). It holds no GitHub credential and makes no pipeline decision.
"""

from theozolith_worker.harness.main import main, run_harness

__all__ = ["main", "run_harness"]
