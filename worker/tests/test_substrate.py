"""Pipeline ⇄ substrate integration: the real drivers against a real
Control Node (uvicorn on a socket), plus the degradation guarantees.

Acceptance 1 (kill the Control Node mid-Run → the PR still ships; events
resume on restart) and acceptance 4 (two Workers, one issue → the pre-filter
serializes them; GitHub verify still runs on the winner) live here, where
the M2 end-to-end harness already is.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import uvicorn
from conftest import Harness
from theozolith_control.app import create_app
from theozolith_control.crypto import SecretBox, generate_key
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store
from theozolith_worker.events import ControlNodeSink
from theozolith_worker.prefilter import ControlNodePrefilter
from theozolith_worker.reviewer import run_reviewer
from theozolith_worker.worker import run_worker

NODE_TOKEN = "node-token"
DEAD_URL = "http://127.0.0.1:9"  # the discard port: nothing ever answers


class LiveControl:
    """A real Control Node on a real socket, state persisted under data_dir
    so a 'restarted' instance resumes the same store."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        settings = ControlSettings(
            data_dir=data_dir,
            config_repo=data_dir / "configs",
            node_token=NODE_TOKEN,
            admin_token="admin-token",
            repo=None,
            github_token=None,
            api_url="",
            zombie_grace_seconds=600,
            janitor_sweep_seconds=60,
            audit_sweep_seconds=300,
            claim_ttl_seconds=120,
        )
        self.store = Store(settings.db_path)
        app = create_app(settings, self.store, SecretBox(generate_key()))
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> LiveControl:
        self._thread.start()
        deadline = time.time() + 15
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("control node did not start")
            time.sleep(0.02)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(10)

    def __exit__(self, *exc) -> None:
        self.stop()

    def sink(self) -> ControlNodeSink:
        return ControlNodeSink(self.url, token=NODE_TOKEN, timeout=2.0)

    def prefilter(self, timeout: float = 2.0) -> ControlNodePrefilter:
        return ControlNodePrefilter(self.url, timeout=timeout, token=NODE_TOKEN)

    def run_phases(self, issue: int) -> list[str]:
        return [e["phase"] for e in self.store.events(type="theozolith.run", issue=issue)]


class DiesAfterClaim:
    """Sink wrapper that kills the Control Node right after the claimed
    event — the Run proceeds with the control plane dead under it."""

    def __init__(self, live: LiveControl):
        self._inner = live.sink()
        self._live = live

    def emit(self, event: dict) -> None:
        self._inner.emit(event)
        if event.get("phase") == "claimed":
            self._live.stop()


def worker_once(harness: Harness, *, sink, prefilter) -> int:
    return run_worker(
        harness.worker_config,
        harness.worker_client,
        harness.session_factory,
        prefilter,
        once=True,
        log=harness.logs.append,
        sink=sink,
    )


# -- acceptance 1: degradation ---------------------------------------------------------


def test_control_node_killed_mid_run_still_ships_the_pr(harness: Harness, tmp_path: Path):
    issue = harness.file_issue("degradation", "survive the outage")

    with LiveControl(tmp_path / "control") as live:
        runs = worker_once(harness, sink=DiesAfterClaim(live), prefilter=live.prefilter())

    assert runs == 1
    pr = harness.worker_client.find_open_pr_by_head(f"ozolith/issue-{issue}")
    assert pr is not None
    assert any(label["name"] == "pr_ready" for label in harness.fake.issues[pr.number]["labels"])
    # The claimed event landed before the kill; nothing after did — and
    # nothing about the Run cared.
    assert live.run_phases(issue) == ["claimed"]


def test_fully_dead_control_node_never_slows_the_pipeline(harness: Harness):
    """GitHub-only operation is the permanent degraded mode (ADR-0002)."""
    issue = harness.file_issue("dead control", "no control node at all")
    sink = ControlNodeSink(DEAD_URL, token=NODE_TOKEN, timeout=0.3)
    prefilter = ControlNodePrefilter(DEAD_URL, timeout=0.3, token=NODE_TOKEN)

    assert worker_once(harness, sink=sink, prefilter=prefilter) == 1
    assert harness.worker_client.find_open_pr_by_head(f"ozolith/issue-{issue}") is not None


