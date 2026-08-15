"""Daemon reconcile loop: convergence, commands, degraded mode, secrets.

Covers the M3 acceptance criteria owned by the Node Daemon: the reconcile
test (both Stack kinds; drain/recycle/update/rebuild observable state
changes; recycling a process Stack leaves no orphaned run containers), the
build test's node half, the secrets tmpfs rule, degraded-mode caching, and
the deletion test's daemon half (boots with no config at all).
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from daemonrig import Rig, container_stack, desired, image_recipe, process_stack
from theozolith_nodedaemon.dockerctl import DockerCtl, DockerError
from theozolith_nodedaemon.stacks import WireStack


def heartbeat_response(stacks, images=None, commands=None) -> dict:
    return {"commands": commands or [], "config": desired(stacks, images)}


def pinned_response(stacks, commands=None, version="0.4.0") -> dict:
    """A heartbeat answer whose desired state pins a product version the
    rig's running daemon (0.3.0) has not converged to."""
    return {
        "commands": commands or [],
        "config": {**desired(stacks), "product_version": version},
    }


# -- convergence: both Stack kinds (acceptance 5) --------------------------------


def test_declaring_stacks_converges_the_node(rig: Rig):
    rig.control.heartbeat_answers.append(
        heartbeat_response([process_stack("worker"), container_stack("flightdeck")])
    )
    rig.daemon.once()

    # The process Stack (worker driver) runs as a supervised child…
    assert [p.args for p in rig.popen.spawned] == [["worker-driver", "--loop"]]
    assert rig.popen.spawned[0].env["THEOZOLITH_REPO"] == "acme/sandbox"
    assert rig.popen.spawned[0].env["THEOZOLITH_NODE_NAME"] == "box1"
    # …and the container Stack runs as a labeled long-running container.
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["image"] == "theozolith-flightdeck:local"
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["ports"] == ["8443:8443"]


def test_stopped_desired_state_stops_the_stack(rig: Rig):
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker")

    rig.control.heartbeat_answers.append(
        heartbeat_response([process_stack("worker", state="stopped")])
    )
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker")


def test_compose_stack_materializes_files_and_ups(rig: Rig):
    stack = container_stack(
        "control",
        image="",
        ports=[],
        compose_files=[
            {"name": "compose/control.yml", "content": "services: {}\n"},
            {"name": "overlays/tailscale.yml", "content": "services: {}\n"},
        ],
    )
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    files = rig.docker.compose_projects["ozolith-control"]
    assert [f.split("/")[-1] for f in files] == ["control.yml", "tailscale.yml"]
    # The inlined desired-state documents landed under the state dir, inside a
    # generation-addressed directory (immutable per record, ADR-0044 amendment).
    (on_disk,) = (rig.config.state_dir / "stacks" / "control").glob("*/compose/control.yml")
    assert on_disk.read_text() == "services: {}\n"
    assert on_disk.parent.parent.parent == rig.config.state_dir / "stacks" / "control"


def test_dead_process_child_is_restarted_next_pass(rig: Rig):
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()
    rig.popen.spawned[0].returncode = 1  # the driver crashed

    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2
    assert rig.daemon._supervisor.alive("worker")


# -- heartbeat reporting -----------------------------------------------------------


def test_heartbeat_reports_stacks_containers_and_builds(rig: Rig):
    recipe = image_recipe()
    stacks = [process_stack("worker")]
    rig.control.heartbeat_answers.append(heartbeat_response(stacks, [recipe]))
    rig.daemon.once()  # first pass: applies config, builds the image
    rig.docker.add_run_container("ozolith-run-r1", "r1", "worker")

    rig.control.heartbeat_answers.append(heartbeat_response(stacks, [recipe]))
    rig.daemon.once()

    body = rig.control.transcript[-1][2]  # the second heartbeat request
    assert body["node"] == "box1"
    worker = next(s for s in body["stacks"] if s["name"] == "worker")
    assert worker["state"] == "running" and worker["kind"] == "process"
    assert body["run_containers"] == [
        {"name": "ozolith-run-r1", "run_id": "r1", "owner": "worker", "status": "Up 5 minutes"}
    ]
    image = body["images"][0]
    assert image["instruction_hash"] == recipe["instruction_hash"]
    assert image["tag"] == recipe["tag"]
    assert image["built_at"]


# -- derived images (acceptance 6, node half) ---------------------------------------


def test_declared_image_builds_once_and_run_image_env_flows(rig: Rig):
    recipe = image_recipe()
    # THEOZOLITH_RUN_IMAGE now rides in the control-authored Stack env
    # (resolved from the worker type, ADR-0044); the daemon no longer maps a
    # run_image field to a built tag. It still builds every declared recipe.
    stacks = [process_stack("worker", env={"THEOZOLITH_RUN_IMAGE": recipe["tag"]})]
    rig.control.heartbeat_answers.append(heartbeat_response(stacks, [recipe]))
    rig.daemon.once()

    assert [b["tag"] for b in rig.docker.builds] == [recipe["tag"]]
    assert rig.popen.spawned[0].env["THEOZOLITH_RUN_IMAGE"] == recipe["tag"]
    # Generic per-process-Stack identity is injected (ADR-0044).
    assert rig.popen.spawned[0].env["THEOZOLITH_STACK"] == "worker"

    # Unchanged instructions => same tag => no rebuild.
    rig.control.heartbeat_answers.append(heartbeat_response(stacks, [recipe]))
    rig.daemon.once()
    assert len(rig.docker.builds) == 1


def test_changed_instructions_trigger_a_rebuild_under_a_new_tag(rig: Rig):
    old = image_recipe()
    rig.control.heartbeat_answers.append(heartbeat_response([], [old]))
    rig.daemon.once()

    new = image_recipe(setup=["pip install uv", "apt-get install -y jq"])
    assert new["tag"] != old["tag"]
    rig.control.heartbeat_answers.append(heartbeat_response([], [new]))
    rig.daemon.once()

    assert [b["tag"] for b in rig.docker.builds] == [old["tag"], new["tag"]]


# -- commands (acceptance 5) ---------------------------------------------------------


def test_drain_stops_the_stack_and_its_run_containers_and_persists(rig: Rig):
    stacks = [process_stack("worker")]
    rig.control.heartbeat_answers.append(heartbeat_response(stacks))
    rig.daemon.once()
    rig.docker.add_run_container("ozolith-run-r1", "r1", "worker")

    rig.control.heartbeat_answers.append(
        heartbeat_response(stacks, commands=[{"id": 7, "verb": "drain", "target": "worker"}])
    )
    rig.daemon.once()

    assert not rig.daemon._supervisor.alive("worker")
    assert "ozolith-run-r1" in rig.docker.removed
    # Drain dominates desired running state on later passes, across restarts.
    rig.control.heartbeat_answers.append(heartbeat_response(stacks))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker")
    assert json.loads(rig.config.drained_path.read_text()) == ["worker"]
    # The ack rode the next heartbeat.
    assert 7 in rig.control.transcript[-1][2]["completed_commands"]


def test_recycle_kills_the_tree_and_restarts_with_no_orphans(rig: Rig):
    stacks = [process_stack("worker")]
    rig.control.heartbeat_answers.append(heartbeat_response(stacks))
    rig.daemon.once()
    first = rig.popen.spawned[0]
    rig.docker.add_run_container("ozolith-run-r1", "r1", "worker")

    rig.control.heartbeat_answers.append(
        heartbeat_response(stacks, commands=[{"id": 8, "verb": "recycle", "target": "worker"}])
    )
    rig.daemon.once()

    assert first.returncode is not None  # the old tree got the signal
    assert rig.daemon._supervisor.alive("worker")  # and a fresh child is up
    assert len(rig.popen.spawned) == 2
    assert "ozolith-run-r1" in rig.docker.removed  # no orphaned run containers
    assert not rig.docker.run_containers()


def test_recycle_clears_a_drain(rig: Rig):
    stacks = [process_stack("worker")]
    rig.control.heartbeat_answers.append(
        heartbeat_response(stacks, commands=[{"id": 1, "verb": "drain", "target": "worker"}])
    )
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker")

    rig.control.heartbeat_answers.append(
        heartbeat_response(stacks, commands=[{"id": 2, "verb": "recycle", "target": "worker"}])
    )
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker")


def test_rebuild_forces_a_no_cache_build_of_the_same_tag(rig: Rig):
    recipe = image_recipe()
    rig.control.heartbeat_answers.append(heartbeat_response([], [recipe]))
    rig.daemon.once()
    rig.control.heartbeat_answers.append(
        heartbeat_response(
            [], [recipe], commands=[{"id": 3, "verb": "rebuild", "target": "claude-dev"}]
        )
    )
    rig.daemon.once()

    assert [(b["tag"], b["no_cache"]) for b in rig.docker.builds] == [
        (recipe["tag"], False),
        (recipe["tag"], True),
    ]


def test_update_stops_stacks_installs_pinned_version_and_reexecs(rig: Rig):
    stacks = [process_stack("worker")]
    rig.control.heartbeat_answers.append(heartbeat_response(stacks))
    rig.daemon.once()
    first = rig.popen.spawned[0]
    rig.control.heartbeat_answers.append(
        {
            "commands": [{"id": 4, "verb": "update", "target": None}],
            "config": {**desired(stacks), "product_version": "0.4.0"},
        }
    )
    rig.daemon.once()

    assert first.returncode is not None  # the old tree was stopped first
    assert any("theozolith-nodedaemon==0.4.0" in call for call in rig.update_calls[0])
    assert rig.execv_calls, "the daemon must re-exec itself after an update"
    # The ack survives the re-exec: it is on disk before execv (a real execv
    # never returns; here the pass continues and reconverges — which is also
    # exactly what the re-exec'd daemon does on its first pass).
    assert json.loads(rig.daemon._acks_path.read_text()) == [4]


# -- product convergence (revision ruling amending ADR-0015) --------------------------


def test_offpin_node_converges_without_any_command(rig: Rig):
    """The pin is desired state: a version mismatch alone triggers the
    self-update — the fanned-out command is only a nudge."""
    rig.control.heartbeat_answers.append(pinned_response([]))
    rig.daemon.once()

    (call,) = rig.update_calls
    assert any("theozolith-nodedaemon==0.4.0" in arg for arg in call)
    assert rig.execv_calls
    assert rig.daemon._completed == []  # no command existed, nothing acked


def test_failed_update_retries_on_a_later_pass_without_a_command(rig: Rig, monkeypatch):
    """Acceptance 1: a failed install is retried by convergence on the next
    pass — no re-queued command, no stranded node."""
    calls: list[list[str]] = []

    def failing_update(args, **_):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1, "", "no matching distribution")

    monkeypatch.setattr(rig.daemon, "_update_runner", failing_update)
    rig.control.heartbeat_answers.append(pinned_response([]))
    rig.daemon.once()
    assert len(calls) == 1 and not rig.execv_calls
    (event,) = rig.control.events
    assert "product update to 0.4.0 failed" in event["message"]

    rig.control.heartbeat_answers.append(pinned_response([]))
    rig.daemon.once()  # the next pass simply tries again
    assert len(calls) == 2


def test_convergence_respects_queue_behind(rig: Rig, tmp_path):
    """Drain-aware semantics unchanged: an in-flight Run defers the
    convergence attempt; the Run's end releases it."""
    import shutil

    stack, jobs = _driver_stack(tmp_path)
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()  # the driver child is up
    (jobs / "r1").mkdir(parents=True)

    rig.control.heartbeat_answers.append(pinned_response([stack]))
    rig.daemon.once()
    assert rig.update_calls == [] and rig.execv_calls == []  # deferred

    shutil.rmtree(jobs / "r1")
    rig.control.heartbeat_answers.append(pinned_response([stack]))
    rig.daemon.once()
    assert rig.update_calls and rig.execv_calls  # applied after the Run


def test_deferred_convergence_rides_the_heartbeat(rig: Rig, tmp_path):
    """Issue #8: the pin and drivers-hash queue-behind deferrals are
    desired-state deferrals — no command id — so they ride dedicated
    heartbeat fields the control-side convergence ladders pause on. The
    fields report the in-flight blocker AS OF this heartbeat's construction
    — never the previous pass's converge decision, whose one-beat lag would
    let the ladder queue a restart before control ever saw the deferral."""
    stack, jobs = _driver_stack(tmp_path)
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()  # the driver child is up; nothing deferred yet
    body = rig.control.transcript[-1][2]
    assert body["update_deferred"] == "" and body["drivers_deferred"] == ""
    (jobs / "r1").mkdir(parents=True)

    # The VERY NEXT heartbeat carries the blocker — before the daemon has
    # even learned of the pin bump riding this exchange's answer. While the
    # node is converged the value is only a potential blocker; control pairs
    # it with its own divergence check, so it is inert — what matters is
    # that no divergent-and-undeferred beat can ever precede it.
    pinned = {
        "commands": [],
        "config": {**desired([stack]), "product_version": "0.4.0", "drivers_hash": "a" * 64},
    }
    rig.control.heartbeat_answers.append(dict(pinned))
    rig.daemon.once()  # this pass's converge then defers behind the Run
    body = rig.control.transcript[-1][2]
    assert body["update_deferred"] == "behind run r1 (stack worker)"
    assert body["drivers_deferred"] == "behind run r1 (stack worker)"
    rig.control.heartbeat_answers.append(dict(pinned))
    rig.daemon.once()  # still draining: the deferral keeps riding
    body = rig.control.transcript[-1][2]
    assert body["update_deferred"] == "behind run r1 (stack worker)"
    assert body["drivers_deferred"] == "behind run r1 (stack worker)"
    assert rig.update_calls == [] and rig.execv_calls == []

    shutil.rmtree(jobs / "r1")
    rig.control.heartbeat_answers.append(dict(pinned))
    rig.daemon.once()  # the Run ended: the product update applies this pass
    body = rig.control.transcript[-1][2]
    assert body["update_deferred"] == "" and body["drivers_deferred"] == ""  # honest again
    assert rig.update_calls and rig.execv_calls


def test_restart_command_reexecs_without_installing(rig: Rig):
    """The off-pin escalation step: stop the tree and re-exec in place —
    no install (convergence owns installs)."""
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()
    first = rig.popen.spawned[0]

    restart = {"id": 15, "verb": "restart", "target": None, "force": False}
    rig.control.heartbeat_answers.append(
        heartbeat_response([process_stack("worker")], commands=[restart])
    )
    rig.daemon.once()

    assert first.returncode is not None  # the tree was stopped first
    assert rig.execv_calls and rig.update_calls == []
    assert 15 in rig.daemon._completed


def test_unreachable_control_backoff_caps_and_recovers(rig: Rig):
    """Acceptance 4: polling backs off exponentially (capped) while the
    Control Node is unreachable and snaps back on recovery."""
    from theozolith_nodedaemon.controlclient import ControlUnreachable

    rig.control.heartbeat_answers.extend([ControlUnreachable("down")] * 4)
    rig.control.heartbeat_answers.append(heartbeat_response([]))
    delays: list[float] = []

    class _Stop(Exception):
        pass

    def sleeper(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= 5:
            raise _Stop

    with contextlib.suppress(_Stop):
        rig.daemon.run(sleep=sleeper)
    assert delays == [60, 120, 240, 300, 60]


def test_update_installs_served_artifacts_from_the_control_node(rig: Rig):
    """The developer build path (ADR-0015, 2026-07-22): a pinned version
    with a served artifact set installs the pulled wheel FILES — the node
    never touches an index, never pulls source, never builds."""
    version = "0.3.0+gabc123def456"
    wheels = {
        "theozolith_nodedaemon-0.3.0+gabc123def456-py3-none-any.whl": b"nodedaemon-wheel",
        "theozolith_worker-0.3.0+gabc123def456-py3-none-any.whl": b"worker-wheel",
    }
    for name, payload in wheels.items():
        rig.control.artifacts[(version, name)] = payload
    rig.control.heartbeat_answers.append(
        {
            "commands": [{"id": 11, "verb": "update", "target": None}],
            "config": {
                **desired([]),
                "product_version": version,
                "product_artifacts": sorted(wheels),
            },
        }
    )
    rig.daemon.once()

    (call,) = rig.update_calls
    installed = [arg for arg in call if arg.endswith(".whl")]
    assert len(installed) == len(wheels)
    for path in installed:
        assert path.startswith(str(rig.config.state_dir / "update-wheels"))
        assert Path(path).read_bytes() == wheels[Path(path).name]
    assert all("==" not in arg for arg in call)  # no index pins on this path
    assert rig.execv_calls, "the daemon must re-exec itself after an update"
    assert json.loads(rig.daemon._acks_path.read_text()) == [11]


def test_failed_command_is_not_acked(rig: Rig, monkeypatch):
    stacks = [process_stack("worker")]

    def broken_stop(*args, **kwargs):
        raise RuntimeError("docker is down")

    monkeypatch.setattr(rig.daemon, "_teardown_all_forms", broken_stop)
    rig.control.heartbeat_answers.append(
        heartbeat_response(stacks, commands=[{"id": 9, "verb": "drain", "target": "worker"}])
    )
    rig.daemon.once()
    rig.control.heartbeat_answers.append(heartbeat_response(stacks))
    rig.daemon.once()
    assert rig.control.transcript[-1][2]["completed_commands"] == []
    # The failure also surfaced as a theozolith.error summary (2026-07-21).
    (event,) = rig.control.events
    assert event["type"] == "theozolith.error"
    assert event["node"] == "box1" and event["component"] == "node-daemon"
    assert event["error_class"] == "RuntimeError"
    assert "docker is down" in event["message"]


def test_flightdeck_style_stack_runs_its_command_and_reports_its_container(rig: Rig):
    """ADR-0019: a container Stack's optional command starts the named tmux
    session, and the heartbeat reports the stack container as web-terminal
    target evidence."""
    stack = container_stack(
        "flightdeck",
        image="ghcr.io/example/flightdeck:1",
        command="tmux new-session -d -s flightdeck claude",
        ports=[],
    )
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    row = rig.docker.stacks["ozolith-stack-flightdeck"]
    assert row["command"] == ["tmux", "new-session", "-d", "-s", "flightdeck", "claude"]

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    payload = rig.control.transcript[-1][2]
    (record,) = payload["stack_containers"]
    assert record["name"] == "ozolith-stack-flightdeck"
    assert record["stack"] == "flightdeck"
    assert record["state"] == "running"
    # Run containers stay in their own list: never attach targets.
    assert payload["run_containers"] == []


# -- error events (2026-07-21 grilling) -------------------------------------------------


def test_container_start_failure_emits_a_size_capped_error_event(rig: Rig, monkeypatch):
    def broken_start(*args, **kwargs):
        raise RuntimeError("docker run failed: " + "x" * 50_000)

    monkeypatch.setattr(rig.docker, "run_stack_container", broken_start)
    rig.control.heartbeat_answers.append(heartbeat_response([container_stack("web")]))
    rig.daemon.once()

    (event,) = rig.control.events
    assert event["type"] == "theozolith.error"
    assert event["component"] == "node-daemon"
    assert event["error_class"] == "RuntimeError"
    assert "stack web: reconcile failed" in event["message"]
    assert len(event["message"]) <= 2_000  # capped at emission


def test_update_install_failure_emits_an_error_event(rig: Rig, monkeypatch):
    def failing_update(args, **_):
        return subprocess.CompletedProcess(args, 1, "", "no matching distribution")

    monkeypatch.setattr(rig.daemon, "_update_runner", failing_update)
    rig.control.heartbeat_answers.append(
        pinned_response([], commands=[{"id": 7, "verb": "update", "target": None}])
    )
    rig.daemon.once()

    assert not rig.execv_calls  # the failed update never re-execs
    (event,) = rig.control.events
    assert event["error_class"] == "RuntimeError"
    assert "pip install failed" in event["message"]
    # The nudge command is consumed regardless: convergence owns the retry.
    assert rig.daemon._completed == [7]


def test_error_emission_failure_is_swallowed(rig: Rig, monkeypatch):
    """A dead events endpoint must never break a daemon pass (no recursion)."""

    def broken_start(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(rig.docker, "run_stack_container", broken_start)
    original = rig.control._answer

    def refuse_events(path, request):
        if path == "/events":
            return 500, {"detail": "events store is down"}
        return original(path, request)

    monkeypatch.setattr(rig.control, "_answer", refuse_events)
    rig.control.heartbeat_answers.append(heartbeat_response([container_stack("web")]))
    rig.daemon.once()  # must not raise
    assert any("error event not delivered" in line for line in rig.logs)


# -- degraded mode (ADR-0006) -----------------------------------------------------------


def test_unreachable_control_node_reconciles_from_cache(rig: Rig):
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()
    rig.popen.spawned[0].returncode = 1  # driver dies during the outage

    # No scripted answer = ControlUnreachable. The pass must still converge.
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker")
    assert any("cached config" in line for line in rig.logs)


def test_fresh_daemon_boots_from_cached_config_when_control_is_down(rig: Rig):
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()

    from theozolith_nodedaemon.daemon import NodeDaemon
    from theozolith_nodedaemon.stacks import ProcessSupervisor

    reborn = NodeDaemon(
        rig.config,
        docker=rig.docker,  # type: ignore[arg-type]
        client=rig.daemon._client,  # still scripted; queue is empty => unreachable
        supervisor=ProcessSupervisor(popen=rig.popen, log=rig.logs.append),
        log=rig.logs.append,
    )
    reborn.once()
    assert reborn._supervisor.alive("worker")


def test_deletion_boot_with_no_config_and_no_control(tmp_path):
    """The deletion test's daemon half: no Config Repo, no Control Node —
    the daemon runs (and reaps orphans) from docker + package + .env."""
    from theozolith_nodedaemon.config import load_daemon_config
    from theozolith_nodedaemon.daemon import NodeDaemon

    config = load_daemon_config(
        {
            "THEOZOLITH_NODE_NAME": "lonely",
            "THEOZOLITH_STATE_DIR": str(tmp_path / "state"),
            "THEOZOLITH_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    from daemonrig import FakeDocker

    docker = FakeDocker()
    docker.add_run_container("ozolith-run-stale", "stale", "worker")
    daemon = NodeDaemon(config, docker=docker, log=lambda *_: None)  # type: ignore[arg-type]
    daemon.once()
    assert docker.run_containers() == []  # the orphan got reaped


# -- orphan reaping (ADR-0013) ---------------------------------------------------------


def test_orphaned_run_containers_are_reaped_live_ones_kept(rig: Rig):
    stacks = [process_stack("worker")]
    rig.control.heartbeat_answers.append(heartbeat_response(stacks))
    rig.daemon.once()
    rig.docker.add_run_container("ozolith-run-live", "live", "worker")  # driver is up
    rig.docker.add_run_container("ozolith-run-dead", "dead", "reviewer")  # no such driver

    rig.control.heartbeat_answers.append(heartbeat_response(stacks))
    rig.daemon.once()

    names = {c["name"] for c in rig.docker.run_containers()}
    assert names == {"ozolith-run-live"}
    assert "ozolith-run-dead" in rig.docker.removed


# -- secrets (acceptance 7, node half) -----------------------------------------------------


def test_secrets_materialize_in_tmpfs_only_and_wire_as_var_file(rig: Rig):
    rig.control.secrets["github-implementer"] = "s3cr3t-value"
    stack = process_stack("worker", secrets={"IMPLEMENTER_GITHUB_TOKEN": "github-implementer"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    secret_file = rig.config.secrets_dir / "github-implementer"
    assert secret_file.read_text() == "s3cr3t-value"
    # The 0700-directory/0444-leaf boundary (ADR-0015 amendment): the
    # non-traversable directory is the host-side barrier; the leaf itself is
    # world-readable so a container running as an arbitrary non-root uid can
    # read the files bind-mounted into it.
    assert (rig.config.secrets_dir.stat().st_mode & 0o777) == 0o700
    assert (secret_file.stat().st_mode & 0o777) == 0o444
    env = rig.popen.spawned[0].env
    assert env["IMPLEMENTER_GITHUB_TOKEN_FILE"] == str(secret_file)
    assert "IMPLEMENTER_GITHUB_TOKEN" not in env  # the value itself never enters env

    # A scan of everything the daemon wrote to DISK finds no trace: the
    # value lives only under the runtime dir (tmpfs under /run).
    for path in rig.config.state_dir.rglob("*"):
        if path.is_file():
            assert "s3cr3t-value" not in path.read_text(errors="replace"), path


def test_denied_secret_pull_skips_the_stack(rig: Rig):
    rig.control.denied_secrets = True
    stack = process_stack("worker", secrets={"IMPLEMENTER_GITHUB_TOKEN": "github-implementer"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker")
    assert any("secrets unavailable" in line for line in rig.logs)


def test_plain_http_control_url_refused_at_construction_without_dev_flag():
    """The bearer token rides every request, not just secret pulls: a
    plain-HTTP channel is refused before the first byte, unless the
    operator explicitly opted into insecure dev mode — and the check is an
    exact parse, so a scheme merely BEGINNING with https never passes."""
    from theozolith_nodedaemon.controlclient import ControlClient, ControlError

    try:
        ControlClient("http://control.test", "tok", insecure_dev=False)
    except ControlError as exc:
        assert "TLS is mandatory" in str(exc)
    else:
        raise AssertionError("a plain-HTTP control URL must be refused")
    # Exact scheme + usable hostname, never a prefix check; the dev flag
    # excuses plain http, never a malformed URL.
    for bad in ("httpsneak://control.test", "https://", "control.test", "https:worker"):
        for dev in (False, True):
            with pytest.raises(ControlError):
                ControlClient(bad, "tok", insecure_dev=dev)
    # Ordinary https constructs (no traffic happens at construction)…
    ControlClient("https://control.test", "tok")
    # …and the explicit dev opt-in still constructs (the test rigs depend on it).
    ControlClient("http://control.test", "tok", insecure_dev=True)


def test_malformed_control_urls_are_controlerror_never_valueerror():
    """The constructor's parse is TOTAL: on unmatched IPv6 brackets and
    NFKC-invalid netlocs urlsplit itself raises ValueError — before .port
    is ever reached — and an explicit :0 names no dialable origin (the
    scheme default fills in only when the port is OMITTED). Every one is
    the documented ControlError, dev flag or not; a leaked ValueError
    would fail the pytest.raises."""
    from theozolith_nodedaemon.controlclient import ControlClient, ControlError

    for bad in (
        "https://[",  # unmatched IPv6 bracket: ValueError inside urlsplit
        "https://[::1",  # same, with address content
        "https://evil\uff0fslash.test",  # NFKC-invalid netloc (full-width slash)
        "https://control.test:0",  # explicit :0 — never rewritten to 443
        "http://control.test:0",  # …and never excused by the dev flag
    ):
        for dev in (False, True):
            with pytest.raises(ControlError):
                ControlClient(bad, "tok", insecure_dev=dev)


def test_container_stack_secret_mounts_read_only_at_run_secrets(rig: Rig):
    rig.control.secrets["admin-token"] = "the-admin-value"
    stack = container_stack("flightdeck", secrets={"THEOZOLITH_ADMIN_TOKEN": "admin-token"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    recorded = rig.docker.stacks["ozolith-stack-flightdeck"]
    host_path = recorded["env_files"]["THEOZOLITH_ADMIN_TOKEN"]
    assert host_path == str(rig.config.secrets_dir / "admin-token")


# -- queue-behind recycle/update (NODE-SUBSTRATE, grilling 2026-07-17) -----------


def _driver_stack(tmp_path) -> tuple[dict, object]:
    """A worker Stack whose jobs dir lives under tmp_path (the in-flight
    signal the daemon watches)."""
    jobs = tmp_path / "jobs"
    stack = process_stack(
        "worker",
        env={"THEOZOLITH_REPO": "acme/sandbox", "THEOZOLITH_JOBS_DIR": str(jobs)},
    )
    return stack, jobs


def test_recycle_mid_run_queues_behind_and_applies_after_the_run(rig: Rig, tmp_path):
    """Acceptance 12: a recycle issued mid-Run applies only after the Run
    ends, with the deferral visible in heartbeats."""
    import shutil

    stack, jobs = _driver_stack(tmp_path)
    (jobs / "20260717T1200-worker-a-1").mkdir(parents=True)
    recycle = {"id": 8, "verb": "recycle", "target": "worker", "force": False}

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    first = rig.popen.spawned[0]

    # Mid-Run: the command defers — no kill, no ack (control re-delivers).
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[recycle]))
    rig.daemon.once()
    assert first.returncode is None and len(rig.popen.spawned) == 1
    assert rig.daemon._completed == []
    assert any("deferred" in line for line in rig.logs)

    # The next heartbeat carries the deferral; the re-delivered command
    # re-checks and stays deferred while the Run is still in flight.
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[recycle]))
    rig.daemon.once()
    reported = rig.control.transcript[-1][2]["deferred_commands"]
    assert reported == [{"id": 8, "reason": "behind run 20260717T1200-worker-a-1 (stack worker)"}]
    assert first.returncode is None

    # The Run ends (job dir gone): the re-delivered command now applies.
    shutil.rmtree(jobs / "20260717T1200-worker-a-1")
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[recycle]))
    rig.daemon.once()
    assert first.returncode is not None  # kill-the-tree, then restart
    assert rig.daemon._supervisor.alive("worker")
    assert 8 in rig.daemon._completed
    # The deferral cleared from the next status payload.
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert rig.control.transcript[-1][2]["deferred_commands"] == []


def test_force_recycle_mid_run_keeps_kill_the_tree(rig: Rig, tmp_path):
    stack, jobs = _driver_stack(tmp_path)
    (jobs / "r1").mkdir(parents=True)

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    first = rig.popen.spawned[0]

    forced = {"id": 9, "verb": "recycle", "target": "worker", "force": True}
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[forced]))
    rig.daemon.once()
    assert first.returncode is not None  # ADR-0013 semantics, immediately
    assert 9 in rig.daemon._completed


def test_dead_driver_child_never_defers(rig: Rig, tmp_path):
    """Orphaned job dirs under a dead child are the boot sweep's business —
    the command proceeds immediately."""
    stack, jobs = _driver_stack(tmp_path)
    (jobs / "r-orphan").mkdir(parents=True)

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    rig.popen.spawned[0].returncode = 0  # the driver died on its own

    recycle = {"id": 10, "verb": "recycle", "target": "worker", "force": False}
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[recycle]))
    rig.daemon.once()
    assert 10 in rig.daemon._completed  # executed, not deferred


