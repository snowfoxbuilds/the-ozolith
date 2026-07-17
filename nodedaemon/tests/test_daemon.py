"""Daemon reconcile loop: convergence, commands, degraded mode, secrets.

Covers the M3 acceptance criteria owned by the Node Daemon: the reconcile
test (both Stack kinds; drain/recycle/update/rebuild observable state
changes; recycling a process Stack leaves no orphaned run containers), the
build test's node half, the secrets tmpfs rule, degraded-mode caching, and
the deletion test's daemon half (boots with no config at all).
"""

from __future__ import annotations

import json

from daemonrig import Rig, container_stack, desired, image_recipe, process_stack


def heartbeat_response(stacks, images=None, commands=None) -> dict:
    return {"commands": commands or [], "config": desired(stacks, images)}


# -- convergence: both Stack kinds (acceptance 5) --------------------------------


def test_declaring_stacks_converges_the_node(rig: Rig):
    rig.control.heartbeat_answers.append(
        heartbeat_response([process_stack("worker"), container_stack("control")])
    )
    rig.daemon.once()

    # The process Stack (worker driver) runs as a supervised child…
    assert [p.args for p in rig.popen.spawned] == [["worker-driver", "--loop"]]
    assert rig.popen.spawned[0].env["THEOZOLITH_REPO"] == "acme/sandbox"
    assert rig.popen.spawned[0].env["THEOZOLITH_NODE_NAME"] == "box1"
    # …and the container Stack runs as a labeled long-running container.
    assert rig.docker.stacks["ozolith-stack-control"]["image"] == "theozolith-control:local"
    assert rig.docker.stacks["ozolith-stack-control"]["ports"] == ["8443:8443"]


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
    stacks = [process_stack("worker", run_image="claude-dev")]
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
    stacks = [process_stack("worker", run_image="claude-dev")]
    rig.control.heartbeat_answers.append(heartbeat_response(stacks, [recipe]))
    rig.daemon.once()

    assert [b["tag"] for b in rig.docker.builds] == [recipe["tag"]]
    assert rig.popen.spawned[0].env["THEOZOLITH_RUN_IMAGE"] == recipe["tag"]

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


def test_failed_command_is_not_acked(rig: Rig, monkeypatch):
    stacks = [process_stack("worker")]

    def broken_stop(*args, **kwargs):
        raise RuntimeError("docker is down")

    monkeypatch.setattr(rig.daemon, "_stop_stack", broken_stop)
    rig.control.heartbeat_answers.append(
        heartbeat_response(stacks, commands=[{"id": 9, "verb": "drain", "target": "worker"}])
    )
    rig.daemon.once()
    rig.control.heartbeat_answers.append(heartbeat_response(stacks))
    rig.daemon.once()
    assert rig.control.transcript[-1][2]["completed_commands"] == []


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
    stack = container_stack("control", secrets={"THEOZOLITH_ADMIN_TOKEN": "admin-token"})
    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    recorded = rig.docker.stacks["ozolith-stack-control"]
    host_path = recorded["env_files"]["THEOZOLITH_ADMIN_TOKEN"]
    assert host_path == str(rig.config.secrets_dir / "admin-token")


# -- queue-behind recycle/update (NODE-SUBSTRATE, grilling 2026-07-17) -----------


def _driver_stack(tmp_path) -> tuple[dict, "Path"]:
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


def test_update_mid_run_queues_behind_node_wide(rig: Rig, tmp_path):
    stack, jobs = _driver_stack(tmp_path)
    (jobs / "r1").mkdir(parents=True)

    rig.control.heartbeat_answers.append(heartbeat_response([stack]))
    rig.daemon.once()

    update = {"id": 11, "verb": "update", "target": None, "force": False}
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[update]))
    rig.daemon.once()
    assert rig.update_calls == [] and rig.execv_calls == []  # deferred
    assert rig.daemon._deferrals[11].startswith("behind run r1")

    import shutil

    shutil.rmtree(jobs / "r1")
    rig.control.heartbeat_answers.append(heartbeat_response([stack], commands=[update]))
    rig.daemon.once()
    assert rig.update_calls and rig.execv_calls  # applied after the Run
