"""deploy/ artifacts, the run-container image, and the CI image-build job."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "deploy"
DOCKERFILE = REPO_ROOT / "worker" / "docker" / "Dockerfile.claude"
CONTROL_DOCKERFILE = REPO_ROOT / "control" / "docker" / "Dockerfile"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def example_config(tmp_path_factory):
    """configs-example is a HUMAN Config Repo (ADR-0048): it becomes loadable
    by going through the real `theozolith config ingest` pipeline — which is
    itself the assertion that the shipped example ingests cleanly (knowledge
    compiles, lints pass, pins resolve)."""
    from theozolith_control.configrepo import load_config
    from theozolith_control.ingest import ingest

    pinned = tmp_path_factory.mktemp("pinned-build")
    ingest(str(DEPLOY / "configs-example"), pinned, log=lambda *_: None)
    return load_config(pinned)


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
    caught (a PR #2 review finding, absorbed here). The build runs through
    buildx (docker/build-push-action) with a GHA layer cache; what the
    image-rot guarantee needs is that this Dockerfile is still built."""
    ci = CI.read_text()
    assert "docker/build-push-action" in ci
    assert "file: worker/docker/Dockerfile.claude" in ci


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
    ci = CI.read_text()
    assert "docker/build-push-action" in ci
    assert "file: control/docker/Dockerfile" in ci


def test_no_tailscale_anywhere_in_product_code_or_deploy():
    """NODE-SUBSTRATE.md: Tailscale is a private-side deployment detail — never
    in product code, product IMAGES, or deploy scripts. Per-container tailnet
    identity enters only via a worker type's setup instructions in the Config
    Repo (ADR-0043); the ONE sanctioned home for the string is
    ``deploy/configs-example/**`` (and its README), which this scan
    deliberately does not touch. The product Dockerfiles are in scope: a
    tailscaled baked into a base image would violate the doctrine just as
    surely as product source would. PR #35's generic product corrections
    (entrypoint-override container commands, cross-UID secret delivery) land
    inside this scan's scope — this test staying green is what proves they
    carry no Tailscale-specific behavior or strings."""
    for component in ("worker", "control", "nodedaemon", "knowledge"):
        for path in (REPO_ROOT / component / "src").rglob("*.py"):
            assert "tailscale" not in path.read_text().lower(), path
    for dockerfile in (DOCKERFILE, CONTROL_DOCKERFILE):
        assert "tailscale" not in dockerfile.read_text().lower(), dockerfile
    for name in ("install-nodedaemon.sh", "compose/control.yml", "README.md"):
        assert "tailscale" not in (DEPLOY / name).read_text().lower(), name


def test_configs_example_parses_and_places_the_builtin_stacks(example_config):
    """The starter Config Repo must stay valid THROUGH INGEST (ADR-0048):
    worker/reviewer as process Stacks, the Flight Deck as a container Stack
    (ADR-0013/0019). Control is never a Stack — the substrate never
    supervises its own control plane (ADR-0035) — so the example must not
    carry one."""
    config = example_config
    kinds = {stack.name: stack.kind for stack in config.stacks}
    assert kinds == {
        "implementer": "process",
        "reviewer": "process",
        "flightdeck": "container",
        # The custom-driver example (ADR-0042): a drivers/<name> worker type
        # resolves to process kind and the generic launcher, staged stopped.
        "hello-logger": "process",
    }
    assert config.product_version
    assert "claude-dev" in config.worker_types
    # The example knowledge tree compiled at ingest and pinned per tree; both
    # claude types share it, so they share the pin (ADR-0048).
    assert config.worker_types["claude-dev"].knowledge == "knowledge/claude-dev"
    assert (
        config.worker_types["claude-dev"].knowledge_pin
        == config.worker_types["claude-review"].knowledge_pin
        != ""
    )
    # The custom driver resolves to the one launcher with a drivers/<name> ref
    # (ADR-0042), and its module is present so the load did not fault.
    hello = next(s for s in config.stacks if s.name == "hello-logger")
    assert hello.command == "theozolith-driver drivers/hello_logger"
    assert hello.state == "stopped"
    assert (REPO_ROOT / "deploy" / "configs-example" / "drivers" / "hello_logger.py").is_file()
    # Every example Stack is STAGED (stopped): the fill-in placeholders would
    # be refused by ingest on a running Stack (ADR-0048).
    assert {stack.state for stack in config.stacks} == {"stopped"}
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