def test_parked_pending_sibling_never_defers(rig: Rig, tmp_path):
    """Evidence parked in the <jobs>-pending sibling (plain or
    collision-suffixed names, M5) is never an in-flight Run: with the jobs
    dir itself empty, a recycle applies immediately."""
    stack, jobs = _driver_stack(tmp_path)
    jobs.mkdir(parents=True)
    pending = jobs.with_name(jobs.name + "-pending")
    (pending / "20260721T1200-worker-a-1").mkdir(parents=True)
    (pending / "20260721T1300-worker-a-3-parked-deadbeef").mkdir(parents=True)

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    first = rig.popen.spawned[0]

    recycle = {"id": 12, "verb": "recycle", "target": "worker", "force": False}
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[recycle]))
    rig.daemon.once()
    assert first.returncode is not None  # applied immediately, not deferred
    assert 12 in rig.daemon._completed


def test_tombstoned_evidence_never_defers_even_with_a_live_driver(rig: Rig, tmp_path):
    """The evidence-loss tombstone (a dot-prefixed dir the worker leaves
    when a completed dir cannot be parked OR deleted, ADR-0019) is never an
    in-flight Run: a recycle applies immediately over it."""
    stack, jobs = _driver_stack(tmp_path)
    (jobs / ".evidence-lost-20260721T1400-worker-a-5-cafe0123").mkdir(parents=True)

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    first = rig.popen.spawned[0]

    recycle = {"id": 13, "verb": "recycle", "target": "worker", "force": False}
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[recycle]))
    rig.daemon.once()
    assert first.returncode is not None  # applied immediately, not deferred
    assert 13 in rig.daemon._completed


def test_update_mid_run_queues_behind_node_wide(rig: Rig, tmp_path):
    stack, jobs = _driver_stack(tmp_path)
    (jobs / "r1").mkdir(parents=True)

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    update = {"id": 11, "verb": "update", "target": None, "force": False}
    rig.control.heartbeat_answers.append(pinned_response([stack], commands=[update]))
    rig.daemon.once()
    assert rig.update_calls == [] and rig.execv_calls == []  # deferred
    assert rig.daemon._deferrals[11].startswith("behind run r1")

    import shutil

    shutil.rmtree(jobs / "r1")
    rig.control.heartbeat_answers.append(pinned_response([stack], commands=[update]))
    rig.daemon.once()
    assert rig.update_calls and rig.execv_calls  # applied after the Run


def test_a_deferred_command_blocks_later_commands_in_the_queue(rig: Rig, tmp_path):
    """Queue-behind blocks the QUEUE: a drain issued after a deferred
    recycle must not jump it (it would be undone when the recycle lands)."""
    import shutil

    stack, jobs = _driver_stack(tmp_path)
    (jobs / "r1").mkdir(parents=True)
    queued = [
        {"id": 20, "verb": "recycle", "target": "worker", "force": False},
        {"id": 21, "verb": "drain", "target": "worker", "force": False},
    ]

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=queued))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker")  # neither command ran
    assert rig.daemon._completed == []

    # The Run ends: both apply, in order — the node ends up drained.
    shutil.rmtree(jobs / "r1")
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=queued))
    rig.daemon.once()
    assert rig.daemon._completed == [20, 21]
    assert not rig.daemon._supervisor.alive("worker")


# -- per-Stack jobs directories (ADR-0019) ---------------------------------------


def test_process_stacks_get_dedicated_injected_jobs_dirs(rig: Rig):
    """Every process Stack receives its own THEOZOLITH_JOBS_DIR by default;
    an explicit env value wins."""
    explicit = process_stack("reviewer", env={"THEOZOLITH_JOBS_DIR": "/srv/jobs/custom"})
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker"), explicit]))
    rig.daemon.once()

    by_name = {p.args[0]: p.env for p in rig.popen.spawned}
    assert by_name["worker-driver"]["THEOZOLITH_JOBS_DIR"] == "/var/tmp/theozolith/jobs/worker"
    assert by_name["reviewer-driver"]["THEOZOLITH_JOBS_DIR"] == "/srv/jobs/custom"


def test_targeted_recycle_ignores_another_stacks_run(rig: Rig, tmp_path):
    """Acceptance 16: the reviewer's active Run must not defer a recycle
    aimed at the worker — each Stack's jobs dir is its own in-flight
    signal — while the worker's own Run still does."""
    worker = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(tmp_path / "jobs-worker")})
    reviewer = process_stack(
        "reviewer", env={"THEOZOLITH_JOBS_DIR": str(tmp_path / "jobs-reviewer")}
    )
    (tmp_path / "jobs-reviewer" / "review-77-round-1").mkdir(parents=True)

    rig.control.heartbeat_answers.append(heartbeat_response([worker, reviewer]))
    rig.daemon.once()

    recycle = {"id": 30, "verb": "recycle", "target": "worker", "force": False}
    rig.control.heartbeat_answers.append(heartbeat_response([worker, reviewer], commands=[recycle]))
    rig.daemon.once()
    assert 30 in rig.daemon._completed  # applied immediately, not deferred

    (tmp_path / "jobs-worker" / "r-own").mkdir(parents=True)
    own = {"id": 31, "verb": "recycle", "target": "worker", "force": False}
    rig.control.heartbeat_answers.append(heartbeat_response([worker, reviewer], commands=[own]))
    rig.daemon.once()
    assert 31 not in rig.daemon._completed  # its own Run defers it
    assert rig.daemon._deferrals[31].startswith("behind run r-own")


def test_node_wide_update_waits_for_every_live_stack_but_not_parked_dirs(rig: Rig, tmp_path):
    """Acceptance 17: an update waits on each live Stack's active Run in
    turn; pending-evidence parking (the -pending sibling) and dead drivers
    never block it."""
    worker = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(tmp_path / "jobs-worker")})
    reviewer = process_stack(
        "reviewer", env={"THEOZOLITH_JOBS_DIR": str(tmp_path / "jobs-reviewer")}
    )
    (tmp_path / "jobs-worker" / "r1").mkdir(parents=True)
    (tmp_path / "jobs-reviewer" / "review-9-round-1").mkdir(parents=True)
    # Retained evidence parked by the drivers: never an in-flight signal.
    (tmp_path / "jobs-worker-pending" / "r-old").mkdir(parents=True)
    (tmp_path / "jobs-reviewer-pending" / "review-old").mkdir(parents=True)

    rig.control.heartbeat_answers.append(heartbeat_response([worker, reviewer]))
    rig.daemon.once()

    import shutil

    update = {"id": 40, "verb": "update", "target": None, "force": False}
    rig.control.heartbeat_answers.append(pinned_response([worker, reviewer], commands=[update]))
    rig.daemon.once()
    assert rig.update_calls == []  # the worker's Run defers it

    shutil.rmtree(tmp_path / "jobs-worker" / "r1")
    rig.control.heartbeat_answers.append(pinned_response([worker, reviewer], commands=[update]))
    rig.daemon.once()
    assert rig.update_calls == []  # …then the reviewer's Run defers it

    shutil.rmtree(tmp_path / "jobs-reviewer" / "review-9-round-1")
    rig.control.heartbeat_answers.append(pinned_response([worker, reviewer], commands=[update]))
    rig.daemon.once()
    assert rig.update_calls and rig.execv_calls  # parked dirs never blocked


def test_dead_drivers_run_dir_never_blocks_node_wide_update(rig: Rig, tmp_path):
    stack, jobs = _driver_stack(tmp_path)
    (jobs / "r-orphan").mkdir(parents=True)

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    rig.popen.spawned[0].returncode = 0  # the driver died on its own

    update = {"id": 41, "verb": "update", "target": None, "force": False}
    rig.control.heartbeat_answers.append(pinned_response([stack], commands=[update]))
    rig.daemon.once()
    assert rig.update_calls and rig.execv_calls  # orphaned dir never deferred


# -- ADR-0023: the daemon owns its drivers' control channel -----------------------


def test_channel_env_is_injected_into_process_stacks(rig: Rig):
    """Node-resident drivers authenticate as their node: the daemon hands its
    control URL, per-node token, and pinned CA down. The provisioned identity
    OVERRIDES Stack env (OZ-03): a Stack cannot point the node token at
    another endpoint by setting only CONTROL_NODE_URL — that would exfiltrate
    the real token."""
    pinned = process_stack("reviewer", env={"CONTROL_NODE_URL": "http://elsewhere.test"})
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker"), pinned]))
    rig.daemon.once()

    by_name = {p.args[0]: p.env for p in rig.popen.spawned}
    assert by_name["worker-driver"]["CONTROL_NODE_URL"] == "http://control.test"
    assert by_name["worker-driver"]["THEOZOLITH_NODE_TOKEN"] == "node-token"
    # The Stack's off-box CONTROL_NODE_URL is overridden by the provisioned one.
    assert by_name["reviewer-driver"]["CONTROL_NODE_URL"] == "http://control.test"
    assert by_name["reviewer-driver"]["THEOZOLITH_NODE_TOKEN"] == "node-token"


def _rebuilt_daemon(rig: Rig, **overrides):
    """The rig's fakes under a modified DaemonConfig (frozen dataclass)."""
    import dataclasses

    from theozolith_nodedaemon.controlclient import ControlClient
    from theozolith_nodedaemon.daemon import NodeDaemon
    from theozolith_nodedaemon.stacks import ProcessSupervisor

    config = dataclasses.replace(rig.config, **overrides)
    client = None
    if config.control_url:
        client = ControlClient(
            config.control_url, config.node_token, insecure_dev=True, transport=rig.control
        )
    daemon = NodeDaemon(
        config,
        docker=rig.docker,  # type: ignore[arg-type]
        client=client,
        supervisor=ProcessSupervisor(popen=rig.popen, log=rig.logs.append),
        log=rig.logs.append,
    )
    return config, daemon


def _channel_stealing_stack(tmp_path: Path) -> dict:
    """A Stack trying every override shape at once: the three direct names,
    their three _FILE forms, and a [secrets] mapping whose materialization
    would generate the _FILE forms again after the daemon's merge."""
    lure = tmp_path / "lure"
    lure.write_text("https://lure.test", encoding="utf-8")
    return process_stack(
        "worker",
        env={
            "THEOZOLITH_REPO": "acme/sandbox",
            "GITHUB_TOKEN": "gh-secret",
            "CONTROL_NODE_URL": "https://elsewhere.test",
            "CONTROL_NODE_URL_FILE": str(lure),
            "THEOZOLITH_NODE_TOKEN": "forged-token",
            "THEOZOLITH_NODE_TOKEN_FILE": str(lure),
            "THEOZOLITH_TLS_CA": "/evil/ca.pem",
            "THEOZOLITH_TLS_CA_FILE": str(lure),
        },
        secrets={
            "CONTROL_NODE_URL": "evil-url",
            "THEOZOLITH_NODE_TOKEN": "evil-token",
            "THEOZOLITH_TLS_CA": "evil-ca",
        },
    )


def test_all_six_reserved_names_are_stripped_and_the_daemon_identity_wins(rig: Rig, tmp_path):
    """The channel identity is ONE value: URL, token, and CA. The reservation
    covers the direct names AND their _FILE forms, from Stack env and from the
    [secrets]-generated <ENV>_FILE mapping alike — the worker's env_value
    gives _FILE precedence, so a surviving _FILE entry would silently outrank
    the injected direct value. The spawned environment is fed through the
    ACTUAL worker load_config path to prove all three components win."""
    from theozolith_worker.config import load_config

    ca = tmp_path / "pinned-ca.pem"
    ca.write_text("pinned", encoding="utf-8")
    _config, daemon = _rebuilt_daemon(rig, tls_ca=str(ca))
    rig.control.secrets.update(
        {"evil-url": "https://evil.test", "evil-token": "stolen", "evil-ca": "evil-ca-pem"}
    )
    rig.control.heartbeat_answers.append(heartbeat_response([_channel_stealing_stack(tmp_path)]))
    daemon.once()

    env = rig.popen.spawned[-1].env
    assert env["CONTROL_NODE_URL"] == "http://control.test"
    assert env["THEOZOLITH_NODE_TOKEN"] == "node-token"
    assert env["THEOZOLITH_TLS_CA"] == str(ca)
    for name in ("CONTROL_NODE_URL", "THEOZOLITH_NODE_TOKEN", "THEOZOLITH_TLS_CA"):
        assert f"{name}_FILE" not in env
    # Non-reserved secret mappings still materialize normally elsewhere; the
    # worker sees exactly the daemon's channel identity.
    config = load_config(env, role="implementer")
    assert config.control_node_url == "http://control.test"
    assert config.control_token == "node-token"
    assert config.control_ca == str(ca)


