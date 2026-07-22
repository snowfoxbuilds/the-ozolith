"""The channel invariant (acceptance 9): a transcript of everything that
crosses the heartbeat/command channel contains desired state and references
only — the sole value payload is the node-scoped secrets-pull response.

The REAL Node Daemon reconciles against the REAL control-plane app for
several passes (registration, heartbeats with status, a queued command and
its ack, a secret-referencing Stack that triggers a pull); every exchange is
captured at the transport seam and then scanned.
"""

from __future__ import annotations

import json
from pathlib import Path

from daemonrig import FakeDocker, FakePopen
from fastapi.testclient import TestClient
from theozolith_control.app import create_app
from theozolith_control.crypto import SecretBox, generate_key
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store
from theozolith_nodedaemon.config import DaemonConfig
from theozolith_nodedaemon.controlclient import ControlClient
from theozolith_nodedaemon.daemon import NodeDaemon
from theozolith_nodedaemon.stacks import ProcessSupervisor

SECRET_VALUE = "ghp_THEACTUALSECRETVALUE999"

KNOWN_CHANNEL_PATHS = {
    "/api/v1/nodes/register",
    "/api/v1/heartbeats",
    "/api/v1/secrets/pull",
    # theozolith.error summaries (2026-07-21 grilling): references and
    # size-capped text only, never secret values.
    "/api/v1/events",
}

HEARTBEAT_REQUEST_KEYS = {
    "node",
    "version",
    "stacks",
    "run_containers",
    "stack_containers",  # web-terminal target evidence (ADR-0019)
    "images",
    "config_commit",
    "completed_commands",
    "deferred_commands",  # queue-behind visibility (references, no values)
}


def test_channel_transcript_is_desired_state_and_references_only(tmp_path: Path, monkeypatch):
    # -- a real Control Node with a secret-referencing worker Stack ---------
    settings = ControlSettings(
        data_dir=tmp_path / "data",
        config_repo=tmp_path / "configs",
        node_token="node-token",
        admin_token="admin-token",
        repo=None,
        github_token=None,
        api_url="",
        zombie_grace_seconds=600,
        janitor_sweep_seconds=60,
        activation_window_seconds=60,
        tail_budget_bytes=10 * 1024**3,
        secrets_channel_ok=True,  # TLS-mandatory is proven in test_tls.py
    )
    stack_toml = tmp_path / "configs" / "stacks" / "worker.toml"
    stack_toml.parent.mkdir(parents=True)
    stack_toml.write_text(
        'kind = "process"\nnode = "box1"\ncommand = "theozolith-worker"\n'
        '[secrets]\nWORKER_GITHUB_TOKEN = "github-worker"\n',
        encoding="utf-8",
    )
    store = Store(settings.db_path)
    box = SecretBox(generate_key())
    store.put_secret("github-worker", box.encrypt(SECRET_VALUE))
    store.queue_command("box1", "recycle", "worker")
    web = TestClient(create_app(settings, store, box))

    # -- the real daemon, its transport tapped --------------------------------
    transcript: list[tuple[str, str, bytes, bytes]] = []

    def tapped_transport(method: str, url: str, headers: dict, body: bytes | None):
        path = url.removeprefix("http://control.test")
        response = web.request(method, path, content=body, headers=headers)
        transcript.append((method, path, body or b"", response.content))
        return response.status_code, response.content

    popen = FakePopen()
    monkeypatch.setattr(
        "theozolith_nodedaemon.stacks.os.killpg",
        lambda pid, sig: popen.registry[pid].__setattr__("returncode", -sig),
    )
    config = DaemonConfig(
        node="box1",
        control_url="http://control.test",
        node_token="node-token",
        tls_ca=None,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        heartbeat_seconds=60,
        stop_grace_seconds=0.2,
        insecure_dev=True,
        version="0.3.0",
    )
    daemon = NodeDaemon(
        config,
        docker=FakeDocker(),  # type: ignore[arg-type]
        client=ControlClient(
            "http://control.test", "node-token", insecure_dev=True, transport=tapped_transport
        ),
        supervisor=ProcessSupervisor(popen=popen, log=lambda *_: None),
        log=lambda *_: None,
    )
    daemon.once()  # register + heartbeat + recycle command + pull + start
    daemon.once()  # ack rides this heartbeat

    # -- the invariant ------------------------------------------------------------
    assert {entry[1] for entry in transcript} <= KNOWN_CHANNEL_PATHS
    assert sum(1 for e in transcript if e[1] == "/api/v1/heartbeats") >= 2

    for _method, path, request, response in transcript:
        # The one value payload: the secrets-pull RESPONSE, nothing else.
        assert SECRET_VALUE.encode() not in request, f"secret value sent up in {path}"
        if path != "/api/v1/secrets/pull":
            assert SECRET_VALUE.encode() not in response, f"secret value leaked via {path}"

        if path == "/api/v1/heartbeats":
            body = json.loads(request)
            assert set(body) == HEARTBEAT_REQUEST_KEYS  # status up…
            answer = json.loads(response)
            assert set(answer) == {"commands", "config"}  # …desired state down
            # References travel: the stack names its secret, never the value.
            stacks = answer["config"]["stacks"]
            assert stacks and stacks[0]["secrets"] == {"WORKER_GITHUB_TOKEN": "github-worker"}

    pulls = [e for e in transcript if e[1] == "/api/v1/secrets/pull"]
    assert len(pulls) == 1  # pulled at deploy time, not per heartbeat
    assert json.loads(pulls[0][2]) == {"node": "box1", "names": ["github-worker"]}
    assert json.loads(pulls[0][3]) == {"secrets": {"github-worker": SECRET_VALUE}}

    # And the daemon really deployed with it: value in tmpfs, _FILE wiring.
    assert (config.secrets_dir / "github-worker").read_text() == SECRET_VALUE
    env = popen.spawned[-1].env
    assert env["WORKER_GITHUB_TOKEN_FILE"] == str(config.secrets_dir / "github-worker")
