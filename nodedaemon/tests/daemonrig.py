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
from theozolith_nodedaemon.dockerctl import DockerError
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
        # Every compose invocation, verbatim: (project, [file paths], verb). The
        # regression tests assert a `down` carries the retained materialized
        # paths (never an empty file list), not merely that state mutated.
        self.compose_calls: list[tuple[str, list[str], str]] = []
        # Failure injection for isolation tests: a remove of a name here, or a
        # compose `down`/`up` of a project here, raises DockerError; an
        # `image_exists` of a tag here raises DockerError (a Docker daemon that
        # is unreachable/timed out during the pending-build scan, ADR-0049).
        self.fail_remove: set[str] = set()
        self.fail_compose_down: set[str] = set()
        self.fail_compose_up: set[str] = set()
        self.fail_image_exists: set[str] = set()
        # Compose projects that are present but NOT running (stopped/exited): a
        # `compose_ps` for one of these reports a non-running row, so the daemon's
        # liveness check treats it as down and brings it up again.
        self.compose_stopped: set[str] = set()

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
        if name in self.fail_remove:
            raise DockerError(f"remove failed for {name}")
        self.removed.append(name)
        self.containers.pop(name, None)
        self.stacks.pop(name, None)

    # -- container Stacks ----------------------------------------------------

    def run_stack_container(
        self, stack, image, *, env_files, env, ports, volumes, command=None, spec=""
    ) -> None:
        self.remove(f"ozolith-stack-{stack}")  # faithful to DockerCtl: rm --force first
        self.stacks[f"ozolith-stack-{stack}"] = {
            "stack": stack,
            "image": image,
            "state": "running",
            "env_files": dict(env_files),
            "env": dict(env),
            "ports": list(ports),
            "volumes": list(volumes),
            "command": list(command or []),
            # Mirrors the real LABEL_STACK_SPEC label; stack_containers surfaces
            # it as a row key, exactly as DockerCtl._ps flattens labels.
            "theozolith.spec": spec,
        }

    def compose(self, project: str, files: list[Path], verb: str) -> None:
        self.compose_calls.append((project, [str(f) for f in files], verb))
        if verb == "down" and project in self.fail_compose_down:
            raise DockerError(f"compose down failed for {project}")
        if verb == "up" and project in self.fail_compose_up:
            raise DockerError(f"compose up failed for {project}")
        if verb == "up":
            self.compose_projects[project] = [str(f) for f in files]
            self.compose_stopped.discard(project)  # a fresh up is running
        else:
            self.compose_projects.pop(project, None)
            self.compose_stopped.discard(project)

    def compose_ps(self, project: str) -> list[dict[str, str]]:
        if project not in self.compose_projects:
            return []
        running = project not in self.compose_stopped
        return [
            {
                "name": f"{project}-control-1",
                "state": "running" if running else "exited",
                "status": "Up" if running else "Exited (0)",
            }
        ]

    # -- images -----------------------------------------------------------------

    def image_exists(self, tag: str) -> bool:
        if tag in self.fail_image_exists:
            raise DockerError(f"image inspect failed for {tag}")
        return tag in self.images

    def image_labels(self, tag: str) -> dict[str, str]:
        return dict(self.images.get(tag, {}))

    def build(self, context_dir: Path, tag: str, *, no_cache: bool = False, docker_config=None):
        dockerfile = (Path(context_dir) / "Dockerfile").read_text(encoding="utf-8")
        labels = {}
        for line in dockerfile.splitlines():
            if line.startswith("LABEL "):
                key, _, value = line.removeprefix("LABEL ").partition("=")
                labels[key.strip()] = value.strip().strip('"')
        self.images[tag] = labels
        # Snapshot the DOCKER_CONFIG handed to THIS build (ADR-0049): the dir,
        # and — since the tmpfs config.json is overwritten on later passes — the
        # auths it decoded to at build time, so assertions read a stable value.
        auths = None
        if docker_config is not None:
            auths = json.loads(
                (Path(docker_config) / "config.json").read_text(encoding="utf-8")
            ).get("auths")
        self.builds.append(
            {
                "tag": tag,
                "no_cache": no_cache,
                "dockerfile": dockerfile,
                "docker_config": docker_config,
                "auths": auths,
            }
        )


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
        # drivers-hash -> config-distribution zip bytes (ADR-0042). A requested
        # hash absent here answers 409, mirroring control's repo-moved-on path.
        self.config_artifacts: dict[str, bytes] = {}
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
        if path == "/events":
            return 200, {"ok": True}
        if path.startswith("/product/artifacts/"):
            _, _, rest = path.partition("/product/artifacts/")
            version, _, filename = rest.partition("/")
            wheel = self.artifacts.get((version, filename))
            if wheel is None:
                return 404, {"detail": f"no artifact {filename} for {version}"}
            return 200, wheel
        if path.startswith("/config/artifacts/"):
            _, _, digest = path.partition("/config/artifacts/")
            artifact = self.config_artifacts.get(digest)
            if artifact is None:
                return 409, {"detail": "config distribution changed"}
            return 200, artifact
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