def test_configs_example_flightdeck_knowledge_wiring(example_config):
    """ADR-0048 (amending ADR-0043) + issue #31: the example Flight Deck wires
    per-instance runtime state, the READ-ONLY bind of the node's applied
    knowledge export, and a per-instance tailnet identity volume; bakes the
    knowledge symlinks into flightdeck-start; and keeps the carve-out
    Flight-Deck-only. The tailscale half is landed behind the #31 gate
    evidence (uid-1000, userspace networking, no added capabilities)."""
    config = example_config
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")

    # Per-instance state + logs + tailnet identity (resolved from {stack});
    # exactly one SHARED read-only knowledge bind that is deliberately NOT
    # per-instance — and nothing else. Mounted at the stable PARENT the Node
    # Daemon maintains, so a tree swap never recreates the container.
    assert set(flightdeck.volumes) == {
        "flightdeck-logs:/var/log/flightdeck",
        "flightdeck-claude-state:/home/ozolith/.claude",
        "/var/lib/theozolith/knowledge:/var/lib/theozolith/knowledge:ro",
        "flightdeck-tailscale-state:/var/lib/tailscale",
    }

    wt = config.worker_types["flightdeck"]
    # The deck SELECTS its tree via the worker-type knowledge field (ADR-0048
    # amendment): validated like a driver's (pin joined at load), delivered
    # as control-injected env — and NEVER baked: the wire recipe carries
    # empty knowledge fields (the state volume shadows ~/.claude), so a
    # content edit moves the pin without touching the image identity.
    assert wt.knowledge == "knowledge/claude-dev"
    assert wt.knowledge_pin == config.worker_types["claude-dev"].knowledge_pin
    recipe = wt.recipe_wire()
    assert recipe["knowledge"] == "" and recipe["knowledge_pin"] == ""
    assert flightdeck.env["THEOZOLITH_KNOWLEDGE_TREE"] == "claude-dev"
    script = "\n".join(wt.setup)

    # The writable clone is RETIRED (ADR-0048): no clone-init, no knowledge
    # URL — the symlinks resolve into the mounted COMPILED tree, selected at
    # container start from the injected env (no hardcoded tree name), and
    # the script fails loud when the selected tree is unavailable. The
    # replace idiom is portable (no GNU-only ln -T): a real directory at a
    # destination is refused, a stale link or file is relinked.
    assert "clone-init" not in script
    assert "github.com" not in script
    assert "ln -sfnT" not in script and "ln -sfn" not in script
    assert "knowledge/claude-dev" not in script  # the selection is env, not a literal
    assert '"${THEOZOLITH_KNOWLEDGE_TREE:?' in script
    assert 'KNOWLEDGE_TREE_DIR="/var/lib/theozolith/knowledge/${THEOZOLITH_KNOWLEDGE_TREE}"' in (
        script
    )
    assert 'if [ ! -d "$KNOWLEDGE_TREE_DIR" ]; then' in script
    assert "link_knowledge" in script and 'ln -s "$1" "$2"' in script
    assert "for entry in skills agents workflows CLAUDE.md; do" in script
    assert '"/home/ozolith/.claude/$entry"' in script

    # The carve-out is Flight-Deck-only: no OTHER stack or worker type mounts
    # the knowledge export, any .claude path, or a tailnet identity.
    for stack in config.stacks:
        if stack.name == "flightdeck":
            continue
        for volume in stack.volumes:
            assert "knowledge" not in volume and ".claude" not in volume, (stack.name, volume)
            assert "tailscale" not in volume, (stack.name, volume)
    for name, other in config.worker_types.items():
        if name == "flightdeck":
            continue
        for volume in other.volumes:
            assert "knowledge" not in volume and ".claude" not in volume, (name, volume)
            assert "tailscale" not in volume, (name, volume)


def test_configs_example_flightdeck_tailscale_wiring(example_config):
    """Issue #31: the tailscale half of the example, landed behind the gate
    evidence recorded on the issue. Static binaries are pinned by version AND
    sha256 with a FAIL-CLOSED placeholder (human-entered, never ingest-
    computed — ADR-0048); the auth key enters as a named secret only; the
    hostname is per-placement Stack env; no capability or device passthrough
    exists anywhere to grant (the worker-type schema has no such field — this
    test pins the example's side of that doctrine)."""
    config = example_config
    flightdeck = next(s for s in config.stacks if s.name == "flightdeck")
    wt = config.worker_types["flightdeck"]
    setup = "\n".join(wt.setup)

    # Fail-closed binary pinning: the checksum is verified before install, and
    # the shipped placeholder can never match a real download.
    assert "pkgs.tailscale.com" in setup
    assert "sha256sum -c" in setup
    assert "TS_SHA256=0000000000000000000000000000000000000000000000000000000000000000" in setup
    assert setup.index("sha256sum -c") < setup.index("install -m 0755")

    # The pinned release is the one the #31 gate evidence was produced with
    # (the spikes/issue-31-tailscale-uid1000 harness runs the identical
    # archive), and it may never regress below the 1.98.9 security floor.
    version = re.search(r"TS_VERSION=(\d+)\.(\d+)\.(\d+)", setup)
    assert version, "TS_VERSION must be pinned major.minor.patch in the setup step"
    assert version.group(0) == "TS_VERSION=1.102.2"
    assert tuple(map(int, version.groups())) >= (1, 98, 9)

    # Userspace daemon, uid-1000 statedir, no privilege words anywhere.
    assert "--tun=userspace-networking" in setup
    assert "chown ozolith:ozolith" in setup and "/var/lib/tailscale" in setup
    for forbidden in ("cap_add", "NET_ADMIN", "/dev/net/tun", "privileged", "sudo"):
        assert forbidden not in setup, forbidden

    # The key is a named secret delivered as TS_AUTHKEY_FILE; the hostname is
    # per-placement Stack env, present on the example Stack.
    assert flightdeck.secrets["TS_AUTHKEY"] == "flightdeck-tailscale-authkey"
    assert flightdeck.env["FLIGHTDECK_TS_HOSTNAME"] == "flightdeck-box1"
    # Only the file path is ever referenced — the value has no other route in.
    assert "TS_AUTHKEY_FILE" in setup


