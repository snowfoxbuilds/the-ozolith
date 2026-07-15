"""The Worker actor: a long-lived poll-claim-run loop bound to one Agent.

Polls one label on one object type (plan_ready issues), claims via the Claim
Protocol, executes Runs sequentially one at a time, and exits after N Runs so
the container restarts fresh (recycled on schedule: long-lived, never
immortal). Crashed Runs are logged and skipped — they leave no PR-side state
and consume no round budget; the zombie-claim janitor (M3) re-queues them.
"""

from __future__ import annotations

import argparse
import sys
import time

from theozolith_worker.adapters import Adapter, make_adapter
from theozolith_worker.bootstrap.vocabulary import PLAN_READY
from theozolith_worker.claim import attempt_claim, claimable
from theozolith_worker.config import ActorConfig, ConfigError, load_config
from theozolith_worker.githubapi import GitHubClient
from theozolith_worker.prefilter import ClaimPrefilter, make_prefilter
from theozolith_worker.runner import execute_run


def _log(message: str) -> None:
    print(message, flush=True)


def run_worker(
    config: ActorConfig,
    client: GitHubClient | None = None,
    adapter: Adapter | None = None,
    prefilter: ClaimPrefilter | None = None,
    *,
    sleep=time.sleep,
    once: bool = False,
    log=_log,
) -> int:
    """The poll-claim-run loop; returns the number of Runs executed."""
    client = client or GitHubClient(config.repo, config.token, api_url=config.api_url)
    adapter = adapter or make_adapter(config.adapter, config.model)
    prefilter = prefilter or make_prefilter(config.control_node_url)
    me = client.viewer_login()
    log(f"worker {config.worker_id} ({me}) polling {config.repo} for {PLAN_READY}")

    runs = 0
    while runs < config.recycle_runs:
        claimed = None
        for issue in client.list_open_issues(PLAN_READY):
            if claimable(issue) and attempt_claim(client, issue, prefilter):
                claimed = issue
                break
        if claimed is not None:
            runs += 1
            log(f"claimed #{claimed.number} ({claimed.title}); run {runs}/{config.recycle_runs}")
            try:
                report = execute_run(config, client, adapter, claimed)
                log(
                    f"run {report.run_id} finished: phase={report.phase} "
                    f"pr={report.pr_number} round={report.round}"
                )
            except Exception as exc:
                log(f"run for #{claimed.number} crashed: {exc}")
        if once:
            break
        if claimed is None:
            sleep(config.poll_seconds)

    log(f"recycle point reached ({runs} runs); exiting for a fresh container")
    return runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-worker",
        description="TheOzolith Worker: poll plan_ready issues, claim, run, ship best-effort PRs.",
    )
    parser.add_argument(
        "--once", action="store_true", help="One poll pass (at most one Run), then exit."
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(role="worker")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run_worker(config, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
