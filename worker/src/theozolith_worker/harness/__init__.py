"""The agent harness: credential-free plumbing, PID 1 of a run container.

It starts the interactive agent session in tmux, injects the prompt, awaits
the per-adapter completion hook (hard timeout as backstop), serves
driver-sequenced jobs (gate steps), writes outputs into the job directory,
and exits — container lifetime = Run lifetime (ADR-0013). It holds no GitHub
credential and makes no pipeline decision.
"""

from theozolith_worker.harness.main import main, run_harness

__all__ = ["main", "run_harness"]