def test_flightdeck_tailscale_version_matches_the_gate_harness(example_config):
    """The version the example ships must be the version the #31 gate harness
    (spikes/issue-31-tailscale-uid1000, PR #34) actually tested — gate evidence
    for one binary says nothing about another. Both sides fetch the same
    archive from the same URL; the harness additionally pins the real official
    SHA-256 (the example's stays a fail-closed placeholder by design)."""
    spike_dockerfile = REPO_ROOT / "spikes" / "issue-31-tailscale-uid1000" / "Dockerfile"
    if not spike_dockerfile.exists():
        pytest.skip("gate harness (PR #34) not present in this checkout yet")
    setup = "\n".join(example_config.worker_types["flightdeck"].setup)
    example = re.search(r"TS_VERSION=(\d+\.\d+\.\d+)", setup)
    spike = re.search(r"TS_VERSION=(\d+\.\d+\.\d+)", spike_dockerfile.read_text())
    assert example and spike, "both sides must pin TS_VERSION major.minor.patch"
    assert example.group(1) == spike.group(1)


# -- flightdeck-start: the generated script is EXECUTED, not just grepped ---------


def _generate_flightdeck_start(tmp_path: Path, config) -> Path:
    """Run the worker type's script-writing setup entry in a real /bin/sh —
    exactly what the image build does — with the baked destination redirected
    into tmp_path, and return the generated script."""
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
    content = content.replace("/var/lib/tailscale", str(sandbox / "tsstate"))
    content = content.replace("/var/lib/theozolith/knowledge", str(sandbox / "knowledge"))
    content = content.replace("/etc/theozolith", str(sandbox / "etc"))
    rewritten = sandbox / "start"
    rewritten.write_text(content)
    rewritten.chmod(0o755)
    (sandbox / "home" / ".claude").mkdir(parents=True)  # the state-volume mountpoint
    (sandbox / "tsstate").mkdir()  # the tailscale-state volume mountpoint
    # The applied knowledge tree the deck's selection resolves to (ADR-0048):
    # present by default so the fail-loud gate passes; the unavailable-tree
    # test removes it.
    (sandbox / "knowledge" / "claude-dev").mkdir(parents=True)
    return rewritten


def _stub(bin_dir: Path, name: str, exit_code: int) -> Path:
    """A recording stand-in: appends its argv to <name>.calls, exits fixed."""
    calls = bin_dir / f"{name}.calls"
    stub = bin_dir / name
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit {exit_code}\n')
    stub.chmod(0o755)
    return calls


def _tailscale_stub(bin_dir: Path, *, status_code: int = 0, up_code: int = 0) -> Path:
    """A `tailscale` CLI stand-in that distinguishes the readiness probe
    (`status --json`) from `up`, so tests can drive each lifecycle branch."""
    calls = bin_dir / "tailscale.calls"
    stub = bin_dir / "tailscale"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        'case "$*" in\n'
        f'*" up "*|*" up") exit {up_code} ;;\n'
        f"*) exit {status_code} ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return calls


def _tailscale_timeout_stub(bin_dir: Path) -> Path:
    """A `tailscale` stand-in modelling the REAL CLI's contract on a tailnet
    that never reaches Running state: `up` WITHOUT a --timeout flag blocks
    indefinitely (the CLI default is an infinite wait), `up` WITH one returns
    non-zero at the bound (emulated instantly for test speed). The readiness
    probe answers ready. A script that drops the native flag therefore hangs
    here and fails the test's OUTER deadline instead of passing."""
    calls = bin_dir / "tailscale.calls"
    stub = bin_dir / "tailscale"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        'case "$*" in\n'
        '*" up "*|*" up")\n'
        '  case "$*" in\n'
        '  *--timeout=*) echo "timeout waiting for Tailscale service" >&2; exit 1 ;;\n'
        "  *) exec /bin/sleep 60 ;;\n"
        "  esac ;;\n"
        "*) exit 0 ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return calls


def _tailscaled_stub(
    bin_dir: Path, *, lifespan: str | None, writes_state: Path | None = None
) -> tuple[Path, Path]:
    """A `tailscaled` stand-in launched in the background by the script.
    lifespan None = exit immediately (a daemon that dies on startup); a
    duration = stay alive that long under /bin/sleep (immune to the test's
    `sleep` stub), recording its pid so tests can verify the EXIT trap
    reaped it. writes_state mimics the real daemon writing its machine key
    into the statedir at launch — BEFORE auth-key registration completes —
    which is exactly why a non-empty state file is not proof of enrollment."""
    calls = bin_dir / "tailscaled.calls"
    pid_file = bin_dir / "tailscaled.pid"
    body = f'#!/bin/sh\necho "$@" >> "{calls}"\necho $$ > "{pid_file}"\n'
    if writes_state is not None:
        body += f'echo machine-key-material > "{writes_state / "tailscaled.state"}"\n'
    body += "exit 0\n" if lifespan is None else f"exec /bin/sleep {lifespan}\n"
    stub = bin_dir / "tailscaled"
    stub.write_text(body)
    stub.chmod(0o755)
    return calls, pid_file