def test_https_channel_without_its_pinned_ca_fails_the_stack_closed(rig: Rig, tmp_path):
    """A provisioned https URL with no pinned CA is an INCOMPLETE identity:
    the Stack is refused (error surfaced, nothing spawned) rather than let a
    Stack-supplied CA fill the gap and re-anchor the channel."""
    _config, daemon = _rebuilt_daemon(rig, control_url="https://control.test", tls_ca=None)
    stack = process_stack("worker", env={"THEOZOLITH_TLS_CA": "/stack/ca.pem"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    daemon.once()

    assert rig.popen.spawned == []
    assert any("one inseparable identity" in line for line in rig.logs)
    # …and the refusal rides the normal error-event channel.
    assert any("inseparable" in json.dumps(event) for event in rig.control.events)


def test_daemonless_dev_keeps_the_stacks_own_channel_settings(rig: Rig, tmp_path):
    """No provisioned control URL (permanent degraded / daemon-less dev,
    ADR-0023): nothing is reserved and nothing injected — the Stack's own
    channel settings, _FILE forms included, reach the worker unchanged and
    still satisfy load_config."""
    from theozolith_nodedaemon.stacks import WireStack as Wire
    from theozolith_worker.config import load_config

    url_file = tmp_path / "dev-url"
    url_file.write_text("http://127.0.0.1:8000", encoding="utf-8")
    _config, daemon = _rebuilt_daemon(rig, control_url=None, node_token="")
    stack = Wire.from_wire(
        process_stack(
            "worker",
            env={
                "THEOZOLITH_REPO": "acme/sandbox",
                "GITHUB_TOKEN": "gh-secret",
                "CONTROL_NODE_URL_FILE": str(url_file),
                "THEOZOLITH_NODE_TOKEN": "dev-token",
            },
        )
    )
    env = daemon._process_env(stack)
    assert env["CONTROL_NODE_URL_FILE"] == str(url_file)
    assert env["THEOZOLITH_NODE_TOKEN"] == "dev-token"
    config = load_config(env, role="implementer")
    assert config.control_node_url == "http://127.0.0.1:8000"
    assert config.control_token == "dev-token"


def test_wire_cadences_apply_unless_locally_overridden(rig: Rig):
    """control.toml cadences ride desired state (ADR-0023): the wire value
    drives the heartbeat delay when the local config sits on its shipped
    default; an explicit local override wins."""
    answer = heartbeat_response([])
    answer["config"]["heartbeat_seconds"] = 5
    answer["config"]["stop_grace_seconds"] = 2
    rig.control.heartbeat_answers.append(answer)
    rig.daemon.once()
    # heartbeat_seconds sits on the shipped default (60): the wire wins.
    assert rig.daemon._next_delay() == 5.0
    # stop_grace_seconds is locally overridden (0.5 in the rig): local wins.
    assert rig.daemon._stop_grace_seconds() == 0.5


# -- effective-spec convergence: process Stacks (ADR-0044 amendment) --------------
#
# A worker-type change now resolves primarily into the process env (repository,
# adapter, run-image tag — the model rides inside the image tag, ADR-0045) and
# the secret mapping, so a live driver must restart when any of those change —
# and must NOT churn when nothing does.


def test_unchanged_process_spec_does_not_restart(rig: Rig):
    stack = process_stack("worker")
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    first = rig.popen.spawned[0]

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 1 and rig.popen.spawned[0] is first


def test_dictionary_order_only_change_does_not_restart(rig: Rig):
    a = process_stack("worker", env={"THEOZOLITH_REPO": "acme/sandbox", "A": "1", "B": "2"})
    rig.control.heartbeat_answers.append(heartbeat_response([a]))
    rig.daemon.once()
    b = process_stack("worker", env={"B": "2", "THEOZOLITH_REPO": "acme/sandbox", "A": "1"})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 1  # reordering is not a spec change


def test_changed_env_value_restarts_the_driver(rig: Rig):
    """The daemon is name-agnostic: ANY effective env change restarts the
    child. (A model change no longer arrives as env at all — ADR-0045 bakes
    it into the run image, so it lands as the changed RUN_IMAGE tag below.)"""
    a = process_stack("worker", env={"THEOZOLITH_REPO": "a/x", "SOME_TUNABLE": "one"})
    rig.control.heartbeat_answers.append(heartbeat_response([a]))
    rig.daemon.once()
    first = rig.popen.spawned[0]
    b = process_stack("worker", env={"THEOZOLITH_REPO": "a/x", "SOME_TUNABLE": "two"})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2
    assert rig.popen.spawned[1].env["SOME_TUNABLE"] == "two"
    assert first.poll() is not None  # kill-the-tree took the old child down


def test_changed_adapter_restarts_the_driver(rig: Rig):
    a = process_stack("worker", env={"THEOZOLITH_REPO": "acme/x", "THEOZOLITH_ADAPTER": "claude"})
    rig.control.heartbeat_answers.append(heartbeat_response([a]))
    rig.daemon.once()
    b = process_stack("worker", env={"THEOZOLITH_REPO": "acme/x", "THEOZOLITH_ADAPTER": "codex"})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2
    assert rig.popen.spawned[1].env["THEOZOLITH_ADAPTER"] == "codex"


def test_changed_workspace_restarts_the_driver(rig: Rig):
    a = process_stack("worker", env={"THEOZOLITH_REPO": "acme/one"})
    rig.control.heartbeat_answers.append(heartbeat_response([a]))
    rig.daemon.once()
    b = process_stack("worker", env={"THEOZOLITH_REPO": "acme/two"})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2
    assert rig.popen.spawned[1].env["THEOZOLITH_REPO"] == "acme/two"


def test_changed_run_image_tag_builds_then_restarts(rig: Rig):
    """The new recipe builds FIRST (reconcile builds images before Stacks),
    then the driver restarts onto the new tag — never onto an unbuilt image."""
    old = image_recipe()
    a = process_stack("worker", env={"THEOZOLITH_REPO": "a/x", "THEOZOLITH_RUN_IMAGE": old["tag"]})
    rig.control.heartbeat_answers.append(heartbeat_response([a], [old]))
    rig.daemon.once()
    first = rig.popen.spawned[0]

    new = image_recipe(setup=["pip install uv", "apt-get install -y jq"])
    assert new["tag"] != old["tag"]
    b = process_stack("worker", env={"THEOZOLITH_REPO": "a/x", "THEOZOLITH_RUN_IMAGE": new["tag"]})
    rig.control.heartbeat_answers.append(heartbeat_response([b], [new]))
    rig.daemon.once()

    assert rig.docker.image_exists(new["tag"])  # built before the restart
    assert len(rig.popen.spawned) == 2
    assert rig.popen.spawned[1].env["THEOZOLITH_RUN_IMAGE"] == new["tag"]
    assert first.poll() is not None


def test_restart_is_deferred_when_the_new_run_image_is_unavailable(rig: Rig):
    """An image build failing before a driver restart must not tear the working
    driver down onto an unavailable image."""
    old = image_recipe()
    a = process_stack("worker", env={"THEOZOLITH_REPO": "a/x", "THEOZOLITH_RUN_IMAGE": old["tag"]})
    rig.control.heartbeat_answers.append(heartbeat_response([a], [old]))
    rig.daemon.once()
    first = rig.popen.spawned[0]

    new = image_recipe(setup=["this build will fail"])

    def failing_build(*_a, **_k):
        raise RuntimeError("build failed")

    rig.docker.build = failing_build  # type: ignore[assignment]
    b = process_stack("worker", env={"THEOZOLITH_REPO": "a/x", "THEOZOLITH_RUN_IMAGE": new["tag"]})
    rig.control.heartbeat_answers.append(heartbeat_response([b], [new]))
    rig.daemon.once()

    assert not rig.docker.image_exists(new["tag"])
    assert len(rig.popen.spawned) == 1 and first.poll() is None  # old child kept alive
    assert any("not built yet" in line for line in rig.logs)


def test_changed_secret_mapping_restarts_and_exposes_the_new_file(rig: Rig):
    rig.control.secrets["tok-a"] = "A"
    rig.control.secrets["tok-b"] = "B"
    a = process_stack("worker", secrets={"IMPLEMENTER_GITHUB_TOKEN": "tok-a"})
    rig.control.heartbeat_answers.append(heartbeat_response([a]))
    rig.daemon.once()
    assert rig.popen.spawned[0].env["IMPLEMENTER_GITHUB_TOKEN_FILE"].endswith("tok-a")

    b = process_stack("worker", secrets={"IMPLEMENTER_GITHUB_TOKEN": "tok-b"})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2
    assert rig.popen.spawned[1].env["IMPLEMENTER_GITHUB_TOKEN_FILE"].endswith("tok-b")


def test_stopped_stack_with_a_config_change_stays_stopped(rig: Rig):
    a = process_stack("worker", state="stopped", env={"SOME_TUNABLE": "a"})
    rig.control.heartbeat_answers.append(heartbeat_response([a]))
    rig.daemon.once()
    b = process_stack("worker", state="stopped", env={"SOME_TUNABLE": "b"})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker")
    assert not rig.popen.spawned  # a stopped-by-desire Stack never launches


# -- fail-closed cutover: driver Stacks require a control-authored run image ------


def test_driver_implementer_without_run_image_does_not_start(rig: Rig):
    stack = process_stack(
        "implementer",
        command="theozolith-driver builtin:implementer",
        env={"THEOZOLITH_REPO": "acme/x"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("implementer") and not rig.popen.spawned
    (event,) = rig.control.events
    assert "THEOZOLITH_RUN_IMAGE" in event["message"]
    assert "incompatible/incomplete desired state" in event["message"]


def test_driver_reviewer_without_run_image_does_not_start(rig: Rig):
    stack = process_stack(
        "reviewer",
        command="theozolith-driver builtin:reviewer",
        env={"THEOZOLITH_REPO": "a/x"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("reviewer") and not rig.popen.spawned
    (event,) = rig.control.events
    assert "builtin:reviewer" in event["message"]


def test_future_custom_driver_ref_without_run_image_does_not_start(rig: Rig):
    # The guard keys on the generic launcher, not a ref registry: a future
    # drivers/<name> ref (ADR-0042) gets the same fail-closed treatment.
    stack = process_stack(
        "custom",
        command="theozolith-driver drivers/example",
        env={"THEOZOLITH_REPO": "a/x"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("custom") and not rig.popen.spawned
    (event,) = rig.control.events
    assert "drivers/example" in event["message"]


def test_live_driver_stops_when_run_image_env_is_lost(rig: Rig):
    recipe = image_recipe()
    good = process_stack(
        "implementer",
        command="theozolith-driver builtin:implementer",
        env={"THEOZOLITH_REPO": "acme/x", "THEOZOLITH_RUN_IMAGE": recipe["tag"]},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([good], [recipe]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("implementer")

    bad = process_stack(
        "implementer",
        command="theozolith-driver builtin:implementer",
        env={"THEOZOLITH_REPO": "acme/x"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([bad]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("implementer")  # stale execution stopped
    assert rig.control.events  # the incompatibility is surfaced


def test_generic_process_stack_without_run_image_starts_normally(rig: Rig):
    stack = process_stack("batch", command="my-batch --run", env={})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("batch")  # a generic Stack needs no run image


def test_driver_guard_uses_parsed_argv_not_substring(rig: Rig):
    # The guard matches shlex-parsed argv[0] — the same parse the supervisor
    # executes — not a substring of the command line. A command that merely
    # MENTIONS the launcher is a generic Stack; a shell-quoted argv[0] naming
    # the launcher is still a driver.
    mentions = process_stack("wrapper", command="echo theozolith-driver", env={})
    quoted = process_stack(
        "quoted",
        command="'theozolith-driver' builtin:implementer",
        env={"THEOZOLITH_REPO": "a/x"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([mentions, quoted]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("wrapper")  # not a driver: starts freely
    assert not rig.daemon._supervisor.alive("quoted")  # driver: fails closed
    (event,) = rig.control.events
    assert "THEOZOLITH_RUN_IMAGE" in event["message"]


def test_other_stacks_reconcile_when_one_fails_closed(rig: Rig):
    bad = process_stack(
        "implementer",
        command="theozolith-driver builtin:implementer",
        env={"THEOZOLITH_REPO": "acme/x"},
    )
    good = process_stack("batch", command="my-batch --run", env={})
    rig.control.heartbeat_answers.append(heartbeat_response([bad, good]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("implementer")
    assert rig.daemon._supervisor.alive("batch")  # reconcile continued past the failure


# -- effective-spec convergence: container Stacks (ADR-0044 amendment) ------------


def _run_container(rig: Rig, stack: dict) -> None:
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()


def test_unchanged_container_spec_is_not_recreated(rig: Rig):
    stack = container_stack("flightdeck")
    _run_container(rig, stack)
    rig.docker.removed.clear()
    _run_container(rig, stack)
    assert "ozolith-stack-flightdeck" not in rig.docker.removed


def test_changed_image_tag_recreates_the_flightdeck(rig: Rig):
    _run_container(rig, container_stack("flightdeck", image="img:1"))
    rig.docker.removed.clear()
    _run_container(rig, container_stack("flightdeck", image="img:2"))
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["image"] == "img:2"
    assert "ozolith-stack-flightdeck" in rig.docker.removed


def test_changed_container_command_recreates_it(rig: Rig):
    _run_container(rig, container_stack("flightdeck", command="tmux new -s a"))
    rig.docker.removed.clear()
    _run_container(rig, container_stack("flightdeck", command="tmux new -s b"))
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["command"][-1] == "b"
    assert "ozolith-stack-flightdeck" in rig.docker.removed


def test_changed_container_env_recreates_it(rig: Rig):
    _run_container(rig, container_stack("flightdeck", env={"MODE": "a"}))
    rig.docker.removed.clear()
    _run_container(rig, container_stack("flightdeck", env={"MODE": "b"}))
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["env"] == {"MODE": "b"}
    assert "ozolith-stack-flightdeck" in rig.docker.removed


def test_changed_container_ports_or_volumes_recreate_it(rig: Rig):
    _run_container(rig, container_stack("flightdeck", ports=["8443:8443"], volumes=[]))
    rig.docker.removed.clear()
    _run_container(rig, container_stack("flightdeck", ports=["8443:8443"], volumes=["v:/p"]))
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["volumes"] == ["v:/p"]
    assert "ozolith-stack-flightdeck" in rig.docker.removed


def test_changed_container_secret_mapping_recreates_it(rig: Rig):
    rig.control.secrets["a"] = "A"
    rig.control.secrets["b"] = "B"
    _run_container(rig, container_stack("flightdeck", secrets={"TOK": "a"}))
    rig.docker.removed.clear()
    _run_container(rig, container_stack("flightdeck", secrets={"TOK": "b"}))
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["env_files"]["TOK"].endswith("b")
    assert "ozolith-stack-flightdeck" in rig.docker.removed


def test_preexisting_unlabeled_container_is_reconciled_once(rig: Rig):
    """Recovery after a daemon restart / a manual start: a running container
    with no applied-spec label is not trusted — it is reconciled exactly once,
    then stays stable."""
    rig.docker.stacks["ozolith-stack-flightdeck"] = {
        "stack": "flightdeck",
        "image": "stale:0",
        "state": "running",
    }  # no theozolith.spec label
    stack = container_stack("flightdeck", image="img:1")
    _run_container(rig, stack)
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["image"] == "img:1"
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["theozolith.spec"]

    rig.docker.removed.clear()
    _run_container(rig, stack)
    assert "ozolith-stack-flightdeck" not in rig.docker.removed  # stable thereafter


# -- desired-state process changes queue behind an in-flight Run (ADR-0044) -------
#
# A worker-type change resolves into a live process Stack's effective spec, but
# it must never interrupt an in-flight Run: the daemon leaves the running child
# (and its Run containers) untouched and restarts exactly once after the Run
# ends. Stopped/drained desire stays immediate.


def _launch_driver(rig: Rig, jobs: Path, images=None, secrets=None, **env) -> object:
    """Launch a generic process driver whose jobs dir is ``jobs``; return its
    child. Generic command (not a built-in) so no run-image gating interferes."""
    stack = process_stack(
        "worker", env={"THEOZOLITH_JOBS_DIR": str(jobs), **env}, secrets=secrets or {}
    )
    rig.control.heartbeat_answers.append(heartbeat_response([stack], images))
    rig.daemon.once()
    return rig.popen.spawned[-1]


def _plant_run(rig: Rig, jobs: Path, run_id: str, owner: str = "worker") -> None:
    """An in-flight Run: a job directory under the driver's jobs dir AND a
    labeled run container the driver owns."""
    (jobs / run_id).mkdir(parents=True, exist_ok=True)
    rig.docker.add_run_container(f"run-{run_id}", run_id, owner)


def test_desired_model_change_during_a_run_is_deferred(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    first = _launch_driver(rig, jobs, SOME_TUNABLE="claude-sonnet-5")
    _plant_run(rig, jobs, "r1")

    b = process_stack(
        "worker", env={"THEOZOLITH_JOBS_DIR": str(jobs), "SOME_TUNABLE": "claude-opus-5"}
    )
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()

    # Deferred: no new child; the old child AND its run container are untouched.
    assert len(rig.popen.spawned) == 1 and first.poll() is None
    assert "run-r1" not in rig.docker.removed
    assert any("restart deferred" in line for line in rig.logs)


@pytest.mark.parametrize(
    "env_a,env_b",
    [
        ({"THEOZOLITH_ADAPTER": "claude"}, {"THEOZOLITH_ADAPTER": "codex"}),
        ({"THEOZOLITH_REPO": "acme/one"}, {"THEOZOLITH_REPO": "acme/two"}),  # workspace
    ],
)
def test_other_env_spec_change_during_a_run_uses_the_same_deferral(
    rig: Rig, tmp_path, env_a, env_b
):
    jobs = tmp_path / "jobs"
    first = _launch_driver(rig, jobs, **env_a)
    _plant_run(rig, jobs, "r1")

    b = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(jobs), **env_b})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 1 and first.poll() is None
    assert "run-r1" not in rig.docker.removed
    assert any("restart deferred" in line for line in rig.logs)


def test_desired_run_image_change_during_a_run_is_deferred(rig: Rig, tmp_path):
    """Even when the NEW run image builds successfully, the restart defers
    behind the Run — the deferral path is the Run, not the missing image."""
    jobs = tmp_path / "jobs"
    old = image_recipe()
    first = _launch_driver(
        rig, jobs, images=[old], THEOZOLITH_REPO="a/x", THEOZOLITH_RUN_IMAGE=old["tag"]
    )
    _plant_run(rig, jobs, "r1")

    new = image_recipe(setup=["pip install uv", "apt-get install -y jq"])
    assert new["tag"] != old["tag"]
    b = process_stack(
        "worker",
        env={
            "THEOZOLITH_JOBS_DIR": str(jobs),
            "THEOZOLITH_REPO": "a/x",
            "THEOZOLITH_RUN_IMAGE": new["tag"],
        },
    )
    rig.control.heartbeat_answers.append(heartbeat_response([b], [new]))
    rig.daemon.once()

    assert rig.docker.image_exists(new["tag"])  # the new image DID build...
    assert len(rig.popen.spawned) == 1 and first.poll() is None  # ...restart still deferred
    assert "run-r1" not in rig.docker.removed


def test_desired_secret_mapping_change_during_a_run_is_deferred(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    rig.control.secrets.update({"tok-a": "A", "tok-b": "B"})
    first = _launch_driver(rig, jobs, secrets={"IMPLEMENTER_GITHUB_TOKEN": "tok-a"})
    _plant_run(rig, jobs, "r1")

    b = process_stack(
        "worker",
        env={"THEOZOLITH_JOBS_DIR": str(jobs)},
        secrets={"IMPLEMENTER_GITHUB_TOKEN": "tok-b"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 1 and first.poll() is None
    assert "run-r1" not in rig.docker.removed


def test_deferred_desired_change_restarts_exactly_once_after_the_run(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    first = _launch_driver(rig, jobs, SOME_TUNABLE="m1")
    _plant_run(rig, jobs, "r1")

    b = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(jobs), "SOME_TUNABLE": "m2"})
    for _ in range(2):  # two deferred passes while the Run is in flight
        rig.control.heartbeat_answers.append(heartbeat_response([b]))
        rig.daemon.once()
    assert len(rig.popen.spawned) == 1

    shutil.rmtree(jobs / "r1")  # the Run ends
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2 and first.poll() is not None
    assert rig.popen.spawned[1].env["SOME_TUNABLE"] == "m2"

    rig.control.heartbeat_answers.append(heartbeat_response([b]))  # and no second restart
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2


def test_jobs_dir_change_still_detects_a_run_under_the_old_path(rig: Rig, tmp_path):
    """When THEOZOLITH_JOBS_DIR itself changes, the in-flight check inspects the
    RUNNING child's old jobs dir — a naive check of the new (empty) desired path
    would wrongly restart mid-Run."""
    old_jobs = tmp_path / "old-jobs"
    new_jobs = tmp_path / "new-jobs"
    first = _launch_driver(rig, old_jobs, SOME_TUNABLE="m1")
    _plant_run(rig, old_jobs, "r1")  # Run under the OLD path
    new_jobs.mkdir(parents=True)  # the NEW desired path is empty

    b = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(new_jobs), "SOME_TUNABLE": "m2"})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 1 and first.poll() is None  # deferred on the old path
    assert any("restart deferred" in line for line in rig.logs)

    shutil.rmtree(old_jobs / "r1")  # the Run ends under the old path
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2
    assert rig.popen.spawned[1].env["THEOZOLITH_JOBS_DIR"] == str(new_jobs)


def test_desired_change_to_stopped_is_immediate_even_mid_run(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    first = _launch_driver(rig, jobs)
    _plant_run(rig, jobs, "r1")

    stopped = process_stack("worker", state="stopped", env={"THEOZOLITH_JOBS_DIR": str(jobs)})
    rig.control.heartbeat_answers.append(heartbeat_response([stopped]))
    rig.daemon.once()
    assert first.poll() is not None  # stopped desire takes the child down at once
    assert not rig.daemon._supervisor.alive("worker")
    assert "run-r1" in rig.docker.removed  # its Run containers go with it (kill-the-tree)


def test_drain_command_is_immediate_even_mid_run(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    first = _launch_driver(rig, jobs)
    _plant_run(rig, jobs, "r1")

    stack = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(jobs)})
    drain = {"id": 30, "verb": "drain", "target": "worker", "force": False}
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[drain]))
    rig.daemon.once()
    assert first.poll() is not None and not rig.daemon._supervisor.alive("worker")
    assert 30 in rig.daemon._completed  # drain is never deferred


# -- container Stacks preserve a working image when the replacement is missing ----
#
# A single-image container (the Flight Deck) whose desired image is one of THIS
# node's own recipe tags must not be torn down onto an unavailable/failed build:
# keep the current container, defer, replace exactly once the image appears.
# Arbitrary external image references are Docker's to pull and are not gated.


def _fail_build(rig: Rig):
    def failing_build(*_a, **_k):
        raise RuntimeError("build failed")

    rig.docker.build = failing_build  # type: ignore[assignment]


def test_failed_flightdeck_build_leaves_the_old_container_running(rig: Rig):
    old = image_recipe("flightdeck")
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("flightdeck", image=old["tag"])], [old])
    )
    rig.daemon.once()
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["image"] == old["tag"]

    new = image_recipe("flightdeck", setup=["pip install uv", "boom"])
    _fail_build(rig)
    rig.docker.removed.clear()
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("flightdeck", image=new["tag"])], [new])
    )
    rig.daemon.once()

    assert not rig.docker.image_exists(new["tag"])
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["image"] == old["tag"]  # unchanged
    assert rig.docker.removed == []  # no remove while the new image is unavailable
    assert any("not built yet" in line for line in rig.logs)


def test_flightdeck_replaced_exactly_once_after_a_later_successful_build(rig: Rig):
    old = image_recipe("flightdeck")
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("flightdeck", image=old["tag"])], [old])
    )
    rig.daemon.once()

    new = image_recipe("flightdeck", setup=["pip install uv", "jq"])
    stack_new = container_stack("flightdeck", image=new["tag"])
    _fail_build(rig)
    rig.docker.removed.clear()  # ignore the initial create's internal rm
    for _ in range(2):  # the build keeps failing: no replacement
        rig.control.heartbeat_answers.append(heartbeat_response([stack_new], [new]))
        rig.daemon.once()
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["image"] == old["tag"]
    assert rig.docker.removed == []

    del rig.docker.build  # the real FakeDocker.build succeeds now
    rig.control.heartbeat_answers.append(heartbeat_response([stack_new], [new]))
    rig.daemon.once()
    assert rig.docker.image_exists(new["tag"])
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["image"] == new["tag"]
    assert rig.docker.removed.count("ozolith-stack-flightdeck") == 1  # replaced exactly once

    rig.docker.removed.clear()
    rig.control.heartbeat_answers.append(heartbeat_response([stack_new], [new]))
    rig.daemon.once()
    assert "ozolith-stack-flightdeck" not in rig.docker.removed  # stable thereafter


def test_initial_deploy_with_an_unavailable_derived_image_creates_nothing(rig: Rig):
    recipe = image_recipe("flightdeck", setup=["boom"])
    _fail_build(rig)
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("flightdeck", image=recipe["tag"])], [recipe])
    )
    rig.daemon.once()

    assert not rig.docker.image_exists(recipe["tag"])
    assert "ozolith-stack-flightdeck" not in rig.docker.stacks  # nothing created
    assert rig.docker.removed == []
    assert any("not built yet" in line for line in rig.logs)


def test_external_container_image_is_not_gated_on_local_existence(rig: Rig):
    """An image reference that is NOT one of this node's recipe tags is left for
    Docker to pull — it is not required to exist locally first."""
    stack = container_stack("flightdeck", image="ghcr.io/x/thing:1.2.3")
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))  # no recipes declared
    rig.daemon.once()
    assert rig.docker.stacks["ozolith-stack-flightdeck"]["image"] == "ghcr.io/x/thing:1.2.3"


# -- process restarts preflight replacement secrets before stopping (ADR-0044) ----
#
# Pull/materialize the new effective spec's secrets BEFORE stopping the old
# child; if they are unavailable and uncached, keep the old child running.


def test_unavailable_replacement_secret_keeps_the_old_child_and_run(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    rig.control.secrets["tok-a"] = "A"
    first = _launch_driver(rig, jobs, secrets={"IMPLEMENTER_GITHUB_TOKEN": "tok-a"})
    assert first.env["IMPLEMENTER_GITHUB_TOKEN_FILE"].endswith("tok-a")
    rig.docker.add_run_container("run-x", "r-x", "worker")  # a Run container it owns

    # Switch to a mapping the Control Node will not serve and that is not cached.
    rig.control.denied_secrets = True
    b = process_stack(
        "worker",
        env={"THEOZOLITH_JOBS_DIR": str(jobs)},
        secrets={"IMPLEMENTER_GITHUB_TOKEN": "tok-b"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 1 and first.poll() is None  # old child kept alive
    assert "run-x" not in rig.docker.removed  # no Run containers removed on failed preflight
    assert any("secrets unavailable" in line for line in rig.logs)

    # The secret becomes available: restart exactly once with the new *_FILE.
    rig.control.denied_secrets = False
    rig.control.secrets["tok-b"] = "B"
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2 and first.poll() is not None
    assert rig.popen.spawned[1].env["IMPLEMENTER_GITHUB_TOKEN_FILE"].endswith("tok-b")
    assert "run-x" in rig.docker.removed  # kill-the-tree only now, after prerequisites ready

    rig.control.heartbeat_answers.append(heartbeat_response([b]))  # stable thereafter
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2


def test_secret_values_never_enter_fingerprint_or_persisted_state(rig: Rig):
    rig.control.secrets["tok-a"] = "super-secret-value"
    stack = process_stack("worker", secrets={"IMPLEMENTER_GITHUB_TOKEN": "tok-a"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    child = rig.daemon._supervisor._children["worker"]
    assert "super-secret-value" not in child.fingerprint
    assert all("super-secret-value" not in v for v in rig.daemon._applied_jobs_dir.values())
    # Nothing the daemon persisted to disk (desired cache, acks, drained marks,
    # applied specs) carries the value — only tmpfs holds it.
    for path in rig.config.state_dir.rglob("*"):
        if path.is_file():
            assert "super-secret-value" not in path.read_text(errors="replace"), path


# -- desired-inventory convergence: runtime objects absent from desired (ADR-0044) -
#
# Removal from desired state is a deletion, immediate: a Stack the desired
# document no longer names must not keep running as a hidden child or an
# orphaned container. Renames and kind flips are the boundary cases — at most
# ONE runtime kind may exist for a Stack name.


def test_desired_rename_stops_old_worker_and_starts_only_implementer(rig: Rig):
    """Renaming worker -> implementer stops the old supervised child and leaves
    exactly the new one running (its old name is gone from desired state)."""
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()
    first = rig.popen.spawned[0]
    assert rig.daemon._supervisor.alive("worker")

    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("implementer")]))
    rig.daemon.once()
    assert first.poll() is not None  # the old child was stopped
    assert not rig.daemon._supervisor.alive("worker")
    assert "worker" not in rig.daemon._supervisor.names()  # not retained as a hidden child
    assert rig.daemon._supervisor.alive("implementer")
    assert [p.args[0] for p in rig.popen.spawned] == ["worker-driver", "implementer-driver"]


def test_empty_desired_stops_process_and_removes_run_containers(rig: Rig):
    """An empty desired Stack list tears a previously supervised process down
    (kill-the-tree): the child stops and its labeled Run containers go with it."""
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()
    first = rig.popen.spawned[0]
    rig.docker.add_run_container("run-r1", "r1", "worker")

    rig.control.heartbeat_answers.append(heartbeat_response([]))
    rig.daemon.once()
    assert first.poll() is not None
    assert not rig.daemon._supervisor.alive("worker")
    assert "worker" not in rig.daemon._supervisor.names()
    assert "run-r1" in rig.docker.removed
    assert rig.docker.run_containers() == []
    assert "worker" not in rig.daemon._applied_jobs_dir  # bookkeeping cleared


def test_removing_a_container_stack_removes_its_labeled_container(rig: Rig):
    """A single-image container Stack dropped from desired state has its labeled
    container removed — discovered from the persistent Docker label, so this
    holds even across a daemon restart."""
    rig.control.heartbeat_answers.append(heartbeat_response([container_stack("flightdeck")]))
    rig.daemon.once()
    assert "ozolith-stack-flightdeck" in rig.docker.stacks

    rig.control.heartbeat_answers.append(heartbeat_response([]))
    rig.daemon.once()
    assert "ozolith-stack-flightdeck" not in rig.docker.stacks
    assert "ozolith-stack-flightdeck" in rig.docker.removed


def test_removing_a_compose_stack_composes_it_down(rig: Rig):
    """A compose Stack this daemon started and desired state no longer names is
    composed down (scoped to this daemon lifetime — the stated limitation)."""
    stack = container_stack(
        "control",
        image="",
        ports=[],
        compose_files=[{"name": "compose/control.yml", "content": "services: {}\n"}],
    )
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert "ozolith-control" in rig.docker.compose_projects

    rig.control.heartbeat_answers.append(heartbeat_response([]))
    rig.daemon.once()
    assert "ozolith-control" not in rig.docker.compose_projects
    assert "control" not in rig.daemon._applied_compose


def test_unrelated_stacks_are_preserved_when_one_is_removed(rig: Rig):
    """Removing one Stack must not disturb the others."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([process_stack("worker"), container_stack("flightdeck")])
    )
    rig.daemon.once()
    keep = rig.popen.spawned[0]

    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker") and keep.poll() is None  # untouched
    assert "ozolith-stack-flightdeck" in rig.docker.removed  # only the dropped one went


# -- Stack kind transitions: at most one runtime kind per name (ADR-0044) ---------


def test_process_to_container_leaves_one_container_and_no_child(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    first = _launch_driver(rig, jobs)
    assert rig.daemon._supervisor.alive("worker")

    cont = container_stack("worker", image="ghcr.io/x/w:1")
    rig.control.heartbeat_answers.append(heartbeat_response([cont]))
    rig.daemon.once()
    assert first.poll() is not None  # process form retired
    assert not rig.daemon._supervisor.alive("worker")
    assert "worker" not in rig.daemon._supervisor.names()
    assert "worker" not in rig.daemon._applied_jobs_dir
    assert rig.docker.stacks["ozolith-stack-worker"]["image"] == "ghcr.io/x/w:1"

    # Stable: the transition happens once, no churn on the next pass.
    rig.docker.removed.clear()
    rig.control.heartbeat_answers.append(heartbeat_response([cont]))
    rig.daemon.once()
    assert "ozolith-stack-worker" not in rig.docker.removed
    assert not rig.daemon._supervisor.alive("worker")


def test_container_to_process_leaves_one_child_and_no_container(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("worker", image="ghcr.io/x/w:1")])
    )
    rig.daemon.once()
    assert "ozolith-stack-worker" in rig.docker.stacks

    proc = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(jobs)})
    rig.control.heartbeat_answers.append(heartbeat_response([proc]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker")
    assert "ozolith-stack-worker" not in rig.docker.stacks  # container form removed
    assert "ozolith-stack-worker" in rig.docker.removed

    # Stable: the process is not restarted and the container does not reappear.
    spawned = len(rig.popen.spawned)
    rig.control.heartbeat_answers.append(heartbeat_response([proc]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == spawned
    assert "ozolith-stack-worker" not in rig.docker.stacks


def test_process_to_container_defers_while_a_run_is_in_flight(rig: Rig, tmp_path):
    """A process->container transition never interrupts an in-flight Run: it
    defers off the child's APPLIED jobs dir (the desired kind is no longer
    process), then applies exactly once after the Run ends."""
    jobs = tmp_path / "jobs"
    first = _launch_driver(rig, jobs)
    _plant_run(rig, jobs, "r1")

    cont = container_stack("worker", image="ghcr.io/x/w:1")
    rig.control.heartbeat_answers.append(heartbeat_response([cont]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker") and first.poll() is None  # child kept
    assert "ozolith-stack-worker" not in rig.docker.stacks  # no container yet
    assert "run-r1" not in rig.docker.removed  # its Run container is untouched
    assert any("process->container transition deferred" in line for line in rig.logs)

    shutil.rmtree(jobs / "r1")  # the Run ends
    rig.control.heartbeat_answers.append(heartbeat_response([cont]))
    rig.daemon.once()
    assert first.poll() is not None
    assert not rig.daemon._supervisor.alive("worker")
    assert rig.docker.stacks["ozolith-stack-worker"]["image"] == "ghcr.io/x/w:1"
    assert "run-r1" in rig.docker.removed  # kill-the-tree, only after the Run


def test_process_to_container_with_unbuilt_image_keeps_the_process(rig: Rig, tmp_path):
    """A failed derived-image build for the target container preserves the
    running process form rather than tearing it down onto a missing image."""
    jobs = tmp_path / "jobs"
    first = _launch_driver(rig, jobs)
    new = image_recipe("wdeck", setup=["boom"])
    _fail_build(rig)

    cont = container_stack("worker", image=new["tag"])
    rig.control.heartbeat_answers.append(heartbeat_response([cont], [new]))
    rig.daemon.once()
    assert not rig.docker.image_exists(new["tag"])
    assert rig.daemon._supervisor.alive("worker") and first.poll() is None  # process kept
    assert "ozolith-stack-worker" not in rig.docker.stacks
    assert any("not built yet" in line for line in rig.logs)


def test_container_to_process_with_unbuilt_run_image_keeps_the_container(rig: Rig, tmp_path):
    """A failed run-image build for the target process preserves the running
    container form (a built-in driver still requires its control-authored run
    image, but the image must actually exist before the container is retired)."""
    old = image_recipe("app")
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("app", image=old["tag"])], [old])
    )
    rig.daemon.once()
    assert rig.docker.stacks["ozolith-stack-app"]["image"] == old["tag"]

    new = image_recipe("apprun", setup=["boom"])
    _fail_build(rig)
    rig.docker.removed.clear()
    proc = process_stack(
        "app",
        command="theozolith-driver builtin:implementer",
        env={"THEOZOLITH_REPO": "acme/x", "THEOZOLITH_RUN_IMAGE": new["tag"]},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([proc], [new]))
    rig.daemon.once()
    assert not rig.docker.image_exists(new["tag"])
    assert not rig.daemon._supervisor.alive("app")  # process not started
    assert rig.docker.stacks["ozolith-stack-app"]["image"] == old["tag"]  # container kept
    assert "ozolith-stack-app" not in rig.docker.removed


def test_container_to_process_with_unavailable_secret_keeps_the_container(rig: Rig, tmp_path):
    """An unavailable, uncached replacement secret preserves the running
    container form until the secret can be materialized."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("app", image="ghcr.io/x/app:1")])
    )
    rig.daemon.once()
    rig.docker.removed.clear()

    rig.control.denied_secrets = True
    proc = process_stack(
        "app",
        env={"THEOZOLITH_JOBS_DIR": str(tmp_path / "jobs")},
        secrets={"IMPLEMENTER_GITHUB_TOKEN": "tok-x"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([proc]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("app")  # process not started
    assert rig.docker.stacks["ozolith-stack-app"]["image"] == "ghcr.io/x/app:1"  # kept
    assert "ozolith-stack-app" not in rig.docker.removed
    assert any("secrets unavailable" in line for line in rig.logs)

    # The secret becomes available: the transition completes exactly once.
    rig.control.denied_secrets = False
    rig.control.secrets["tok-x"] = "X"
    rig.control.heartbeat_answers.append(heartbeat_response([proc]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("app")
    assert "ozolith-stack-app" not in rig.docker.stacks  # container form finally removed


def test_heartbeat_cannot_hide_a_stale_running_process(rig: Rig):
    """The pre-fix hidden-child bug: a Stack removed from desired state kept
    running while the heartbeat (which reports only desired Stacks) omitted it.
    The omission must be HONEST — the daemon stops what it stops reporting."""
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker")]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker")

    rig.control.heartbeat_answers.append(heartbeat_response([]))  # worker vanishes
    rig.daemon.once()

    rig.control.heartbeat_answers.append(heartbeat_response([]))
    rig.daemon.once()
    reported = {s["name"] for s in rig.control.transcript[-1][2]["stacks"]}
    assert "worker" not in reported  # omitted from status…
    assert not rig.daemon._supervisor.alive("worker")  # …and genuinely not running
    assert "worker" not in rig.daemon._supervisor.names()


# -- stopped/drained desire tears down EVERY form, whatever kind is live -----------
#
# A stopped or drained Stack must leave NO runtime form under its name — process
# child, single-image container, tracked compose project, or owned Run containers
# — even when the desired (but stopped) kind/form differs from what is actually
# running (ADR-0044 amendment: task 1 of the runtime-form/cleanup hardening).


def _compose_stack(name: str = "svc", files=None, **overrides) -> dict:
    files = files or [{"name": "compose/base.yml", "content": "services: {}\n"}]
    return container_stack(name, image="", ports=[], compose_files=files, **overrides)


def test_running_container_with_desired_stopped_process_removes_the_container(rig: Rig):
    """running single-image container -> desired stopped process: the container
    is removed and no child is started."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()
    assert "ozolith-stack-svc" in rig.docker.stacks

    rig.control.heartbeat_answers.append(
        heartbeat_response([process_stack("svc", state="stopped")])
    )
    rig.daemon.once()
    assert "ozolith-stack-svc" not in rig.docker.stacks
    assert "ozolith-stack-svc" in rig.docker.removed
    assert not rig.daemon._supervisor.alive("svc")
    assert rig.popen.spawned == []  # no child started


def test_running_process_with_desired_stopped_container_stops_the_child(rig: Rig):
    """running process -> desired stopped container: the child is stopped and no
    container is started."""
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("svc")]))
    rig.daemon.once()
    first = rig.popen.spawned[0]
    assert rig.daemon._supervisor.alive("svc")

    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1", state="stopped")])
    )
    rig.daemon.once()
    assert first.poll() is not None and not rig.daemon._supervisor.alive("svc")
    assert "svc" not in rig.daemon._supervisor.names()
    assert "ozolith-stack-svc" not in rig.docker.stacks  # no container started


def test_running_compose_with_desired_stopped_single_image_composes_down(rig: Rig):
    """running compose project -> desired stopped single-image Stack: the compose
    project is composed down."""
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert "ozolith-svc" in rig.docker.compose_projects

    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1", state="stopped")])
    )
    rig.daemon.once()
    assert "ozolith-svc" not in rig.docker.compose_projects
    assert "svc" not in rig.daemon._applied_compose


def test_stopped_compose_desire_downs_an_untracked_project_post_restart(rig: Rig):
    """A compose Stack still named in desired state (now stopped) but whose
    in-memory tracking was lost across a daemon restart is still composed down —
    with the DESIRED compose files, a valid non-empty teardown, not left running
    unseen. (Absent-from-desired projects remain under the tracked-only limit.)"""
    stack = _compose_stack("svc")
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert "ozolith-svc" in rig.docker.compose_projects
    # Simulate a daemon restart: the compose project survives, the tracking does not.
    rig.daemon._applied_compose.clear()

    stopped = _compose_stack("svc", state="stopped")
    rig.control.heartbeat_answers.append(heartbeat_response([stopped]))
    rig.daemon.once()
    assert "ozolith-svc" not in rig.docker.compose_projects  # downed despite no tracking
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[0] == "ozolith-svc" and down[1] != []  # valid, non-empty file list


def test_running_single_image_with_desired_stopped_compose_removes_it(rig: Rig):
    """running single-image container -> desired stopped compose Stack: the
    single-image container is removed."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()
    assert "ozolith-stack-svc" in rig.docker.stacks

    rig.control.heartbeat_answers.append(
        heartbeat_response([_compose_stack("svc", state="stopped")])
    )
    rig.daemon.once()
    assert "ozolith-stack-svc" not in rig.docker.stacks
    assert "ozolith-stack-svc" in rig.docker.removed


def test_drain_removes_a_running_form_of_a_different_desired_kind(rig: Rig):
    """A drain must stop every runtime form under the name even when the desired
    kind has flipped to a form that is not the one running."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()
    assert "ozolith-stack-svc" in rig.docker.stacks

    drain = {"id": 7, "verb": "drain", "target": "svc", "force": False}
    rig.control.heartbeat_answers.append(
        heartbeat_response([process_stack("svc")], commands=[drain])
    )
    rig.daemon.once()
    assert "ozolith-stack-svc" not in rig.docker.stacks  # container form gone
    assert not rig.daemon._supervisor.alive("svc")  # not started (drained)
    assert 7 in rig.daemon._completed


# -- single-image <-> compose transitions: at most one container form per name ----


def test_single_image_to_compose_leaves_only_the_compose_project(rig: Rig):
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()
    assert "ozolith-stack-svc" in rig.docker.stacks

    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert "ozolith-stack-svc" not in rig.docker.stacks  # single-image retired
    assert "ozolith-stack-svc" in rig.docker.removed
    assert "ozolith-svc" in rig.docker.compose_projects  # compose is the only form
    assert "svc" in rig.daemon._applied_compose

    # Stable: no churn on the following pass.
    rig.docker.removed.clear()
    ups_before = sum(1 for c in rig.docker.compose_calls if c[2] == "up")
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert rig.docker.removed == []
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before
    assert "ozolith-svc" in rig.docker.compose_projects


def test_compose_to_single_image_leaves_only_the_single_image_container(rig: Rig):
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    up_files = dict(rig.docker.compose_projects)["ozolith-svc"]
    assert up_files  # materialized files were used to bring it up

    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()
    assert "ozolith-svc" not in rig.docker.compose_projects  # compose retired
    assert "svc" not in rig.daemon._applied_compose
    assert rig.docker.stacks["ozolith-stack-svc"]["image"] == "ghcr.io/x/s:1"  # only form
    # The down carried the retained materialized paths, never an empty file list.
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[0] == "ozolith-svc" and down[1] == up_files

    # Stable: no churn on the following pass.
    rig.docker.removed.clear()
    spawned = len(rig.popen.spawned)
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()
    assert "ozolith-stack-svc" not in rig.docker.removed
    assert len(rig.popen.spawned) == spawned
    assert rig.docker.stacks["ozolith-stack-svc"]["image"] == "ghcr.io/x/s:1"


def test_single_image_to_compose_with_unavailable_secret_keeps_the_container(rig: Rig):
    """single-image -> compose defers, retaining the single-image container,
    until the compose form's secrets can be materialized."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()
    rig.docker.removed.clear()

    rig.control.denied_secrets = True
    stack = _compose_stack("svc", secrets={"TOK": "tok-x"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert "ozolith-stack-svc" in rig.docker.stacks  # single-image kept
    assert "ozolith-stack-svc" not in rig.docker.removed
    assert "ozolith-svc" not in rig.docker.compose_projects  # compose not brought up
    assert "svc" not in rig.daemon._applied_compose

    # Secret becomes available: the transition completes, one form remains.
    rig.control.denied_secrets = False
    rig.control.secrets["tok-x"] = "X"
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert "ozolith-stack-svc" not in rig.docker.stacks
    assert "ozolith-svc" in rig.docker.compose_projects


def test_compose_to_single_image_with_unbuilt_image_keeps_compose(rig: Rig):
    """compose -> single-image defers, retaining the compose project, until the
    desired derived image is actually built."""
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert "ozolith-svc" in rig.docker.compose_projects

    new = image_recipe("svcimg", setup=["boom"])
    _fail_build(rig)
    stack = container_stack("svc", image=new["tag"])
    rig.control.heartbeat_answers.append(heartbeat_response([stack], [new]))
    rig.daemon.once()
    assert not rig.docker.image_exists(new["tag"])
    assert "ozolith-svc" in rig.docker.compose_projects  # compose kept
    assert "svc" in rig.daemon._applied_compose
    assert "ozolith-stack-svc" not in rig.docker.stacks  # no single-image yet
    assert any("not built yet" in line for line in rig.logs)

    # The image builds: the transition completes, one form remains.
    del rig.docker.build
    rig.control.heartbeat_answers.append(heartbeat_response([stack], [new]))
    rig.daemon.once()
    assert rig.docker.image_exists(new["tag"])
    assert "ozolith-svc" not in rig.docker.compose_projects
    assert rig.docker.stacks["ozolith-stack-svc"]["image"] == new["tag"]


# -- retained compose teardown context + real command construction (task 3/5) ------


def test_same_lifetime_compose_deletion_uses_retained_paths(rig: Rig):
    """A compose project this daemon started, then dropped from desired state, is
    composed down with the very files it was brought up with — never an empty
    file list (ADR-0044 amendment: valid retained teardown context)."""
    files = [
        {"name": "compose/base.yml", "content": "services: {}\n"},
        {"name": "overlays/net.yml", "content": "services: {}\n"},
    ]
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc", files=files)]))
    rig.daemon.once()
    up = [c for c in rig.docker.compose_calls if c[2] == "up"][-1]
    assert len(up[1]) == 2 and all(p for p in up[1])  # two real paths

    rig.control.heartbeat_answers.append(heartbeat_response([]))
    rig.daemon.once()
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[0] == "ozolith-svc"
    assert down[1] == up[1]  # SAME materialized paths, not []
    assert down[1] != []
    assert "svc" not in rig.daemon._applied_compose


def test_dockerctl_compose_builds_valid_file_scoped_commands():
    """The real DockerCtl turns retained paths into a valid file-scoped
    ``docker compose … --file … up/down`` — asserted on the actual argv the
    runner receives, not merely on FakeDocker state (task 5: command-construction
    coverage)."""
    calls: list[list[str]] = []

    def runner(args, timeout=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    files = [Path("/state/stacks/svc/compose/base.yml"), Path("/state/stacks/svc/overlays/net.yml")]
    ctl.compose("ozolith-svc", files, "up")
    ctl.compose("ozolith-svc", files, "down")

    up, down = calls
    assert up == [
        "docker",
        "compose",
        "--project-name",
        "ozolith-svc",
        "--file",
        "/state/stacks/svc/compose/base.yml",
        "--file",
        "/state/stacks/svc/overlays/net.yml",
        "up",
        "--detach",
        "--remove-orphans",
    ]
    assert down == [
        "docker",
        "compose",
        "--project-name",
        "ozolith-svc",
        "--file",
        "/state/stacks/svc/compose/base.yml",
        "--file",
        "/state/stacks/svc/overlays/net.yml",
        "down",
        "--remove-orphans",
    ]
    assert "--file" in down  # a down is never issued with an empty file set


def test_dockerctl_configured_command_overrides_the_inherited_entrypoint():
    """A configured single-image command is the FULL start command: its first
    token rides as ``--entrypoint`` BEFORE the image — replacing an ENTRYPOINT
    inherited from the base (the derived run image ships
    ``ENTRYPOINT ["theozolith-harness"]``, which would otherwise receive the
    Flight Deck's start script as a rejected argument) — and the remaining
    tokens follow the image as that entrypoint's argv."""
    calls: list[list[str]] = []

    def runner(args, timeout=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    ctl.run_stack_container(
        "deck",
        "ghcr.io/x/run:1",
        env_files={},
        env={"A": "1"},
        ports=[],
        volumes=["v:/data"],
        command=["/usr/local/bin/flightdeck-start", "--verbose"],
        spec="specdigest",
    )
    run = next(c for c in calls if c[1] == "run")
    image_at = run.index("ghcr.io/x/run:1")
    entrypoint_at = run.index("--entrypoint")
    assert entrypoint_at < image_at
    assert run[entrypoint_at + 1] == "/usr/local/bin/flightdeck-start"
    # Only the REMAINING tokens ride after the image, as the entrypoint's argv.
    assert run[image_at + 1 :] == ["--verbose"]
    assert run.count("--entrypoint") == 1


def test_dockerctl_single_token_command_runs_alone_after_entrypoint():
    """The Flight Deck's real shape — one token — produces ``--entrypoint
    <cmd>`` with NOTHING after the image (an appended duplicate would become
    the entrypoint's first argument)."""
    calls: list[list[str]] = []

    def runner(args, timeout=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    ctl.run_stack_container(
        "deck",
        "ghcr.io/x/run:1",
        env_files={},
        env={},
        ports=[],
        volumes=[],
        command=["/usr/local/bin/flightdeck-start"],
    )
    run = next(c for c in calls if c[1] == "run")
    assert run[run.index("--entrypoint") + 1] == "/usr/local/bin/flightdeck-start"
    assert run[-1] == "ghcr.io/x/run:1"  # the image is the final argument


def test_dockerctl_no_command_preserves_the_inherited_entrypoint_and_cmd():
    """No configured command: no ``--entrypoint`` and nothing after the image
    — the image's own ENTRYPOINT/CMD run exactly as built (regression: the
    override must never fire for command-less container Stacks)."""
    calls: list[list[str]] = []

    def runner(args, timeout=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    ctl.run_stack_container(
        "svc",
        "ghcr.io/x/svc:1",
        env_files={},
        env={},
        ports=["80:80"],
        volumes=[],
        command=None,
        spec="specdigest",
    )
    run = next(c for c in calls if c[1] == "run")
    assert "--entrypoint" not in run
    assert run[-1] == "ghcr.io/x/svc:1"


# -- removed-runtime teardown failures are isolated (task 4) -----------------------


def test_one_stale_compose_down_failure_does_not_block_an_unrelated_start(rig: Rig):
    """A compose-down failure for an absent Stack is logged and emitted, its
    retry state is retained, and an unrelated desired Stack still starts."""
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("gone")]))
    rig.daemon.once()
    assert "gone" in rig.daemon._applied_compose

    rig.docker.fail_compose_down.add("ozolith-gone")
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("keep")]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("keep")  # unrelated Stack converged past the failure
    assert "gone" in rig.daemon._applied_compose  # retry state retained (not forgotten)
    assert "ozolith-gone" in rig.docker.compose_projects
    assert any(e["error_class"] == "DockerError" for e in rig.control.events)


def test_one_stale_process_stop_failure_does_not_block_reconciliation(rig: Rig, monkeypatch):
    """A process-stop failure for an absent Stack is isolated: an unrelated
    desired Stack still converges and the process is retried next pass."""
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("gone")]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("gone")

    real_stop = rig.daemon._supervisor.stop

    def flaky_stop(name, **kwargs):
        if name == "gone":
            raise RuntimeError("stop failed")
        return real_stop(name, **kwargs)

    monkeypatch.setattr(rig.daemon._supervisor, "stop", flaky_stop)
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("keep", image="ghcr.io/x/k:1")])
    )
    rig.daemon.once()
    assert rig.docker.stacks["ozolith-stack-keep"]["image"] == "ghcr.io/x/k:1"  # converged
    assert "gone" in rig.daemon._supervisor.names()  # not lost: retried next pass
    assert any(e["error_class"] == "RuntimeError" for e in rig.control.events)


def test_failed_stale_cleanup_is_retried_on_a_later_pass(rig: Rig):
    """A stale compose-down that fails is retried — and succeeds — on a later
    pass, using the retained compose paths."""
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("gone")]))
    rig.daemon.once()

    rig.docker.fail_compose_down.add("ozolith-gone")
    rig.control.heartbeat_answers.append(heartbeat_response([]))
    rig.daemon.once()
    assert "gone" in rig.daemon._applied_compose  # first attempt failed, retained
    assert "ozolith-gone" in rig.docker.compose_projects

    rig.docker.fail_compose_down.clear()  # docker recovers
    rig.control.heartbeat_answers.append(heartbeat_response([]))
    rig.daemon.once()
    assert "gone" not in rig.daemon._applied_compose  # retried and cleared
    assert "ozolith-gone" not in rig.docker.compose_projects
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[1] != []  # the successful down still used retained paths


