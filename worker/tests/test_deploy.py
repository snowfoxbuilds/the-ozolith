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
    # Headless sessions (ADR-0019): no tmux anywhere in the run image — the
    # session is a one-shot process and the container is never an attach
    # target. The agent must not run as root.
    assert "tmux" not in dockerfile
    assert "USER ozolith" in dockerfile
    assert "OZOLITH_UID" in dockerfile  # job-dir ownership knob
    # Knowledge Source is baked at BUILD time (never at container start).
    assert "theozolith-knowledge bake" in dockerfile


def test_ci_builds_the_run_container_image():
    """M2 brief: the CI must build the run-container image so image rot is
    caught (a PR #2 review finding, absorbed here)."""
    ci = CI.read_text()
    assert "docker build -f worker/docker/Dockerfile.claude" in ci


# -- M3 substrate artifacts -------------------------------------------------------


def test_env_example_covers_the_substrate_config_surface():
    documented = documented_env_names()
    required = {
        "CONTROL_NODE_URL",
        "THEOZOLITH_NODE_TOKEN",
        "THEOZOLITH_ADMIN_TOKEN",
        "THEOZOLITH_TLS_CA",
        "THEOZOLITH_NODE_NAME",
        "CONTROL_GITHUB_TOKEN",
        "THEOZOLITH_CONTROL_DATA",
        "THEOZOLITH_CONFIG_REPO",
        "THEOZOLITH_HEARTBEAT_SECONDS",
        "THEOZOLITH_ZOMBIE_GRACE_SECONDS",
        "THEOZOLITH_JANITOR_SWEEP_SECONDS",
        "THEOZOLITH_ACTIVATION_WINDOW_SECONDS",
        "THEOZOLITH_TAIL_BUDGET_BYTES",
        "THEOZOLITH_PROGRESS_SECONDS",
        "THEOZOLITH_STOP_GRACE_SECONDS",
        "THEOZOLITH_STATE_DIR",
        "THEOZOLITH_RUNTIME_DIR",
    }
    missing = required - documented
    assert not missing, f".env.example is missing: {sorted(missing)}"


def test_nodedaemon_unit_enforces_kill_the_tree():
    unit = (DEPLOY / "systemd" / "theozolith-nodedaemon.service").read_text()
    assert "KillMode=control-group" in unit  # ADR-0013: no zombie processes
    assert "RuntimeDirectory=theozolith" in unit  # secrets tmpfs under /run
    assert "StateDirectory=theozolith" in unit
    assert "EnvironmentFile=" in unit
    assert "Restart=always" in unit
    assert "User=ozolith" in unit  # never root


def test_installer_provisions_tls_and_the_unit():
    installer = (DEPLOY / "install-nodedaemon.sh").read_text()
    assert "--ca" in installer and "ca.pem" in installer  # TLS provisioning
    assert "systemctl enable --now theozolith-nodedaemon" in installer
    assert "THEOZOLITH_NODE_TOKEN" in installer
    assert "usermod -aG docker ozolith" in installer
    # Tokens never travel through argv.
    assert "read -r -s" in installer


def test_control_compose_mounts_data_and_the_config_repo():
    compose = (DEPLOY / "compose" / "control.yml").read_text()
    assert "control-data:/data" in compose
    assert ":/configs:ro" in compose
    assert "tls-init" in compose  # the mandatory-TLS bootstrap is documented
    assert "8443" in compose


def test_ci_builds_the_control_image():
    assert "docker build -f control/docker/Dockerfile" in CI.read_text()


def test_no_tailscale_anywhere_in_product_code_or_deploy():
    """NODE-SUBSTRATE.md: Tailscale is a private-side deployment detail —
    never in product code, images, or deploy scripts (overlays may name it
    as an example of the extension point, nothing more)."""
    for component in ("worker", "control", "nodedaemon", "knowledge"):
        for path in (REPO_ROOT / component / "src").rglob("*.py"):
            assert "tailscale" not in path.read_text().lower(), path
    for name in ("install-nodedaemon.sh", "compose/control.yml", ".env.example"):
        assert "tailscale" not in (DEPLOY / name).read_text().lower(), name


def test_configs_example_parses_and_places_the_builtin_stacks():
    """The starter Config Repo must stay valid: worker/reviewer as process
    Stacks, control as a container Stack (ADR-0013)."""
    from theozolith_control.configrepo import load_config

    config = load_config(REPO_ROOT / "deploy" / "configs-example")
    kinds = {stack.name: stack.kind for stack in config.stacks}
    assert kinds == {"worker": "process", "reviewer": "process", "control": "container"}
    assert config.product_version
    assert "claude-dev" in config.images
    # The worker Stack's node gets exactly its referenced secrets.
    worker = next(s for s in config.stacks if s.name == "worker")
    assert config.secret_names_for(worker.node) >= {"github-worker", "anthropic-api-key"}
    # Desired state renders (compose text inlines) for every placed node.
    for node in {stack.node for stack in config.stacks}:
        state = config.desired_state_for(node)
        assert state["commit"]
