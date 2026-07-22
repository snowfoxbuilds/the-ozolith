"""Node Daemon test rig: fake docker, fake children, scripted control plane.

The daemon under test is the real thing except at three seams: DockerCtl is
replaced by an in-memory FakeDocker (same method surface), process children
are FakeProcess objects whose "kill" is a monkeypatched os.killpg, and the
Control Node is a scripted transport speaking the real wire format. The
supervisor's kill-the-tree behavior against REAL processes is covered
separately in test_stacks.py.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from theozolith_nodedaemon.config import DaemonConfig
from theozolith_nodedaemon.controlclient import ControlClient, ControlUnreachable
from theozolith_nodedaemon.daemon import NodeDaemon
from theozolith_nodedaemon.stacks import ProcessSupervisor

_PID = itertools.count(50_000)


class FakeProcess:
    """A Popen-shaped child; dies only when the fake killpg says so."""

    def __init__(self, args: list[str], env: dict[str, str]):
        self.args = args
        self.env = env
        self.pid = next(_PID)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


@dataclass
class FakePopen:
    """The supervisor's popen seam; registers children for fake killpg."""

    registry: dict[int, FakeProcess] = field(default_factory=dict)
    spawned: list[FakeProcess] = field(default_factory=list)

    def __call__(self, args, env=None, start_new_session=False, **_) -> FakeProcess:
        assert start_new_session, "children must own their process group (kill-the-tree)"
        process = FakeProcess(list(args), dict(env or {}))
        self.registry[process.pid] = process
        self.spawned.append(process)
        return process


class FakeDocker:
    """DockerCtl's surface, in memory."""

    def __init__(self):
        self.containers: dict[str, dict[str, str]] = {}  # run containers, by name
        self.stacks: dict[str, dict[str, Any]] = {}  # single-image stack containers
        self.compose_projects: dict[str, list[str]] = {}  # project -> file paths
        self.images: dict[str, dict[str, str]] = {}  # tag -> labels
        self.builds: list[dict[str, Any]] = []
        self.removed: list[str] = []

    def add_run_container(self, name: str, run_id: str, owner: str) -> None:
        self.containers[name] = {
            "name": name,
            "run_id": run_id,
            "owner": owner,
            "status": "Up 5 minutes",
        }

    # -- observation (DockerCtl surface) -----------------------------------

    def run_containers(self) -> list[dict[str, str]]:
        return [dict(row) for row in self.containers.values()]

    def stack_containers(self, stack: str | None = None) -> list[dict[str, str]]:
        rows = []
        for name, data in self.stacks.items():
            if stack is None or data["stack"] == stack:
                rows.append({"name": name, "state": data["state"], "status": "", **data})
        return rows

    def remove(self, name: str) -> None:
        self.removed.append(name)
        self.containers.pop(name, None)
        self.stacks.pop(name, None)

    # -- container Stacks ----------------------------------------------------

    def run_stack_container(
        self, stack, image, *, env_files, env, ports, volumes, command=None
    ) -> None:
        self.stacks[f"ozolith-stack-{stack}"] = {
            "stack": stack,
            "image": image,
            "state": "running",
            "env_files": dict(env_files),
            "env": dict(env),
            "ports": list(ports),
            "volumes": list(volumes),
            "command": list(command or []),
        }

    def compose(self, project: str, files: list[Path], verb: str) -> None:
        if verb == "up":
            self.compose_projects[project] = [str(f) for f in files]
        else:
            self.compose_projects.pop(project, None)

    def compose_ps(self, project: str) -> list[dict[str, str]]:
        if project not in self.compose_projects:
            return []
        return [{"name": f"{project}-control-1", "state": "running", "status": "Up"}]

    # -- images -----------------------------------------------------------------

    def image_exists(self, tag: str) -> bool:
        return tag in self.images

    def image_labels(self, tag: str) -> dict[str, str]:
        return dict(self.images.get(tag, {}))

    def build(self, context_dir: Path, tag: str, *, no_cache: bool = False) -> None:
        dockerfile = (Path(context_dir) / "Dockerfile").read_text(encoding="utf-8")
        labels = {}
        for line in dockerfile.splitlines():
            if line.startswith("LABEL "):
                key, _, value = line.removeprefix("LABEL ").partition("=")
                labels[key.strip()] = value.strip().strip('"')
        self.images[tag] = labels
        self.builds.append({"tag": tag, "no_cache": no_cache, "dockerfile": dockerfile})