# -- compose restart-boundary recovery (ADR-0044 amendment) ------------------------
#
# A compose project can outlive the daemon. Across a REAL restart — a brand-new
# NodeDaemon over the same state dir and docker runtime — the daemon must recover
# enough non-secret applied metadata (fingerprint + retained file paths, reloaded
# from disk) to distinguish and tear that project down during a later same-name
# kind/form transition, and must verify it is actually running before declaring a
# still-desired compose Stack converged. No transition may leave two runtime forms
# under one name; secret values never enter the persisted record.


def _restart_daemon(rig: Rig, *, popen=None) -> None:
    """Simulate a real daemon restart: a brand-new NodeDaemon over the SAME
    state dir and FakeDocker runtime, with a fresh supervisor. In-memory state
    (live children, the applied-compose map) is gone — only what was persisted
    to the state dir and what survives in the docker runtime carries across,
    exactly like the daemon process restarting on a live node."""
    from theozolith_nodedaemon.daemon import NodeDaemon
    from theozolith_nodedaemon.stacks import ProcessSupervisor

    rig.daemon = NodeDaemon(
        rig.config,
        docker=rig.docker,  # type: ignore[arg-type]
        client=rig.daemon._client,
        supervisor=ProcessSupervisor(popen=popen or rig.popen, log=rig.logs.append),
        log=rig.logs.append,
        execv=lambda path, argv: rig.execv_calls.append((path, list(argv))),
    )


def _order_probe(rig: Rig) -> tuple[list[str], object]:
    """Record the ORDER of teardown vs. start across the docker + popen seams so
    a test can prove a compose ``down`` precedes the new form's docker run or
    child spawn (the two forms never coexist). Returns (events, popen); pass the
    returned popen to ``_restart_daemon`` so a spawned child lands in the
    timeline. Call it AFTER the initial up so only the transition pass is seen."""
    events: list[str] = []
    real_compose = rig.docker.compose

    def compose_spy(project, files, verb):
        real_compose(project, files, verb)
        events.append(f"compose:{verb}")

    rig.docker.compose = compose_spy  # type: ignore[method-assign]
    real_run = rig.docker.run_stack_container

    def run_spy(*args, **kwargs):
        real_run(*args, **kwargs)
        events.append("docker-run")

    rig.docker.run_stack_container = run_spy  # type: ignore[method-assign]
    real_popen = rig.popen

    def popen_spy(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        events.append("spawn")
        return proc

    return events, popen_spy


def _active_forms(rig: Rig, name: str) -> set[str]:
    """Which runtime forms are currently live for a Stack name — the invariant a
    transition must keep to one at a time."""
    forms = set()
    if rig.daemon._supervisor.alive(name):
        forms.add("process")
    if f"ozolith-stack-{name}" in rig.docker.stacks:
        forms.add("single-image")
    if f"ozolith-{name}" in rig.docker.compose_projects:
        forms.add("compose")
    return forms


def _bring_up_compose(rig: Rig, name: str = "svc", files=None) -> list[str]:
    """Bring a compose Stack up on the original daemon; return its retained
    file paths (the ones a later down must reuse, not an empty list)."""
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack(name, files=files)]))
    rig.daemon.once()
    assert f"ozolith-{name}" in rig.docker.compose_projects
    return list(rig.docker.compose_projects[f"ozolith-{name}"])


def test_compose_to_single_image_after_restart_downs_before_docker_run(rig: Rig):
    up_files = _bring_up_compose(rig)

    events, _ = _order_probe(rig)
    _restart_daemon(rig)  # fresh daemon: applied-compose reloaded from disk
    assert rig.daemon._applied_compose["svc"].files == up_files  # recovered, non-empty

    rig.control.heartbeat_answers.append(heartbeat_response([container_stack("svc")]))
    rig.daemon.once()

    assert events == ["compose:down", "docker-run"]  # down BEFORE run — never two forms
    assert _active_forms(rig, "svc") == {"single-image"}
    assert "svc" not in rig.daemon._applied_compose
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[1] == up_files  # retained paths, not an empty file list


def test_compose_to_process_after_restart_downs_before_spawn(rig: Rig):
    _bring_up_compose(rig)

    events, popen = _order_probe(rig)
    _restart_daemon(rig, popen=popen)
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("svc")]))
    rig.daemon.once()

    assert events == ["compose:down", "spawn"]  # compose retired before the child starts
    assert _active_forms(rig, "svc") == {"process"}
    assert "svc" not in rig.daemon._applied_compose


def test_compose_to_stopped_process_after_restart_leaves_nothing(rig: Rig):
    _bring_up_compose(rig)

    _restart_daemon(rig)
    rig.control.heartbeat_answers.append(
        heartbeat_response([process_stack("svc", state="stopped")])
    )
    rig.daemon.once()

    assert _active_forms(rig, "svc") == set()  # composed down, no child started
    assert "svc" not in rig.daemon._supervisor.names()
    assert "svc" not in rig.daemon._applied_compose
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[1] != []


def test_compose_to_stopped_single_image_after_restart_leaves_nothing(rig: Rig):
    _bring_up_compose(rig)

    _restart_daemon(rig)
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", state="stopped")])
    )
    rig.daemon.once()

    assert _active_forms(rig, "svc") == set()  # neither compose project nor container
    assert "svc" not in rig.daemon._applied_compose
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[1] != []


def test_drain_after_restart_tears_down_recovered_compose(rig: Rig):
    _bring_up_compose(rig)

    _restart_daemon(rig)
    drain = {"id": 30, "verb": "drain", "target": "svc", "force": False}
    rig.control.heartbeat_answers.append(
        heartbeat_response([_compose_stack("svc")], commands=[drain])
    )
    rig.daemon.once()

    assert _active_forms(rig, "svc") == set()  # drained: the old compose form is down
    assert "svc" not in rig.daemon._applied_compose
    assert 30 in rig.daemon._completed
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[1] != []


