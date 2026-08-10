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
from theozolith_nodedaemon.dockerctl import DockerCtl


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
    # The inlined desired-state documents landed under the state dir.
    on_disk = rig.config.state_dir / "stacks" / "control" / "compose" / "control.yml"
    assert on_disk.read_text() == "services: {}\n"


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
    rig.control.secrets["github-worker"] = "s3cr3t-value"
    stack = process_stack("worker", secrets={"WORKER_GITHUB_TOKEN": "github-worker"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    secret_file = rig.config.secrets_dir / "github-worker"
    assert secret_file.read_text() == "s3cr3t-value"
    assert (secret_file.stat().st_mode & 0o777) == 0o600
    env = rig.popen.spawned[0].env
    assert env["WORKER_GITHUB_TOKEN_FILE"] == str(secret_file)
    assert "WORKER_GITHUB_TOKEN" not in env  # the value itself never enters env

    # A scan of everything the daemon wrote to DISK finds no trace: the
    # value lives only under the runtime dir (tmpfs under /run).
    for path in rig.config.state_dir.rglob("*"):
        if path.is_file():
            assert "s3cr3t-value" not in path.read_text(errors="replace"), path


def test_denied_secret_pull_skips_the_stack(rig: Rig):
    rig.control.denied_secrets = True
    stack = process_stack("worker", secrets={"WORKER_GITHUB_TOKEN": "github-worker"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker")
    assert any("secrets unavailable" in line for line in rig.logs)


def test_secret_pull_refused_over_plain_http_without_dev_flag(tmp_path):
    from theozolith_nodedaemon.controlclient import ControlClient, ControlError

    client = ControlClient("http://control.test", "tok", insecure_dev=False)
    try:
        client.pull_secrets("box1", ["github-worker"])
    except ControlError as exc:
        assert "TLS is mandatory" in str(exc)
    else:
        raise AssertionError("plain-HTTP secret pull must be refused")


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
    """Node-resident drivers authenticate as their node: the daemon hands
    its control URL, per-node token, and pinned CA down — Stack env wins
    (the daemon-less dev override)."""
    pinned = process_stack("reviewer", env={"CONTROL_NODE_URL": "http://elsewhere.test"})
    rig.control.heartbeat_answers.append(heartbeat_response([process_stack("worker"), pinned]))
    rig.daemon.once()

    by_name = {p.args[0]: p.env for p in rig.popen.spawned}
    assert by_name["worker-driver"]["CONTROL_NODE_URL"] == "http://control.test"
    assert by_name["worker-driver"]["THEOZOLITH_NODE_TOKEN"] == "node-token"
    assert by_name["reviewer-driver"]["CONTROL_NODE_URL"] == "http://elsewhere.test"


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
# adapter, model, run-image tag) and the secret mapping, so a live driver must
# restart when any of those change — and must NOT churn when nothing does.


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


def test_changed_model_restarts_the_driver(rig: Rig):
    a = process_stack(
        "worker", env={"THEOZOLITH_REPO": "a/x", "THEOZOLITH_MODEL": "claude-sonnet-5"}
    )
    rig.control.heartbeat_answers.append(heartbeat_response([a]))
    rig.daemon.once()
    first = rig.popen.spawned[0]
    b = process_stack(
        "worker", env={"THEOZOLITH_REPO": "a/x", "THEOZOLITH_MODEL": "claude-opus-5"}
    )
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2
    assert rig.popen.spawned[1].env["THEOZOLITH_MODEL"] == "claude-opus-5"
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
    a = process_stack("worker", secrets={"WORKER_GITHUB_TOKEN": "tok-a"})
    rig.control.heartbeat_answers.append(heartbeat_response([a]))
    rig.daemon.once()
    assert rig.popen.spawned[0].env["WORKER_GITHUB_TOKEN_FILE"].endswith("tok-a")

    b = process_stack("worker", secrets={"WORKER_GITHUB_TOKEN": "tok-b"})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2
    assert rig.popen.spawned[1].env["WORKER_GITHUB_TOKEN_FILE"].endswith("tok-b")


def test_stopped_stack_with_a_config_change_stays_stopped(rig: Rig):
    a = process_stack("worker", state="stopped", env={"THEOZOLITH_MODEL": "a"})
    rig.control.heartbeat_answers.append(heartbeat_response([a]))
    rig.daemon.once()
    b = process_stack("worker", state="stopped", env={"THEOZOLITH_MODEL": "b"})
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker")
    assert not rig.popen.spawned  # a stopped-by-desire Stack never launches


# -- fail-closed cutover: built-in drivers require a control-authored run image ---


def test_old_builtin_implementer_without_run_image_does_not_start(rig: Rig):
    stack = process_stack("worker", command="theozolith-worker", env={"THEOZOLITH_REPO": "acme/x"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker") and not rig.popen.spawned
    (event,) = rig.control.events
    assert "THEOZOLITH_RUN_IMAGE" in event["message"]
    assert "incompatible/incomplete desired state" in event["message"]


def test_old_builtin_reviewer_without_run_image_does_not_start(rig: Rig):
    stack = process_stack(
        "reviewer", command="theozolith-reviewer", env={"THEOZOLITH_REPO": "a/x"}
    )
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("reviewer") and not rig.popen.spawned
    (event,) = rig.control.events
    assert "theozolith-reviewer" in event["message"]


def test_live_builtin_driver_stops_when_run_image_env_is_lost(rig: Rig):
    recipe = image_recipe()
    good = process_stack(
        "worker", command="theozolith-worker",
        env={"THEOZOLITH_REPO": "acme/x", "THEOZOLITH_RUN_IMAGE": recipe["tag"]},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([good], [recipe]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("worker")

    bad = process_stack("worker", command="theozolith-worker", env={"THEOZOLITH_REPO": "acme/x"})
    rig.control.heartbeat_answers.append(heartbeat_response([bad]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker")  # stale execution stopped
    assert rig.control.events  # the incompatibility is surfaced


def test_generic_process_stack_without_run_image_starts_normally(rig: Rig):
    stack = process_stack("batch", command="my-batch --run", env={})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()
    assert rig.daemon._supervisor.alive("batch")  # a generic Stack needs no run image


def test_other_stacks_reconcile_when_one_fails_closed(rig: Rig):
    bad = process_stack("worker", command="theozolith-worker", env={"THEOZOLITH_REPO": "acme/x"})
    good = process_stack("batch", command="my-batch --run", env={})
    rig.control.heartbeat_answers.append(heartbeat_response([bad, good]))
    rig.daemon.once()
    assert not rig.daemon._supervisor.alive("worker")
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
    first = _launch_driver(rig, jobs, THEOZOLITH_MODEL="claude-sonnet-5")
    _plant_run(rig, jobs, "r1")

    b = process_stack(
        "worker", env={"THEOZOLITH_JOBS_DIR": str(jobs), "THEOZOLITH_MODEL": "claude-opus-5"}
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
        env={"THEOZOLITH_JOBS_DIR": str(jobs), "THEOZOLITH_REPO": "a/x",
             "THEOZOLITH_RUN_IMAGE": new["tag"]},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([b], [new]))
    rig.daemon.once()

    assert rig.docker.image_exists(new["tag"])  # the new image DID build...
    assert len(rig.popen.spawned) == 1 and first.poll() is None  # ...restart still deferred
    assert "run-r1" not in rig.docker.removed


def test_desired_secret_mapping_change_during_a_run_is_deferred(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    rig.control.secrets.update({"tok-a": "A", "tok-b": "B"})
    first = _launch_driver(rig, jobs, secrets={"WORKER_GITHUB_TOKEN": "tok-a"})
    _plant_run(rig, jobs, "r1")

    b = process_stack(
        "worker",
        env={"THEOZOLITH_JOBS_DIR": str(jobs)},
        secrets={"WORKER_GITHUB_TOKEN": "tok-b"},
    )
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 1 and first.poll() is None
    assert "run-r1" not in rig.docker.removed


def test_deferred_desired_change_restarts_exactly_once_after_the_run(rig: Rig, tmp_path):
    jobs = tmp_path / "jobs"
    first = _launch_driver(rig, jobs, THEOZOLITH_MODEL="m1")
    _plant_run(rig, jobs, "r1")

    b = process_stack("worker", env={"THEOZOLITH_JOBS_DIR": str(jobs), "THEOZOLITH_MODEL": "m2"})
    for _ in range(2):  # two deferred passes while the Run is in flight
        rig.control.heartbeat_answers.append(heartbeat_response([b]))
        rig.daemon.once()
    assert len(rig.popen.spawned) == 1

    shutil.rmtree(jobs / "r1")  # the Run ends
    rig.control.heartbeat_answers.append(heartbeat_response([b]))
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2 and first.poll() is not None
    assert rig.popen.spawned[1].env["THEOZOLITH_MODEL"] == "m2"

    rig.control.heartbeat_answers.append(heartbeat_response([b]))  # and no second restart
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2


def test_jobs_dir_change_still_detects_a_run_under_the_old_path(rig: Rig, tmp_path):
    """When THEOZOLITH_JOBS_DIR itself changes, the in-flight check inspects the
    RUNNING child's old jobs dir — a naive check of the new (empty) desired path
    would wrongly restart mid-Run."""
    old_jobs = tmp_path / "old-jobs"
    new_jobs = tmp_path / "new-jobs"
    first = _launch_driver(rig, old_jobs, THEOZOLITH_MODEL="m1")
    _plant_run(rig, old_jobs, "r1")  # Run under the OLD path
    new_jobs.mkdir(parents=True)  # the NEW desired path is empty

    b = process_stack(
        "worker", env={"THEOZOLITH_JOBS_DIR": str(new_jobs), "THEOZOLITH_MODEL": "m2"}
    )
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
    first = _launch_driver(rig, jobs, secrets={"WORKER_GITHUB_TOKEN": "tok-a"})
    assert first.env["WORKER_GITHUB_TOKEN_FILE"].endswith("tok-a")
    rig.docker.add_run_container("run-x", "r-x", "worker")  # a Run container it owns

    # Switch to a mapping the Control Node will not serve and that is not cached.
    rig.control.denied_secrets = True
    b = process_stack(
        "worker", env={"THEOZOLITH_JOBS_DIR": str(jobs)}, secrets={"WORKER_GITHUB_TOKEN": "tok-b"}
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
    assert rig.popen.spawned[1].env["WORKER_GITHUB_TOKEN_FILE"].endswith("tok-b")
    assert "run-x" in rig.docker.removed  # kill-the-tree only now, after prerequisites ready

    rig.control.heartbeat_answers.append(heartbeat_response([b]))  # stable thereafter
    rig.daemon.once()
    assert len(rig.popen.spawned) == 2


def test_secret_values_never_enter_fingerprint_or_persisted_state(rig: Rig):
    rig.control.secrets["tok-a"] = "super-secret-value"
    stack = process_stack("worker", secrets={"WORKER_GITHUB_TOKEN": "tok-a"})
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
        command="theozolith-worker",
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
        secrets={"WORKER_GITHUB_TOKEN": "tok-x"},
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
        "docker", "compose", "--project-name", "ozolith-svc",
        "--file", "/state/stacks/svc/compose/base.yml",
        "--file", "/state/stacks/svc/overlays/net.yml",
        "up", "--detach", "--remove-orphans",
    ]
    assert down == [
        "docker", "compose", "--project-name", "ozolith-svc",
        "--file", "/state/stacks/svc/compose/base.yml",
        "--file", "/state/stacks/svc/overlays/net.yml",
        "down", "--remove-orphans",
    ]
    assert "--file" in down  # a down is never issued with an empty file set


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
    rig.control.heartbeat_answers.append(
        heartbeat_response([_compose_stack(name, files=files)])
    )
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
