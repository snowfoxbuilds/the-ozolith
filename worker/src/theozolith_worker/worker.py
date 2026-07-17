"""The Worker driver: a node-resident dispatch-run loop bound to one Agent.

The trusted, credentialed half of the Worker (ADR-0013): it holds the
machine-user PAT, performs all non-claim GitHub I/O, and never executes
repository code or model output. Claims arrive through the Control Node's
dispatch endpoint (ADR-0017): the Control Node writes each claim to GitHub
itself and hands the issue over in the same response — the driver never
assigns, labels, or verifies a claim into existence. Runs execute
sequentially, one at a time, each in a fresh ephemeral run container, with
the ADR-0016 local-retry budget handled per claim (``execute_claim``).

Startup and idle passes run the boot-time evidence sweep: orphaned job
directories are pushed to the evidence branch (swept: true) and deleted only
after the push confirms.

Two modes: the continuous loop (production; supervised by the Node Daemon)
and ``--once`` — a single dispatch-run pass. Both require a reachable
Control Node: with it down, in-flight Runs finish and publish while new
claims pause (ADR-0017 — no second claim path exists).
"""

from __future__ import annotations

import argparse
import sys
import time

from theozolith_worker.config import ConfigError, DriverConfig, load_config
from theozolith_worker.containers import DockerEngine, Engine
from theozolith_worker.dispatch import DispatchClient, WorkDispatch
from theozolith_worker.events import EventSink, make_sink
from theozolith_worker.githubapi import GitHubClient, Issue
from theozolith_worker.runner import execute_claim
from theozolith_worker.sessions import ContainerSession, SessionFactory
from theozolith_worker.sweep import sweep_orphans


def _log(message: str) -> None:
    print(message, flush=True)


def container_session_factory(engine: Engine) -> SessionFactory:
    return lambda spec, job, manifest: ContainerSession(engine, spec, job, manifest)


def _granted_issue(granted: dict, login: str) -> Issue:
    """The dispatch answer as an Issue: the claim already exists on GitHub
    (assigned to this driver, in_progress applied) — no re-read needed."""
    return Issue(
        number=int(granted["number"]),
        title=str(granted.get("title", "")),
        body=str(granted.get("body", "")),
        labels=set(granted.get("labels", [])),
        assignees=[login],
        is_pr=False,
    )


def run_worker(
    config: DriverConfig,
    client: GitHubClient | None = None,
    session_factory: SessionFactory | None = None,
    dispatch: WorkDispatch | None = None,
    *,
    sleep=time.sleep,
    once: bool = False,
    log=_log,
    sink: EventSink | None = None,
) -> int:
    """The dispatch-run loop; returns the number of Runs executed."""
    client = client or GitHubClient(config.repo, config.token, api_url=config.api_url)
    session_factory = session_factory or container_session_factory(DockerEngine())
    dispatch = dispatch or DispatchClient(
        config.control_node_url, config.control_token, ca=config.control_ca, log=log
    )
    sink = sink or make_sink(config, log)
    me = client.viewer_login()
    log(f"worker driver {config.worker_id} ({me}) requesting work for {config.repo} via dispatch")
    sweep_orphans(config, log=log)  # boot-time evidence sweep (ADR-0016)

    runs = 0
    while True:
        granted: dict | None = None
        try:
            granted = dispatch.request_work(config.worker_id, config.node_name, me)
        except Exception as exc:
            log(f"dispatch pass failed: {exc}")
        if granted is not None:
            issue = _granted_issue(granted, me)
            log(f"granted #{issue.number} ({issue.title}); executing the claim")
            try:
                reports = execute_claim(
                    config, client, issue, session_factory, log=log, sink=sink
                )
                runs += len(reports)
                for report in reports:
                    log(
                        f"run {report.run_id} finished: phase={report.phase} "
                        f"pr={report.pr_number} round={report.round} "
                        f"agent={report.agent_outcome or 'n/a'}"
                    )
            except Exception as exc:
                log(f"claim for #{issue.number} crashed: {exc}")
        if once:
            return runs
        if granted is None:
            sweep_orphans(config, log=log)  # retry any kept job dirs while idle
            sleep(config.poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-worker",
        description=(
            "TheOzolith Worker driver: request claims from the Control Node, execute Runs in "
            "ephemeral containers, ship best-effort PRs."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="One dispatch-run pass (at most one claim), then exit. Requires a reachable"
        " Control Node (ADR-0017).",
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
