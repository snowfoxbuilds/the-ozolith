"""deploy/ artifacts, the run-container image, and the CI image-build job."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "deploy"
DOCKERFILE = REPO_ROOT / "worker" / "docker" / "Dockerfile.claude"
CONTROL_DOCKERFILE = REPO_ROOT / "control" / "docker" / "Dockerfile"
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
    # One generic launcher per driver (ADR-0020): the unit names the built-in
    # worker type by ref, never a per-type console script.
    for role in ("implementer", "reviewer"):
        unit = (DEPLOY / "systemd" / f"theozolith-{role}.service").read_text()
        assert f"theozolith-driver builtin:{role}" in unit
        assert "EnvironmentFile=" in unit
        assert "Restart=on-failure" in unit
        assert "M3" in unit  # explicitly a convenience until daemon supervision


def test_run_image_contract():
    dockerfile = DOCKERFILE.read_text()
    # PID 1 is the harness; the actors never run in this image.
    assert 'ENTRYPOINT ["theozolith-harness"]' in dockerfile
    assert "theozolith-driver" not in re.findall(r"ENTRYPOINT.*|CMD.*", dockerfile)
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
    assert "ozjoin" in installer  # the join string is the one input
    assert "usermod -aG docker ozolith" in installer
    assert "read -r -s" not in installer  # no token prompting remains
    assert "theozolith join-token create" in installer  # the refusal says where to go
    # Steps after the last comment: pip install precedes provision.
    assert installer.index("pip install") < installer.index('provision "$JOIN"')


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
    """NODE-SUBSTRATE.md: Tailscale is a private-side deployment detail — never
    in product code, product IMAGES, or deploy scripts. Per-container tailnet
    identity enters only via a worker type's setup instructions in the Config
    Repo (ADR-0043); the ONE sanctioned home for the string is
    ``deploy/configs-example/**`` (and its README), which this scan
    deliberately does not touch. The product Dockerfiles are in scope: a
    tailscaled baked into a base image would violate the doctrine just as
    surely as product source would."""
    for component in ("worker", "control", "nodedaemon", "knowledge"):
        for path in (REPO_ROOT / component / "src").rglob("*.py"):
            assert "tailscale" not in path.read_text().lower(), path
    for dockerfile in (DOCKERFILE, CONTROL_DOCKERFILE):
        assert "tailscale" not in dockerfile.read_text().lower(), dockerfile
    for name in ("install-nodedaemon.sh", "compose/control.yml", "README.md"):
        assert "tailscale" not in (DEPLOY / name).read_text().lower(), name


def test_configs_example_parses_and_places_the_builtin_stacks():
    """The starter Config Repo must stay valid: worker/reviewer as process
    Stacks, the Flight Deck as a container Stack (ADR-0013/0019). Control is
    never a Stack — the substrate never supervises its own control plane
    (ADR-0035) — so the example must not carry one."""
    from theozolith_control.configrepo import load_config

    config = load_config(REPO_ROOT / "deploy" / "configs-example")
    kinds = {stack.name: stack.kind for stack in config.stacks}
    assert kinds == {
        "implementer": "process",
        "reviewer": "process",
        "flightdeck": "container",
    }
    assert config.product_version
    assert "claude-dev" in config.worker_types
    # The Implementer Stack's node gets exactly its referenced secrets (the
    # worker type owns them, ADR-0044).
    implementer = next(s for s in config.stacks if s.name == "implementer")
    assert config.secret_names_for(implementer.node) >= {"github-implementer", "anthropic-api-key"}
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


def test_configs_example_flightdeck_knowledge_and_tailscale_wiring():
    """ADR-0043 + issue #20 §1: the example Flight Deck wires per-instance
    runtime state + tailnet identity and ONE shared knowledge clone, bakes the
    knowledge symlinks and the userspace-tailscale start sequence, and keeps
    the carve-out Flight-Deck-only."""
    from theozolith_control.configrepo import load_config

    config = load_config(REPO_ROOT / "deploy" / "configs-example")
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")

    # Per-instance state + tailnet volumes (resolved from {stack}); exactly one
    # SHARED knowledge-* clone that is deliberately NOT per-instance.
    assert "flightdeck-claude-state:/home/ozolith/.claude" in flightdeck.volumes
    assert "flightdeck-tailscale-state:/var/lib/tailscale" in flightdeck.volumes
    knowledge_mounts = [v for v in flightdeck.volumes if v.split(":")[0].startswith("knowledge-")]
    assert len(knowledge_mounts) == 1
    assert "{stack}" not in knowledge_mounts[0]  # shared across siblings, not per-instance

    wt = config.worker_types["flightdeck"]
    assert wt.knowledge_source == ""  # never baked; the clone is live (ADR-0043)
    script = "\n".join(wt.setup)

    # clone-init + all four symlinks are baked into flightdeck-start.
    assert "theozolith-knowledge clone-init" in script
    for target in (
        "ln -sfnT /home/ozolith/knowledge/skills",
        "ln -sfnT /home/ozolith/knowledge/agents/claude",
        "ln -sfnT /home/ozolith/knowledge/workflows",
        "ln -sfnT /home/ozolith/knowledge/AGENTS.md",
    ):
        assert target in script, target
    for claude_dir in (
        "/home/ozolith/.claude/skills",
        "/home/ozolith/.claude/agents",
        "/home/ozolith/.claude/workflows",
        "/home/ozolith/.claude/CLAUDE.md",
    ):
        assert claude_dir in script, claude_dir

    # The tailscale download is pinned twice — a concrete version AND a sha256
    # verification — and the start sequence is userspace + Tailscale SSH with
    # the file:-form auth key consumed only on an empty state volume.
    assert "1.80.0" in script and "sha256sum -c" in script
    assert "--tun=userspace-networking" in script
    assert "--ssh" in script
    assert "file:${TS_AUTHKEY_FILE}" in script
    assert "if [ -s /var/lib/tailscale/tailscaled.state ]; then" in script

    # The carve-out is Flight-Deck-only: no OTHER stack or worker type mounts a
    # knowledge-* clone or any .claude path.
    for stack in config.stacks:
        if stack.name == "flightdeck":
            continue
        for volume in stack.volumes:
            assert "knowledge-" not in volume and ".claude" not in volume, (stack.name, volume)
    for name, other in config.worker_types.items():
        if name == "flightdeck":
            continue
        for volume in other.volumes:
            assert "knowledge-" not in volume and ".claude" not in volume, (name, volume)

    # The tailscale auth-key secret is the flightdeck type's alone — disjoint
    # from every driver PAT/secret.
    driver_secret_names = {
        n for s in config.stacks if s.kind == "process" for n in s.secrets.values()
    }
    assert flightdeck.secrets["TS_AUTHKEY"] == "flightdeck-tailscale-authkey"
    assert "flightdeck-tailscale-authkey" not in driver_secret_names
