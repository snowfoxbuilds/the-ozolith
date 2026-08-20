"""The channel invariant (acceptance 9): a transcript of everything that
crosses the heartbeat/command channel contains desired state and references
only — the sole value payload is the node-scoped secrets-pull response.

The REAL Node Daemon reconciles against the REAL control-plane app for
several passes (per-node-token heartbeats with status, a queued command and
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
from theozolith_control.secretstore import SecretStore
from theozolith_control.settings import ControlSettings
from theozolith_control.store import Store
from theozolith_nodedaemon.config import DaemonConfig
from theozolith_nodedaemon.controlclient import ControlClient
from theozolith_nodedaemon.daemon import NodeDaemon
from theozolith_nodedaemon.stacks import ProcessSupervisor

SECRET_VALUE = "ghp_THEACTUALSECRETVALUE999"
REGISTRY_VALUE = "ozolith-bot:ghp_REGISTRYCREDENTIAL777"

KNOWN_CHANNEL_PATHS = {
    "/api/v1/heartbeats",
    "/api/v1/secrets/pull",
    # theozolith.error summaries (2026-07-21 grilling): references and
    # size-capped text only, never secret values.
    "/api/v1/events",
    # Built-artifact pulls (ADR-0015, 2026-07-22) use
    # /api/v1/product/artifacts/<version>/<filename> — wheel bytes flow
    # control->node only; the daemon sends nothing but the bearer header.
}

HEARTBEAT_REQUEST_KEYS = {
    "node",
    "version",
    "stacks",
    "run_containers",
    "stack_containers",  # web-terminal target evidence (ADR-0019)
    "images",
    "config_commit",
    "drivers_hash",  # applied config-distribution reference (ADR-0042)
    "drivers_built_against",  # advisory stamp — a product version, never a value
    "completed_commands",
    "deferred_commands",  # queue-behind visibility (references, no values)
    # Desired-state convergence deferrals (issue #8): run-id/stack-name
    # references only, so the control-side ladders pause on a drain.
    "update_deferred",
    "drivers_deferred",
}


def test_channel_transcript_is_desired_state_and_references_only(tmp_path: Path, monkeypatch):
    # -- a real Control Node with a secret-referencing worker Stack ---------
    settings = ControlSettings(
        data_dir=tmp_path / "data",
        config_repo=tmp_path / "configs",
        admin_token="admin-token",
        repo=None,
        github_token=None,
        api_url="",
        secrets_channel_ok=True,  # TLS-mandatory is proven in test_tls.py
    )
    # A worker-types Config Repo (ADR-0044): a thin Stack + its worker type.
    # The resolved process Stack still references its secret by NAME only.
    wt_toml = tmp_path / "configs" / "worker-types" / "claude-dev.toml"
    wt_toml.parent.mkdir(parents=True)
    wt_toml.write_text(
        'driver = "builtin:implementer"\nadapter = "claude"\nmodel = "claude-sonnet-5"\n'
        'workspace = "acme/sandbox"\n'
        f'base = "ghcr.io/x/run:1.0@sha256:{"0" * 64}"\n'
        '[secrets]\nIMPLEMENTER_GITHUB_TOKEN = "github-implementer"\n',
        encoding="utf-8",
    )
    stack_toml = tmp_path / "configs" / "stacks" / "worker.toml"
    stack_toml.parent.mkdir(parents=True)
    stack_toml.write_text(
        'worker_type = "claude-dev"\nnode = "box1"\n',
        encoding="utf-8",
    )
    store = Store(settings.cache_db_path)
    secret_store = SecretStore(settings.store_db_path)
    box = SecretBox(generate_key())
    secret_store.put_secret("github-implementer", box.encrypt(SECRET_VALUE))
    # Provisioning is registration (ADR-0023): the daemon presents the
    # per-node token the join exchange minted.
    node_token = secret_store.mint_node_token("box1")
    store.queue_command("box1", "recycle", "worker")
    web = TestClient(create_app(settings, store, secret_store, box))

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
        node_token=node_token,
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
            "http://control.test", node_token, insecure_dev=True, transport=tapped_transport
        ),
        supervisor=ProcessSupervisor(popen=popen, log=lambda *_: None),
        log=lambda *_: None,
    )
    daemon.once()  # heartbeat + recycle command + pull + start
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
            assert stacks and stacks[0]["secrets"] == {
                "IMPLEMENTER_GITHUB_TOKEN": "github-implementer"
            }

    pulls = [e for e in transcript if e[1] == "/api/v1/secrets/pull"]
    assert len(pulls) == 1  # pulled at deploy time, not per heartbeat
    assert json.loads(pulls[0][2]) == {"node": "box1", "names": ["github-implementer"]}
    assert json.loads(pulls[0][3]) == {"secrets": {"github-implementer": SECRET_VALUE}}

    # And the daemon really deployed with it: value in tmpfs, _FILE wiring.
    assert (config.secrets_dir / "github-implementer").read_text() == SECRET_VALUE
    env = popen.spawned[-1].env
    assert env["IMPLEMENTER_GITHUB_TOKEN_FILE"] == str(config.secrets_dir / "github-implementer")


def test_registry_credential_value_only_crosses_on_the_pull(tmp_path: Path, monkeypatch):
    """The channel invariant extends to the ADR-0049 registry pull credential:
    the heartbeat carries the names-only ``registry_secrets`` reference, and
    the ``<user>:<token>`` value crosses ONLY inside the node-scoped
    secrets-pull response — the same boundary every Stack secret obeys."""
    import base64

    settings = ControlSettings(
        data_dir=tmp_path / "data",
        config_repo=tmp_path / "configs",
        admin_token="admin-token",
        repo=None,
        github_token=None,
        api_url="",
        secrets_channel_ok=True,
    )
    # A running worker-type Stack with a PRIVATE ghcr.io base: its derived image
    # rides desired state, so the node builds it and needs the pull credential.
    wt_toml = tmp_path / "configs" / "worker-types" / "claude-dev.toml"
    wt_toml.parent.mkdir(parents=True)
    wt_toml.write_text(
        'driver = "builtin:implementer"\nadapter = "claude"\nmodel = "claude-sonnet-5"\n'
        'workspace = "acme/sandbox"\n'
        f'base = "ghcr.io/snowfoxbuilds/run:1.0@sha256:{"0" * 64}"\n'
        '[secrets]\nIMPLEMENTER_GITHUB_TOKEN = "github-implementer"\n',
        encoding="utf-8",
    )
    stack_toml = tmp_path / "configs" / "stacks" / "worker.toml"
    stack_toml.parent.mkdir(parents=True)
    stack_toml.write_text('worker_type = "claude-dev"\nnode = "box1"\n', encoding="utf-8")
    store = Store(settings.cache_db_path)
    secret_store = SecretStore(settings.store_db_path)
    box = SecretBox(generate_key())
    secret_store.put_secret("github-implementer", box.encrypt(SECRET_VALUE))
    secret_store.put_secret("registry:ghcr.io", box.encrypt(REGISTRY_VALUE))
    node_token = secret_store.mint_node_token("box1")
    web = TestClient(create_app(settings, store, secret_store, box))

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
        node_token=node_token,
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
            "http://control.test", node_token, insecure_dev=True, transport=tapped_transport
        ),
        supervisor=ProcessSupervisor(popen=popen, log=lambda *_: None),
        log=lambda *_: None,
    )
    daemon.once()

    # The value crosses ONLY on a secrets-pull response, never up, never on any
    # other path (heartbeat/events included).
    for _method, path, request, response in transcript:
        assert REGISTRY_VALUE.encode() not in request
        if path != "/api/v1/secrets/pull":
            assert REGISTRY_VALUE.encode() not in response

    # The heartbeat answer references the credential by NAME only.
    heartbeats = [e for e in transcript if e[1] == "/api/v1/heartbeats"]
    reg = json.loads(heartbeats[0][3])["config"]["registry_secrets"]
    assert reg == {"ghcr.io": "registry:ghcr.io"}

    # The value crossed exactly once, in the node-scoped pull response.
    reg_pulls = [
        e for e in transcript if e[1] == "/api/v1/secrets/pull" and b"registry:ghcr.io" in e[2]
    ]
    assert len(reg_pulls) == 1
    assert json.loads(reg_pulls[0][3]) == {"secrets": {"registry:ghcr.io": REGISTRY_VALUE}}

    # And the daemon built the derived image under a DOCKER_CONFIG that decodes
    # to the credential — the acceptance-3 path end to end.
    auth = base64.b64encode(REGISTRY_VALUE.encode()).decode()
    assert daemon._docker.builds[-1]["auths"] == {"ghcr.io": {"auth": auth}}
