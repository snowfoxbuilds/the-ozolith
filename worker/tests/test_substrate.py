"""Pipeline ⇄ substrate integration: the real drivers against a real
Control Node (uvicorn on a socket), plus the ADR-0017 availability rules.

Claim dispatch (write-through), grant activation, the killed-mid-Run
degradation guarantee, and the failed→escalated event sequence live here,
where the M2 end-to-end harness already is.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import uvicorn
from conftest import Harness, RecordingSink
from theozolith_control.app import create_app
from theozolith_control.crypto import SecretBox, generate_key
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store
from theozolith_worker.dispatch import DispatchClient
from theozolith_worker.events import ControlNodeSink
from theozolith_worker.githubapi import GitHubClient
from theozolith_worker.jobdir import AgentOutcome
from theozolith_worker.reviewer import run_reviewer
from theozolith_worker.worker import run_worker

NODE_TOKEN = "node-token"
CONTROL_LOGIN = "ozolith-control"
DEAD_URL = "http://127.0.0.1:9"  # the discard port: nothing ever answers


class LiveControl:
    """A real Control Node on a real socket, state persisted under data_dir
    so a 'restarted' instance resumes the same store. Its GitHub half is the
    harness's FakeGitHub, spoken through the real rate-limited client — so
    dispatch's write-through claims land in the same repo the drivers see."""

    def __init__(self, data_dir: Path, harness: Harness):
        self.data_dir = data_dir
        settings = ControlSettings(
            data_dir=data_dir,
            config_repo=data_dir / "configs",
            node_token=NODE_TOKEN,
            admin_token="admin-token",
            repo=harness.fake.repo,
            github_token="tok-control",
            api_url="",
            zombie_grace_seconds=600,
            janitor_sweep_seconds=60,
            activation_window_seconds=60,
            tail_budget_bytes=10 * 1024**3,
        )
        harness.fake.register("tok-control", CONTROL_LOGIN)
        self.store = Store(settings.db_path)
        github = GitHubClient(
            harness.fake.repo, "tok-control", transport=harness.fake, sleep=lambda s: None
        )
        app = create_app(settings, self.store, SecretBox(generate_key()), github_client=github)
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

    def dispatch(self, timeout: float = 5.0) -> DispatchClient:
        return DispatchClient(self.url, NODE_TOKEN, timeout=timeout)

    def run_phases(self, issue: int) -> list[str]:
        return [e["phase"] for e in self.store.events(type="theozolith.run", issue=issue)]


class DiesAfterClaim:
    """Sink wrapper that kills the Control Node right after the claimed
    event — the Run proceeds with the control plane dead under it."""

    def __init__(self, live: LiveControl):
        self._inner = live.sink()
        self._live = live

    def emit(self, event: dict) -> bool:
        landed = self._inner.emit(event)
        if event.get("phase") == "claimed":
            self._live.stop()
        return landed


def worker_once(harness: Harness, *, sink, dispatch) -> int:
    return run_worker(
        harness.worker_config,
        harness.worker_client,
        harness.session_factory,
        dispatch,
        once=True,
        log=harness.logs.append,
        sink=sink,
    )


# -- write-through dispatch against a live Control Node -------------------------


def test_live_dispatch_claims_on_github_before_the_driver_acts(harness: Harness, tmp_path: Path):
    """Acceptance 9 end to end: real driver, real Control Node, real client
    — the claim on GitHub is the Control Node's write, not the driver's."""
    issue = harness.file_issue("dispatched", "claim me through the control node")

    with LiveControl(tmp_path / "control", harness) as live:
        runs = worker_once(harness, sink=live.sink(), dispatch=live.dispatch())

        assert runs == 1
        # The claim writes came from the control identity, none from the driver.
        claim_writers = {
            actor
            for actor, _, path, _ in harness.fake.write_log
            if f"/issues/{issue}/" in path and ("assignees" in path or "labels" in path)
        }
        assert claim_writers == {CONTROL_LOGIN}
        assert harness.fake.assignees_of(issue) == ["ozolith-worker-a"]
        # Activation: the claimed event landed, so the grant is retired.
        assert live.store.granted_issues() == set()
        assert live.run_phases(issue) == ["claimed", "gate", "pr-open"]

    pr = harness.worker_client.find_open_pr_by_head(f"ozolith/issue-{issue}")
    assert pr is not None