def test_no_restart_transition_leaves_two_forms(rig: Rig):
    """A full compose -> single-image -> process cycle, each leg across a
    restart, never has two forms coexisting at any observed step."""
    _bring_up_compose(rig)
    assert _active_forms(rig, "svc") == {"compose"}

    _restart_daemon(rig)  # compose -> single-image
    rig.control.heartbeat_answers.append(heartbeat_response([container_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"single-image"}

    _restart_daemon(rig)  # single-image -> process (container form discovered by label)
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"process"}


def test_recovered_applied_compose_has_no_secret_values(rig: Rig):
    rig.control.secrets["tok-a"] = "super-secret-value"
    stack = _compose_stack(
        "svc",
        files=[{"name": "compose/base.yml", "content": "services: {x: {env_file: TOK_FILE}}\n"}],
        secrets={"TOK": "tok-a"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    # The persisted applied record — and nothing else under the state dir —
    # carries the secret value; only tmpfs holds it.
    assert "super-secret-value" not in rig.config.applied_compose_path.read_text()
    for path in rig.config.state_dir.rglob("*"):
        if path.is_file():
            assert "super-secret-value" not in path.read_text(errors="replace"), path

    # The record recovered on restart is likewise value-free.
    _restart_daemon(rig)
    recovered = rig.daemon._applied_compose["svc"]
    assert "super-secret-value" not in recovered.fingerprint
    assert all("super-secret-value" not in f for f in recovered.files)


def test_healthy_unchanged_compose_after_restart_does_not_churn(rig: Rig):
    _bring_up_compose(rig)
    ups_before = sum(1 for c in rig.docker.compose_calls if c[2] == "up")

    _restart_daemon(rig)
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    # Healthy project, unchanged fingerprint: no re-up, no down.
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before
    assert not any(c[2] == "down" for c in rig.docker.compose_calls)
    assert _active_forms(rig, "svc") == {"compose"}


def test_missing_compose_with_matching_fingerprint_is_brought_up_again(rig: Rig):
    _bring_up_compose(rig)

    _restart_daemon(rig)
    del rig.docker.compose_projects["ozolith-svc"]  # died while the daemon was down
    ups_before = sum(1 for c in rig.docker.compose_calls if c[2] == "up")

    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before + 1
    assert _active_forms(rig, "svc") == {"compose"}
    assert any("not running" in line for line in rig.logs)


def test_stopped_compose_with_matching_fingerprint_is_brought_up_again(rig: Rig):
    _bring_up_compose(rig)

    _restart_daemon(rig)
    rig.docker.compose_stopped.add("ozolith-svc")  # present but not running
    ups_before = sum(1 for c in rig.docker.compose_calls if c[2] == "up")

    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before + 1
    assert "ozolith-svc" not in rig.docker.compose_stopped  # the re-up cleared the stop
    assert _active_forms(rig, "svc") == {"compose"}


def test_failed_compose_recovery_is_surfaced_and_retried_without_blocking_others(rig: Rig):
    _bring_up_compose(rig)

    _restart_daemon(rig)
    del rig.docker.compose_projects["ozolith-svc"]  # died during the outage…
    rig.docker.fail_compose_up.add("ozolith-svc")  # …and the recovery up will fail

    # An unrelated Stack shares the pass and must still converge.
    rig.control.heartbeat_answers.append(
        heartbeat_response([_compose_stack("svc"), process_stack("worker")])
    )
    rig.daemon.once()

    assert "ozolith-svc" not in rig.docker.compose_projects  # up failed
    assert "svc" in rig.daemon._applied_compose  # record retained for retry
    assert rig.daemon._supervisor.alive("worker")  # unrelated Stack unblocked
    assert any("reconcile failed" in line for line in rig.logs)
    assert any("svc" in e.get("message", "") for e in rig.control.events)  # surfaced

    # Docker recovers: the next pass retries the up successfully.
    rig.docker.fail_compose_up.discard("ozolith-svc")
    rig.control.heartbeat_answers.append(
        heartbeat_response([_compose_stack("svc"), process_stack("worker")])
    )
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"compose"}


# -- crash-consistent compose startup: write-ahead recovery record (ADR-0044) ------
#
# compose up is not atomic with recording it: Docker can create the project and
# the daemon can die before it writes anything. A recovery record is therefore
# written AHEAD of compose up (state "pending") and only confirmed ("applied")
# after a successful up. A crash anywhere across that window must still leave a
# durable record able to inspect, retry, or tear the project down — and no such
# scenario may leave two runtime forms active or leak a secret value.


def _bring_up_compose_crash_before_confirm(rig: Rig, name: str = "svc", files=None, **overrides):
    """Bring a compose Stack up but simulate a crash after Docker Compose creates
    the project and BEFORE the applied-state confirmation lands: the write-ahead
    persist (the first call) succeeds, so a durable PENDING record reaches disk;
    the post-up confirmation persist (the second call) fails, exactly as a crash
    at that instant would leave things. Returns the retained file paths."""
    real_persist = rig.daemon._persist_applied_compose
    calls = {"n": 0}

    def flaky_persist() -> None:
        calls["n"] += 1
        if calls["n"] >= 2:  # the post-up confirmation
            raise OSError("simulated crash before applied-state confirmation")
        real_persist()

    rig.daemon._persist_applied_compose = flaky_persist  # type: ignore[method-assign]
    rig.control.heartbeat_answers.append(
        heartbeat_response([_compose_stack(name, files=files, **overrides)])
    )
    rig.daemon.once()
    rig.daemon._persist_applied_compose = real_persist  # type: ignore[method-assign]
    return list(rig.docker.compose_projects[f"ozolith-{name}"])


def test_crash_after_up_before_confirm_leaves_a_durable_recovery_record(rig: Rig):
    up_files = _bring_up_compose_crash_before_confirm(rig)

    # On disk despite the confirmation never landing: name, fingerprint, non-empty
    # retained paths, pending state.
    on_disk = json.loads(rig.config.applied_compose_path.read_text())["svc"]
    assert on_disk["state"] == "pending"
    assert on_disk["files"] == up_files and up_files  # non-empty materialized paths
    assert on_disk["fingerprint"]

    # A fresh daemon (all in-memory state gone) recovers it, and it is sufficient
    # to inspect the deterministic compose project — compose_ps is the authority.
    _restart_daemon(rig)
    recovered = rig.daemon._applied_compose["svc"]
    assert recovered.files == up_files and recovered.fingerprint and recovered.state == "pending"
    assert rig.daemon._compose_running("svc")


def test_confirmation_failure_does_not_lose_recovery_metadata(rig: Rig):
    up_files = _bring_up_compose_crash_before_confirm(rig)

    # Only the confirmation failed, and it was isolated: the pass did not fail and
    # the healthy project is untouched.
    assert "ozolith-svc" in rig.docker.compose_projects
    assert not any("reconcile failed" in line for line in rig.logs)
    assert any("confirm applied compose" in line for line in rig.logs)

    # The durable write-ahead record survives on disk with its files intact.
    on_disk = json.loads(rig.config.applied_compose_path.read_text())["svc"]
    assert on_disk["state"] == "pending"
    assert on_disk["files"] == up_files and up_files


def test_pending_record_transitions_to_single_image_downing_before_run(rig: Rig):
    up_files = _bring_up_compose_crash_before_confirm(rig)

    events, _ = _order_probe(rig)
    _restart_daemon(rig)  # fresh daemon: the pending record reloads from disk
    assert rig.daemon._applied_compose["svc"].state == "pending"

    rig.control.heartbeat_answers.append(heartbeat_response([container_stack("svc")]))
    rig.daemon.once()

    assert events == ["compose:down", "docker-run"]  # down BEFORE run — never two forms
    assert _active_forms(rig, "svc") == {"single-image"}
    assert "svc" not in rig.daemon._applied_compose
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[1] == up_files  # retained paths, not an empty file list


def test_pending_record_transitions_to_process_downing_before_spawn(rig: Rig):
    _bring_up_compose_crash_before_confirm(rig)

    events, popen = _order_probe(rig)
    _restart_daemon(rig, popen=popen)
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("svc")]))
    rig.daemon.once()

    assert events == ["compose:down", "spawn"]  # compose retired before the child starts
    assert _active_forms(rig, "svc") == {"process"}
    assert "svc" not in rig.daemon._applied_compose


def test_stopped_desire_after_crash_tears_the_pending_project_down(rig: Rig):
    _bring_up_compose_crash_before_confirm(rig)

    _restart_daemon(rig)
    rig.control.heartbeat_answers.append(
        heartbeat_response([_compose_stack("svc", state="stopped")])
    )
    rig.daemon.once()

    assert _active_forms(rig, "svc") == set()  # torn down, nothing left
    assert "svc" not in rig.daemon._applied_compose
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[1] != []  # real teardown context, never an empty file list


def test_drained_desire_after_crash_tears_the_pending_project_down(rig: Rig):
    _bring_up_compose_crash_before_confirm(rig)

    _restart_daemon(rig)
    drain = {"id": 41, "verb": "drain", "target": "svc", "force": False}
    rig.control.heartbeat_answers.append(
        heartbeat_response([_compose_stack("svc")], commands=[drain])
    )
    rig.daemon.once()

    assert _active_forms(rig, "svc") == set()  # drained: the recovered project is down
    assert "svc" not in rig.daemon._applied_compose
    assert 41 in rig.daemon._completed


def test_write_ahead_persist_failure_prevents_compose_up(rig: Rig):
    real_persist = rig.daemon._persist_applied_compose

    def boom() -> None:
        raise OSError("state dir unwritable")

    rig.daemon._persist_applied_compose = boom  # type: ignore[method-assign]
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    # No project was created and no record falsely claims one — the write-ahead
    # failure rolled the in-memory record back and re-raised before compose up.
    assert not any(verb == "up" for _, _, verb in rig.docker.compose_calls)
    assert "ozolith-svc" not in rig.docker.compose_projects
    assert "svc" not in rig.daemon._applied_compose
    assert not rig.config.applied_compose_path.exists()
    assert any("reconcile failed" in line for line in rig.logs)

    # Persistence recovers: the next pass write-aheads and ups successfully.
    rig.daemon._persist_applied_compose = real_persist  # type: ignore[method-assign]
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"compose"}
    assert rig.daemon._applied_compose["svc"].state == "applied"


def test_failed_first_compose_up_retains_the_write_ahead_record_and_retries(rig: Rig):
    rig.docker.fail_compose_up.add("ozolith-svc")
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    # up failed, but the write-ahead record persisted for both retry and later
    # teardown — it does not claim health (compose_ps is the authority).
    assert "ozolith-svc" not in rig.docker.compose_projects
    assert rig.daemon._applied_compose["svc"].state == "pending"
    assert json.loads(rig.config.applied_compose_path.read_text())["svc"]["state"] == "pending"
    assert not rig.daemon._compose_running("svc")
    assert any("reconcile failed" in line for line in rig.logs)

    # Docker recovers: the same compose desire retries and comes up.
    rig.docker.fail_compose_up.discard("ozolith-svc")
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"compose"}
    assert rig.daemon._applied_compose["svc"].state == "applied"


def test_pending_record_without_a_project_is_retried_for_running_desire(rig: Rig):
    _bring_up_compose_crash_before_confirm(rig)
    _restart_daemon(rig)
    del rig.docker.compose_projects["ozolith-svc"]  # a pending record, no live project
    ups_before = sum(1 for c in rig.docker.compose_calls if c[2] == "up")

    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before + 1
    assert _active_forms(rig, "svc") == {"compose"}
    assert rig.daemon._applied_compose["svc"].state == "applied"


def test_pending_record_without_a_project_is_cleared_for_stopped_and_removed_desire(rig: Rig):
    _bring_up_compose_crash_before_confirm(rig)
    _restart_daemon(rig)
    del rig.docker.compose_projects["ozolith-svc"]  # a pending record, no live project

    # Stopped desire: a safe (no-op) down clears the record.
    rig.control.heartbeat_answers.append(
        heartbeat_response([_compose_stack("svc", state="stopped")])
    )
    rig.daemon.once()
    assert "svc" not in rig.daemon._applied_compose
    assert _active_forms(rig, "svc") == set()

    # And removal from desired state clears an equivalent pending record too.
    _bring_up_compose_crash_before_confirm(rig, name="two")
    _restart_daemon(rig)
    del rig.docker.compose_projects["ozolith-two"]
    rig.control.heartbeat_answers.append(heartbeat_response([]))  # "two" absent from desired
    rig.daemon.once()
    assert "two" not in rig.daemon._applied_compose


def test_no_crash_boundary_transition_leaves_two_forms(rig: Rig):
    """compose -> single-image -> process, each leg seeded from a crash-boundary
    pending record across a restart, never has two forms coexisting."""
    _bring_up_compose_crash_before_confirm(rig)
    assert _active_forms(rig, "svc") == {"compose"}

    _restart_daemon(rig)  # recover the pending record; compose -> single-image
    rig.control.heartbeat_answers.append(heartbeat_response([container_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"single-image"}

    _restart_daemon(rig)  # single-image -> process (container form discovered by label)
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"process"}


def test_pending_recovery_metadata_has_no_secret_values(rig: Rig):
    rig.control.secrets["tok-a"] = "super-secret-value"
    files = [{"name": "compose/base.yml", "content": "services: {x: {env_file: TOK_FILE}}\n"}]
    _bring_up_compose_crash_before_confirm(rig, files=files, secrets={"TOK": "tok-a"})

    # The pending write-ahead record — and nothing else under the state dir —
    # carries the secret value; only tmpfs holds it.
    text = rig.config.applied_compose_path.read_text()
    assert json.loads(text)["svc"]["state"] == "pending"
    assert "super-secret-value" not in text
    for path in rig.config.state_dir.rglob("*"):
        if path.is_file():
            assert "super-secret-value" not in path.read_text(errors="replace"), path

    # The record recovered on restart is likewise value-free.
    _restart_daemon(rig)
    recovered = rig.daemon._applied_compose["svc"]
    assert recovered.state == "pending"
    assert "super-secret-value" not in recovered.fingerprint
    assert all("super-secret-value" not in f for f in recovered.files)


# -- write-ahead ORDERING: the pending record is durable BEFORE any teardown ------
#
# The write-ahead recovery record is persisted BEFORE the losing runtime form is
# retired and BEFORE compose up (ADR-0044 amendment). So a persist failure — the
# state dir momentarily unwritable — must leave the OLD form running untouched:
# no process is stopped, no Run container or single-image container is removed,
# no live compose project is disturbed. Only once the pending record is durable
# do the teardown and the up proceed; a teardown failure after that still gates
# the up and keeps the pending record for a later retry. No failure path may
# leave two runtime forms coexisting or leak a secret value.


def _break_persist(rig: Rig):
    """Install a ``_persist_applied_compose`` that always raises (an unwritable
    state dir), and return the real method so a later pass can restore it. Every
    write-ahead in the interim fails and rolls back — nothing reaches disk."""
    real = rig.daemon._persist_applied_compose

    def boom() -> None:
        raise OSError("state dir unwritable")

    rig.daemon._persist_applied_compose = boom  # type: ignore[method-assign]
    return real


def test_single_image_to_compose_write_ahead_failure_keeps_the_container(rig: Rig):
    """single-image -> compose: a write-ahead persist failure keeps the losing
    single-image container running and performs NO remove — the record is durable
    before any teardown. Recovery then completes the transition exactly once."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()
    assert "ozolith-stack-svc" in rig.docker.stacks
    rig.docker.removed.clear()

    real_persist = _break_persist(rig)
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    # The single-image container is untouched: still running, never removed. No
    # compose up, no false record on disk or in memory.
    assert "ozolith-stack-svc" in rig.docker.stacks
    assert "ozolith-stack-svc" not in rig.docker.removed
    assert not any(verb == "up" for _, _, verb in rig.docker.compose_calls)
    assert "ozolith-svc" not in rig.docker.compose_projects
    assert "svc" not in rig.daemon._applied_compose
    assert not rig.config.applied_compose_path.exists()
    assert _active_forms(rig, "svc") == {"single-image"}  # exactly one form
    assert any("reconcile failed" in line for line in rig.logs)

    # Persistence recovers: the transition completes exactly once.
    rig.daemon._persist_applied_compose = real_persist  # type: ignore[method-assign]
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"compose"}
    assert rig.docker.removed.count("ozolith-stack-svc") == 1  # retired exactly once
    assert rig.daemon._applied_compose["svc"].state == "applied"

    # Stable on a further pass — no second up, no churn.
    ups = sum(1 for c in rig.docker.compose_calls if c[2] == "up")
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups
    assert _active_forms(rig, "svc") == {"compose"}


def test_process_to_compose_write_ahead_failure_keeps_the_child_and_run_containers(rig: Rig):
    """process -> compose: a write-ahead persist failure keeps the original
    process child alive and its owned Run containers in place — the child is not
    stopped and its containers are not removed. Recovery completes it once."""
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("svc")]))
    rig.daemon.once()
    child = rig.popen.spawned[0]
    rig.docker.add_run_container("run-svc-1", "run-1", "svc")
    assert rig.daemon._supervisor.alive("svc")

    real_persist = _break_persist(rig)
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    # Child and its owned Run container both survive; nothing composed up.
    assert rig.daemon._supervisor.alive("svc") and child.poll() is None
    assert "run-svc-1" in rig.docker.containers
    assert "run-svc-1" not in rig.docker.removed
    assert not any(verb == "up" for _, _, verb in rig.docker.compose_calls)
    assert "svc" not in rig.daemon._applied_compose
    assert _active_forms(rig, "svc") == {"process"}  # exactly one form
    assert any("reconcile failed" in line for line in rig.logs)

    # Persistence recovers: the child (and its Run container) retire, compose ups.
    rig.daemon._persist_applied_compose = real_persist  # type: ignore[method-assign]
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("svc") and child.poll() is not None
    assert "run-svc-1" not in rig.docker.containers  # owned Run container removed
    assert _active_forms(rig, "svc") == {"compose"}
    assert rig.daemon._applied_compose["svc"].state == "applied"


def test_compose_spec_change_write_ahead_failure_keeps_the_old_project(rig: Rig):
    """compose spec change: a write-ahead persist failure for the NEW record keeps
    the old compose project running and preserves the previous applied record —
    compose up is gated on the persist, so the live project is never disturbed."""
    files_a = [{"name": "compose/base.yml", "content": "services: {a: {}}\n"}]
    files_b = [{"name": "compose/base.yml", "content": "services: {b: {}}\n"}]
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc", files=files_a)]))
    rig.daemon.once()
    old = rig.daemon._applied_compose["svc"]
    old_fingerprint = old.fingerprint
    ups_before = sum(1 for c in rig.docker.compose_calls if c[2] == "up")

    real_persist = _break_persist(rig)
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc", files=files_b)]))
    rig.daemon.once()

    # The old project keeps running and the previous applied record is preserved;
    # no up was invoked for the new spec.
    assert "ozolith-svc" in rig.docker.compose_projects
    assert rig.daemon._applied_compose["svc"].fingerprint == old_fingerprint
    assert rig.daemon._applied_compose["svc"].state == "applied"
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before
    assert _active_forms(rig, "svc") == {"compose"}  # exactly one form
    assert any("reconcile failed" in line for line in rig.logs)

    # Persistence recovers: the new spec ups exactly once and becomes the record.
    rig.daemon._persist_applied_compose = real_persist  # type: ignore[method-assign]
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc", files=files_b)]))
    rig.daemon.once()
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before + 1
    assert rig.daemon._applied_compose["svc"].fingerprint != old_fingerprint
    assert rig.daemon._applied_compose["svc"].state == "applied"
    assert _active_forms(rig, "svc") == {"compose"}


def test_teardown_failure_after_write_ahead_prevents_up_and_retains_pending(rig: Rig):
    """A teardown failure AFTER a successful write-ahead prevents compose up and
    retains the pending record for retry — the two forms never coexist. Recovery
    completes the transition once the teardown succeeds."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()

    rig.docker.fail_remove.add("ozolith-stack-svc")  # the predecessor teardown fails
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    # Write-ahead persisted a pending record; the single-image teardown failed, so
    # compose up never ran and the old form is still the only one live.
    assert rig.daemon._applied_compose["svc"].state == "pending"
    assert json.loads(rig.config.applied_compose_path.read_text())["svc"]["state"] == "pending"
    assert not any(verb == "up" for _, _, verb in rig.docker.compose_calls)
    assert "ozolith-svc" not in rig.docker.compose_projects
    assert not rig.daemon._compose_running("svc")
    assert _active_forms(rig, "svc") == {"single-image"}  # never two forms
    assert any("reconcile failed" in line for line in rig.logs)

    # Teardown recovers: the retry removes the container and ups exactly once.
    rig.docker.fail_remove.discard("ozolith-stack-svc")
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"compose"}
    assert rig.daemon._applied_compose["svc"].state == "applied"


def test_write_ahead_orders_persist_then_teardown_then_up(rig: Rig):
    """An order probe over the persistence, teardown, and up seams proves the
    exact sequence: (1) pending persistence, (2) predecessor teardown, (3) compose
    up — followed by the best-effort applied confirmation."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()

    events: list[str] = []
    real_persist = rig.daemon._persist_applied_compose

    def persist_spy() -> None:
        real_persist()
        events.append("persist")

    rig.daemon._persist_applied_compose = persist_spy  # type: ignore[method-assign]
    real_remove = rig.docker.remove

    def remove_spy(name: str) -> None:
        real_remove(name)
        events.append(f"remove:{name}")

    rig.docker.remove = remove_spy  # type: ignore[method-assign]
    real_compose = rig.docker.compose

    def compose_spy(project, files, verb) -> None:
        real_compose(project, files, verb)
        events.append(f"compose:{verb}")

    rig.docker.compose = compose_spy  # type: ignore[method-assign]

    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    # Pending persistence FIRST, then the predecessor teardown, then compose up —
    # the applied confirmation persists last (best-effort).
    assert events == [
        "persist",
        "remove:ozolith-stack-svc",
        "compose:up",
        "persist",
    ]
    assert _active_forms(rig, "svc") == {"compose"}


def test_crash_after_pending_persist_before_teardown_recovers_safely(rig: Rig):
    """A crash AFTER the pending record persists but BEFORE the predecessor
    teardown leaves the old form running and a durable pending record; a real
    restart recovers to exactly one form, retiring the old form before up."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()

    # Persist the write-ahead record, then interrupt the pass right after it (the
    # single-image teardown raises), exactly as a crash at that instant would.
    rig.docker.fail_remove.add("ozolith-stack-svc")
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    on_disk = json.loads(rig.config.applied_compose_path.read_text())["svc"]
    assert on_disk["state"] == "pending"  # durable write-ahead landed
    assert "ozolith-stack-svc" in rig.docker.stacks  # old form still running
    assert not any(verb == "up" for _, _, verb in rig.docker.compose_calls)
    assert _active_forms(rig, "svc") == {"single-image"}

    # A real restart (all in-memory state gone) recovers from disk and completes
    # the transition — the old single-image form is retired before compose up.
    rig.docker.fail_remove.discard("ozolith-stack-svc")
    _restart_daemon(rig)
    assert rig.daemon._applied_compose["svc"].state == "pending"  # recovered pending
    events, _ = _order_probe(rig)
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    assert events == ["compose:up"]  # only the up on this seam; teardown is a remove
    assert "ozolith-stack-svc" not in rig.docker.stacks  # old form retired
    assert _active_forms(rig, "svc") == {"compose"}  # exactly one form, converged
    assert rig.daemon._applied_compose["svc"].state == "applied"


def test_no_write_ahead_failure_path_leaves_two_forms(rig: Rig):
    """Across every write-ahead failure mode — persist failure on a single-image
    transition, then a teardown failure, then recovery — never are two runtime
    forms live at once for the same name."""
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()
    assert len(_active_forms(rig, "svc")) == 1

    # Write-ahead persist failure: still one form (the single-image container).
    real_persist = _break_persist(rig)
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert len(_active_forms(rig, "svc")) == 1

    # Persist recovers but the teardown fails: still one form, up gated.
    rig.daemon._persist_applied_compose = real_persist  # type: ignore[method-assign]
    rig.docker.fail_remove.add("ozolith-stack-svc")
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert len(_active_forms(rig, "svc")) == 1

    # Teardown recovers: converges to exactly one form (compose).
    rig.docker.fail_remove.discard("ozolith-stack-svc")
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"compose"}


def test_transition_write_ahead_pending_record_has_no_secret_values(rig: Rig):
    """The pending record written AHEAD of a single-image -> compose transition —
    and nothing else under the state dir — carries a secret value; the value lives
    only in the secrets tmpfs the compose files reference by VAR_FILE."""
    rig.control.secrets["tok-a"] = "super-secret-value"
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("svc", image="ghcr.io/x/s:1")])
    )
    rig.daemon.once()

    files = [{"name": "compose/base.yml", "content": "services: {x: {env_file: TOK_FILE}}\n"}]
    stack = _compose_stack("svc", files=files, secrets={"TOK": "tok-a"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    record = rig.daemon._applied_compose["svc"]
    assert "super-secret-value" not in record.fingerprint
    assert all("super-secret-value" not in f for f in record.files)
    for path in rig.config.state_dir.rglob("*"):
        if path.is_file():
            assert "super-secret-value" not in path.read_text(errors="replace"), path


# -- immutable, generation-addressed compose materialization (ADR-0044) ------------
#
# An applied/pending record's compose files must be IMMUTABLE for that record's
# lifetime: a new spec materializes its own generation-addressed directory and
# never overwrites the bytes the predecessor's record still references. So a
# failed pending persist mutates nothing the old record points at, a crash before
# persistence recovers with the old generation fully usable, and a successful
# transition reclaims obsolete generations.

_FILES_A = [{"name": "compose/base.yml", "content": "services: {a: {}}\n"}]
_FILES_B = [{"name": "compose/base.yml", "content": "services: {b: {}}\n"}]


def _generation_dir(files: list[str]) -> Path:
    """The generation directory a record's files live in:
    <state>/stacks/<name>/<generation>/<file-name>."""
    return Path(files[0]).parent.parent


def _generation_dirs(rig: Rig, name: str) -> set[str]:
    stack_dir = rig.config.state_dir / "stacks" / name
    return {p.name for p in stack_dir.iterdir() if p.is_dir()} if stack_dir.is_dir() else set()


def _push_compose(rig: Rig, files: list, name: str = "svc") -> None:
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack(name, files=files)]))


