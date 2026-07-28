"""deploy/ artifacts, the run-container image, and the CI image-build job."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "deploy"
DOCKERFILE = REPO_ROOT / "worker" / "docker" / "Dockerfile.claude"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_compose_no_longer_runs_the_actors():
    """ADR-0013: run containers are created by the drivers, not compose."""
    assert not (DEPLOY / "docker-compose.yml").exists()


def test_dot_env_is_no_longer_a_user_facing_surface():
    """ADR-0023 deletion test: `.env`-driven setup is gone — no example
    file ships, the installer writes none, and the compose stub needs none.
    (Env vars survive only as validated expert overrides.)"""
    assert not (DEPLOY / ".env.example").exists()
    assert "/etc/theozolith/.env" not in (DEPLOY / "install-nodedaemon.sh").read_text()
    assert "env_file" not in (DEPLOY / "compose" / "control.yml").read_text()
    # The dev-shape documentation kept its non-negotiables.
    readme = (DEPLOY / "README.md").read_text()
    assert "different GitHub identities" in readme  # no self-grading (ADR-0008)
    assert "VAR_FILE" in readme  # the secrets convention is documented


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


def test_nodedaemon_unit_enforces_kill_the_tree():
    """The unit is embedded in the installer (curl|bash needs no sidecar
    files); its cgroup and directory contract is unchanged — but there is
    no EnvironmentFile: configuration is the provisioned state dir."""
    installer = (DEPLOY / "install-nodedaemon.sh").read_text()
    assert "KillMode=control-group" in installer  # ADR-0013: no zombie processes
    assert "RuntimeDirectory=theozolith" in installer  # secrets tmpfs under /run
    assert "StateDirectory=theozolith" in installer
    assert "EnvironmentFile" not in installer
    assert "Restart=always" in installer
    assert "User=ozolith" in installer  # never root
    assert not (DEPLOY / "systemd" / "theozolith-nodedaemon.service").exists()


def test_installer_hands_off_to_provision_as_its_final_step():
    """ADR-0023 installer consolidation: the manual-configuration half is
    gone — the installer installs the distribution and unit, then runs
    `theozolith-nodedaemon provision <join-string>`; a run without a join
    string is refused (no fingerprint-less manual path)."""
    installer = (DEPLOY / "install-nodedaemon.sh").read_text()
    assert "theozolith-nodedaemon provision" in installer
    assert 'ozjoin' in installer  # the join string is the one input
    assert "usermod -aG docker ozolith" in installer
    assert "read -r -s" not in installer  # no token prompting remains
    assert "theozolith join-token create" in installer  # the refusal says where to go
    # Steps after the last comment: pip install precedes provision.
    assert installer.index("pip install") < installer.index("provision \"$JOIN\"")


def test_control_compose_mounts_the_partitioned_home():
    compose = (DEPLOY / "compose" / "control.yml").read_text()
    assert "~/.theozolith}:/data" in compose  # ADR-0024: the one home
    assert "THEOZOLITH_DATA_DIR: /data" in compose
    assert "run --rm control init" in compose  # the unified first run (ADR-0023)
    assert "8443" in compose
    assert "6965:6965" in compose  # the bootstrap listener rides its own port


def test_ci_builds_the_control_image():
    assert "docker build -f control/docker/Dockerfile" in CI.read_text()


def test_no_tailscale_anywhere_in_product_code_or_deploy():
    """NODE-SUBSTRATE.md: Tailscale is a private-side deployment detail —
    never in product code, images, or deploy scripts (overlays may name it
    as an example of the extension point, nothing more)."""
    for component in ("worker", "control", "nodedaemon", "knowledge"):
        for path in (REPO_ROOT / component / "src").rglob("*.py"):
            assert "tailscale" not in path.read_text().lower(), path
    for name in ("install-nodedaemon.sh", "compose/control.yml", "README.md"):
        assert "tailscale" not in (DEPLOY / name).read_text().lower(), name


def test_configs_example_parses_and_places_the_builtin_stacks():
    """The starter Config Repo must stay valid: worker/reviewer as process
    Stacks, control and the Flight Deck as container Stacks (ADR-0013/0019)."""
    from theozolith_control.configrepo import load_config

    config = load_config(REPO_ROOT / "deploy" / "configs-example")
    kinds = {stack.name: stack.kind for stack in config.stacks}
    assert kinds == {
        "worker": "process",
        "reviewer": "process",
        "control": "container",
        "flightdeck": "container",
    }
    assert config.product_version
    assert "claude-dev" in config.images
    # The worker Stack's node gets exactly its referenced secrets.
    worker = next(s for s in config.stacks if s.name == "worker")
    assert config.secret_names_for(worker.node) >= {"github-worker", "anthropic-api-key"}
    # Desired state renders (compose text inlines) for every placed node.
    for node in {stack.node for stack in config.stacks}:
        state = config.desired_state_for(node)
        assert state["commit"]
    # ADR-0019: run containers are never attach targets — no process Stack
    # carries an attach command (the parser enforces it; this pins the
    # example). The Flight Deck is the attach target, under its own
    # dedicated machine-identity secret, distinct from every driver PAT.
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")
    assert flightdeck.attach and "tmux" in flightdeck.attach
    driver_secrets = {
        name
        for stack in config.stacks
        if stack.kind == "process"
        for name in stack.secrets.values()
    }
    assert flightdeck.secrets["GITHUB_TOKEN"] == "flightdeck-github-token"
    assert "flightdeck-github-token" not in driver_secrets
