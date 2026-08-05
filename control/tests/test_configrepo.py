"""Config Repo parsing: format validation, deterministic tags, scoping, and
the wire-model handshake with the Node Daemon."""

from __future__ import annotations

import pytest
from theozolith_control.configrepo import ConfigRepoError, load_config
from theozolith_nodedaemon.stacks import WireStack

DIGEST = "0" * 64


def write(tmp_path, relpath: str, text: str) -> None:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_missing_repo_is_an_empty_deployment(tmp_path):
    config = load_config(tmp_path / "nope")
    assert config.stacks == () and config.images == {}
    assert config.desired_state_for("box1") == {
        "commit": "",
        "product_version": "",
        "stacks": [],
        "images": [],
    }


def test_full_repo_parses_and_scopes(tmp_path):
    write(
        tmp_path,
        "stacks/worker.toml",
        'kind = "process"\nnode = "box1"\ncommand = "theozolith-worker"\n'
        'run_image = "claude-dev"\n[secrets]\nWORKER_GITHUB_TOKEN = "github-worker"\n',
    )
    write(
        tmp_path,
        "stacks/flightdeck.toml",
        'kind = "container"\nnode = "pi"\ncompose = "compose/flightdeck.yml"\n'
        'overlays = ["overlays/ts.yml"]\n',
    )
    write(tmp_path, "compose/flightdeck.yml", "services: {}\n")
    write(tmp_path, "overlays/ts.yml", "services: {}\n")
    write(tmp_path, "images/claude-dev.toml", f'base = "ghcr.io/x/run:1.2@sha256:{DIGEST}"\n')
    write(tmp_path, "product.toml", '[product]\nversion = "0.3.0"\n')

    config = load_config(tmp_path)
    assert config.product_version == "0.3.0"
    assert config.secret_names_for("box1") == {"github-worker"}
    assert config.secret_names_for("pi") == set()
    assert config.secret_names_for("stranger") == set()

    box1 = config.desired_state_for("box1")
    assert [s["name"] for s in box1["stacks"]] == ["worker"]
    image = box1["images"][0]
    assert image["tag"] == f"theozolith/claude-dev:1.2-{image['instruction_hash'][:12]}"

    pi = config.desired_state_for("pi")
    files = pi["stacks"][0]["compose_files"]
    assert [f["name"] for f in files] == ["compose/flightdeck.yml", "overlays/ts.yml"]
    assert files[0]["content"] == "services: {}\n"


def test_instruction_changes_change_the_tag(tmp_path):
    write(tmp_path, "images/i.toml", f'base = "ghcr.io/x/run:1.2@sha256:{DIGEST}"\nsetup = ["a"]\n')
    first = load_config(tmp_path).images["i"].tag
    write(tmp_path, "images/i.toml", f'base = "ghcr.io/x/run:1.2@sha256:{DIGEST}"\nsetup = ["b"]\n')
    second = load_config(tmp_path).images["i"].tag
    assert first != second
    assert first.startswith("theozolith/i:1.2-") and second.startswith("theozolith/i:1.2-")


def test_unpinned_base_is_rejected(tmp_path):
    write(tmp_path, "images/i.toml", 'base = "ghcr.io/x/run:latest"\n')
    with pytest.raises(ConfigRepoError, match="pinned by digest"):
        load_config(tmp_path)


def test_stack_format_violations_are_rejected(tmp_path):
    write(tmp_path, "stacks/bad.toml", 'kind = "quantum"\nnode = "box1"\n')
    with pytest.raises(ConfigRepoError, match="kind"):
        load_config(tmp_path)

    write(tmp_path, "stacks/bad.toml", 'kind = "process"\nnode = "box1"\n')  # no command
    with pytest.raises(ConfigRepoError, match="command"):
        load_config(tmp_path)

    write(tmp_path, "stacks/bad.toml", 'kind = "container"\nnode = "box1"\n')  # no image/compose
    with pytest.raises(ConfigRepoError, match="exactly one of image/compose"):
        load_config(tmp_path)

    write(
        tmp_path,
        "stacks/bad.toml",
        'kind = "container"\nnode = "box1"\nimage = "x"\ncompose = "y.yml"\n',  # both
    )
    with pytest.raises(ConfigRepoError, match="exactly one of image/compose"):
        load_config(tmp_path)


