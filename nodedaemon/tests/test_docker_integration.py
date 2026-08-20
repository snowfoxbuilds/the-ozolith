"""Real-engine contracts a fake docker cannot prove (PR #35 corrections):
entrypoint-override semantics against an image that actually carries an
inherited ENTRYPOINT, and cross-UID readability of a materialized secret
through a read-only bind mount.

Skips ONLY when a Docker engine is genuinely unavailable (the dev container
has none — no socket, and seccomp denies namespaces); GitHub CI's ubuntu
runners have one, so both tests run on every PR.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid

import pytest
from theozolith_nodedaemon.dockerctl import DockerCtl, DockerError
from theozolith_nodedaemon.stacks import materialize_secrets

BASE_IMAGE = "busybox:stable"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(["docker", "info"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="docker engine genuinely unavailable (dev container); runs in GitHub CI",
)


def _docker(*args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _logs_containing(container: str, marker: str, deadline_seconds: float = 30) -> str:
    """The container's log once ``marker`` appears — polled, because a
    detached start returns before pid 1 has written anything."""
    deadline = time.monotonic() + deadline_seconds
    logs = ""
    while time.monotonic() < deadline:
        logs = _docker("logs", container, timeout=60).stdout
        if marker in logs:
            return logs
        time.sleep(0.5)
    raise AssertionError(f"{marker!r} never appeared in {container} logs; got: {logs!r}")


@pytest.fixture
def entrypoint_image(tmp_path):
    """A minimal image whose ENTRYPOINT stands in for the run image's
    ``theozolith-harness`` — anything appended after the image would reach it
    as argv, exactly the defect that kept the Flight Deck from starting."""
    tag = f"ozolith-test-entrypoint:{uuid.uuid4().hex[:12]}"
    (tmp_path / "Dockerfile").write_text(
        f'FROM {BASE_IMAGE}\nENTRYPOINT ["/bin/echo", "inherited-harness-entrypoint"]\n'
    )
    build = _docker("build", "--tag", tag, str(tmp_path))
    assert build.returncode == 0, build.stderr
    yield tag
    _docker("rmi", "--force", tag, timeout=60)


@pytest.fixture
def stack_name():
    name = f"citest-{uuid.uuid4().hex[:12]}"
    yield name
    _docker("rm", "--force", f"ozolith-stack-{name}", timeout=60)


def test_configured_command_replaces_a_real_inherited_entrypoint(entrypoint_image, stack_name):
    """Against a real engine: the configured command IS pid 1 (inspect
    ``.Path``/``.Args``), it executes (its output reaches the log), and the
    image's inherited ENTRYPOINT never runs — the regression where the start
    command was appended as the harness entrypoint's argument."""
    DockerCtl().run_stack_container(
        stack_name,
        entrypoint_image,
        env_files={},
        env={},
        ports=[],
        volumes=[],
        command=["/bin/sh", "-c", "echo configured-command-ran && sleep 300"],
    )
    container = f"ozolith-stack-{stack_name}"
    inspect = _docker("inspect", "--format", "{{.Path}}\t{{json .Args}}", container, timeout=60)
    assert inspect.returncode == 0, inspect.stderr
    path, _, args_json = inspect.stdout.strip().partition("\t")
    assert path == "/bin/sh"  # pid 1 is the configured command…
    assert "configured-command-ran" in args_json
    logs = _logs_containing(container, "configured-command-ran")
    # …and the inherited entrypoint's marker never printed.
    assert "inherited-harness-entrypoint" not in logs


def test_no_command_runs_the_inherited_entrypoint_unchanged(entrypoint_image, stack_name):
    """No configured command: the image's own ENTRYPOINT/CMD run exactly as
    built (its marker prints; pid 1 is the inherited entrypoint)."""
    DockerCtl().run_stack_container(
        stack_name,
        entrypoint_image,
        env_files={},
        env={},
        ports=[],
        volumes=[],
        command=None,
    )
    container = f"ozolith-stack-{stack_name}"
    inspect = _docker("inspect", "--format", "{{.Path}}", container, timeout=60)
    assert inspect.returncode == 0, inspect.stderr
    assert inspect.stdout.strip() == "/bin/echo"
    _logs_containing(container, "inherited-harness-entrypoint")


def test_build_honors_the_docker_config_env(tmp_path):
    """Against a real engine: DockerCtl.build genuinely plumbs DOCKER_CONFIG
    into the build subprocess (ADR-0049). A malformed config.json in the
    pointed-at dir makes the docker CLI fail at config load — before any
    build — so a private-base pull credential written there is truly in
    effect; the same trivial context builds fine with no DOCKER_CONFIG."""
    context = tmp_path / "ctx"
    context.mkdir()
    (context / "Dockerfile").write_text(f"FROM {BASE_IMAGE}\n")
    tag = f"ozolith-test-dockercfg:{uuid.uuid4().hex[:12]}"
    bad = tmp_path / "docker"
    bad.mkdir()
    (bad / "config.json").write_text("this is not valid json")
    try:
        # Baseline: the ambient (or empty) config builds the trivial context.
        DockerCtl().build(context, tag)
        # With DOCKER_CONFIG pointed at the malformed dir, the CLI errors at
        # config load — proof the env var reached the build subprocess.
        with pytest.raises(DockerError):
            DockerCtl().build(context, tag, docker_config=bad)
    finally:
        _docker("rmi", "--force", tag, timeout=60)


def test_materialized_secret_is_readable_by_uid_1000_through_a_ro_bind_mount(tmp_path):
    """The 0700-directory/0444-leaf boundary, proven across the real uid
    boundary: a dummy secret materialized by the HOST test user is readable
    by container uid 1000 through the same read-only single-file bind mount
    DockerCtl issues — and stays unwritable (the mount is ro)."""
    paths = materialize_secrets(
        tmp_path / "secrets", {"dummy-cross-uid": "cross-uid-readability-proof"}
    )
    leaf = paths["dummy-cross-uid"]
    mount = f"{leaf}:/run/secrets/dummy-cross-uid:ro"
    read = _docker(
        "run",
        "--rm",
        "--user",
        "1000:1000",
        "--volume",
        mount,
        BASE_IMAGE,
        "cat",
        "/run/secrets/dummy-cross-uid",
    )
    assert read.returncode == 0, read.stderr
    assert read.stdout == "cross-uid-readability-proof"
    write = _docker(
        "run",
        "--rm",
        "--user",
        "1000:1000",
        "--volume",
        mount,
        BASE_IMAGE,
        "sh",
        "-c",
        "echo overwrite > /run/secrets/dummy-cross-uid",
    )
    assert write.returncode != 0  # read-only means read-only, even at 0444