def _tmux_stub(bin_dir: Path, *, has_session_code: int) -> Path:
    """A `tmux` stand-in; has-session's exit code drives the supervisor loop
    (0 = session alive, 1 = session gone). Besides the flat `tmux.calls`
    line, every invocation is recorded boundary-preserving into `tmux.argv`
    (`argc N` then one `arg <value>` line each) — `echo "$@"` flattens a
    multi-word command argument into indistinguishable words, and the baked
    model contract is exactly about tmux receiving ONE command string."""
    calls = bin_dir / "tmux.calls"
    argv = bin_dir / "tmux.argv"
    stub = bin_dir / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        f'echo "argc $#" >> "{argv}"\n'
        f'for a in "$@"; do echo "arg $a" >> "{argv}"; done\n'
        f'case "$1" in has-session) exit {has_session_code} ;; esac\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return calls


def _tmux_invocations(bin_dir: Path) -> list[list[str]]:
    """The argv of every recorded tmux invocation, boundaries intact."""
    lines = (bin_dir / "tmux.argv").read_text().splitlines()
    invocations: list[list[str]] = []
    index = 0
    while index < len(lines):
        head = lines[index]
        assert head.startswith("argc "), head
        argc = int(head.split()[1])
        args = lines[index + 1 : index + 1 + argc]
        assert len(args) == argc and all(arg.startswith("arg ") for arg in args)
        invocations.append([arg[len("arg ") :] for arg in args])
        index += 1 + argc
    return invocations


def _instant_sleep(bin_dir: Path) -> None:
    """Neutralize the script's wait/supervision intervals so bounded loops run
    at test speed. Stub daemons dodge this by calling /bin/sleep directly."""
    stub = bin_dir / "sleep"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)


def _run_start(
    script: Path, bin_dir: Path, *, timeout: float | None = None, **extra_env: str | None
) -> subprocess.CompletedProcess:
    """The container-start env: the knowledge selection rides as the
    control-injected THEOZOLITH_KNOWLEDGE_TREE (ADR-0048) — defaulted here so
    every lifecycle test starts like a resolved deck Stack; pass ``None`` to
    UNSET a variable (the missing-selection test)."""
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "THEOZOLITH_KNOWLEDGE_TREE": "claude-dev",
    }
    env.pop("TS_AUTHKEY_FILE", None)
    for key, value in extra_env.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=timeout)


def _assert_daemon_reaped(pid_file: Path) -> None:
    """The EXIT trap must kill the backgrounded tailscaled on every path."""
    pid = int(pid_file.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 15)  # do not leak the stub past the test
    raise AssertionError("tailscaled survived flightdeck-start exiting")


def test_flightdeck_start_generation_is_literal_until_runtime(tmp_path, example_config):
    """The generator is one classic-Dockerfile-safe printf; the script it emits
    must carry every runtime expansion UNTOUCHED by the build — the hostname,
    the key path, and the daemon pid expand at container start, never at image
    build (an issue #31 test requirement)."""
    script = _generate_flightdeck_start(tmp_path, example_config).read_text()
    lines = script.splitlines()
    assert lines[0] == "#!/bin/sh"
    assert lines[1] == "set -eu"  # fail-fast: a failed step exits the container
    # Runtime values survived generation literally — a build-time expansion
    # would have resolved $! to nothing and the variables to empty strings.
    assert "TAILSCALED_PID=$!" in lines
    assert '--hostname="$FLIGHTDECK_TS_HOSTNAME"' in script
    assert '--auth-key="file:${TS_AUTHKEY_FILE}"' in script
    # ADR-0045 §4: the baked-model command substitution also survives
    # generation LITERALLY — the model file is read at container start, never
    # expanded into the script at image build — and the whole launch command
    # is one quoted tmux argument, guarded by the file's non-emptiness.
    assert "if [ -s /etc/theozolith/model ]; then" in script
    assert (
        "tmux new-session -d -s flightdeck"
        ' "claude --model \\"$(cat /etc/theozolith/model)\\""' in script
    )
    # ... and the only variables in the script are its own runtime ones.
    assert set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", script)) == {
        "FLIGHTDECK_TS_HOSTNAME",
        "THEOZOLITH_KNOWLEDGE_TREE",
        "KNOWLEDGE_TREE_DIR",
        "entry",
        "TS_AUTHKEY_FILE",
        "TS_ENROLL",
        "TS_TRIES",
        "TAILSCALED_PID",
    }
    # Order is the issue #31 lifecycle: the knowledge selection gate and
    # symlinks first (into the read-only mount, ADR-0048 — no clone step
    # exists anymore), then the enrollment decision — read from the
    # Ozolith-owned COMPLETION MARKER, never from tailscaled.state — BEFORE
    # the daemon launches, readiness before `up`, tmux last, and a supervisor
    # — never the removed draft's `exec tmux wait-for`.
    assert "clone-init" not in script
    assert (
        script.index("KNOWLEDGE_TREE_DIR=")
        < script.index("link_knowledge")
        < script.index(".theozolith-enrolled-v1 ]")
        < script.index("tailscaled --tun=userspace-networking")
        < script.index(" up --ssh")
        < script.index("new-session")
        < script.index("has-session")
    )
    # The decision predicate is the final marker name, never the state file or
    # the promotion temp; promotion is atomic (tmp write, then same-volume mv)
    # and strictly AFTER a successful `up` — so no interruption can leave a
    # false success marker, and tailscaled.state is never deleted or rewritten.
    assert "if [ -f /var/lib/tailscale/.theozolith-enrolled-v1 ]" in script
    assert "-s /var/lib/tailscale/tailscaled.state ]" not in script
    # Nothing on the TAILNET-IDENTITY volume is ever deleted; the only rm in
    # the script is the portable symlink replace under ~/.claude (never a
    # real directory — the guard above it refuses those), which GNU ln -sfnT
    # performed implicitly before.
    for line in lines:
        if "rm " in line:
            assert "tailscale" not in line, line
            assert 'rm -f "$2"' in line, line
    assert (
        script.index(" up --ssh")
        < script.index("> /var/lib/tailscale/.theozolith-enrolled-v1.tmp")
        < script.index("mv /var/lib/tailscale/.theozolith-enrolled-v1.tmp")
        < script.index("new-session")
    )
    assert "wait-for" not in script
    assert lines[-1] == "exit 0"
    # No unbounded loop anywhere: the readiness loop carries its bound, and
    # `up` is never wrapped in a loop.
    for line in lines:
        if " up --ssh" in line:
            assert not line.strip().startswith(("until", "while")), line


