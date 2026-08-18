"""Drain-aware convergence lifecycles (issue #8): the REAL Node Daemon
converging against the REAL control-plane app, bridged at the transport seam
(the pattern of test_channel_invariant).

The heartbeat's update_deferred/drivers_deferred fields carry one transition
guarantee per edge of a drain. ENTRY: the current whole-node in-flight
signal is computed as the heartbeat is CONSTRUCTED, so a Run that began
since the last pass is deferred in the very next beat — never a replay of
the prior pass's converge decision, whose one-beat lag would let the ladder
queue a restart before control ever saw the deferral. EXIT: a Run that
ENDED between passes leaves no current blocker, but returned commands
execute before the pass's own converge step — so the retained prior-pass
converge blocker (the one-pass exit latch) keeps exactly that one beat
deferred, and the same pass then makes the first real post-drain attempt.
Both are proven at ``offpin_beats = 1``, the harshest ladder, where a
single divergent-and-undeferred beat queues a restart and a second
escalates. Assertions run against the control store itself — no convergence
restart is ever CREATED for a healthy drain, not merely never executed —
while a genuinely stuck node still climbs to restart and escalation once
its drain ends and its first post-drain attempt fails.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from daemonrig import FakeDocker, FakePopen
from fastapi.testclient import TestClient
from theozolith_control import configdist as control_configdist
from theozolith_control.app import create_app
from theozolith_control.crypto import SecretBox, generate_key
from theozolith_control.secretstore import SecretStore
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store
from theozolith_nodedaemon.config import DaemonConfig
from theozolith_nodedaemon.controlclient import ControlClient
from theozolith_nodedaemon.daemon import NodeDaemon
from theozolith_nodedaemon.stacks import ProcessSupervisor


class LifecycleRig:
    """One node ("box1") heartbeating to a real control app. The daemon and
    the control plane are the real thing; the fakes are docker, process
    children, pip, and execv — the same seams as the daemon rig."""

    def __init__(self, tmp_path: Path, monkeypatch, *, offpin_beats: int, update_rc: int = 0):
        self.jobs = tmp_path / "jobs"
        self.settings = ControlSettings(
            data_dir=tmp_path / "data",
            config_repo=tmp_path / "configs",
            admin_token="admin-token",
            repo=None,
            github_token=None,
            api_url="",
            offpin_beats=offpin_beats,
        )
        # A generic driver Stack whose jobs dir the test controls: a job
        # directory inside it IS the in-flight Run the daemon defers behind.
        self.write_config(
            "stacks/worker.toml",
            'kind = "process"\nnode = "box1"\ncommand = "worker-driver --loop"\n'
            f'[env]\nTHEOZOLITH_JOBS_DIR = "{self.jobs}"\n',
        )
        self.store = Store(self.settings.cache_db_path)
        secret_store = SecretStore(self.settings.store_db_path)
        self._node_token = secret_store.mint_node_token("box1")
        self.web = TestClient(
            create_app(self.settings, self.store, secret_store, SecretBox(generate_key()))
        )
        # The response-in-flight Run-end seam: naming a run id here removes
        # its job directory right after the NEXT heartbeat is served — the
        # driver child finishing while the answer is in flight. The NATURAL
        # ending (the Run finishing between passes) needs no seam: the test
        # just removes the directory before calling once().
        self.end_run_after_next_heartbeat: str | None = None

        def bridge(method: str, url: str, headers: dict, body: bytes | None):
            path = url.removeprefix("http://control.test")
            response = self.web.request(method, path, content=body, headers=headers)
            if path == "/api/v1/heartbeats" and self.end_run_after_next_heartbeat:
                shutil.rmtree(self.jobs / self.end_run_after_next_heartbeat)
                self.end_run_after_next_heartbeat = None
            return response.status_code, response.content

        self._bridge = bridge
        self.popen = FakePopen()

        def fake_killpg(pid: int, sig: int) -> None:
            process = self.popen.registry.get(pid)
            if process is None or process.returncode is not None:
                raise ProcessLookupError(pid)
            process.returncode = -sig

        monkeypatch.setattr("theozolith_nodedaemon.stacks.os.killpg", fake_killpg)
        self.docker = FakeDocker()
        self.update_calls: list[list[str]] = []
        self.execv_calls: list[tuple[str, list[str]]] = []
        self.update_rc = update_rc
        self._tmp_path = tmp_path
        self.daemon = self._make_daemon("0.3.0")

    def _make_daemon(self, version: str) -> NodeDaemon:
        config = DaemonConfig(
            node="box1",
            control_url="http://control.test",
            node_token=self._node_token,
            tls_ca=None,
            state_dir=self._tmp_path / "state",
            runtime_dir=self._tmp_path / "run",
            heartbeat_seconds=60,
            stop_grace_seconds=0.2,
            insecure_dev=True,
            version=version,
        )

        def update_runner(args, **_):
            self.update_calls.append(list(args))
            return subprocess.CompletedProcess(args, self.update_rc, "", "pip says no")

        return NodeDaemon(
            config,
            docker=self.docker,  # type: ignore[arg-type]
            client=ControlClient(
                "http://control.test", self._node_token, insecure_dev=True, transport=self._bridge
            ),
            supervisor=ProcessSupervisor(popen=self.popen, log=lambda *_: None),
            log=lambda *_: None,
            execv=lambda path, argv: self.execv_calls.append((path, list(argv))),
            update_runner=update_runner,
        )

    def once(self) -> None:
        self.daemon.once()

    def reexec(self, version: str) -> None:
        """The recorded execv, made real: a brand-new daemon over the SAME
        state dir, now running the freshly installed version."""
        self.daemon = self._make_daemon(version)

    def write_config(self, relpath: str, text: str) -> None:
        target = self.settings.config_repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def restart_commands(self) -> list[dict]:
        """Every restart the command store has EVER held, completed included:
        the promise is that none is CREATED, not merely that none has run."""
        return [c for c in self.store.fleet_state()["commands"] if c["verb"] == "restart"]

    def ladder_rows(self, table: str) -> list:
        # The ladders' tracking tables, read directly: "tracking clears"
        # means the row is GONE, not merely that nothing fired this beat.
        assert table in ("version_health", "drivers_health")
        with self.store._lock:
            return self.store._db.execute(f"SELECT * FROM {table}").fetchall()

    def control_errors(self) -> list[dict]:
        """The control-side not-converging feed only — the daemon's own
        install-failure events carry component node-daemon and are expected
        on the stuck paths."""
        return self.store.error_events(component="control-node")


def test_threshold_one_drain_never_restarts_and_both_ladders_converge(tmp_path, monkeypatch):
    """offpin_beats = 1: ONE divergent-and-undeferred heartbeat queues a
    restart and a second escalates, so surviving this test proves the
    deferral covers every beat of a drain INCLUDING the first beat after it
    ends. The product pin and then the drivers-hash each move while a Run is
    in flight; each Run ends BETWEEN passes — the natural ending, where the
    next heartbeat is built with no current blocker and only the one-pass
    exit latch stands between control and a restart that would preempt the
    very convergence attempt that same pass then makes. Each subsystem
    converges in its first post-drain pass; the command store never holds a
    single command."""
    rig = LifecycleRig(tmp_path, monkeypatch, offpin_beats=1)
    rig.once()  # nothing divergent; the driver child starts
    assert rig.popen.spawned  # the in-flight signal needs the live child

    (rig.jobs / "r1").mkdir(parents=True)  # a Run begins...
    rig.write_config("product.toml", '[product]\nversion = "0.4.0"\n')  # ...then the pin moves

    for _ in range(5):  # far past 2N deferred beats (restart rung 1, error rung 2)
        rig.once()
        assert rig.restart_commands() == [] and rig.control_errors() == []
    assert rig.update_calls == []  # the daemon really is deferring the install
    assert rig.store.node_version("box1") == "0.3.0"  # the dispatch gate's input, intact

    # The Run ends BETWEEN passes. The next heartbeat sees no current
    # blocker; the exit latch keeps that one beat deferred, control queues
    # nothing, and the SAME pass makes the first real post-drain attempt:
    # install, re-exec, on-pin — no command was ever created to race it.
    shutil.rmtree(rig.jobs / "r1")
    rig.once()
    assert rig.restart_commands() == [] and rig.control_errors() == []
    assert len(rig.update_calls) == 1 and rig.execv_calls
    rig.reexec("0.4.0")
    rig.once()  # first post-re-exec beat: on-pin, tracking gone
    assert rig.store.node_version("box1") == "0.4.0"
    assert rig.ladder_rows("version_health") == []

    # The drivers-hash analog in the same lifecycle (ADR-0042): a new Run
    # begins, then the deployment grows a config distribution; the Run again
    # ends between passes, and the swap lands in the first post-drain pass.
    (rig.jobs / "r2").mkdir(parents=True)
    rig.write_config("drivers/custom/impl.py", "def run():\n    return 1\n")
    digest = control_configdist.dist_hash(rig.settings.config_repo)
    for _ in range(5):
        rig.once()
        assert rig.restart_commands() == [] and rig.control_errors() == []
    assert rig.store.node_drivers_hash("box1") == ""  # off-hash the whole while

    shutil.rmtree(rig.jobs / "r2")
    rig.once()  # exit-latch beat: the swap fetches, verifies, and applies
    assert rig.restart_commands() == [] and rig.control_errors() == []
    rig.once()  # and the next heartbeat reports the applied digest
    assert rig.store.node_drivers_hash("box1") == digest
    assert rig.ladder_rows("drivers_health") == []

    # The whole lifecycle — two drains, two convergences — created NOTHING:
    # the command store never held a convergence restart (or any command at
    # all), and the error feed never saw a not-converging escalation.
    assert rig.store.fleet_state()["commands"] == []
    assert rig.control_errors() == []


def test_run_ending_while_the_answer_is_in_flight_converges_in_that_pass(tmp_path, monkeypatch):
    """The OTHER healthy ending (offpin_beats = 1): the Run finishes while a
    deferred heartbeat's answer is still in flight. That beat already carried
    the fresh construction-time blocker, so nothing was queued; the same pass
    then finds the node free and installs — no exit latch is needed on this
    path, and none lingers past the re-exec."""
    rig = LifecycleRig(tmp_path, monkeypatch, offpin_beats=1)
    rig.once()  # the driver child starts
    (rig.jobs / "r1").mkdir(parents=True)
    rig.write_config("product.toml", '[product]\nversion = "0.4.0"\n')
    rig.once()  # a deferred off-pin beat; the install waits
    assert rig.restart_commands() == [] and rig.update_calls == []

    rig.end_run_after_next_heartbeat = "r1"
    rig.once()  # deferred beat served; the Run ends; this pass converges
    assert rig.restart_commands() == [] and rig.control_errors() == []
    assert len(rig.update_calls) == 1 and rig.execv_calls
    rig.reexec("0.4.0")
    rig.once()  # first post-re-exec beat: on-pin, tracking gone
    assert rig.store.node_version("box1") == "0.4.0"
    assert rig.ladder_rows("version_health") == []
    assert rig.store.fleet_state()["commands"] == [] and rig.control_errors() == []


def test_deferral_beginning_at_beat_n_minus_one_prevents_the_restart(tmp_path, monkeypatch):
    """The stale-report regression, end to end: a node already N-1 undeferred
    beats off-pin (its installs genuinely fail) begins a Run. The next real
    daemon heartbeat computes the blocker at construction time, so the beat
    that would have reached the threshold RESETS the ladder instead — the
    restart is never enqueued. A report replayed from the prior pass would
    have said "" here (the last converge attempt ran unblocked, before the
    Run began) and control would have restarted a draining node. Once the
    Run ends with the node still stuck, the exit latch holds ONE more beat —
    during which the first post-drain attempt runs and fails — and then the
    ladder climbs afresh: restart at N, escalation at 2N — the undeferred
    contract intact end to end."""
    rig = LifecycleRig(tmp_path, monkeypatch, offpin_beats=2, update_rc=1)
    rig.once()  # the child starts; nothing divergent yet
    rig.write_config("product.toml", '[product]\nversion = "0.4.0"\n')
    rig.once()  # undeferred off-pin beat 1 of 2; the install attempt fails
    assert len(rig.update_calls) == 1 and rig.restart_commands() == []

    (rig.jobs / "r1").mkdir(parents=True)  # a Run begins at N-1 beats
    rig.once()  # the would-be threshold beat arrives DEFERRED: full reset
    assert rig.restart_commands() == []  # never enqueued, not merely unrun
    assert rig.ladder_rows("version_health") == []
    rig.once()  # and the ladder stays down while the Run lives
    assert rig.restart_commands() == [] and rig.control_errors() == []
    assert len(rig.update_calls) == 1  # no install attempt mid-drain

    shutil.rmtree(rig.jobs / "r1")  # the Run ends; the node is STILL stuck
    rig.once()  # the exit-latch beat: still deferred, and this same pass
    assert rig.restart_commands() == [] and rig.ladder_rows("version_health") == []
    assert len(rig.update_calls) == 2  # ...makes the post-drain attempt (it fails)
    rig.once()  # undeferred beat 1: patience restarts from zero...
    assert rig.restart_commands() == []
    rig.once()  # ...beat 2 = N: control queues THE restart; the daemon obeys
    assert len(rig.restart_commands()) == 1 and rig.execv_calls
    rig.once()  # beat 3: the ack rides; still stuck, not yet 2N
    assert rig.control_errors() == []
    rig.once()  # beat 4 = 2N: the escalation, exactly once
    errors = rig.control_errors()
    assert [e["payload"]["error_class"] for e in errors] == ["update-not-converging"]
    assert len(rig.restart_commands()) == 1  # the ladder's one restart, ever