class ScriptedControl:
    """A transport speaking the real control-plane wire format.

    Heartbeat answers pop from a queue (dicts, or ControlUnreachable to
    simulate an outage); every exchange lands in ``transcript`` —
    (method, path, request body, response body) — which the channel-invariant
    test inspects.
    """

    def __init__(self):
        self.heartbeat_answers: list[Any] = []
        self.secrets: dict[str, str] = {}
        self.denied_secrets = False
        # (version, filename) -> wheel bytes served to artifact pulls.
        self.artifacts: dict[tuple[str, str], bytes] = {}
        self.transcript: list[tuple[str, str, Any, Any]] = []

    @property
    def events(self) -> list[dict]:
        return [request for _, path, request, _ in self.transcript if path == "/events"]

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        path = url.split("/api/v1", 1)[-1]
        request = json.loads(body or b"{}")
        status, answer = self._answer(path, request)
        self.transcript.append((method, path, request, answer))
        if isinstance(answer, bytes):  # artifact pulls are raw, not JSON
            return status, answer
        return status, json.dumps(answer).encode()

    def _answer(self, path: str, request: dict) -> tuple[int, Any]:
        if path == "/nodes/register":
            return 200, {"ok": True}
        if path == "/events":
            return 200, {"ok": True}
        if path.startswith("/product/artifacts/"):
            _, _, rest = path.partition("/product/artifacts/")
            version, _, filename = rest.partition("/")
            wheel = self.artifacts.get((version, filename))
            if wheel is None:
                return 404, {"detail": f"no artifact {filename} for {version}"}
            return 200, wheel
        if path == "/heartbeats":
            if not self.heartbeat_answers:
                raise ControlUnreachable("no scripted answer")
            answer = self.heartbeat_answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return 200, answer
        if path == "/secrets/pull":
            if self.denied_secrets:
                return 403, {"detail": "node has no Stack referencing these"}
            return 200, {"secrets": {n: self.secrets[n] for n in request["names"]}}
        return 404, {"detail": f"unexpected path {path}"}


@dataclass
class Rig:
    config: DaemonConfig
    docker: FakeDocker
    popen: FakePopen
    control: ScriptedControl
    daemon: NodeDaemon
    logs: list[str]
    update_calls: list[list[str]]
    execv_calls: list[tuple[str, list[str]]]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch) -> Rig:
    popen = FakePopen()

    def fake_killpg(pid: int, sig: int) -> None:
        process = popen.registry.get(pid)
        if process is None or process.returncode is not None:
            raise ProcessLookupError(pid)
        process.returncode = -sig

    monkeypatch.setattr("theozolith_nodedaemon.stacks.os.killpg", fake_killpg)

    config = DaemonConfig(
        node="box1",
        control_url="http://control.test",
        node_token="node-token",
        tls_ca=None,
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
        heartbeat_seconds=60,
        stop_grace_seconds=0.5,
        insecure_dev=True,
        version="0.3.0",
    )
    logs: list[str] = []
    docker = FakeDocker()
    control = ScriptedControl()
    update_calls: list[list[str]] = []
    execv_calls: list[tuple[str, list[str]]] = []

    def update_runner(args, **_):
        update_calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    daemon = NodeDaemon(
        config,
        docker=docker,  # type: ignore[arg-type]
        client=ControlClient(
            config.control_url or "",
            config.node_token,
            insecure_dev=True,
            transport=control,
        ),
        supervisor=ProcessSupervisor(popen=popen, log=logs.append),
        log=logs.append,
        execv=lambda path, argv: execv_calls.append((path, list(argv))),
        update_runner=update_runner,
    )
    return Rig(config, docker, popen, control, daemon, logs, update_calls, execv_calls)


def desired(stacks: list[dict], images: list[dict] | None = None, **extra) -> dict:
    return {"commit": "abc123", "stacks": stacks, "images": images or [], **extra}


def process_stack(name: str = "worker", **overrides) -> dict:
    stack = {
        "name": name,
        "kind": "process",
        "node": "box1",
        "state": "running",
        "env": {"THEOZOLITH_REPO": "acme/sandbox"},
        "secrets": {},
        "command": f"{name}-driver --loop",
        "run_image": "",
        "image": "",
        "ports": [],
        "volumes": [],
        "compose_files": [],
    }
    stack.update(overrides)
    return stack


def container_stack(name: str = "control", **overrides) -> dict:
    stack = {
        "name": name,
        "kind": "container",
        "node": "box1",
        "state": "running",
        "env": {},
        "secrets": {},
        "command": "",
        "run_image": "",
        "image": "theozolith-control:local",
        "ports": ["8443:8443"],
        "volumes": [],
        "compose_files": [],
    }
    stack.update(overrides)
    return stack


def image_recipe(name: str = "claude-dev", setup: list[str] | None = None) -> dict:
    setup = setup if setup is not None else ["pip install uv"]
    import hashlib

    canonical = json.dumps(
        {
            "base": "ghcr.io/x/run:1.0@sha256:" + "0" * 64,
            "setup": setup,
            "knowledge_source": "",
            "knowledge_pin": "",
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return {
        "name": name,
        "base": "ghcr.io/x/run:1.0@sha256:" + "0" * 64,
        "setup": setup,
        "knowledge_source": "",
        "knowledge_pin": "",
        "tag": f"theozolith/{name}:1.0-{digest[:12]}",
        "base_digest": "sha256:" + "0" * 64,
        "instruction_hash": digest,
    }