def test_flightdeck_start_up_attempts_are_natively_bounded(tmp_path, example_config):
    """`tailscale up` waits for Running state FOREVER by default, so "one
    attempt" alone never guaranteed prompt failure. BOTH up commands — fresh
    enrollment and marker-present reuse — must carry the CLI's NATIVE
    --timeout=30s, and the bound must be that flag, never an external
    `timeout` process wrapping the command."""
    script = _generate_flightdeck_start(tmp_path, example_config).read_text()
    up_commands = [
        line
        for line in script.splitlines()
        if line.strip().startswith("tailscale ") and " up " in line
    ]
    assert len(up_commands) == 2
    for line in up_commands:
        assert "--timeout=30s" in line, line
        # The filter above already proves `tailscale` is the command itself,
        # not an argument to an external `timeout` wrapper.
    assert not any(line.strip().startswith("timeout ") for line in script.splitlines())


def test_flightdeck_start_unavailable_tree_fails_before_the_daemon(tmp_path, example_config):
    """ADR-0048 amendment: the selected knowledge tree is a HARD prerequisite
    — when the node has not converged a distribution carrying it (or the tree
    was retired), the deck fails loud BEFORE tailscaled ever launches, and
    docker restart policy owns the retry. Silently starting without skills is
    exactly what this replaces."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    shutil.rmtree(sandbox / "knowledge" / "claude-dev")  # not converged yet
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-x\n")

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "not available on this node" in proc.stderr
    assert not daemon_calls.exists()
    assert not (sandbox / "home" / ".claude" / "skills").exists()


def test_flightdeck_start_missing_selection_fails_before_the_daemon(tmp_path, example_config):
    """Without the control-injected THEOZOLITH_KNOWLEDGE_TREE (a deck run
    outside a resolved worker-type Stack), the script refuses to guess."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        THEOZOLITH_KNOWLEDGE_TREE=None,
    )
    assert proc.returncode != 0
    assert "THEOZOLITH_KNOWLEDGE_TREE" in proc.stderr
    assert not daemon_calls.exists()


def test_flightdeck_start_replaces_stale_links_but_refuses_a_real_directory(
    tmp_path, example_config
):
    """The portable replace idiom keeps GNU ln -sfnT's safety: a stale symlink
    or plain file at a ~/.claude destination is replaced, a REAL directory
    (state-volume debris) fails the start loudly instead of being deleted."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-x\n")
    claude = sandbox / "home" / ".claude"
    (claude / "skills").symlink_to(sandbox / "elsewhere")  # stale link
    (claude / "CLAUDE.md").write_text("stale file\n")

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    # Both stale entries were relinked into the selected tree before the
    # (deliberately dying) stub daemon failed the container.
    assert daemon_calls.exists()
    assert os.readlink(claude / "skills") == str(sandbox / "knowledge" / "claude-dev" / "skills")
    assert os.readlink(claude / "CLAUDE.md") == str(
        sandbox / "knowledge" / "claude-dev" / "CLAUDE.md"
    )

    (claude / "agents").unlink()
    (claude / "agents").mkdir()  # a real directory must never be deleted
    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "refusing to symlink over it" in proc.stderr
    assert (claude / "agents").is_dir() and not (claude / "agents").is_symlink()


def test_flightdeck_start_missing_hostname_fails_before_the_daemon(tmp_path, example_config):
    """FLIGHTDECK_TS_HOSTNAME comes from the Stack [env]; without it the
    container fails with a naming message before tailscaled ever launches."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)

    proc = _run_start(script, bin_dir)
    assert proc.returncode != 0
    assert "FLIGHTDECK_TS_HOSTNAME" in proc.stderr
    assert not daemon_calls.exists()


