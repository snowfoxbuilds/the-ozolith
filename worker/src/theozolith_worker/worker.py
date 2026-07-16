"""The Worker driver: a node-resident poll-claim-run loop bound to one Agent.

The trusted, credentialed half of the Worker (ADR-0013): it holds the
machine-user PAT, performs all GitHub I/O, and never executes repository
code or model output. It polls one label on one object type (plan_ready
issues), claims via the Claim Protocol, and executes Runs sequentially, one
at a time, each in a fresh ephemeral run container.

Two modes: the continuous loop (production; supervised by the Node Daemon
from M3 on) and ``--once`` — a single poll-claim-run pass, the daemon-less
dev mode. Crashed Runs are logged and skipped: they leave no PR-side state
and consume no round budget; the zombie-claim janitor (M3) re-queues them.
"""

from __future__ import annotations

import argparse
import sys
import time

from theozolith_worker.bootstrap.vocabulary import PLAN_READY
from theozolith_worker.claim import attempt_claim, claimable
from theozolith_worker.config import ConfigError, DriverConfig, load_config
from theozolith_worker.containers import DockerEngine, Engine
from theozolith_worker.githubapi import GitHubClient, Issue
from theozolith_worker.prefilter import ClaimPrefilter, make_prefilter
from theozolith_worker.runner import execute_run
from theozolith_worker.sessions import ContainerSession, SessionFactory


def _log(message: str) -> None:
    print(message, flush=True)


def container_session_factory(engine: Engine) -> SessionFactory:
    return lambda spec, job, manifest: ContainerSession(engine, spec, job, manifest)


def _claim_next(client: GitHubClient, prefilter: ClaimPrefilter) -> Issue | None:
    for issue in client.list_open_issues(PLAN_READY):
        if claimable(issue) and attempt_claim(client, issue, prefilter):
            return issue
    return None


def run_worker(
    config: DriverConfig,
    client: GitHubClient | None = None,
    session_factory: SessionFactory | None = None,
    prefilter: ClaimPrefilter | None = None,
    *,
    sleep=time.sleep,
    once: bool = False,
    log=_log,
) -> int:
    """The poll-claim-run loop; returns the number of Runs executed."""
    client = client or GitHubClient(config.repo, config.token, api_url=config.api_url)
    session_factory = session_factory or container_session_factory(DockerEngine())
    prefilter = prefilter or make_prefilter(config.control_node_url)
    me = client.viewer_login()
    log(f"worker driver {config.worker_id} ({me}) polling {config.repo} for {PLAN_READY}")

    runs = 0
    while True:
        claimed: Issue | None = None
        try:
            claimed = _claim_next(client, prefilter)
        except Exception as exc:
            log(f"poll pass failed: {exc}")
        if claimed is not None:
            runs += 1
            log(f"claimed #{claimed.number} ({claimed.title}); starting run {runs}")
            try:
                report = execute_run(config, client, claimed, session_factory, log=log)
                log(
                    f"run {report.run_id} finished: phase={report.phase} "
                    f"pr={report.pr_number} round={report.round} "
                    f"agent={report.agent_outcome or 'n/a'}"
                )
            except Exception as exc:
                log(f"run for #{claimed.number} crashed: {exc}")
        if once:
            return runs
        if claimed is None:
            sleep(config.poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-worker",
        description=(
            "TheOzolith Worker driver: poll plan_ready issues, claim, execute Runs in "
            "ephemeral containers, ship best-effort PRs."
        ),
    )
    parser.add_argument(
        "--once", action="store_true", help="One poll-claim-run pass (at most one Run), then exit."
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(role="worker")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        run_worker(config, once=args.once)
    except KeyboardInterrupt:
        print("worker driver stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
