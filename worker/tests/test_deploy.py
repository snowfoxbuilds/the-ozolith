"""deploy/ artifacts, the run-container image, and the CI image-build job."""

from __future__ import annotations

import os
import re
import subprocess
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


def test_configs_example_flightdeck_knowledge_wiring():
    """ADR-0043: the example Flight Deck wires per-instance runtime state and
    ONE shared knowledge clone, bakes the knowledge symlinks into
    flightdeck-start, and keeps the carve-out Flight-Deck-only. The one-hop
    tailnet half was split out to issue #31 (gated on the #24 Step 0 spike):
    until it lands with spike evidence, the example ships no tailscale content
    at all."""
    from theozolith_control.configrepo import load_config

    config = load_config(REPO_ROOT / "deploy" / "configs-example")
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")

    # Per-instance state + logs (resolved from {stack}); exactly one SHARED
    # knowledge-* clone that is deliberately NOT per-instance — and nothing else.
    assert set(flightdeck.volumes) == {
        "flightdeck-logs:/var/log/flightdeck",
        "flightdeck-claude-state:/home/ozolith/.claude",
        "knowledge-claude-dev:/home/ozolith/knowledge",
    }

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

    # The split-out is complete: no tailscale content anywhere in the example
    # (its sanctioned home once #31 lands behind the spike), no auth-key
    # secret, no tailnet env on the Stack.
    assert "TS_AUTHKEY" not in flightdeck.secrets
    for path in (REPO_ROOT / "deploy" / "configs-example").rglob("*"):
        if path.is_file():
            assert "tailscale" not in path.read_text().lower(), path


# -- flightdeck-start: the generated script is EXECUTED, not just grepped ---------


def _generate_flightdeck_start(tmp_path: Path) -> Path:
    """Run the worker type's script-writing setup entry in a real /bin/sh —
    exactly what the image build does — with the baked destination redirected
    into tmp_path, and return the generated script."""
    from theozolith_control.configrepo import load_config

    config = load_config(REPO_ROOT / "deploy" / "configs-example")
    wt = config.worker_types["flightdeck"]
    generators = [s for s in wt.setup if "/usr/local/bin/flightdeck-start" in s]
    assert len(generators) == 1
    dest = tmp_path / "flightdeck-start"
    command = generators[0].replace("/usr/local/bin/flightdeck-start", str(dest))
    subprocess.run(["/bin/sh", "-c", command], check=True, capture_output=True, text=True)
    assert dest.stat().st_mode & 0o111, "flightdeck-start must be executable"
    return dest


def _sandboxed_script(script: Path, sandbox: Path) -> Path:
    """Rewrite the generated script's absolute paths into a sandbox so it can
    run as the test user; the command sequence is untouched."""
    content = script.read_text()
    content = content.replace("/home/ozolith", str(sandbox / "home"))
    content = content.replace("/var/log/flightdeck", str(sandbox / "log"))
    rewritten = sandbox / "start"
    rewritten.write_text(content)
    rewritten.chmod(0o755)
    (sandbox / "home" / ".claude").mkdir(parents=True)  # the state-volume mountpoint
    return rewritten


def _stub(bin_dir: Path, name: str, exit_code: int) -> Path:
    """A recording stand-in: appends its argv to <name>.calls, exits fixed."""
    calls = bin_dir / f"{name}.calls"
    stub = bin_dir / name
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit {exit_code}\n')
    stub.chmod(0o755)
    return calls


def test_flightdeck_start_generation_expands_nothing_at_build_time(tmp_path):
    """The generator is one classic-Dockerfile-safe printf; the script it emits
    must carry no shell expansion the BUILD could have resolved — every command
    line arrives literal, to run at container start."""
    script = _generate_flightdeck_start(tmp_path).read_text()
    lines = script.splitlines()
    assert lines[0] == "#!/bin/sh"
    assert lines[1] == "set -eu"  # fail-fast: a failed step exits the container
    assert "$" not in script  # nothing expanded at build; nothing left to expand
    assert lines[-1] == "exec tmux wait-for flightdeck-forever"
    # The sequence is clone -> symlinks -> tmux: knowledge must be live before
    # the agent CLI starts.
    assert script.index("clone-init") < script.index("ln -sfnT") < script.index("tmux")


def test_flightdeck_start_clone_failure_fails_the_container(tmp_path):
    """A failed clone-init must exit the container non-zero BEFORE any symlink
    or tmux step — Docker's restart policy owns the retry; there is no
    in-container retry loop to hide the failure."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path), sandbox)
    _stub(bin_dir, "theozolith-knowledge", exit_code=7)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)

    proc = subprocess.run(
        [str(script)],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 7
    assert not tmux_calls.exists()  # tmux never launched over broken knowledge
    assert not (sandbox / "home" / ".claude" / "skills").is_symlink()


def test_flightdeck_start_success_wires_symlinks_then_tmux(tmp_path):
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path), sandbox)
    knowledge_calls = _stub(bin_dir, "theozolith-knowledge", exit_code=0)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)

    proc = subprocess.run(
        [str(script)],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    home = sandbox / "home"
    assert "clone-init --source" in knowledge_calls.read_text()
    for link, target in (
        (".claude/skills", "knowledge/skills"),
        (".claude/agents", "knowledge/agents/claude"),
        (".claude/workflows", "knowledge/workflows"),
        (".claude/CLAUDE.md", "knowledge/AGENTS.md"),
    ):
        assert os.readlink(home / link) == str(home / target), link
    calls = tmux_calls.read_text().splitlines()
    assert calls[0].startswith("new-session -d -s flightdeck")
    assert calls[1].startswith("pipe-pane -o -t flightdeck")
    assert calls[2] == "wait-for flightdeck-forever"