def test_failed_pending_persist_leaves_predecessor_generation_bytes_untouched(rig: Rig):
    """Spec A is running with an applied record; a spec-B write-ahead persist
    fails. A stays running, the applied fingerprint stays A, every retained file
    of A is byte-for-byte A, and no compose up or down occurs."""
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    record_a = rig.daemon._applied_compose["svc"]
    a_files = list(record_a.files)
    a_fingerprint = record_a.fingerprint
    a_bytes = {p: Path(p).read_bytes() for p in a_files}
    ups = sum(1 for c in rig.docker.compose_calls if c[2] == "up")
    downs = sum(1 for c in rig.docker.compose_calls if c[2] == "down")

    real_persist = _break_persist(rig)
    _push_compose(rig, _FILES_B)
    rig.daemon.once()

    assert _active_forms(rig, "svc") == {"compose"}
    assert rig.daemon._applied_compose["svc"].fingerprint == a_fingerprint
    assert rig.daemon._applied_compose["svc"].files == a_files
    for path, original in a_bytes.items():
        assert Path(path).read_bytes() == original  # byte-for-byte A, never overwritten by B
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups  # no up
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "down") == downs  # no down
    assert any("reconcile failed" in line for line in rig.logs)

    rig.daemon._persist_applied_compose = real_persist  # type: ignore[method-assign]


def test_crash_before_pending_persist_recovers_with_the_old_generation(rig: Rig):
    """A crash immediately after candidate B is materialized but before its
    pending record persists: a restart from disk recovers A's record intact, and
    a later transition composes A's retained generation down — never B's."""
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    a_files = list(rig.daemon._applied_compose["svc"].files)

    # Materialize B, then fail the pending persist (the crash point).
    _break_persist(rig)
    _push_compose(rig, _FILES_B)
    rig.daemon.once()

    _restart_daemon(rig)  # fresh daemon: only disk carries across
    assert rig.daemon._applied_compose["svc"].files == a_files  # A fully usable

    # A transition now downs A's retained generation, not B's.
    events, _ = _order_probe(rig)
    rig.control.heartbeat_answers.append(heartbeat_response([container_stack("svc")]))
    rig.daemon.once()
    assert events == ["compose:down", "docker-run"]
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[1] == a_files  # A's generation, never an empty file list
    assert _active_forms(rig, "svc") == {"single-image"}


def test_successful_retry_to_new_spec_references_new_generation_and_gcs_old(rig: Rig):
    """After a failed spec-B write-ahead, a successful retry brings B up, the
    record references B's immutable generation, and A's generation is reclaimed."""
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    a_files = list(rig.daemon._applied_compose["svc"].files)
    a_gen = _generation_dir(a_files).name
    assert _generation_dirs(rig, "svc") == {a_gen}

    real_persist = _break_persist(rig)
    _push_compose(rig, _FILES_B)
    rig.daemon.once()  # write-ahead fails: A preserved, B's candidate dir left unreferenced

    rig.daemon._persist_applied_compose = real_persist  # type: ignore[method-assign]
    _push_compose(rig, _FILES_B)
    rig.daemon.once()  # retry succeeds

    record_b = rig.daemon._applied_compose["svc"]
    assert record_b.state == "applied"
    assert record_b.files != a_files  # references B's own immutable generation
    b_gen = _generation_dir(record_b.files).name
    assert b_gen != a_gen
    assert _generation_dirs(rig, "svc") == {b_gen}  # A's generation reclaimed
    assert not _generation_dir(a_files).exists()
    assert _active_forms(rig, "svc") == {"compose"}


def test_added_removed_and_renamed_compose_files_get_a_distinct_generation(rig: Rig):
    """A change that renames a file AND adds an overlay (not merely a content edit
    at an identical path) resolves to a distinct generation with its own paths;
    the old generation is reclaimed after the transition."""
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    a_gen = _generation_dir(rig.daemon._applied_compose["svc"].files).name

    files_b = [
        {"name": "compose/main.yml", "content": "services: {a: {}}\n"},  # renamed base.yml
        {"name": "overlays/net.yml", "content": "services: {}\n"},  # added overlay
    ]
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc", files=files_b)]))
    rig.daemon.once()

    record_b = rig.daemon._applied_compose["svc"]
    b_gen = _generation_dir(record_b.files).name
    assert b_gen != a_gen  # a rename/add is a new generation, not an in-place rewrite
    assert [Path(f).name for f in record_b.files] == ["main.yml", "net.yml"]
    up = [c for c in rig.docker.compose_calls if c[2] == "up"][-1]
    assert [f.split("/")[-1] for f in up[1]] == ["main.yml", "net.yml"]
    assert _generation_dirs(rig, "svc") == {b_gen}  # old generation (with base.yml) reclaimed


def test_state_dir_cleanup_never_deletes_a_referenced_generation(rig: Rig):
    """A healthy, unchanged compose Stack keeps its referenced generation across
    passes — GC only reclaims OTHER generations, never the live one."""
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    gen = _generation_dir(rig.daemon._applied_compose["svc"].files).name

    for _ in range(2):
        rig.control.heartbeat_answers.append(
            heartbeat_response([_compose_stack("svc", files=_FILES_A)])
        )
        rig.daemon.once()
    assert _generation_dirs(rig, "svc") == {gen}  # still present, still referenced
    assert _active_forms(rig, "svc") == {"compose"}


# -- pending records never claim specification convergence (ADR-0044) --------------
#
# A pending record proves its spec was write-ahead-persisted, NOT that compose up
# ran it. The deterministic project name is identical across generations, so a
# `compose_ps` running row cannot tell "B is up" from "the predecessor A is still
# running and B never applied." A pending record therefore never takes the healthy
# early return: for a running desire it reapplies through compose up (converging
# the project in place, never a down) and the post-up confirmation promotes it.


def _crash_pending_spec_change(rig: Rig, name: str = "svc") -> str:
    """Bring compose spec A up (applied), then desire spec B and simulate a real
    crash AFTER B's pending record persists but BEFORE compose up runs: the pass
    dies mid-``_reconcile`` (a BaseException the per-stack Exception handler does
    not catch), so no sweep runs and A stays running under a durable pending B
    record. Returns A's generation dir name. A's project (``ozolith-<name>``)
    keeps its original file list — B never reached Docker."""
    _push_compose(rig, _FILES_A, name)
    rig.daemon.once()
    a_files = list(rig.daemon._applied_compose[name].files)

    real_compose = rig.docker.compose

    def crash_before_up(project, files, verb):
        if verb == "up" and project == f"ozolith-{name}":
            raise KeyboardInterrupt  # process death before compose up
        real_compose(project, files, verb)

    rig.docker.compose = crash_before_up  # type: ignore[method-assign]
    _push_compose(rig, _FILES_B, name)
    with contextlib.suppress(KeyboardInterrupt):
        rig.daemon.once()
    rig.docker.compose = real_compose  # type: ignore[method-assign]
    return _generation_dir(a_files).name


def test_pending_spec_change_reapplies_the_new_generation_after_restart(rig: Rig):
    """A->B pending record recovered across a restart while A is still running:
    the daemon must compose up B's retained generation (converging the project in
    place, never treating A's running workload as B), promote to applied, and
    reclaim A's generation only after B succeeds — never showing two forms."""
    a_gen = _crash_pending_spec_change(rig)

    _restart_daemon(rig)  # fresh daemon: the pending B record reloads from disk
    record = rig.daemon._applied_compose["svc"]
    assert record.state == "pending"
    b_gen = _generation_dir(record.files).name
    assert b_gen != a_gen
    # A is still the running project and BOTH generations survived the crash: A's
    # obsolete generation is not reclaimed until B succeeds.
    assert rig.daemon._compose_running("svc")  # a running row — but it is A's
    assert _generation_dirs(rig, "svc") == {a_gen, b_gen}
    assert _active_forms(rig, "svc") == {"compose"}
    ups_before = sum(1 for c in rig.docker.compose_calls if c[2] == "up")

    _push_compose(rig, _FILES_B)
    rig.daemon.once()

    # compose up ran with B's retained generation despite a running row, the record
    # is applied for B, A's generation is reclaimed, and only one form was seen.
    ups = [c for c in rig.docker.compose_calls if c[2] == "up"]
    assert len(ups) == ups_before + 1
    assert [Path(f).parent.parent.name for f in ups[-1][1]] == [b_gen]  # B's generation
    assert rig.daemon._applied_compose["svc"].state == "applied"
    assert _generation_dirs(rig, "svc") == {b_gen}  # A reclaimed only after B's up
    assert _active_forms(rig, "svc") == {"compose"}

    # A later healthy pass performs no additional up.
    _push_compose(rig, _FILES_B)
    rig.daemon.once()
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before + 1
    assert _active_forms(rig, "svc") == {"compose"}


def test_pending_record_over_a_running_project_reapplies_and_confirms(rig: Rig):
    """Post-up confirmation-failure case: disk carries a pending B while B is
    already running. A restart reapplies B through compose up (safe/idempotent)
    and converges to applied; a later pass is stable with no further up."""
    _bring_up_compose_crash_before_confirm(rig)  # pending record, project running
    _restart_daemon(rig)
    assert rig.daemon._applied_compose["svc"].state == "pending"
    ups_before = sum(1 for c in rig.docker.compose_calls if c[2] == "up")

    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()

    # A pending record never claims health: it reapplies even though compose_ps
    # reports running, and the safe re-up promotes it to applied.
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before + 1
    assert rig.daemon._applied_compose["svc"].state == "applied"
    assert _active_forms(rig, "svc") == {"compose"}

    # Stable thereafter: confirmed-applied and running → no further up.
    rig.control.heartbeat_answers.append(heartbeat_response([_compose_stack("svc")]))
    rig.daemon.once()
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups_before + 1
    assert _active_forms(rig, "svc") == {"compose"}


# -- generation cleanup has a real retry path (ADR-0044) ---------------------------
#
# `_gc_compose_generations` isolates rmtree failures, but a failed deletion must
# not be forgotten: a healthy Stack early-returns before any per-transition GC,
# and a downed/removed/transitioned Stack no longer has a record to trigger one.
# `_sweep_compose_generations` runs every pass, keyed off the on-disk stacks/ tree
# and every current applied/pending record, so it retries failed deletions and
# discovers orphans with no surviving project or record — never a referenced one.


def _fail_rmtree_for(monkeypatch, *gen_names: str) -> set[str]:
    """Make ``shutil.rmtree`` raise for generation dirs whose leaf name is in the
    returned (mutable) set, delegating to the real rmtree otherwise. Clear or
    discard from the set to let a later deletion succeed."""
    from theozolith_nodedaemon import daemon as daemon_mod

    failing = set(gen_names)
    real = daemon_mod.shutil.rmtree

    def fake_rmtree(path, *a, **k):
        if Path(path).name in failing:
            raise OSError(f"rmtree blocked for {path}")
        return real(path, *a, **k)

    monkeypatch.setattr(daemon_mod.shutil, "rmtree", fake_rmtree)
    return failing


def test_failed_generation_deletion_is_retried_on_a_later_pass(rig: Rig, monkeypatch):
    """Replace A with B while A's generation deletion fails: B stays healthy and
    A's dir lingers. A later unchanged-B pass — which early-returns healthy, past
    any per-transition GC — retries the deletion and removes A with no new up."""
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    a_gen = _generation_dir(rig.daemon._applied_compose["svc"].files).name
    failing = _fail_rmtree_for(monkeypatch, a_gen)

    _push_compose(rig, _FILES_B)
    rig.daemon.once()
    b_gen = _generation_dir(rig.daemon._applied_compose["svc"].files).name
    assert rig.daemon._applied_compose["svc"].state == "applied"  # B healthy
    assert _active_forms(rig, "svc") == {"compose"}
    assert _generation_dirs(rig, "svc") == {a_gen, b_gen}  # A's dir survived the failure
    assert any("reclaiming obsolete compose generation" in line for line in rig.logs)
    assert any("cleanup failed" in line for line in rig.logs)

    failing.clear()  # deletion recovers
    ups = sum(1 for c in rig.docker.compose_calls if c[2] == "up")
    _push_compose(rig, _FILES_B)
    rig.daemon.once()
    assert _generation_dirs(rig, "svc") == {b_gen}  # A reclaimed on the retry pass
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups  # no new up


def test_removed_stack_orphan_generation_is_retried_without_a_record(rig: Rig, monkeypatch):
    """Compose a Stack down (removed from desired state) while its generation
    deletion fails: no record remains, yet a later pass still discovers and
    retries the orphaned generation once deletion succeeds — cleanup does not
    require a surviving project or AppliedCompose record."""
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    a_gen = _generation_dir(rig.daemon._applied_compose["svc"].files).name
    failing = _fail_rmtree_for(monkeypatch, a_gen)

    rig.control.heartbeat_answers.append(heartbeat_response([]))  # svc absent from desired
    rig.daemon.once()
    assert "svc" not in rig.daemon._applied_compose  # record dropped by the down
    assert "ozolith-svc" not in rig.docker.compose_projects
    assert _generation_dirs(rig, "svc") == {a_gen}  # orphaned; deletion failed

    failing.clear()
    rig.control.heartbeat_answers.append(heartbeat_response([]))  # still absent, still no record
    rig.daemon.once()
    assert _generation_dirs(rig, "svc") == set()  # orphan reclaimed with no record present


def test_compose_to_process_orphan_generation_is_retried(rig: Rig, monkeypatch):
    """compose -> process transition while the old generation deletion fails: the
    process form runs, no compose record remains, and a later pass retries and
    reclaims the orphan without disturbing the running child."""
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    a_gen = _generation_dir(rig.daemon._applied_compose["svc"].files).name
    blocked = _fail_rmtree_for(monkeypatch, a_gen)

    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("svc")]))
    rig.daemon.once()
    assert _active_forms(rig, "svc") == {"process"}
    assert "svc" not in rig.daemon._applied_compose
    assert _generation_dirs(rig, "svc") == {a_gen}  # orphaned after the transition

    blocked.clear()  # deletion recovers
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("svc")]))
    rig.daemon.once()
    assert _generation_dirs(rig, "svc") == set()  # orphan reclaimed on retry
    assert _active_forms(rig, "svc") == {"process"}  # process undisturbed


def test_sweep_keeps_a_pending_records_generation(rig: Rig):
    """A pending record's retained candidate generation is never swept as an
    orphan: it is referenced by the pending record until the up succeeds."""
    rig.docker.fail_compose_up.add("ozolith-svc")
    _push_compose(rig, _FILES_A)
    rig.daemon.once()  # up fails: a pending record is retained
    assert rig.daemon._applied_compose["svc"].state == "pending"
    a_gen = _generation_dir(rig.daemon._applied_compose["svc"].files).name
    assert _generation_dirs(rig, "svc") == {a_gen}  # sweep kept the pending candidate

    # Another failing pass: the pending generation is still referenced, not swept.
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    assert _generation_dirs(rig, "svc") == {a_gen}

    # Recovery: the up succeeds, the record promotes, its generation is retained.
    rig.docker.fail_compose_up.discard("ozolith-svc")
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    assert rig.daemon._applied_compose["svc"].state == "applied"
    assert _generation_dirs(rig, "svc") == {a_gen}


def test_sweep_never_deletes_referenced_generation_while_others_reconcile(rig: Rig, monkeypatch):
    """While an obsolete generation's deletion keeps failing across passes, the
    referenced generation is never touched, unrelated Stacks keep reconciling, and
    the failed candidate stays retryable — never permanently unreachable state."""
    _push_compose(rig, _FILES_A)
    rig.daemon.once()
    a_gen = _generation_dir(rig.daemon._applied_compose["svc"].files).name
    failing = _fail_rmtree_for(monkeypatch, a_gen)
    _push_compose(rig, _FILES_B)
    rig.daemon.once()
    b_gen = _generation_dir(rig.daemon._applied_compose["svc"].files).name

    for _ in range(3):
        rig.control.heartbeat_answers.append(
            heartbeat_response([_compose_stack("svc", files=_FILES_B), process_stack("worker")])
        )
        rig.daemon.once()
        assert b_gen in _generation_dirs(rig, "svc")  # referenced generation retained
        assert rig.daemon._supervisor.alive("worker")  # unrelated Stack reconciles
    assert _generation_dirs(rig, "svc") == {a_gen, b_gen}  # candidate still tracked, retryable

    failing.clear()  # not permanently unreachable: it is reclaimed once deletion works
    _push_compose(rig, _FILES_B)
    rig.daemon.once()
    assert _generation_dirs(rig, "svc") == {b_gen}


# -- a pending transition retains every predecessor generation (ADR-0044) ----------
#
# For compose A -> B the pending B record REPLACES A's applied record before
# compose up runs, so no surviving record references A's generation — yet A is
# still the running project if the up fails (or the daemon dies first). The
# sweep therefore deletes NOTHING under a Stack with a pending record:
# predecessor generations are reclaimed only after the successor is confirmed
# applied, or the project is composed down and the record cleared.


def _fail_up_to_pending_b(rig: Rig, name: str = "svc") -> tuple[list[str], str, str]:
    """Bring spec A up (confirmed applied), then desire spec B with compose up
    failing: leaves a durable pending B record while A remains the running
    project. Returns (A's file list, A's generation name, B's generation name)."""
    _push_compose(rig, _FILES_A, name)
    rig.daemon.once()
    a_files = list(rig.daemon._applied_compose[name].files)
    assert rig.daemon._applied_compose[name].state == "applied"

    rig.docker.fail_compose_up.add(f"ozolith-{name}")
    _push_compose(rig, _FILES_B, name)
    rig.daemon.once()
    b_gen = _generation_dir(rig.daemon._applied_compose[name].files).name
    return a_files, _generation_dir(a_files).name, b_gen


def test_failed_up_of_successor_retains_predecessor_generation(rig: Rig):
    """A -> B where compose up B fails: the pending B record, B's candidate
    generation, AND A's predecessor generation all survive the sweep, with A
    still the running project. The retried up brings B up and promotes the
    record; only after that success is A's generation reclaimed; a later
    healthy pass performs no additional up."""
    a_files, a_gen, b_gen = _fail_up_to_pending_b(rig)

    assert rig.daemon._applied_compose["svc"].state == "pending"
    assert _generation_dirs(rig, "svc") == {a_gen, b_gen}  # both intact after the sweep
    assert rig.docker.compose_projects["ozolith-svc"] == a_files  # A remains running
    assert _active_forms(rig, "svc") == {"compose"}

    # Another failing pass changes nothing: still pending, both still retained.
    _push_compose(rig, _FILES_B)
    rig.daemon.once()
    assert _generation_dirs(rig, "svc") == {a_gen, b_gen}
    assert rig.docker.compose_projects["ozolith-svc"] == a_files

    # The up recovers: B applies, and only NOW is A's generation reclaimed.
    rig.docker.fail_compose_up.discard("ozolith-svc")
    _push_compose(rig, _FILES_B)
    rig.daemon.once()
    record = rig.daemon._applied_compose["svc"]
    assert record.state == "applied"
    assert _generation_dir(record.files).name == b_gen
    assert _generation_dirs(rig, "svc") == {b_gen}  # A reclaimed after B converged
    assert _active_forms(rig, "svc") == {"compose"}

    # A later healthy pass is stable: no additional up, nothing re-reclaimed.
    ups = sum(1 for c in rig.docker.compose_calls if c[2] == "up")
    _push_compose(rig, _FILES_B)
    rig.daemon.once()
    assert sum(1 for c in rig.docker.compose_calls if c[2] == "up") == ups
    assert _generation_dirs(rig, "svc") == {b_gen}


def test_predecessor_reclaim_failure_after_success_is_retried(rig: Rig, monkeypatch):
    """After the retried up succeeds, a failing deletion of A's now-obsolete
    generation is retried by a later pass — never forgotten, never blocking the
    healthy Stack."""
    _, a_gen, b_gen = _fail_up_to_pending_b(rig)
    failing = _fail_rmtree_for(monkeypatch, a_gen)

    rig.docker.fail_compose_up.discard("ozolith-svc")
    _push_compose(rig, _FILES_B)
    rig.daemon.once()  # B applies; A's reclaim fails and its dir stays on disk
    assert rig.daemon._applied_compose["svc"].state == "applied"
    assert _generation_dirs(rig, "svc") == {a_gen, b_gen}
    assert any("cleanup failed" in line for line in rig.logs)

    failing.clear()
    _push_compose(rig, _FILES_B)
    rig.daemon.once()  # the per-pass sweep retries and reclaims A
    assert _generation_dirs(rig, "svc") == {b_gen}
    assert _active_forms(rig, "svc") == {"compose"}


# -- legacy name-addressed records: actual file paths are the references -----------
#
# A pre-generation daemon materialized compose files at NAME-addressed paths —
# <state>/stacks/<name>/compose/base.yml — and its persisted records point there,
# with no ``state`` field (loads as applied). The sweep protects what a record's
# files ACTUALLY reference, never a generation inferred from the fingerprint;
# otherwise it would delete teardown context a valid record still needs.

_LEGACY_FILES = [
    {"name": "compose/base.yml", "content": "services: {a: {}}\n"},
    {"name": "overlays/net.yml", "content": "services: {}\n"},
]


def _seed_legacy_record(rig: Rig, files=None, name: str = "svc") -> list[str]:
    """Persist a pre-write-ahead applied record whose files live at legacy
    name-addressed paths (no generation dir, no ``state`` field), materialize
    those files, and leave the corresponding compose project running — exactly
    what an upgraded daemon inherits from its predecessor. The fingerprint
    matches the desired spec so unchanged passes take the healthy early return.
    Restarts the rig's daemon so it loads the record; returns the legacy paths."""
    files = files if files is not None else _LEGACY_FILES
    paths = []
    for entry in files:
        target = rig.config.state_dir / "stacks" / name / entry["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["content"], encoding="utf-8")
        paths.append(str(target))
    stack = WireStack.from_wire(_compose_stack(name, files=files))
    record = {"fingerprint": rig.daemon._compose_fingerprint(stack), "files": paths}
    rig.config.applied_compose_path.parent.mkdir(parents=True, exist_ok=True)
    rig.config.applied_compose_path.write_text(json.dumps({name: record}))
    rig.docker.compose_projects[f"ozolith-{name}"] = paths
    _restart_daemon(rig)
    return paths


def test_legacy_record_files_survive_unchanged_passes_and_restart(rig: Rig):
    """Multiple unchanged passes over a legacy name-addressed record (files under
    two top-level legacy directories): the healthy early return holds — no up —
    and no referenced legacy file is ever deleted, including across a restart."""
    paths = _seed_legacy_record(rig)
    record = rig.daemon._applied_compose["svc"]
    assert record.state == "applied"  # a missing state field loads as applied
    assert record.files == paths

    for _ in range(3):
        _push_compose(rig, _LEGACY_FILES)
        rig.daemon.once()
        assert all(Path(p).is_file() for p in paths)  # referenced: never deleted
        assert _generation_dirs(rig, "svc") == {"compose", "overlays"}
    assert not any(c[2] == "up" for c in rig.docker.compose_calls)  # healthy early return
    assert _active_forms(rig, "svc") == {"compose"}

    _restart_daemon(rig)  # the record and its referenced paths survive a restart
    _push_compose(rig, _LEGACY_FILES)
    rig.daemon.once()
    assert all(Path(p).is_file() for p in paths)
    assert rig.daemon._applied_compose["svc"].files == paths
    assert not any(c[2] == "up" for c in rig.docker.compose_calls)


def test_legacy_record_transition_to_process_downs_with_existing_files(rig: Rig):
    """Legacy record -> desired process: compose down receives the legacy paths —
    still existing on disk at the moment of the call — BEFORE the process child
    spawns; the legacy dirs become unreferenced (recordless) and are reclaimed
    only after the down."""
    paths = _seed_legacy_record(rig)

    events, popen = _order_probe(rig)
    down_files_existed = []
    probed_compose = rig.docker.compose  # the _order_probe spy

    def existence_probe(project, files, verb):
        if verb == "down":
            down_files_existed.append(all(Path(f).is_file() for f in files))
        probed_compose(project, files, verb)

    rig.docker.compose = existence_probe  # type: ignore[method-assign]
    _restart_daemon(rig, popen=popen)
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("svc")]))
    rig.daemon.once()

    assert events == ["compose:down", "spawn"]  # down before the child spawns
    down = [c for c in rig.docker.compose_calls if c[2] == "down"][-1]
    assert down[1] == paths  # the still-existing legacy files, never an empty list
    assert down_files_existed == [True]
    assert _active_forms(rig, "svc") == {"process"}
    assert "svc" not in rig.daemon._applied_compose
    assert _generation_dirs(rig, "svc") == set()  # recordless: legacy dirs reclaimed


def test_legacy_record_survives_sweep_failures_and_restarts(rig: Rig, monkeypatch):
    """An orphan generation beside a legacy record, its deletion failing every
    pass: the sweep failure never spills over into the referenced legacy paths —
    across passes and a restart — and once deletion recovers the orphan is
    reclaimed with the legacy paths still intact."""
    paths = _seed_legacy_record(rig)
    orphan = rig.config.state_dir / "stacks" / "svc" / "0123456789abcdef"
    orphan.mkdir()
    (orphan / "stale.yml").write_text("services: {}\n")
    failing = _fail_rmtree_for(monkeypatch, orphan.name)

    for _ in range(2):
        _push_compose(rig, _LEGACY_FILES)
        rig.daemon.once()
        assert any("cleanup failed" in line for line in rig.logs)
        assert all(Path(p).is_file() for p in paths)  # referenced: never deleted

    _restart_daemon(rig)  # a restart changes nothing: still referenced, still kept
    _push_compose(rig, _LEGACY_FILES)
    rig.daemon.once()
    assert all(Path(p).is_file() for p in paths)

    failing.clear()
    _push_compose(rig, _LEGACY_FILES)
    rig.daemon.once()
    assert _generation_dirs(rig, "svc") == {"compose", "overlays"}  # orphan reclaimed
    assert all(Path(p).is_file() for p in paths)