def make_config_dist(
    files: dict[str, str], *, built_against: str = "0.3.0", raw_metadata: bytes | None = None
) -> tuple[str, bytes]:
    """Build a config-distribution zip and its drivers-hash (ADR-0042) using the
    node-side canonical algorithm — ``files`` are ``drivers/...`` relpaths. The
    hash is computed exactly as ``manifest_hash_of_tree`` will recompute it on
    unpack, so a faithful artifact verifies; corrupt one member to force a
    mismatch. ``raw_metadata`` replaces the ``config-dist.json`` member bytes
    verbatim — the escape hatch for malformed-envelope shapes (invalid UTF-8,
    wrong format, mismatching hash) a real build would never produce; the
    drivers-hash is metadata-independent, so the returned digest still names
    the tree content."""
    import io
    import json
    import tempfile
    import zipfile

    from theozolith_nodedaemon import configdist

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for relpath, content in files.items():
            target = root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        digest = configdist.manifest_hash_of_tree(root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for relpath, content in sorted(files.items()):
            archive.writestr(relpath, content)
        if raw_metadata is None:
            raw_metadata = json.dumps(
                {
                    "format": 1,
                    "drivers_hash": digest,
                    "built_against": built_against,
                    "built_at": "1980-01-01T00:00:00+00:00",
                }
            ).encode("utf-8")
        archive.writestr(configdist.ARTIFACT_METADATA, raw_metadata)
    return digest, buffer.getvalue()


def driver_stack(name: str = "custom-impl", ref: str = "drivers/custom", **overrides) -> dict:
    """A config-distribution driver Stack as it rides the wire (ADR-0042):
    ``theozolith-driver drivers/<ref>``. Written directly (control does not yet
    resolve such Stacks) to exercise the daemon-side stop/restart on swap."""
    stack = process_stack(name=name, command=f"theozolith-driver {ref}")
    stack["env"] = {"THEOZOLITH_REPO": "acme/sandbox", "THEOZOLITH_RUN_IMAGE": "theozolith/x:1"}
    stack.update(overrides)
    return stack


def process_stack(name: str = "worker", **overrides) -> dict:
    stack = {
        "name": name,
        "kind": "process",
        "node": "box1",
        "state": "running",
        "env": {"THEOZOLITH_REPO": "acme/sandbox"},
        "secrets": {},
        "command": f"{name}-driver --loop",
        "image": "",
        "ports": [],
        "volumes": [],
        "compose_files": [],
    }
    stack.update(overrides)
    return stack


def container_stack(name: str = "flightdeck", **overrides) -> dict:
    stack = {
        "name": name,
        "kind": "container",
        "node": "box1",
        "state": "running",
        "env": {},
        "secrets": {},
        "command": "",
        "image": "theozolith-flightdeck:local",
        "ports": ["8443:8443"],
        "volumes": [],
        "compose_files": [],
    }
    stack.update(overrides)
    return stack


def image_recipe(
    name: str = "claude-dev",
    setup: list[str] | None = None,
    knowledge: str = "",
    knowledge_pin: str = "",
) -> dict:
    setup = setup if setup is not None else ["pip install uv"]
    import hashlib

    canonical = json.dumps(
        {
            "base": "ghcr.io/x/run:1.0@sha256:" + "0" * 64,
            "setup": setup,
            "knowledge": knowledge,
            "knowledge_pin": knowledge_pin,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return {
        "name": name,
        "base": "ghcr.io/x/run:1.0@sha256:" + "0" * 64,
        "setup": setup,
        "knowledge": knowledge,
        "knowledge_pin": knowledge_pin,
        "tag": f"theozolith/{name}:1.0-{digest[:12]}",
        "base_digest": "sha256:" + "0" * 64,
        "instruction_hash": digest,
    }