def test_two_drivers_one_issue_dispatch_serializes(harness: Harness, tmp_path: Path):
    issue = harness.file_issue("contended", "two workers want this")

    with LiveControl(tmp_path / "control", harness) as live:
        first = live.dispatch().request_work("worker-a", "box1", "ozolith-worker-a")
        second = live.dispatch().request_work("worker-b", "box2", "ozolith-worker-b")

    assert first is not None and first["number"] == issue
    assert second is None  # granted (unactivated) issues are never re-granted
    assert harness.fake.assignees_of(issue) == ["ozolith-worker-a"]


# -- availability (ADR-0017) ----------------------------------------------------


def test_control_node_killed_mid_run_still_ships_the_pr(harness: Harness, tmp_path: Path):
    issue = harness.file_issue("degradation", "survive the outage")

    with LiveControl(tmp_path / "control", harness) as live:
        dispatch = live.dispatch()
        runs = worker_once(harness, sink=DiesAfterClaim(live), dispatch=dispatch)

    assert runs == 1
    pr = harness.worker_client.find_open_pr_by_head(f"ozolith/issue-{issue}")
    assert pr is not None
    assert any(label["name"] == "pr_ready" for label in harness.fake.issues[pr.number]["labels"])
    # The claimed event landed before the kill; nothing after did — and
    # nothing about the Run cared.
    assert live.run_phases(issue) == ["claimed"]


def test_dead_control_node_pauses_new_claims(harness: Harness):
    """ADR-0017: no second claim path — a dead Control Node means no new
    claims, and the issue is left exactly as it was."""
    issue = harness.file_issue("waiting", "nobody claims me")
    dispatch = DispatchClient(DEAD_URL, NODE_TOKEN, timeout=0.3)
    sink = ControlNodeSink(DEAD_URL, token=NODE_TOKEN, timeout=0.3)

    assert worker_once(harness, sink=sink, dispatch=dispatch) == 0
    assert harness.fake.assignees_of(issue) == []
    assert "plan_ready" in harness.fake.labels_of(issue)
    assert harness.worker_client.find_open_pr_by_head(f"ozolith/issue-{issue}") is None


def test_events_resume_when_the_control_node_restarts(harness: Harness, tmp_path: Path):
    data = tmp_path / "control"
    with LiveControl(data, harness) as live:
        first = harness.file_issue("first", "before the outage")
        worker_once(harness, sink=DiesAfterClaim(live), dispatch=live.dispatch())

    # Restart on the same data dir: the same store picks back up.
    with LiveControl(data, harness) as reborn:
        second = harness.file_issue("second", "after the restart")
        worker_once(harness, sink=reborn.sink(), dispatch=reborn.dispatch())

        assert reborn.run_phases(first) == ["claimed"]
        assert reborn.run_phases(second) == ["claimed", "gate", "pr-open"]


# -- the events the substrate consumes ---------------------------------------------------


def test_full_review_cycle_emits_run_and_review_events(harness: Harness, tmp_path: Path):
    with LiveControl(tmp_path / "control", harness) as live:
        issue = harness.file_issue("observed", "watch me work")
        worker_once(harness, sink=live.sink(), dispatch=live.dispatch())
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
            live.dispatch(),
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
        # Progress telemetry rode the same channel (ADR-0016).
        progress = live.store.events(type="theozolith.run.progress", issue=issue)
        assert progress and progress[0]["run_id"]
        assert "transcript_tail" in progress[0]


def test_failed_runs_emit_failed_then_escalated(harness: Harness):
    issue = harness.file_issue("doomed", "the agent times out twice")
    sink = RecordingSink()

    harness.worker_behaviors.append(lambda prompt, cwd: AgentOutcome(timed_out=True))
    harness.worker_behaviors.append(lambda prompt, cwd: AgentOutcome(timed_out=True))
    assert harness.worker_once(sink=sink) == 2

    assert sink.run_phases(issue) == ["claimed", "failed", "claimed", "failed", "escalated"]