# -- real DockerCtl.remove failure semantics (ADR-0044) ----------------------------


def test_dockerctl_remove_raises_on_a_genuine_failure():
    """A non-zero ``docker rm --force`` whose container genuinely survives raises
    DockerError — a suppressed failure would let a caller start a successor beside
    a still-running predecessor."""

    def runner(args, timeout=None):
        if args[1] == "rm":
            return subprocess.CompletedProcess(
                args, 1, "", "Error response from daemon: cannot remove container"
            )
        if args[1] == "ps":  # postcondition: the container is still present
            return subprocess.CompletedProcess(args, 0, "ozolith-stack-svc\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    with pytest.raises(DockerError):
        ctl.remove("ozolith-stack-svc")


def test_dockerctl_remove_absent_container_is_idempotent_success():
    """``no such container`` classifies as already-absent: idempotent success."""

    def runner(args, timeout=None):
        if args[1] == "rm":
            return subprocess.CompletedProcess(args, 1, "", "Error: No such container: ghost")
        raise AssertionError("no postcondition ps needed when classified absent")

    ctl = DockerCtl(runner=runner)
    ctl.remove("ghost")  # must not raise


def test_dockerctl_remove_postcondition_absence_is_success():
    """An opaque non-zero result whose container is nonetheless gone is treated as
    idempotent success via the postcondition existence check."""

    def runner(args, timeout=None):
        if args[1] == "rm":
            return subprocess.CompletedProcess(args, 1, "", "some transient noise")
        if args[1] == "ps":
            return subprocess.CompletedProcess(args, 0, "", "")  # not present
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    ctl.remove("ghost")  # postcondition confirms absence: no raise


def test_dockerctl_remove_success_is_a_no_op_raise():
    """A zero return code is plain success — no postcondition ps, no raise."""
    seen = []

    def runner(args, timeout=None):
        seen.append(args[1])
        return subprocess.CompletedProcess(args, 0, "", "")

    ctl = DockerCtl(runner=runner)
    ctl.remove("ozolith-stack-svc")
    assert seen == ["rm"]  # no postcondition check on success


# -- container -> process with a failed predecessor removal (ADR-0044) -------------


def test_container_to_process_with_failed_removal_gates_the_spawn(rig: Rig, tmp_path):
    """A failed predecessor container removal must not spawn the process child:
    the old container remains, the error is emitted, no two forms coexist, and the
    retry completes exactly once. Guards the 'matching child early-returns while a
    stale container remains' state."""
    jobs = tmp_path / "jobs"
    rig.control.heartbeat_answers.append(
        heartbeat_response([container_stack("worker", image="ghcr.io/x/w:1")])
    )
    rig.daemon.once()
    assert "ozolith-stack-worker" in rig.docker.stacks

    rig.docker.fail_remove.add("ozolith-stack-worker")
    proc = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(jobs)})
    rig.control.heartbeat_answers.append(heartbeat_response([proc]))
    rig.daemon.once()

    # Removal failed: no child, container still there, error surfaced, one form.
    assert not rig.daemon._supervisor.alive("worker")
    assert rig.popen.spawned == []
    assert "ozolith-stack-worker" in rig.docker.stacks
    assert _active_forms(rig, "worker") == {"single-image"}
    assert any(e["error_class"] == "DockerError" for e in rig.control.events)

    # Removal recovers: the retry removes the container, then spawns exactly once.
    rig.docker.fail_remove.discard("ozolith-stack-worker")
    rig.control.heartbeat_answers.append(heartbeat_response([proc]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker")
    assert "ozolith-stack-worker" not in rig.docker.stacks
    assert _active_forms(rig, "worker") == {"process"}
    assert len(rig.popen.spawned) == 1  # spawned exactly once

    # Stable thereafter: no early return leaves a stale form, no re-spawn.
    rig.control.heartbeat_answers.append(heartbeat_response([proc]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 1
    assert _active_forms(rig, "worker") == {"process"}


def test_converged_child_does_not_mask_a_stale_single_image_container(rig: Rig, tmp_path):
    """A live, converged process child beside a stale single-image container (a
    predecessor whose removal failed earlier) must NOT be skipped past by the
    early-return: the next pass reaps the stale container rather than declaring the
    successor converged and returning."""
    jobs = tmp_path / "jobs"
    _launch_driver(rig, jobs)
    assert rig.daemon._supervisor.alive("worker")

    # Inject a stale single-image container under the same name (as if an earlier
    # container->process removal had silently failed).
    rig.docker.stacks["ozolith-stack-worker"] = {
        "stack": "worker",
        "image": "stale:0",
        "state": "running",
    }
    assert _active_forms(rig, "worker") == {"process", "single-image"}  # the pathological state

    proc = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(jobs)})
    spawned_before = len(rig.popen.spawned)
    rig.control.heartbeat_answers.append(heartbeat_response([proc]))
    rig.daemon.once()

    # The stale container is reaped; the child is untouched (not restarted).
    assert "ozolith-stack-worker" not in rig.docker.stacks
    assert "ozolith-stack-worker" in rig.docker.removed
    assert len(rig.popen.spawned) == spawned_before  # child not churned
    assert _active_forms(rig, "worker") == {"process"}  # sole form


# -- config distribution (ADR-0042) ---------------------------------------------

from daemonrig import driver_stack, make_config_dist  # noqa: E402


def dist_response(stacks, drivers_hash, commands=None) -> dict:
    return {"commands": commands or [], "config": desired(stacks, drivers_hash=drivers_hash)}


def test_config_dist_fetches_verifies_and_swaps_atomically(rig: Rig):
    digest, data = make_config_dist(
        {"drivers/custom/impl.py": "def run():\n    return 1\n"}, built_against="0.2.9"
    )
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()

    root = rig.config.state_dir / "config-dist"
    assert (root / digest / "drivers" / "custom" / "impl.py").is_file()
    assert (root / "current").read_text().strip() == digest
    # The next status payload reports the applied hash and its stamp.
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    reported = rig.control.transcript[-1][2]
    assert reported["drivers_hash"] == digest
    assert reported["drivers_built_against"] == "0.2.9"


def test_config_dist_fetch_failure_retries_next_pass(rig: Rig):
    digest, data = make_config_dist({"drivers/custom/impl.py": "x = 1\n"})
    # Artifact NOT registered yet → 409 → ControlError, nothing swapped.
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert not (rig.config.state_dir / "config-dist" / "current").exists()
    assert any("config distribution" in line and "failed" in line for line in rig.logs)
    assert rig.control.events  # a theozolith.error was emitted
    # It becomes available; the next pass converges.
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert (rig.config.state_dir / "config-dist" / "current").read_text().strip() == digest


def test_config_dist_verify_mismatch_is_rejected(rig: Rig):
    digest, _ = make_config_dist({"drivers/custom/impl.py": "real = 1\n"})
    _, tampered = make_config_dist({"drivers/custom/impl.py": "TAMPERED = 2\n"})
    rig.control.config_artifacts[digest] = tampered  # served bytes recompute to a different hash
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    # Nothing swapped; old hash still reported ("").
    assert not (rig.config.state_dir / "config-dist" / digest).exists()
    assert not (rig.config.state_dir / "config-dist" / "current").exists()
    assert any("mismatch" in line for line in rig.logs)


def test_config_dist_defers_behind_an_in_flight_run(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    (jobs / "20260808T1200-worker-a-1").mkdir(parents=True)
    worker = process_stack(
        "worker", env={"THEOZOLITH_REPO": "acme/sandbox", "THEOZOLITH_JOBS_DIR": str(jobs)}
    )
    digest, data = make_config_dist({"drivers/custom/impl.py": "x = 1\n"})
    rig.control.config_artifacts[digest] = data
    # Pass 1: worker runs, no distribution yet.
    rig.control.heartbeat_answers.append(heartbeat_response([worker]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker")
    # Pass 2: a distribution is desired, but a Run is in flight → deferred.
    rig.control.heartbeat_answers.append(dist_response([worker], digest))
    rig.daemon.once()
    assert not (rig.config.state_dir / "config-dist" / "current").exists()
    assert any("config distribution" in line and "deferred" in line for line in rig.logs)
    # Run ends → the next pass converges.
    shutil.rmtree(jobs / "20260808T1200-worker-a-1")
    rig.control.heartbeat_answers.append(dist_response([worker], digest))
    rig.daemon.once()
    assert (rig.config.state_dir / "config-dist" / "current").read_text().strip() == digest


def test_config_dist_swap_restarts_only_drivers_stacks(rig: Rig):
    """On a swap the daemon stops config-distribution driver Stacks (argv[0] the
    launcher, argv[1] under drivers/) so reconcile restarts them; a builtin:*
    driver Stack and a generic process Stack are untouched (ADR-0042)."""
    custom = driver_stack("custom-impl", "drivers/custom")
    builtin = process_stack("impl")
    builtin["command"] = "theozolith-driver builtin:implementer"
    builtin["env"] = {"THEOZOLITH_REPO": "acme/sandbox", "THEOZOLITH_RUN_IMAGE": "theozolith/y:1"}
    generic = process_stack("plain")  # command "plain-driver --loop"
    stacks = [custom, builtin, generic]

    a_hash, a_data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    b_hash, b_data = make_config_dist({"drivers/custom/impl.py": "v = 2\n"})
    rig.control.config_artifacts[a_hash] = a_data
    rig.control.config_artifacts[b_hash] = b_data

    rig.control.heartbeat_answers.append(dist_response(stacks, a_hash))
    rig.daemon.once()
    started = {p.args[1]: p for p in rig.popen.spawned}  # driver ref / arg by command
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns_before = len(rig.popen.spawned)

    # Swap to B: the custom driver Stack is stopped then restarted; the others aren't.
    rig.control.heartbeat_answers.append(dist_response(stacks, b_hash))
    rig.daemon.once()
    assert (rig.config.state_dir / "config-dist" / "current").read_text().strip() == b_hash
    # The custom driver child was restarted exactly once (a new spawn).
    custom_spawns = [
        p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "drivers/custom"]
    ]
    assert len(custom_spawns) == 2
    # The builtin and generic children were never restarted.
    builtin_spawns = [
        p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "builtin:implementer"]
    ]
    generic_spawns = [p for p in rig.popen.spawned if p.args[0] == "plain-driver"]
    assert len(builtin_spawns) == 1 and len(generic_spawns) == 1
    assert started  # (silence unused)
    assert spawns_before < len(rig.popen.spawned)


def test_config_dist_empty_desired_retires(rig: Rig):
    """Retirement stops the drivers/* Stack AND the same pass's reconcile must
    not relaunch it: '' means no distribution exists, never converged, so the
    start gate demands a non-empty desired hash plus fresh verified equality —
    equality of two empty sentinels is not authorization (ADR-0042)."""
    custom = driver_stack("custom-impl", "drivers/custom")
    digest, data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert (rig.config.state_dir / "config-dist" / digest).is_dir()
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns_after_apply = len(rig.popen.spawned)

    # Desired distribution goes away entirely (the Stack stays desired): the
    # driver is stopped and stays stopped — reconcile spawns nothing.
    rig.control.heartbeat_answers.append(dist_response([custom], ""))
    rig.daemon.once()
    assert not (rig.config.state_dir / "config-dist" / "current").exists()
    assert not (rig.config.state_dir / "config-dist" / digest).exists()
    assert not rig.daemon._supervisor.alive("custom-impl")
    assert len(rig.popen.spawned) == spawns_after_apply

    # A further pass with the same empty desired hash: still stopped, no churn.
    rig.control.heartbeat_answers.append(dist_response([custom], ""))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("custom-impl")
    assert len(rig.popen.spawned) == spawns_after_apply


def test_config_dist_empty_hashes_never_start_custom_driver(rig: Rig):
    """A drivers/* Stack that has NEVER had a distribution does not start while
    both the current and desired hashes are empty; a builtin driver and a
    generic process are unaffected by the gate; a valid non-empty distribution
    then lets the custom driver start normally (ADR-0042)."""
    custom = driver_stack("custom-impl", "drivers/custom")
    builtin = process_stack("impl")
    builtin["command"] = "theozolith-driver builtin:implementer"
    builtin["env"] = {"THEOZOLITH_REPO": "acme/sandbox", "THEOZOLITH_RUN_IMAGE": "theozolith/y:1"}
    generic = process_stack("plain")  # command "plain-driver --loop"
    stacks = [custom, builtin, generic]

    rig.control.heartbeat_answers.append(dist_response(stacks, ""))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("custom-impl")
    assert rig.daemon._supervisor.alive("impl")
    assert rig.daemon._supervisor.alive("plain")
    assert not any(p.args[:2] == ["theozolith-driver", "drivers/custom"] for p in rig.popen.spawned)

    # A valid distribution converges: the custom driver now starts, once.
    digest, data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response(stacks, digest))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    custom_spawns = [
        p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "drivers/custom"]
    ]
    assert len(custom_spawns) == 1
    # The builtin and generic children were never restarted along the way.
    assert (
        len([p for p in rig.popen.spawned if p.args[1:2] == ["builtin:implementer"]]) == 1
        and len([p for p in rig.popen.spawned if p.args[0] == "plain-driver"]) == 1
    )


def test_config_dist_zip_traversal_refused(rig: Rig):
    import io
    import zipfile

    digest = "e" * 64
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("drivers/ok.py", "ok\n")
        archive.writestr("../escape.py", "pwned\n")
    rig.control.config_artifacts[digest] = buffer.getvalue()
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert not (rig.config.state_dir / "config-dist" / "current").exists()
    assert not (rig.config.state_dir.parent / "escape.py").exists()
    assert rig.control.events  # error emitted


# -- evidence-based applied-state reporting (ADR-0042) -----------------------


def _last_heartbeat(rig: Rig) -> dict:
    """The most recent status payload the daemon POSTed (the report is sent at
    the START of a pass, before convergence acts)."""
    return [req for _, path, req, _ in rig.control.transcript if path == "/heartbeats"][-1]


def _apply_dist(rig: Rig, files: dict[str, str], *, built_against: str = "0.3.0") -> str:
    digest, data = make_config_dist(files, built_against=built_against)
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert (rig.config.state_dir / "config-dist" / "current").read_text().strip() == digest
    return digest


def test_config_dist_pointer_without_tree_is_not_converged_then_repaired(rig: Rig):
    """Partial restore: the ``current`` pointer survived but its tree did not.
    The pointer alone is not proof — the node reports non-convergence and the
    next fetch repairs it (ADR-0042)."""
    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    root = rig.config.state_dir / "config-dist"
    shutil.rmtree(root / digest)  # only the pointer remains
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    # Reported non-converged at status time, then repaired this same pass.
    assert _last_heartbeat(rig)["drivers_hash"] == ""
    assert (root / digest / "drivers" / "custom" / "impl.py").is_file()
    assert (root / "current").read_text().strip() == digest


def test_config_dist_modified_tree_is_not_converged_then_repaired(rig: Rig):
    """A runtime-mutated applied tree recomputes to a different hash, so it is
    reported non-converged and the next fetch restores the verified content."""
    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    tree = rig.config.state_dir / "config-dist" / digest
    (tree / "drivers" / "custom" / "impl.py").write_text("MUTATED = 9\n", encoding="utf-8")
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""  # recompute mismatch
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "x = 1\n"  # repaired
    assert any("recomputes to" in line for line in rig.logs)


def test_config_dist_malformed_pointer_is_not_converged(rig: Rig):
    """A malformed ``current`` value never reads as an applied hash."""
    root = rig.config.state_dir / "config-dist"
    root.mkdir(parents=True, exist_ok=True)
    (root / "current").write_text("not-a-valid-hash\n", encoding="utf-8")
    rig.control.heartbeat_answers.append(dist_response([], ""))  # nothing desired
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""
    assert any("malformed" in line for line in rig.logs)


def test_config_dist_foreign_metadata_is_not_converged_then_repaired(rig: Rig):
    """A stale/foreign ``config-dist.json`` (wrong format, wrong hash) is more
    than an untrusted stamp: convergence proves the COMPLETE applied artifact —
    recomputed content PLUS a valid matching metadata envelope — so a foreign
    envelope reads as non-converged, never reports a wrong stamp, and the next
    fetch repairs the whole tree (ADR-0042 amendment)."""
    import json

    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"}, built_against="0.2.9")
    meta_path = rig.config.state_dir / "config-dist" / digest / "config-dist.json"
    meta_path.write_text(
        json.dumps({"format": 999, "drivers_hash": "z" * 64, "built_against": "9.9.9"}),
        encoding="utf-8",
    )
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    reported = _last_heartbeat(rig)
    assert reported["drivers_hash"] == ""  # the envelope is part of the proof
    assert reported["drivers_built_against"] == ""  # never a wrong/foreign stamp
    assert any("metadata failed verification" in line for line in rig.logs)
    # That same pass repaired the tree; the next heartbeat reports the truth.
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    reported = _last_heartbeat(rig)
    assert reported["drivers_hash"] == digest
    assert reported["drivers_built_against"] == "0.2.9"


def test_config_dist_old_distribution_stays_until_replacement_verified(rig: Rig):
    """A swap whose served artifact fails verification leaves the old, verified
    distribution in place and still usable (ADR-0042)."""
    a_hash = _apply_dist(rig, {"drivers/custom/impl.py": "v = 1\n"})
    b_hash, _good = make_config_dist({"drivers/custom/impl.py": "v = 2\n"})
    _, tampered = make_config_dist({"drivers/custom/impl.py": "TAMPERED = 3\n"})
    rig.control.config_artifacts[b_hash] = tampered  # bytes recompute to a different hash
    rig.control.heartbeat_answers.append(dist_response([], b_hash))
    rig.daemon.once()
    root = rig.config.state_dir / "config-dist"
    assert (root / "current").read_text().strip() == a_hash  # old still current
    assert (root / a_hash / "drivers" / "custom" / "impl.py").read_text() == "v = 1\n"
    assert not (root / b_hash).exists()
    assert _last_heartbeat(rig)["drivers_hash"] == a_hash  # still reports the old good hash


# -- fail-closed applied-tree verification (ADR-0042 amendment) ---------------


def test_config_dist_symlink_in_applied_tree_is_not_converged_then_repaired(rig: Rig):
    """A symlink planted in the applied tree after extraction FAILS verification
    closed — it is never silently skipped as unhashed-but-present content — so
    the node reports non-convergence and the next fetch repairs the tree."""
    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    tree = rig.config.state_dir / "config-dist" / digest
    outside = rig.config.state_dir / "outside.py"
    outside.write_text("evil = 1\n", encoding="utf-8")
    (tree / "drivers" / "custom" / "planted.py").symlink_to(outside)
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""  # fail closed, never skipped
    assert not (tree / "drivers" / "custom" / "planted.py").is_symlink()  # repaired away
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "x = 1\n"
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


def test_config_dist_fifo_in_applied_tree_is_not_converged_then_repaired(rig: Rig):
    """An irregular entry (FIFO/socket/device) below the applied drivers root
    fails verification closed and triggers repair."""
    import os

    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    tree = rig.config.state_dir / "config-dist" / digest
    os.mkfifo(tree / "drivers" / "custom" / "pipe")
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""
    assert not (tree / "drivers" / "custom" / "pipe").exists()  # repaired away
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


def test_config_dist_rogue_applied_root_entry_is_not_converged_then_repaired(rig: Rig):
    """A rogue sibling at the applied ROOT (outside drivers/, so outside the
    manifest) would be unhashed-but-present content riding a converged report —
    the shape check refuses it and the next fetch repairs the tree."""
    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    tree = rig.config.state_dir / "config-dist" / digest
    (tree / "rogue.py").write_text("evil = 1\n", encoding="utf-8")
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""
    assert any("unexpected" in line for line in rig.logs)
    assert not (tree / "rogue.py").exists()  # repaired away
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


def test_config_dist_symlinked_applied_drivers_root_is_not_converged_then_repaired(rig: Rig):
    """A drivers root replaced by a symlink — even one pointing at the original
    content — is refused: a pointer is never integrity evidence (ADR-0042)."""
    import os

    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    tree = rig.config.state_dir / "config-dist" / digest
    hidden = rig.config.state_dir / "hidden-drivers"
    os.rename(tree / "drivers", hidden)
    (tree / "drivers").symlink_to(hidden)
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""
    assert not (tree / "drivers").is_symlink()  # repaired to a real directory
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


def test_config_dist_unenumerable_applied_subtree_is_not_converged(rig: Rig, monkeypatch):
    """A failure to enumerate an included subtree of the APPLIED tree reads as
    non-converged (fail closed) — never a silently truncated recompute that
    still reports the hash. Injected via os.scandir, so the test holds under
    root (chmod would not)."""
    import os

    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    locked = rig.config.state_dir / "config-dist" / digest / "drivers" / "custom"
    real_scandir = os.scandir

    def boom(path):
        if Path(path) == locked:
            raise OSError(13, "injected: cannot list applied subtree")
        return real_scandir(path)

    monkeypatch.setattr("theozolith_nodedaemon.configdist.os.scandir", boom)
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""
    assert any("failed verification" in line for line in rig.logs)


# -- applied-tree structural validation (ADR-0042 amendment) ------------------
#
# Source exclusion and applied-tree validation are DIFFERENT LAYERS: control's
# source selection skips excluded names (a working repo holds them
# legitimately), but an accepted artifact can never contain one — so an
# excluded-name entry found in an APPLIED tree is a planted foreign entry and
# must fail verification, never be silently skipped around.


def test_config_dist_planted_pyc_in_applied_tree_is_not_converged_then_repaired(rig: Rig):
    """An importable ``*.pyc`` planted under the applied drivers/ would ride
    ``sys.path`` without ever entering the hash. It fails structural validation
    (never skipped as a source-excluded name), the heartbeat reports
    non-convergence, and repair removes it."""
    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    tree = rig.config.state_dir / "config-dist" / digest
    (tree / "drivers" / "custom" / "evil.pyc").write_bytes(b"\x00evil")
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""  # fail closed, never skipped
    assert not (tree / "drivers" / "custom" / "evil.pyc").exists()  # repaired away
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "x = 1\n"
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


@pytest.mark.parametrize("plant", ["pycache_pyc", "dot_file", "dot_dir", "dot_symlink", "dot_fifo"])
def test_config_dist_excluded_named_entry_in_applied_tree_is_not_converged_then_repaired(
    rig: Rig, plant
):
    """Every entry whose NAME an accepted artifact can never contain — a
    ``__pycache__``d ``*.pyc``, a dot-prefixed regular file, directory,
    symlink, or FIFO — fails applied-tree validation instead of being skipped,
    and repair removes it."""
    import os

    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    custom = rig.config.state_dir / "config-dist" / digest / "drivers" / "custom"
    if plant == "pycache_pyc":
        planted = custom / "__pycache__" / "evil.pyc"
        planted.parent.mkdir()
        planted.write_bytes(b"\x00evil")
    elif plant == "dot_file":
        planted = custom / ".hidden.py"
        planted.write_text("evil = 1\n", encoding="utf-8")
    elif plant == "dot_dir":
        planted = custom / ".hidden"
        planted.mkdir()
        (planted / "evil.py").write_text("evil = 1\n", encoding="utf-8")
    elif plant == "dot_symlink":
        planted = custom / ".editor-swap"
        planted.symlink_to(rig.config.state_dir / "outside-nowhere")
    else:
        planted = custom / ".pipe"
        os.mkfifo(planted)
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""  # fail closed, never skipped
    assert not planted.exists() and not planted.is_symlink()  # repaired away
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


@pytest.mark.parametrize("name", [".hidden.py", ".hidden-dir", ".pipe", "evil.pyc"])
def test_config_dist_excluded_named_applied_root_sibling_fails_shape_then_repairs(rig: Rig, name):
    """The applied root permits ONLY a real drivers/ directory and a regular
    config-dist.json — the source-tree exclusion predicate does not apply to
    root siblings, so dot-prefixed files/directories, irregular hidden entries,
    and ``*.pyc`` siblings all fail structural validation and are repaired."""
    import os

    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"})
    tree = rig.config.state_dir / "config-dist" / digest
    planted = tree / name
    if name == ".hidden-dir":
        planted.mkdir()
    elif name == ".pipe":
        os.mkfifo(planted)
    else:
        planted.write_text("evil = 1\n", encoding="utf-8")
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""
    assert any("unexpected" in line for line in rig.logs)
    assert not planted.exists()  # repaired away with the whole exchanged tree
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


# -- lifecycle-safe same-hash repair (ADR-0042 amendment) ---------------------


def test_config_dist_same_hash_repair_stops_live_driver_before_exchange(rig: Rig, monkeypatch):
    """Repair of a mutated tree while its custom driver is ALIVE: the child is
    stopped only after the replacement fully verified and BEFORE the active
    path is exchanged (never removed beneath a running process), the failing
    tree is renamed aside rather than deleted first, and the same pass's
    reconcile restarts the driver on the repaired tree."""
    custom = driver_stack("custom-impl", "drivers/custom")
    digest, data = make_config_dist({"drivers/custom/impl.py": "x = 1\n"})
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    tree = rig.config.state_dir / "config-dist" / digest
    (tree / "drivers" / "custom" / "impl.py").write_text("MUTATED = 9\n", encoding="utf-8")

    alive_at_exchange: list[bool] = []
    real_exchange = rig.daemon._exchange_config_dist_tree

    def observing(staging, final):
        alive_at_exchange.append(rig.daemon._supervisor.alive("custom-impl"))
        assert final.is_dir()  # the failing tree is still fully present here
        real_exchange(staging, final)

    monkeypatch.setattr(rig.daemon, "_exchange_config_dist_tree", observing)
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    # Honest report at status time; stopped before the exchange; repaired;
    # restarted by the same pass's reconcile.
    assert _last_heartbeat(rig)["drivers_hash"] == ""
    assert alive_at_exchange == [False]
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "x = 1\n"
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns = [p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "drivers/custom"]]
    assert len(spawns) == 2  # restarted exactly once
    # Only the COMPLETE transition is reported on the next heartbeat.
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


