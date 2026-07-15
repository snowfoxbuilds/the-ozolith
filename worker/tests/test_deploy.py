"""deploy/ artifacts: compose + .env.example cover the full config surface."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "deploy"
COMPOSE = DEPLOY / "docker-compose.yml"
ENV_EXAMPLE = DEPLOY / ".env.example"


def _compose_data() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_compose_runs_one_worker_and_the_reviewer():
    services = _compose_data()["services"]
    assert set(services) == {"worker", "reviewer"}
    assert services["worker"]["command"] == ["theozolith-worker"]
    assert services["reviewer"]["command"] == ["theozolith-reviewer"]
    # Both long-lived: the restart policy is what makes recycle-by-exit work.
    assert services["worker"]["restart"] == "unless-stopped"
    assert services["reviewer"]["restart"] == "unless-stopped"


def test_each_actor_has_its_own_identity_and_model():
    services = _compose_data()["services"]
    worker_env = services["worker"]["environment"]
    reviewer_env = services["reviewer"]["environment"]
    assert "WORKER_GITHUB_TOKEN" in worker_env["GITHUB_TOKEN"]
    assert "REVIEWER_GITHUB_TOKEN" in reviewer_env["GITHUB_TOKEN"]
    # Defaults: the Reviewer runs a stronger model than the Worker (ADR-0008).
    assert "claude-sonnet-5" in worker_env["THEOZOLITH_MODEL"]
    assert "claude-fable-5" in reviewer_env["THEOZOLITH_MODEL"]
    # The VAR_FILE convention is wired through for the secrets.
    assert "GITHUB_TOKEN_FILE" in worker_env
    assert "ANTHROPIC_API_KEY_FILE" in worker_env


def test_env_example_documents_every_compose_variable():
    referenced = set(re.findall(r"\$\{([A-Z_]+)", COMPOSE.read_text()))
    documented = set(re.findall(r"^#?([A-Z_]+)=", ENV_EXAMPLE.read_text(), re.MULTILINE))
    # _FILE variants are the convention applied to a documented variable.
    missing = {name for name in referenced - documented if not name.endswith("_FILE")}
    assert not missing, f".env.example is missing: {sorted(missing)}"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_available(), reason="docker unavailable")
def test_compose_config_resolves():
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "THEOZOLITH_REPO": "acme/sandbox",
            "WORKER_GITHUB_TOKEN": "x",
            "REVIEWER_GITHUB_TOKEN": "y",
            "ANTHROPIC_API_KEY": "z",
        },
    )
    assert proc.returncode == 0, proc.stderr