def test_events_resume_when_the_control_node_restarts(harness: Harness, tmp_path: Path):
    data = tmp_path / "control"
    with LiveControl(data) as live:
        first = harness.file_issue("first", "before the outage")
        worker_once(harness, sink=DiesAfterClaim(live), prefilter=live.prefilter())

    # Restart on the same data dir: the same store picks back up.
    with LiveControl(data) as reborn:
        second = harness.file_issue("second", "after the restart")
        worker_once(harness, sink=reborn.sink(), prefilter=reborn.prefilter())

        assert reborn.run_phases(first) == ["claimed"]
        assert reborn.run_phases(second) == ["claimed", "gate", "pr-open"]


# -- acceptance 4: the pre-filter race --------------------------------------------------


def test_prefilter_serializes_two_workers_and_github_verify_still_runs(
    harness: Harness, tmp_path: Path
):
    issue = harness.file_issue("contended", "two workers want this")

    with LiveControl(tmp_path / "control") as live:
        first = live.prefilter()
        second = live.prefilter()

        # Both Workers race to the pre-filter; it serializes them. (The
        # Claim Protocol identifies a Worker by its GitHub login.)
        assert first.allows(issue, "ozolith-worker-a") is True
        assert second.allows(issue, "ozolith-worker-b") is False

        # The winner proceeds through the FULL Claim Protocol on GitHub —
        # the pre-filter answer was never a claim (ADR-0002).
        runs = worker_once(harness, sink=live.sink(), prefilter=first)

    assert runs == 1
    assign_writes = [
        path for method, path in harness.worker_client.writes if path.endswith("/assignees")
    ]
    assert assign_writes, "GitHub assign-and-verify must still run on the winner"
    assert harness.worker_client.find_open_pr_by_head(f"ozolith/issue-{issue}") is not None


# -- the events the substrate consumes ---------------------------------------------------


def test_full_review_cycle_emits_run_and_review_events(harness: Harness, tmp_path: Path):
    with LiveControl(tmp_path / "control") as live:
        issue = harness.file_issue("observed", "watch me work")
        worker_once(harness, sink=live.sink(), prefilter=live.prefilter())
        harness.reviewer_replies.append(
            {
                "verdict": "approve",
                "deviation": "low",
                "risk": "low",
                "evidence": "The change matches the issue's acceptance criteria.",
                "revised_plan": "",
                "resume_commit": "",
                "cherry_pick": [],
            }
        )
        run_reviewer(
            harness.reviewer_config,
            harness.reviewer_client,
            harness.session_factory,
            once=True,
            log=harness.logs.append,
            sink=live.sink(),
        )

        assert live.run_phases(issue) == ["claimed", "gate", "pr-open"]
        reviews = live.store.events(type="theozolith.review", issue=issue)
        assert [(r["round"], r["verdict"]) for r in reviews] == [(1, "approve")]
        run_events = live.store.events(type="theozolith.run", issue=issue)
        assert {e["worker"] for e in run_events} == {"worker-a"}
        assert all(e["node"] for e in run_events)  # the janitor keys on this


class RecordingSink:
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


def test_failed_run_emits_failed_then_escalated(harness: Harness):
    from theozolith_worker.jobdir import AgentOutcome

    issue = harness.file_issue("doomed", "the agent times out twice")
    sink = RecordingSink()

    harness.worker_behaviors.append(lambda prompt, cwd: AgentOutcome(timed_out=True))
    run_worker(
        harness.worker_config,
        harness.worker_client,
        harness.session_factory,
        once=True,
        log=harness.logs.append,
        sink=sink,
    )
    harness.worker_behaviors.append(lambda prompt, cwd: AgentOutcome(timed_out=True))
    run_worker(
        harness.worker_config,
        harness.worker_client,
        harness.session_factory,
        once=True,
        log=harness.logs.append,
        sink=sink,
    )

    phases = [(e["issue"], e["phase"]) for e in sink.events]
    assert phases == [
        (issue, "claimed"),
        (issue, "failed"),
        (issue, "claimed"),
        (issue, "escalated"),
    ]