def test_flightdeck_start_missing_authkey_fails_fast_before_the_daemon(tmp_path, example_config):
    """Issue #31 lifecycle point 2: enrollment due (completion marker absent)
    with the auth-key secret absent fails fast with a DISTINCT
    restore-the-mapping message, BEFORE tailscaled launches (point 1) — and a
    non-empty tailscaled.state left by a failed prior attempt must not dodge
    that check: without the marker, the decision is still "enroll"."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    # Debris of a rejected first enrollment: state present, no marker.
    (sandbox / "tsstate" / "tailscaled.state").write_text("machine-key-material")
    daemon_calls, _ = _tailscaled_stub(bin_dir, lifespan=None)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 1
    assert "restore the TS_AUTHKEY binding" in proc.stderr
    assert not daemon_calls.exists()  # decided before launching, not after
    assert not tmux_calls.exists()


def test_flightdeck_start_fresh_enrollment_consumes_the_key_by_path_only(tmp_path, example_config):
    """The success path end-to-end over an empty state volume (a first start,
    or the volume after deliberate state loss — both correctly route to
    enrollment): knowledge symlinks, enrollment via file:$TS_AUTHKEY_FILE
    (the VALUE never enters any argv), atomic completion-marker promotion,
    tmux session with transcript piping — and on a clean session end, exit 0
    with the EXIT trap reaping the daemon (issue #31 lifecycle points 6/7)."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=0)
    tmux_calls = _tmux_stub(bin_dir, has_session_code=1)  # session already over

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 0, proc.stderr
    home = sandbox / "home"
    # The symlinks target the COMPILED trees on the read-only mount (ADR-0048).
    for link, target in (
        (".claude/skills", "claude-dev/skills"),
        (".claude/agents", "claude-dev/agents"),
        (".claude/workflows", "claude-dev/workflows"),
        (".claude/CLAUDE.md", "claude-dev/CLAUDE.md"),
    ):
        assert os.readlink(home / link) == str(sandbox / "knowledge" / target), link
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert "--ssh" in up_lines[0]
    assert "--hostname=flightdeck-test" in up_lines[0]
    assert f"--auth-key=file:{key_file}" in up_lines[0]
    # The key VALUE never appears in any recorded argv — path form only.
    for calls in (ts_calls, tmux_calls):
        assert "SECRETVALUE" not in calls.read_text(), calls
    tmux_lines = tmux_calls.read_text().splitlines()
    assert tmux_lines[0].startswith("new-session -d -s flightdeck")
    assert tmux_lines[1].startswith("pipe-pane -o -t flightdeck")
    # Success promoted the completion marker atomically: the final name
    # exists, the promotion temp does not survive.
    assert (sandbox / "tsstate" / ".theozolith-enrolled-v1").is_file()
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1.tmp").exists()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_marker_present_reuses_identity_without_the_key(tmp_path, example_config):
    """Issue #31 lifecycle point 1's other branch: a PROMOTED completion
    marker (a successful prior enrollment) routes to `up` WITHOUT the auth
    key — so the remove-the-mapping hardening (no TS_AUTHKEY_FILE at all)
    keeps working for enrolled instances across restarts."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 0, proc.stderr
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert "--auth-key" not in up_lines[0]
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_baked_model_launches_the_session_with_the_flag(tmp_path, example_config):
    """ADR-0045 §4 end-to-end: with the image-baked model file present, the
    generated script (produced by the REAL worker-type setup command) starts
    the session as `claude --model "claude-fable-5"` — delivered to tmux as
    exactly ONE shell-command argument, so the whole launch command runs in
    the session instead of tmux misparsing the flag as its own — while the
    rest of the lifecycle (enrollment by key path, transcript piping, clean
    daemon reaping) behaves exactly as without a model."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "etc").mkdir()  # the sandboxed /etc/theozolith
    (sandbox / "etc" / "model").write_text("claude-fable-5\n")
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    tmux_calls = _tmux_stub(bin_dir, has_session_code=1)  # session already over

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 0, proc.stderr
    invocations = _tmux_invocations(bin_dir)
    assert invocations[0] == [
        "new-session",
        "-d",
        "-s",
        "flightdeck",
        'claude --model "claude-fable-5"',
    ]
    assert invocations[1][:4] == ["pipe-pane", "-o", "-t", "flightdeck"]
    assert "transcript.log" in invocations[1][4]
    # The key VALUE stays out of every recorded argv on this branch too.
    assert "SECRETVALUE" not in tmux_calls.read_text()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_absent_model_file_launches_bare_claude(tmp_path, example_config):
    """The compatibility branch (ADR-0045 §4): NO model file in the image —
    a model-less worker type, or a pre-§4 base — must launch exactly `claude`
    as the session command, with no --model flag and no empty argument."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 0, proc.stderr
    assert _tmux_invocations(bin_dir)[0] == ["new-session", "-d", "-s", "flightdeck", "claude"]
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_empty_model_file_launches_bare_claude(tmp_path, example_config):
    """An EMPTY model file takes the same bare-`claude` branch as an absent
    one (the guard is `-s`, non-empty): a build that materialized nothing
    must never produce `claude --model ""`."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "etc").mkdir()
    (sandbox / "etc" / "model").write_text("")
    (sandbox / "tsstate" / "tailscaled.state").write_text("{}")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 0, proc.stderr
    assert _tmux_invocations(bin_dir)[0] == ["new-session", "-d", "-s", "flightdeck", "claude"]
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_failed_enrollment_is_retried_with_the_key_not_reused(
    tmp_path, example_config
):
    """The defect the completion marker exists to fix: tailscaled writes its
    machine key BEFORE auth-key registration completes, so a REJECTED first
    enrollment leaves a non-empty tailscaled.state behind. The next start
    over that same state volume must take the enrollment branch again
    (file:$TS_AUTHKEY_FILE — the marker is absent), never the keyless reuse
    branch it cannot recover from; a successful retry then promotes the
    marker."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60", writes_state=sandbox / "tsstate")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=1)  # rejected key
    _stub(bin_dir, "tmux", exit_code=0)
    _instant_sleep(bin_dir)

    first = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert first.returncode == 1
    assert "enrollment failed" in first.stderr
    state = sandbox / "tsstate" / "tailscaled.state"
    assert state.read_text()  # the failed attempt left a NON-EMPTY state file
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1").exists()
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1.tmp").exists()
    _assert_daemon_reaped(daemon_pid)

    # Second start, same state volume, corrected/working key: the absent
    # marker routes to enrollment WITH the key, and success promotes it.
    ts_calls.unlink()
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)
    second = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert second.returncode == 0, second.stderr
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert f"--auth-key=file:{key_file}" in up_lines[0]
    assert (sandbox / "tsstate" / ".theozolith-enrolled-v1").is_file()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_interrupted_promotion_is_not_a_marker(tmp_path, example_config):
    """A leftover promotion temp file — an interruption between `up` success
    and the same-volume mv — must NOT count as enrolled: only the final
    marker name flips the decision, so the next start re-enrolls with the
    reusable key (documented as safe) and re-promotes."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("machine-key-material")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1.tmp").write_text("enrolled")
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=1)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 0, proc.stderr
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert f"--auth-key=file:{key_file}" in up_lines[0]  # enrollment, not reuse
    assert (sandbox / "tsstate" / ".theozolith-enrolled-v1").is_file()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_daemon_death_before_ready_fails_promptly(tmp_path, example_config):
    """Issue #31 lifecycle point 3: while waiting for the LocalAPI, a daemon
    that already exited is detected and fails the container — the wait is
    never served out against a corpse, and `up` is never attempted."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")  # reuse branch
    _tailscaled_stub(bin_dir, lifespan=None)  # dies immediately
    ts_calls = _tailscale_stub(bin_dir, status_code=1)  # LocalAPI never answers
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)
    _instant_sleep(bin_dir)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 1
    assert "exited before its LocalAPI answered" in proc.stderr
    assert not any(" up " in c for c in ts_calls.read_text().splitlines())
    assert not tmux_calls.exists()


def test_flightdeck_start_readiness_wait_is_bounded(tmp_path, example_config):
    """Issue #31 lifecycle point 3/4: a live daemon whose LocalAPI never
    answers exhausts a BOUNDED wait and fails the container — no unbounded
    loop, no `up` attempt against a dead socket."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")  # reuse branch
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=1)  # never ready
    _instant_sleep(bin_dir)

    proc = _run_start(script, bin_dir, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 1
    assert "not ready after 30 tries" in proc.stderr
    assert not any(" up " in c for c in ts_calls.read_text().splitlines())
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_enrollment_failure_is_permanent_not_retried(tmp_path, example_config):
    """Issue #31 lifecycle point 4: a failing `tailscale up` (invalid/expired
    key, rejected flags) gets exactly ONE attempt and fails the container
    promptly — the removed draft's invisible `until ... sleep 5` loop must
    never come back. Docker's restart policy owns any retry."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-EXPIRED\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_stub(bin_dir, status_code=0, up_code=1)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)
    _instant_sleep(bin_dir)

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "enrollment failed" in proc.stderr
    assert sum(" up " in c for c in ts_calls.read_text().splitlines()) == 1
    assert not tmux_calls.exists()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_fresh_enrollment_timeout_fails_finitely(tmp_path, example_config):
    """A tailnet that never reaches Running must not hang a fresh enrollment:
    readiness succeeds, the single `up` attempt hits the CLI's native 30s
    bound, and the container exits non-zero within a finite interval — the
    stub blocks any `up` lacking a --timeout flag, so a script that drops the
    bound fails this test's outer deadline. The timeout must prevent marker
    promotion (no final marker, no promotion temp) and tmux startup, retain
    the daemon's state debris for the Docker-owned retry, and reach the EXIT
    trap so tailscaled is reaped."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60", writes_state=sandbox / "tsstate")
    ts_calls = _tailscale_timeout_stub(bin_dir)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)

    proc = _run_start(
        script,
        bin_dir,
        timeout=20,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "enrollment failed" in proc.stderr
    assert sum(" up " in c for c in ts_calls.read_text().splitlines()) == 1
    # The daemon's machine-key debris is retained; neither the final marker
    # nor the promotion temp may exist after a timed-out enrollment.
    assert (sandbox / "tsstate" / "tailscaled.state").read_text()
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1").exists()
    assert not (sandbox / "tsstate" / ".theozolith-enrolled-v1.tmp").exists()
    assert not tmux_calls.exists()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_reuse_timeout_fails_finitely_preserving_identity(
    tmp_path, example_config
):
    """The marker-present branch gets the same native bound: a stalled
    tailnet fails the container within a finite interval after ONE keyless
    `up` attempt, with no destructive recovery — the completion marker and
    tailscaled.state are preserved unchanged for the Docker-owned retry,
    tmux never starts, and the EXIT trap reaps the daemon."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    (sandbox / "tsstate" / "tailscaled.state").write_text("machine-key-material")
    (sandbox / "tsstate" / ".theozolith-enrolled-v1").write_text("enrolled")
    _, daemon_pid = _tailscaled_stub(bin_dir, lifespan="60")
    ts_calls = _tailscale_timeout_stub(bin_dir)
    tmux_calls = _stub(bin_dir, "tmux", exit_code=0)

    proc = _run_start(script, bin_dir, timeout=20, FLIGHTDECK_TS_HOSTNAME="flightdeck-test")
    assert proc.returncode == 1
    assert "up on existing state failed" in proc.stderr
    up_lines = [c for c in ts_calls.read_text().splitlines() if " up " in c]
    assert len(up_lines) == 1
    assert "--auth-key" not in up_lines[0]
    assert (sandbox / "tsstate" / "tailscaled.state").read_text() == "machine-key-material"
    assert (sandbox / "tsstate" / ".theozolith-enrolled-v1").read_text() == "enrolled"
    assert not tmux_calls.exists()
    _assert_daemon_reaped(daemon_pid)


def test_flightdeck_start_daemon_death_after_start_fails_the_container(tmp_path, example_config):
    """Issue #31 lifecycle point 5: after the session is up, a dying tailscaled
    must fail the container — never a nominally healthy container with dead
    one-hop access (the removed draft's `exec tmux wait-for` defect)."""
    sandbox = tmp_path / "sandbox"
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True)
    script = _sandboxed_script(_generate_flightdeck_start(tmp_path, example_config), sandbox)
    key_file = sandbox / "authkey"
    key_file.write_text("tskey-auth-SECRETVALUE\n")
    _tailscaled_stub(bin_dir, lifespan="1")  # dies shortly after startup
    _tailscale_stub(bin_dir, status_code=0, up_code=0)
    _tmux_stub(bin_dir, has_session_code=0)  # the session stays alive
    _instant_sleep(bin_dir)  # the supervisor polls at test speed

    proc = _run_start(
        script,
        bin_dir,
        FLIGHTDECK_TS_HOSTNAME="flightdeck-test",
        TS_AUTHKEY_FILE=str(key_file),
    )
    assert proc.returncode == 1
    assert "tailscaled exited" in proc.stderr


# -- repo mirror cache provisioning + retention (#51 amendment) ---------------------


def test_installer_provisions_the_mirror_cache_root():
    """The trusted cache root exists before the service ever starts —
    service-user owned, no group/world write — so the drivers' fail-closed
    runtime validation passes on the happy path."""
    installer = (DEPLOY / "install-nodedaemon.sh").read_text()
    line = (
        "install -d -m 0750 -o ozolith -g ozolith /var/tmp/theozolith /var/tmp/theozolith/mirrors"
    )
    assert line in installer
    # Provisioned before the unit is registered or provision could start it.
    assert installer.index(line) < installer.index("systemctl daemon-reload")


def test_cleanup_removes_the_default_mirror_cache_root():
    """Uninstall removes the node-shared scratch root (job dirs + mirror
    cache — full repo history, possibly private refs) only after the daemon
    and its drivers are down, and tells the operator that a custom
    THEOZOLITH_MIRRORS_DIR is theirs to remove separately."""
    readme = (DEPLOY / "README.md").read_text()
    cleanup = readme[readme.index("## Cleanup / deletion test") :]
    assert "sudo rm -rf /var/tmp/theozolith" in cleanup
    assert cleanup.index("systemctl disable --now theozolith-nodedaemon") < cleanup.index(
        "sudo rm -rf /var/tmp/theozolith"
    )
    assert "THEOZOLITH_MIRRORS_DIR" in cleanup


def test_mirror_cache_docs_state_the_retention_and_trust_facts():
    """Operator docs carry the #51 amendment doctrine: caches are never
    backup/restore inputs, they hold repo history (possibly private refs),
    ownership/mode rules are stated, and the timeout knob is documented."""
    readme = (DEPLOY / "README.md").read_text()
    assert "never backed up and never restored" in readme
    assert "may include private refs" in readme
    assert "THEOZOLITH_GIT_TIMEOUT_SECONDS" in readme
    assert "no group/world write" in readme
