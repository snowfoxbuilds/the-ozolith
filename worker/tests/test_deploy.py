"""deploy/ artifacts, the run-container image, and the CI image-build job."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "deploy"
ENV_EXAMPLE = DEPLOY / ".env.example"
DOCKERFILE = REPO_ROOT / "worker" / "docker" / "Dockerfile.claude"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def documented_env_names() -> set[str]:
    return set(re.findall(r"^#?([A-Z_]+)=", ENV_EXAMPLE.read_text(), re.MULTILINE))


def test_compose_no_longer_runs_the_actors():
    """ADR-0013: run containers are created by the drivers, not compose."""
    assert not (DEPLOY / "docker-compose.yml").exists()


def test_env_example_covers_the_driver_config_surface():
    documented = documented_env_names()
    required = {
        "THEOZOLITH_REPO",
        "WORKER_GITHUB_TOKEN",
        "REVIEWER_GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "THEOZOLITH_RUN_IMAGE",
        "WORKER_MODEL",
        "REVIEWER_MODEL",
        "THEOZOLITH_JOBS_DIR",
        "THEOZOLITH_CACHE_VOLUMES",
        "CONTROL_NODE_URL",
        "POLL_SECONDS",
    }
    missing = required - documented
    assert not missing, f".env.example is missing: {sorted(missing)}"
    # Distinct identities are the no-self-grading precondition (ADR-0008).
    text = ENV_EXAMPLE.read_text()
    assert "DIFFERENT GitHub identities" in text
    assert "VAR_FILE" in text  # the secrets convention is documented


def test_systemd_units_exist_for_both_drivers():
    for role in ("worker", "reviewer"):
        unit = (DEPLOY / "systemd" / f"theozolith-{role}.service").read_text()
        assert f"theozolith-{role}" in unit
        assert "EnvironmentFile=" in unit
        assert "Restart=on-failure" in unit
        assert "M3" in unit  # explicitly a convenience until daemon supervision


def test_run_image_contract():
    dockerfile = DOCKERFILE.read_text()
    # PID 1 is the harness; the actors never run in this image.
    assert 'ENTRYPOINT ["theozolith-harness"]' in dockerfile
    assert "theozolith-worker" not in re.findall(r"ENTRYPOINT.*|CMD.*", dockerfile)
    # The agent session needs tmux; the agent must not run as root.
    assert "tmux" in dockerfile
    assert "USER ozolith" in dockerfile
    assert "OZOLITH_UID" in dockerfile  # job-dir ownership knob
    # Knowledge Source is baked at BUILD time (never at container start).
    assert "theozolith-knowledge bake" in dockerfile


def test_ci_builds_the_run_container_image():
    """M2 brief: the CI must build the run-container image so image rot is
    caught (a PR #2 review finding, absorbed here)."""
    ci = CI.read_text()
    assert "docker build -f worker/docker/Dockerfile.claude" in ci