def test_control_stack_is_rejected_at_validation(tmp_path):
    """M8 acceptance 6: a control Stack in the Config Repo is rejected with
    the ADR-0035 pointer — the substrate never supervises its own control
    plane, whatever the Stack's kind or shape."""
    write(tmp_path, "stacks/control.toml", 'kind = "container"\nnode = "pi"\nimage = "x"\n')
    with pytest.raises(ConfigRepoError, match="ADR-0035"):
        load_config(tmp_path)
    write(tmp_path, "stacks/control.toml", 'kind = "process"\nnode = "pi"\ncommand = "x"\n')
    with pytest.raises(ConfigRepoError, match=r"never\s+supervises its own control plane"):
        load_config(tmp_path)


def test_stopped_stacks_ship_no_image_recipes(tmp_path):
    """ADR-0037 stage-don't-deploy: a stopped Stack's run_image rides no
    desired state — the daemon builds nothing until the flip to running."""
    write(
        tmp_path,
        "stacks/worker.toml",
        'kind = "process"\nnode = "box1"\nstate = "stopped"\n'
        'command = "theozolith-worker"\nrun_image = "claude-dev"\n',
    )
    write(tmp_path, "images/claude-dev.toml", f'base = "ghcr.io/x/run:1.2@sha256:{DIGEST}"\n')
    stopped = load_config(tmp_path).desired_state_for("box1")
    assert [s["name"] for s in stopped["stacks"]] == ["worker"]  # the Stack itself rides
    assert stopped["images"] == []  # its image recipe does not

    write(
        tmp_path,
        "stacks/worker.toml",
        'kind = "process"\nnode = "box1"\nstate = "running"\n'
        'command = "theozolith-worker"\nrun_image = "claude-dev"\n',
    )
    running = load_config(tmp_path).desired_state_for("box1")
    assert [i["name"] for i in running["images"]] == ["claude-dev"]


def test_compose_paths_may_not_escape_the_repo(tmp_path):
    write(
        tmp_path,
        "stacks/evil.toml",
        'kind = "container"\nnode = "box1"\ncompose = "../../etc/passwd"\n',
    )
    config = load_config(tmp_path)
    with pytest.raises(ConfigRepoError, match="escapes the Config Repo"):
        config.desired_state_for("box1")


def test_wire_stack_roundtrips_into_the_daemon_model(tmp_path):
    """The handshake: what control puts on the wire, the daemon parses —
    one shared shape, two stdlib implementations."""
    write(
        tmp_path,
        "stacks/worker.toml",
        'kind = "process"\nnode = "box1"\ncommand = "theozolith-worker --once"\n'
        '[env]\nA = "1"\n[secrets]\nT = "name"\n',
    )
    wire = load_config(tmp_path).desired_state_for("box1")["stacks"][0]
    stack = WireStack.from_wire(wire)
    assert stack.name == "worker" and stack.kind == "process"
    assert stack.command == "theozolith-worker --once"
    assert stack.env == {"A": "1"} and stack.secrets == {"T": "name"}
    assert stack.state == "running"


# -- attach argv + jobs-dir uniqueness (ADR-0019) --------------------------------


def test_attach_must_be_an_argv_array(tmp_path):
    write(
        tmp_path,
        "stacks/worker.toml",
        'kind = "process"\nnode = "box1"\ncommand = "w"\n'
        'attach = "ssh {host} -t docker exec -it {container} tmux attach"\n',
    )
    with pytest.raises(ConfigRepoError, match="argv array"):
        load_config(tmp_path)


def test_attach_placeholders_only_as_complete_arguments(tmp_path):
    write(
        tmp_path,
        "stacks/flightdeck.toml",
        'kind = "container"\nnode = "box1"\nimage = "ghcr.io/x/deck:1"\n'
        'attach = ["ssh", "user@{host}", "tmux", "attach"]\n',
    )
    with pytest.raises(ConfigRepoError, match="complete arguments"):
        load_config(tmp_path)

    write(
        tmp_path,
        "stacks/flightdeck.toml",
        'kind = "container"\nnode = "box1"\nimage = "ghcr.io/x/deck:1"\n'
        'attach = ["docker", "exec", "-it", "c-{container}", "sh"]\n',
    )
    with pytest.raises(ConfigRepoError, match="complete arguments"):
        load_config(tmp_path)

    write(
        tmp_path,
        "stacks/flightdeck.toml",
        'kind = "container"\nnode = "box1"\nimage = "ghcr.io/x/deck:1"\n'
        'attach = ["ssh", "{host}", "-t",'
        ' "docker", "exec", "-it", "{container}", "tmux", "attach", "-t", "flightdeck"]\n',
    )
    stack = load_config(tmp_path).stacks[0]
    assert stack.attach[1] == "{host}" and stack.attach[6] == "{container}"