def test_config_dist_same_hash_repair_defers_behind_inflight_run(rig: Rig, tmp_path):
    """Repair queues behind an active Run exactly like a new distribution: the
    node keeps reporting non-convergence, but the live child and its tree are
    untouched — no active Run is ever killed to repair a distribution."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    custom = driver_stack(
        "custom-impl",
        "drivers/custom",
        env={
            "THEOZOLITH_REPO": "acme/sandbox",
            "THEOZOLITH_RUN_IMAGE": "theozolith/x:1",
            "THEOZOLITH_JOBS_DIR": str(jobs),
        },
    )
    digest, data = make_config_dist({"drivers/custom/impl.py": "x = 1\n"})
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    tree = rig.config.state_dir / "config-dist" / digest
    (tree / "drivers" / "custom" / "impl.py").write_text("MUTATED = 9\n", encoding="utf-8")
    (jobs / "20260810T0900-custom-impl-1").mkdir()
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""  # honest while deferred
    assert rig.daemon._supervisor.alive("custom-impl")  # the Run was not killed
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "MUTATED = 9\n"
    assert any("deferred" in line for line in rig.logs)
    # The Run ends; the next pass repairs and restarts the driver.
    shutil.rmtree(jobs / "20260810T0900-custom-impl-1")
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "x = 1\n"
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


def test_config_dist_exchange_failure_is_honest_and_retried(rig: Rig):
    """An injected failure BEFORE the failing tree is renamed aside leaves
    retryable state: the tree is still the known-mutated original, the stopped
    driver is NOT restarted by reconciliation (never against known-invalid
    state), the desired hash is NEVER memoized, the next heartbeat reports the
    evidence-based value, and the next pass repairs honestly and restarts the
    driver exactly once."""
    custom = driver_stack("custom-impl", "drivers/custom")
    digest, data = make_config_dist({"drivers/custom/impl.py": "x = 1\n"})
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    tree = rig.config.state_dir / "config-dist" / digest
    (tree / "drivers" / "custom" / "impl.py").write_text("MUTATED = 9\n", encoding="utf-8")
    calls: list[str] = []

    def failing(staging, final):
        calls.append("boom")
        raise OSError(5, "injected: exchange failed")

    rig.daemon._exchange_config_dist_tree = failing
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert calls == ["boom"]
    assert any("config distribution" in line and "failed" in line for line in rig.logs)
    assert rig.control.events  # a theozolith.error was emitted
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "MUTATED = 9\n"
    # Supervisor state, not just files: the driver was stopped for the
    # replacement and the same pass's reconcile must NOT have restarted it
    # against the known-mutated tree.
    assert not rig.daemon._supervisor.alive("custom-impl")
    assert any("deferred" in line and "not converged" in line for line in rig.logs)
    del rig.daemon._exchange_config_dist_tree  # restore the real method
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == ""  # never memoized on failure
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "x = 1\n"  # repaired
    # The retry pass repaired, published, and restarted the driver exactly once.
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns = [p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "drivers/custom"]]
    assert len(spawns) == 2  # the initial start + exactly one post-repair restart
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


def test_config_dist_pointer_failure_keeps_reporting_old_hash_then_converges(rig: Rig):
    """An injected failure during pointer publication (new-hash path): the old
    pointer and old tree stay authoritative, the heartbeat never reports the
    new hash before the COMPLETE transition, and the next pass converges."""
    a_hash = _apply_dist(rig, {"drivers/custom/impl.py": "v = 1\n"})
    b_hash, b_data = make_config_dist({"drivers/custom/impl.py": "v = 2\n"})
    rig.control.config_artifacts[b_hash] = b_data

    def failing(drivers_hash):
        raise OSError(5, "injected: pointer publication failed")

    rig.daemon._write_current_pointer = failing
    rig.control.heartbeat_answers.append(dist_response([], b_hash))
    rig.daemon.once()
    root = rig.config.state_dir / "config-dist"
    assert (root / "current").read_text().strip() == a_hash  # pointer untouched
    assert (root / a_hash / "drivers" / "custom" / "impl.py").read_text() == "v = 1\n"
    assert rig.control.events  # a theozolith.error was emitted
    del rig.daemon._write_current_pointer  # restore the real method
    rig.control.heartbeat_answers.append(dist_response([], b_hash))
    rig.daemon.once()
    # The heartbeat sent at the start of THAT pass still reported A — the new
    # hash is only ever reported after tree AND pointer both transitioned.
    assert _last_heartbeat(rig)["drivers_hash"] == a_hash
    assert (root / "current").read_text().strip() == b_hash
    rig.control.heartbeat_answers.append(dist_response([], b_hash))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == b_hash


def test_config_dist_crash_between_renames_keeps_driver_stopped_then_repairs(rig: Rig):
    """Injected failure AFTER the failing tree is renamed aside but BEFORE the
    verified staging is renamed in: the active path is absent, so the stopped
    driver must NOT restart (the pointer names a missing tree — never inferred
    safe from its contents); the next pass repairs and restarts it exactly
    once."""
    import os

    custom = driver_stack("custom-impl", "drivers/custom")
    digest, data = make_config_dist({"drivers/custom/impl.py": "x = 1\n"})
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    tree = rig.config.state_dir / "config-dist" / digest
    (tree / "drivers" / "custom" / "impl.py").write_text("MUTATED = 9\n", encoding="utf-8")

    def crashed_between_renames(staging, final):
        retired = final.parent / f".{final.name}.retired"
        os.replace(final, retired)
        raise OSError(5, "injected: crashed between the renames")

    rig.daemon._exchange_config_dist_tree = crashed_between_renames
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert not tree.exists()  # the active path is absent mid-transition
    assert not rig.daemon._supervisor.alive("custom-impl")  # stopped, NOT restarted
    del rig.daemon._exchange_config_dist_tree  # restore the real method
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "x = 1\n"  # repaired
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns = [p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "drivers/custom"]]
    assert len(spawns) == 2  # the initial start + exactly one post-repair restart
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


def test_config_dist_pointer_failure_after_repair_allows_freshly_verified_restart(rig: Rig):
    """Injected failure AFTER the verified staging is renamed in but BEFORE
    pointer publication, on a same-hash repair: reporting stays based on the
    old pointer — which here selects the freshly exchanged tree, and that tree
    independently verifies to the desired hash, so the driver may restart. The
    decision comes from re-verification of the pointer-selected tree, never
    from the directory name or the pointer value alone."""
    custom = driver_stack("custom-impl", "drivers/custom")
    digest, data = make_config_dist({"drivers/custom/impl.py": "x = 1\n"})
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    tree = rig.config.state_dir / "config-dist" / digest
    (tree / "drivers" / "custom" / "impl.py").write_text("MUTATED = 9\n", encoding="utf-8")

    def failing(drivers_hash):
        raise OSError(5, "injected: pointer publication failed")

    rig.daemon._write_current_pointer = failing
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    # The heartbeat at the start of the failing pass was honest ("").
    assert _last_heartbeat(rig)["drivers_hash"] == ""
    # The tree was repaired; the untouched pointer selects it; it verifies to
    # the desired hash — so the driver restarted, exactly once.
    assert (tree / "drivers" / "custom" / "impl.py").read_text() == "x = 1\n"
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns = [p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "drivers/custom"]]
    assert len(spawns) == 2
    del rig.daemon._write_current_pointer  # restore the real method
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == digest


def test_config_dist_pointer_failure_on_new_hash_keeps_driver_stopped(rig: Rig):
    """Injected pointer-publication failure on a NEW-hash swap: the old pointer
    stays authoritative and selects the OLD tree — which verifies, but not to
    the desired hash — so the stopped driver stays stopped rather than restart
    against state the desired distribution never published. The next pass
    completes the transition and restarts it exactly once."""
    custom = driver_stack("custom-impl", "drivers/custom")
    a_hash, a_data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    b_hash, b_data = make_config_dist({"drivers/custom/impl.py": "v = 2\n"})
    rig.control.config_artifacts[a_hash] = a_data
    rig.control.config_artifacts[b_hash] = b_data
    rig.control.heartbeat_answers.append(dist_response([custom], a_hash))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")

    def failing(drivers_hash):
        raise OSError(5, "injected: pointer publication failed")

    rig.daemon._write_current_pointer = failing
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    root = rig.config.state_dir / "config-dist"
    assert (root / "current").read_text().strip() == a_hash  # the old pointer stands
    assert not rig.daemon._supervisor.alive("custom-impl")  # stopped, NOT restarted
    del rig.daemon._write_current_pointer  # restore the real method
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    # The heartbeat at the start of the retry pass still reported the OLD hash
    # (evidence-based: the old pointer selects the old, still-valid tree).
    assert _last_heartbeat(rig)["drivers_hash"] == a_hash
    assert (root / "current").read_text().strip() == b_hash
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns = [p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "drivers/custom"]]
    assert len(spawns) == 2  # the initial start + exactly one post-publication restart
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == b_hash


def test_config_dist_failed_verification_never_stops_the_driver(rig: Rig):
    """Restart only after the replacement is verified: a swap whose served
    artifact fails verification leaves the live driver untouched — never
    stopped for an unverified replacement — and the old tree current."""
    custom = driver_stack("custom-impl", "drivers/custom")
    a_hash, a_data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    rig.control.config_artifacts[a_hash] = a_data
    rig.control.heartbeat_answers.append(dist_response([custom], a_hash))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns_before = len(rig.popen.spawned)
    b_hash, _good = make_config_dist({"drivers/custom/impl.py": "v = 2\n"})
    _, tampered = make_config_dist({"drivers/custom/impl.py": "TAMPERED = 3\n"})
    rig.control.config_artifacts[b_hash] = tampered
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    assert len(rig.popen.spawned) == spawns_before  # never stopped, never restarted
    root = rig.config.state_dir / "config-dist"
    assert (root / "current").read_text().strip() == a_hash
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == a_hash


# -- the complete metadata envelope gates the stop boundary (ADR-0042 amendment) --


def test_config_dist_invalid_utf8_metadata_rejected_before_stop(rig: Rig):
    """A served artifact whose drivers tree recomputes to the requested hash
    but whose ``config-dist.json`` is invalid UTF-8 is rejected IN STAGING:
    the live custom driver is never stopped, the old tree stays current, and
    the failure is a normalized ConfigDistError, never a leaked
    UnicodeDecodeError."""
    custom = driver_stack("custom-impl", "drivers/custom")
    a_hash, a_data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    rig.control.config_artifacts[a_hash] = a_data
    rig.control.heartbeat_answers.append(dist_response([custom], a_hash))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns_before = len(rig.popen.spawned)
    b_files = {"drivers/custom/impl.py": "v = 2\n"}
    b_hash, _ = make_config_dist(b_files)
    _, bad_data = make_config_dist(b_files, raw_metadata=b"\xff\xfe not utf-8 {")
    rig.control.config_artifacts[b_hash] = bad_data
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")  # never stopped
    assert len(rig.popen.spawned) == spawns_before  # never restarted either
    root = rig.config.state_dir / "config-dist"
    assert (root / "current").read_text().strip() == a_hash  # old tree current
    assert not (root / b_hash).exists()  # nothing exchanged or published
    assert any("config distribution" in line and "failed" in line for line in rig.logs)
    assert not any("UnicodeDecodeError" in line for line in rig.logs)
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == a_hash  # still honestly on A


@pytest.mark.parametrize(
    "raw_meta_shape",
    [
        "invalid_utf8",
        "malformed_json",
        "json_array",
        "wrong_format",
        "hash_mismatch",
        "nonstring_hash",
    ],
)
def test_config_dist_malformed_metadata_envelope_rejected_before_exchange(rig: Rig, raw_meta_shape):
    """Every malformed-envelope shape — invalid UTF-8, malformed JSON, a
    non-object document, a wrong ``format``, a mismatching or non-string
    ``drivers_hash`` — is refused in staging before any tree is exchanged or
    pointer published, and the very next pass converges once a valid artifact
    is served (the failure normalizes to ConfigDistError and retries)."""
    files = {"drivers/custom/impl.py": "x = 1\n"}
    digest, good_data = make_config_dist(files)
    raw = {
        "invalid_utf8": b"\xff\xfe not utf-8 {",
        "malformed_json": b"{ not json",
        "json_array": b"[]",
        "wrong_format": json.dumps({"format": 999, "drivers_hash": digest}).encode(),
        "hash_mismatch": json.dumps({"format": 1, "drivers_hash": "0" * 64}).encode(),
        "nonstring_hash": json.dumps({"format": 1, "drivers_hash": 7}).encode(),
    }[raw_meta_shape]
    _, bad_data = make_config_dist(files, raw_metadata=raw)
    rig.control.config_artifacts[digest] = bad_data
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    root = rig.config.state_dir / "config-dist"
    assert not (root / "current").exists()  # nothing published
    assert not (root / digest).exists()  # nothing exchanged
    assert any("config distribution" in line and "failed" in line for line in rig.logs)
    # A valid artifact repairs on the next pass — the failure was retryable.
    rig.control.config_artifacts[digest] = good_data
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    assert (root / "current").read_text().strip() == digest


def test_config_dist_invalid_utf8_restored_metadata_keeps_heartbeat_alive_then_repairs(rig: Rig):
    """A restored/corrupted applied tree whose ``config-dist.json`` holds
    invalid UTF-8 must not wedge the heartbeat loop: the pass completes, the
    node honestly reports non-convergence with an empty advisory stamp, and
    the next fetch repairs the whole tree."""
    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"}, built_against="0.2.9")
    meta_path = rig.config.state_dir / "config-dist" / digest / "config-dist.json"
    meta_path.write_bytes(b"\xff\xfe not utf-8 {")
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()  # completes — malformed on-disk metadata never raises out
    reported = _last_heartbeat(rig)
    assert reported["drivers_hash"] == ""
    assert reported["drivers_built_against"] == ""
    assert any("metadata failed verification" in line for line in rig.logs)
    # The same pass repaired; the next heartbeat proves the loop stayed healthy.
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    reported = _last_heartbeat(rig)
    assert reported["drivers_hash"] == digest
    assert reported["drivers_built_against"] == "0.2.9"


@pytest.mark.parametrize("stamp_shape", ["absent", "nonstring"])
def test_config_dist_missing_or_nonstring_built_against_is_empty_advisory(rig: Rig, stamp_shape):
    """``built_against`` stays advisory (ADR-0042): an envelope that omits it
    or carries a non-string value still validates and converges — the stamp
    reads as '' on both the apply path and every re-verification, and
    convergence is never affected."""
    files = {"drivers/custom/impl.py": "x = 1\n"}
    digest, _ = make_config_dist(files)
    meta: dict = {"format": 1, "drivers_hash": digest, "built_at": "1980-01-01T00:00:00+00:00"}
    if stamp_shape == "nonstring":
        meta["built_against"] = 123
    _, data = make_config_dist(files, raw_metadata=json.dumps(meta).encode())
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    root = rig.config.state_dir / "config-dist"
    assert (root / "current").read_text().strip() == digest  # converged
    # The next pass re-verifies the applied envelope from disk: still
    # converged, still the empty advisory stamp.
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    reported = _last_heartbeat(rig)
    assert reported["drivers_hash"] == digest
    assert reported["drivers_built_against"] == ""


# -- filesystem-corruption normalization: pointer bytes and entry classifiers --


def test_config_dist_invalid_utf8_pointer_keeps_heartbeat_alive_then_repairs(rig: Rig):
    """A corrupted/restored ``current`` pointer holding invalid UTF-8 bytes is
    malformed state exactly like a non-64-hex value: the heartbeat pass
    completes (no UnicodeDecodeError escapes), the node honestly reports
    non-convergence, and the same pass refetches and repairs the desired
    distribution (ADR-0042)."""
    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"}, built_against="0.2.9")
    pointer = rig.config.state_dir / "config-dist" / "current"
    pointer.write_bytes(b"\xff\xfe not a hash")
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()  # completes — the undecodable pointer never raises out
    reported = _last_heartbeat(rig)
    assert reported["drivers_hash"] == ""
    assert reported["drivers_built_against"] == ""
    assert any("pointer is not valid UTF-8" in line for line in rig.logs)
    assert not any("UnicodeDecodeError" in line for line in rig.logs)
    # The same pass repaired the pointer and tree; the next heartbeat reports
    # the applied hash and its advisory stamp again.
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    reported = _last_heartbeat(rig)
    assert reported["drivers_hash"] == digest
    assert reported["drivers_built_against"] == "0.2.9"


class _UnstatableEntry:
    """A DirEntry stand-in whose metadata classifiers raise OSError — the
    stat-after-scandir failure mode (an entry ACL-blocked or lost between the
    directory read and the lazy lstat). ``name``/``path`` still read fine;
    only classification fails."""

    def __init__(self, entry):
        self.name = entry.name
        self.path = entry.path

    def is_symlink(self):
        raise OSError(5, "injected: cannot stat entry")

    def is_dir(self, follow_symlinks=True):
        raise OSError(5, "injected: cannot stat entry")

    def is_file(self, follow_symlinks=True):
        raise OSError(5, "injected: cannot stat entry")


def _boom_entry_classifiers(monkeypatch, target: Path) -> dict:
    """Patch ``os.scandir`` so the single entry at ``target`` fails
    CLASSIFICATION (DirEntry metadata OSError) while enumeration itself still
    succeeds; every other directory — and every fd-based scandir, e.g. inside
    ``shutil.rmtree`` — passes through untouched. Returns the ``{'on': bool}``
    toggle that clears the injected fault."""
    import os

    active = {"on": True}
    real_scandir = os.scandir

    @contextlib.contextmanager
    def fake(path="."):
        with real_scandir(path) as it:
            entries = list(it)
        if active["on"] and isinstance(path, (str, os.PathLike)):
            entries = [_UnstatableEntry(e) if Path(e.path) == target else e for e in entries]
        yield iter(entries)

    monkeypatch.setattr(os, "scandir", fake)
    return active


@pytest.mark.parametrize("target_rel", ["drivers", "config-dist.json", "drivers/custom"])
def test_config_dist_applied_entry_classifier_failure_not_converged_then_repaired(
    rig: Rig, monkeypatch, target_rel
):
    """An applied-tree entry whose DirEntry metadata cannot be read after a
    successful scandir — the root ``drivers`` dir, the ``config-dist.json``
    envelope, or an entry below ``drivers/`` — is a normalized verification
    failure, never an OSError escaping ``_status_payload``: the heartbeat
    survives, reports ``("", "")``, and the same pass refetches and repairs
    (ADR-0042)."""
    digest = _apply_dist(rig, {"drivers/custom/impl.py": "x = 1\n"}, built_against="0.2.9")
    tree = rig.config.state_dir / "config-dist" / digest
    active = _boom_entry_classifiers(monkeypatch, tree / target_rel)
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()  # completes — the classifier OSError never escapes
    reported = _last_heartbeat(rig)
    assert reported["drivers_hash"] == ""
    assert reported["drivers_built_against"] == ""
    assert any("cannot be classified" in line or "cannot classify" in line for line in rig.logs)
    active["on"] = False  # the fault clears; the repair already landed this pass
    rig.control.heartbeat_answers.append(dist_response([], digest))
    rig.daemon.once()
    reported = _last_heartbeat(rig)
    assert reported["drivers_hash"] == digest
    assert reported["drivers_built_against"] == "0.2.9"


def test_config_dist_staging_classifier_failure_rejected_before_stop(rig: Rig, monkeypatch):
    """A fetched artifact whose STAGING verification hits a per-entry
    classifier OSError is rejected as a normalized ConfigDistError with the
    live custom driver never stopped and the old tree still current — the
    staging-filesystem failure lands before the stop boundary, and the swap
    completes once the fault clears (ADR-0042)."""
    custom = driver_stack("custom-impl", "drivers/custom")
    a_hash, a_data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    rig.control.config_artifacts[a_hash] = a_data
    rig.control.heartbeat_answers.append(dist_response([custom], a_hash))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    spawns_before = len(rig.popen.spawned)
    b_files = {"drivers/custom/impl.py": "v = 2\n"}
    b_hash, b_data = make_config_dist(b_files)
    rig.control.config_artifacts[b_hash] = b_data
    root = rig.config.state_dir / "config-dist"
    active = _boom_entry_classifiers(monkeypatch, root / f".{b_hash}.tmp" / "drivers" / "custom")
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")  # never stopped
    assert len(rig.popen.spawned) == spawns_before  # never restarted either
    assert (root / "current").read_text().strip() == a_hash  # old tree current
    assert not (root / b_hash).exists()  # nothing exchanged or published
    assert any("cannot classify" in line for line in rig.logs)
    active["on"] = False
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    assert (root / "current").read_text().strip() == b_hash  # converged after the fault
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    assert _last_heartbeat(rig)["drivers_hash"] == b_hash
    assert rig.daemon._supervisor.alive("custom-impl")


# -- THEOZOLITH_DRIVERS_DIR env injection (ADR-0042, issue #25) -----------------


def _drivers_dir(rig: Rig, digest: str) -> str:
    return str(rig.config.state_dir / "config-dist" / digest)


def _custom_spawns(rig: Rig):
    return [p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "drivers/custom"]]


def test_config_dist_stack_gets_drivers_dir_env(rig: Rig):
    """A theozolith-driver drivers/<name> Stack launches with THEOZOLITH_DRIVERS_DIR
    pointing at the VERIFIED applied distribution root (ADR-0042)."""
    custom = driver_stack("custom-impl", "drivers/custom")
    digest, data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    rig.control.config_artifacts[digest] = data
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("custom-impl")
    (child,) = _custom_spawns(rig)
    assert child.env["THEOZOLITH_DRIVERS_DIR"] == _drivers_dir(rig, digest)


def test_config_dist_stack_without_dist_refuses_and_emits_error(rig: Rig):
    """A drivers/<name> Stack with no verified distribution refuses to start,
    injects no THEOZOLITH_DRIVERS_DIR, and surfaces a config-dist-missing
    theozolith.error — the fail-closed posture of the run-image guard (ADR-0042)."""
    custom = driver_stack("custom-impl", "drivers/custom")
    rig.control.heartbeat_answers.append(dist_response([custom], ""))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("custom-impl")
    assert not _custom_spawns(rig)
    errors = [e for e in rig.control.events if e.get("error_class") == "config-dist-missing"]
    assert errors and "custom-impl" in errors[0]["message"]


def test_config_dist_stack_desired_but_unconverged_refuses_and_emits_error(rig: Rig):
    """A non-empty desired hash whose artifact never lands (409) refuses the
    start and emits config-dist-missing — the applied side is still empty."""
    custom = driver_stack("custom-impl", "drivers/custom")
    digest, _ = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})  # never registered → 409
    rig.control.heartbeat_answers.append(dist_response([custom], digest))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("custom-impl")
    assert any(e.get("error_class") == "config-dist-missing" for e in rig.control.events)


def test_config_dist_hash_swap_restarts_with_the_new_drivers_dir(rig: Rig):
    """A hash swap restarts the custom driver on the fresh path: the second
    child launches with THEOZOLITH_DRIVERS_DIR pointing at the new hash dir."""
    custom = driver_stack("custom-impl", "drivers/custom")
    a_hash, a_data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    b_hash, b_data = make_config_dist({"drivers/custom/impl.py": "v = 2\n"})
    rig.control.config_artifacts[a_hash] = a_data
    rig.control.config_artifacts[b_hash] = b_data
    rig.control.heartbeat_answers.append(dist_response([custom], a_hash))
    rig.daemon.once()
    rig.control.heartbeat_answers.append(dist_response([custom], b_hash))
    rig.daemon.once()
    spawns = _custom_spawns(rig)
    assert len(spawns) == 2
    assert spawns[0].env["THEOZOLITH_DRIVERS_DIR"] == _drivers_dir(rig, a_hash)
    assert spawns[1].env["THEOZOLITH_DRIVERS_DIR"] == _drivers_dir(rig, b_hash)


def test_builtin_driver_stack_gets_no_drivers_dir_and_is_untouched_by_swaps(rig: Rig):
    """A builtin:* driver Stack never carries THEOZOLITH_DRIVERS_DIR and is not
    restarted by a config-distribution swap (ADR-0042)."""
    custom = driver_stack("custom-impl", "drivers/custom")
    builtin = process_stack("impl")
    builtin["command"] = "theozolith-driver builtin:implementer"
    builtin["env"] = {"THEOZOLITH_REPO": "acme/sandbox", "THEOZOLITH_RUN_IMAGE": "theozolith/y:1"}
    stacks = [custom, builtin]
    a_hash, a_data = make_config_dist({"drivers/custom/impl.py": "v = 1\n"})
    b_hash, b_data = make_config_dist({"drivers/custom/impl.py": "v = 2\n"})
    rig.control.config_artifacts[a_hash] = a_data
    rig.control.config_artifacts[b_hash] = b_data
    rig.control.heartbeat_answers.append(dist_response(stacks, a_hash))
    rig.daemon.once()
    rig.control.heartbeat_answers.append(dist_response(stacks, b_hash))
    rig.daemon.once()
    builtin_spawns = [
        p for p in rig.popen.spawned if p.args[:2] == ["theozolith-driver", "builtin:implementer"]
    ]
    assert len(builtin_spawns) == 1  # never restarted by the swap
    assert "THEOZOLITH_DRIVERS_DIR" not in builtin_spawns[0].env