def test_attach_is_container_kind_only(tmp_path):
    """ADR-0019: run containers are headless — a process Stack declaring an
    attach command is a Config Repo error, so no configuration can ever
    expose a run container to the terminal."""
    write(
        tmp_path,
        "stacks/worker.toml",
        'kind = "process"\nnode = "box1"\ncommand = "w"\n'
        'attach = ["ssh", "{host}", "-t", "docker", "exec", "-it", "{container}", "sh"]\n',
    )
    with pytest.raises(ConfigRepoError, match="ADR-0019"):
        load_config(tmp_path)


def test_container_command_is_single_image_form_only(tmp_path):
    write(
        tmp_path,
        "stacks/flightdeck.toml",
        'kind = "container"\nnode = "box1"\nimage = "ghcr.io/x/deck:1"\n'
        'command = "tmux new-session -d -s flightdeck claude"\n',
    )
    stack = load_config(tmp_path).stacks[0]
    assert stack.command == "tmux new-session -d -s flightdeck claude"

    write(tmp_path, "compose/deck.yml", "services: {}\n")
    write(
        tmp_path,
        "stacks/composedeck.toml",
        'kind = "container"\nnode = "box1"\ncompose = "compose/deck.yml"\ncommand = "tmux"\n',
    )
    with pytest.raises(ConfigRepoError, match="single-image container form"):
        load_config(tmp_path)


def test_duplicate_resolved_jobs_dirs_are_rejected_per_node(tmp_path):
    """Acceptance 15: two process Stacks on one node may not share a
    resolved jobs directory — including via the -pending parking sibling."""
    write(
        tmp_path,
        "stacks/worker.toml",
        'kind = "process"\nnode = "box1"\ncommand = "w"\n'
        '[env]\nTHEOZOLITH_JOBS_DIR = "/srv/jobs/shared/"\n',
    )
    write(
        tmp_path,
        "stacks/reviewer.toml",
        'kind = "process"\nnode = "box1"\ncommand = "r"\n'
        '[env]\nTHEOZOLITH_JOBS_DIR = "/srv/jobs/shared"\n',  # same after normalization
    )
    with pytest.raises(ConfigRepoError, match="collides"):
        load_config(tmp_path)

    # A jobs dir that lands on another Stack's parking sibling collides too.
    write(
        tmp_path,
        "stacks/reviewer.toml",
        'kind = "process"\nnode = "box1"\ncommand = "r"\n'
        '[env]\nTHEOZOLITH_JOBS_DIR = "/srv/jobs/shared-pending"\n',
    )
    with pytest.raises(ConfigRepoError, match="collides"):
        load_config(tmp_path)

    # The same path on DIFFERENT nodes is two different filesystems: legal.
    write(
        tmp_path,
        "stacks/reviewer.toml",
        'kind = "process"\nnode = "box2"\ncommand = "r"\n'
        '[env]\nTHEOZOLITH_JOBS_DIR = "/srv/jobs/shared"\n',
    )
    assert len(load_config(tmp_path).stacks) == 2


def test_default_jobs_dirs_are_per_stack_and_unique(tmp_path):
    """Two driver Stacks with no explicit jobs dir resolve to distinct
    per-Stack defaults (the daemon injects the same paths)."""
    from theozolith_control.configrepo import resolved_jobs_dir

    write(tmp_path, "stacks/worker.toml", 'kind = "process"\nnode = "box1"\ncommand = "w"\n')
    write(tmp_path, "stacks/reviewer.toml", 'kind = "process"\nnode = "box1"\ncommand = "r"\n')
    config = load_config(tmp_path)
    resolved = {resolved_jobs_dir(s) for s in config.stacks}
    assert resolved == {
        "/var/tmp/theozolith/jobs/worker",
        "/var/tmp/theozolith/jobs/reviewer",
    }
