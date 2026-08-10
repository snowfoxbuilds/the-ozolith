"""Config Repo parsing: worker-type + thin-Stack format (ADR-0044),
deterministic tags, scoping, hard-cutover rejections, and the wire-model
handshake with the Node Daemon."""

from __future__ import annotations

import pytest
from theozolith_control.configrepo import ConfigRepoError, load_config
from theozolith_nodedaemon.stacks import WireStack

DIGEST = "0" * 64
BASE = f"ghcr.io/snowfoxbuilds/theozolith-run-claude:1.2@sha256:{DIGEST}"


def write(tmp_path, relpath: str, text: str) -> None:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def driver_type(tmp_path, name: str = "claude-dev", **fields) -> None:
    """A minimal valid driver (pipeline) worker type."""
    body = {
        "driver": '"builtin:implementer"',
        "adapter": '"claude"',
        "workspace": '"acme/sandbox"',
        "base": f'"{BASE}"',
        **{k: v for k, v in fields.items()},
    }
    lines = [f"{k} = {v}" for k, v in body.items() if v is not None]
    write(tmp_path, f"worker-types/{name}.toml", "\n".join(lines) + "\n")


def thin_stack(tmp_path, name: str, worker_type: str, **fields) -> None:
    lines = [f'worker_type = "{worker_type}"', 'node = "box1"']
    for key, value in fields.items():
        lines.append(f"{key} = {value}")
    write(tmp_path, f"stacks/{name}.toml", "\n".join(lines) + "\n")


# -- empty / migration ----------------------------------------------------------


def test_missing_repo_is_an_empty_deployment(tmp_path):
    config = load_config(tmp_path / "nope")
    assert config.stacks == () and config.worker_types == {}
    assert config.desired_state_for("box1") == {
        "commit": "",
        "product_version": "",
        "drivers_hash": "",
        "stacks": [],
        "images": [],
    }


def test_images_directory_is_rejected_with_the_new_home(tmp_path):
    """Hard cutover (ADR-0044): images/ is absorbed into worker-types/."""
    write(tmp_path, "images/claude-dev.toml", f'base = "{BASE}"\n')
    with pytest.raises(ConfigRepoError, match=r"images/ is gone.*worker-types/"):
        load_config(tmp_path)


# -- worker types ---------------------------------------------------------------


def test_driver_worker_type_and_thin_stack_resolve_end_to_end(tmp_path):
    write(
        tmp_path,
        "worker-types/claude-dev.toml",
        f'driver = "builtin:implementer"\nadapter = "claude"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{BASE}"\nsetup = ["apt-get update"]\n'
        f'[secrets]\nGITHUB_TOKEN = "github-implementer"\n'
        f'ANTHROPIC_API_KEY = "anthropic-api-key"\n',
    )
    thin_stack(tmp_path, "implementer", "claude-dev", state='"running"')

    config = load_config(tmp_path)
    stack = next(s for s in config.stacks if s.name == "implementer")
    assert stack.kind == "process"
    assert stack.worker_type == "claude-dev"  # kept for display
    assert stack.command == "theozolith-driver builtin:implementer"
    assert stack.env == {
        "THEOZOLITH_REPO": "acme/sandbox",
        "THEOZOLITH_ADAPTER": "claude",
        "THEOZOLITH_MODEL": "claude-sonnet-5",
        "THEOZOLITH_RUN_IMAGE": config.worker_types["claude-dev"].tag,
    }
    assert stack.secrets == {
        "GITHUB_TOKEN": "github-implementer",
        "ANTHROPIC_API_KEY": "anthropic-api-key",
    }
    # Type-owned secrets are node-scoped through the resolved Stack.
    assert config.secret_names_for("box1") == {"github-implementer", "anthropic-api-key"}


def test_stack_env_overrides_injected_worker_type_env(tmp_path):
    driver_type(tmp_path, model='"claude-sonnet-5"')
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\n'
        '[env]\nTHEOZOLITH_MODEL = "claude-opus-5"\nWORKER_ID = "w1"\n',
    )
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "implementer")
    assert stack.env["THEOZOLITH_MODEL"] == "claude-opus-5"  # Stack wins
    assert stack.env["WORKER_ID"] == "w1"
    assert stack.env["THEOZOLITH_REPO"] == "acme/sandbox"


def test_model_omitted_leaves_no_model_env(tmp_path):
    driver_type(tmp_path)  # no model
    thin_stack(tmp_path, "implementer", "claude-dev")
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "implementer")
    assert "THEOZOLITH_MODEL" not in stack.env


def test_driverless_worker_type_resolves_to_a_flightdeck_container(tmp_path):
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "tmux new-session -d -s flightdeck claude"\n'
        f'volumes = ["fd-logs:/var/log/flightdeck"]\nworkspace = "acme/sandbox"\n'
        f'[secrets]\nGITHUB_TOKEN = "flightdeck-github-token"\n',
    )
    thin_stack(
        tmp_path,
        "flightdeck",
        "flightdeck",
        attach='["ssh", "{host}", "-t", "docker", "exec", "-it", "{container}", "sh"]',
    )
    config = load_config(tmp_path)
    stack = next(s for s in config.stacks if s.name == "flightdeck")
    assert stack.kind == "container"
    assert stack.image == config.worker_types["flightdeck"].tag
    assert stack.command == "tmux new-session -d -s flightdeck claude"
    assert stack.volumes == ("fd-logs:/var/log/flightdeck",)
    assert stack.env == {"THEOZOLITH_REPO": "acme/sandbox"}
    assert stack.attach[1] == "{host}" and stack.attach[6] == "{container}"
    assert stack.secrets == {"GITHUB_TOKEN": "flightdeck-github-token"}


# -- CRITICAL: derived-image identity must not change (ADR-0044) -----------------


def test_tag_is_golden_stable_over_image_fields_only(tmp_path):
    """A fixed set of image inputs must produce the EXACT tag today's formula
    computes — driver/adapter/model/workspace/secrets excluded. Renaming an
    images/<name>.toml into worker-types/ with unchanged image fields must
    rebuild nothing. This is the easiest thing to get wrong."""
    golden_base = "ghcr.io/snowfoxbuilds/theozolith-run-claude:0.3.0@sha256:" + "a" * 64
    write(
        tmp_path,
        "worker-types/goldtype.toml",
        f'driver = "builtin:implementer"\nworkspace = "acme/sandbox"\n'
        f'base = "{golden_base}"\n'
        f'setup = ["apt-get update && apt-get install -y ripgrep"]\n'
        f'knowledge_source = "https://github.com/acme/my-knowledge.git"\n'
        f'knowledge_pin = "{"b" * 40}"\n'
        # These do NOT enter the hash:
        f'adapter = "claude"\nmodel = "claude-sonnet-5"\n'
        f'[secrets]\nGITHUB_TOKEN = "github-implementer"\n',
    )
    wt = load_config(tmp_path).worker_types["goldtype"]
    assert wt.tag == "theozolith/goldtype:0.3.0-48a66bc6e009"
    assert wt.instruction_hash == (
        "48a66bc6e009a3a84ebaf7bf7d05dc2c9df09851e4fddfb9344eacffbfd59f68"
    )


def test_per_type_fields_do_not_change_the_tag(tmp_path):
    driver_type(tmp_path, name="a", model='"claude-sonnet-5"')
    driver_type(tmp_path, name="b", model='"claude-opus-5"', adapter='"claude"')
    types = load_config(tmp_path).worker_types
    # Same image fields, different model -> identical instruction hash (only
    # the name differs in the tag prefix).
    assert types["a"].instruction_hash == types["b"].instruction_hash


def test_image_field_changes_change_the_tag(tmp_path):
    driver_type(tmp_path, name="i", setup='["a"]')
    first = load_config(tmp_path).worker_types["i"].tag
    driver_type(tmp_path, name="i", setup='["b"]')
    second = load_config(tmp_path).worker_types["i"].tag
    assert first != second
    assert first.startswith("theozolith/i:1.2-") and second.startswith("theozolith/i:1.2-")


# -- worker-type validation -----------------------------------------------------


def test_unpinned_base_is_rejected(tmp_path):
    write(
        tmp_path,
        "worker-types/i.toml",
        'driver = "builtin:implementer"\nworkspace = "a/b"\nbase = "ghcr.io/x/run:latest"\n',
    )
    with pytest.raises(ConfigRepoError, match="pinned by digest"):
        load_config(tmp_path)


def test_unknown_builtin_driver_lists_the_known_set(tmp_path):
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "builtin:teleporter"\nworkspace = "a/b"\nbase = "{BASE}"\n',
    )
    with pytest.raises(ConfigRepoError, match=r"unknown built-in driver.*builtin:implementer"):
        load_config(tmp_path)


def test_bad_driver_ref_shape_is_rejected(tmp_path):
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "implementer"\nworkspace = "a/b"\nbase = "{BASE}"\n',
    )
    with pytest.raises(ConfigRepoError, match=r"builtin:<name>.*drivers/<name>"):
        load_config(tmp_path)


def test_custom_driver_ref_errors_at_load_not_yet_implemented(tmp_path):
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "drivers/custom"\nworkspace = "a/b"\nbase = "{BASE}"\n',
    )
    thin_stack(tmp_path, "s", "i")
    with pytest.raises(ConfigRepoError, match=r"driver delivery is not yet implemented.*ADR-0042"):
        load_config(tmp_path)


def test_driver_requires_workspace(tmp_path):
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "builtin:implementer"\nbase = "{BASE}"\n',
    )
    with pytest.raises(ConfigRepoError, match=r"'workspace'.*required when a driver is set"):
        load_config(tmp_path)


def test_workspace_must_be_owner_name(tmp_path):
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "builtin:implementer"\nworkspace = "justname"\nbase = "{BASE}"\n',
    )
    with pytest.raises(ConfigRepoError, match="must be owner/name"):
        load_config(tmp_path)


@pytest.mark.parametrize("field", ["command", "volumes"])
def test_driverless_fields_are_rejected_with_a_driver(tmp_path, field):
    value = '"x"' if field == "command" else '["v:/p"]'
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "builtin:implementer"\nworkspace = "a/b"\nbase = "{BASE}"\n{field} = {value}\n',
    )
    with pytest.raises(ConfigRepoError, match=rf"'{field}' is a driverless"):
        load_config(tmp_path)


# -- thin-Stack validation & hard-cutover rejections ----------------------------


def test_run_image_on_any_stack_is_rejected(tmp_path):
    write(
        tmp_path,
        "stacks/worker.toml",
        'kind = "process"\nnode = "box1"\ncommand = "sleep 30"\nrun_image = "claude-dev"\n',
    )
    with pytest.raises(ConfigRepoError, match=r"run_image is gone.*worker_type.*ADR-0044"):
        load_config(tmp_path)


def test_fat_fields_on_a_worker_type_stack_name_the_new_home(tmp_path):
    driver_type(tmp_path)
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\nkind = "process"\n',
    )
    with pytest.raises(ConfigRepoError, match=r"'kind' moved to worker-types/claude-dev.toml"):
        load_config(tmp_path)

    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\ncommand = "x"\n',
    )
    with pytest.raises(ConfigRepoError, match=r"'command' moved to worker-types/"):
        load_config(tmp_path)


def test_unknown_key_on_a_worker_type_stack_is_rejected(tmp_path):
    driver_type(tmp_path)
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\nnonsense = "x"\n',
    )
    with pytest.raises(ConfigRepoError, match="unknown key 'nonsense'"):
        load_config(tmp_path)


def test_builtin_driver_map_matches_the_launcher_registry():
    """The one place control names driver commands (BUILTIN_DRIVERS) and the
    one place the launcher resolves refs to classes (drivercli.BUILTIN_WORKERS)
    must carry identical keys, or a worker type control resolves would name a
    ref the launcher cannot run (ADR-0020). Test-time import of the worker
    launcher only — control never imports it at runtime."""
    from theozolith_control.configrepo import BUILTIN_DRIVERS
    from theozolith_worker.drivercli import BUILTIN_WORKERS

    assert set(BUILTIN_DRIVERS) == set(BUILTIN_WORKERS)
    # Each command routes through the one launcher with its own ref as argv.
    for ref, command in BUILTIN_DRIVERS.items():
        assert command == f"theozolith-driver {ref}"


def test_builtin_driver_as_plain_command_is_rejected(tmp_path):
    """A plain process Stack may not invoke the driver launcher directly — any
    ref, builtin:* or drivers/* — the driver only works with the env a worker
    type injects (ADR-0044/ADR-0020)."""
    for cmd in ("theozolith-driver builtin:implementer", "theozolith-driver drivers/x"):
        write(tmp_path, "stacks/w.toml", f'kind = "process"\nnode = "box1"\ncommand = "{cmd}"\n')
        with pytest.raises(ConfigRepoError, match=r"invokes the driver launcher.*worker_type"):
            load_config(tmp_path)


def test_missing_worker_type_definition_is_rejected(tmp_path):
    thin_stack(tmp_path, "implementer", "ghost")
    with pytest.raises(ConfigRepoError, match=r"worker_type 'ghost' has no worker-types/ghost"):
        load_config(tmp_path)


def test_attach_on_a_driver_worker_type_stack_is_rejected(tmp_path):
    driver_type(tmp_path)
    thin_stack(
        tmp_path,
        "implementer",
        "claude-dev",
        attach='["ssh", "{host}", "-t", "docker", "exec", "-it", "{container}", "sh"]',
    )
    with pytest.raises(ConfigRepoError, match="only valid on container-kind Stacks"):
        load_config(tmp_path)


# -- generic (substrate) Stacks stay legal --------------------------------------


def test_plain_generic_stacks_remain_fully_legal(tmp_path):
    """The substrate keeps its workload-agnostic Stack format: a plain Stack
    with no worker_type is unchanged (ADR-0044)."""
    write(
        tmp_path,
        "stacks/flightdeck.toml",
        'kind = "container"\nnode = "pi"\ncompose = "compose/flightdeck.yml"\n'
        'overlays = ["overlays/ts.yml"]\n',
    )
    write(tmp_path, "compose/flightdeck.yml", "services: {}\n")
    write(tmp_path, "overlays/ts.yml", "services: {}\n")
    write(tmp_path, "stacks/job.toml", 'kind = "process"\nnode = "box1"\ncommand = "sleep 30"\n')

    config = load_config(tmp_path)
    assert {s.name: s.kind for s in config.stacks} == {
        "flightdeck": "container",
        "job": "process",
    }
    pi = config.desired_state_for("pi")
    files = pi["stacks"][0]["compose_files"]
    assert [f["name"] for f in files] == ["compose/flightdeck.yml", "overlays/ts.yml"]


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


# -- desired state / stage-don't-deploy -----------------------------------------


def test_stopped_worker_type_stacks_ship_no_image_recipes(tmp_path):
    """ADR-0037 stage-don't-deploy: a stopped Stack's derived image rides no
    desired state — the daemon builds nothing until the flip to running.
    Recipes ship for running Stacks of both kinds via worker_type."""
    driver_type(tmp_path)
    thin_stack(tmp_path, "implementer", "claude-dev", state='"stopped"')
    stopped = load_config(tmp_path).desired_state_for("box1")
    assert [s["name"] for s in stopped["stacks"]] == ["implementer"]  # the Stack itself rides
    assert stopped["images"] == []  # its image recipe does not

    thin_stack(tmp_path, "implementer", "claude-dev", state='"running"')
    running = load_config(tmp_path).desired_state_for("box1")
    assert [i["name"] for i in running["images"]] == ["claude-dev"]


def test_flightdeck_image_recipe_ships_when_running(tmp_path):
    """Both kinds' recipes ride: the Flight Deck's derived image builds
    through the same running-only list as a driver's (ADR-0044)."""
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "sleep 30"\n',
    )
    thin_stack(tmp_path, "flightdeck", "flightdeck", state='"running"')
    running = load_config(tmp_path).desired_state_for("box1")
    assert [i["name"] for i in running["images"]] == ["flightdeck"]


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
    """The handshake: what control puts on the wire (a resolved worker-type
    Stack is an ordinary Stack), the daemon parses — one shared shape, two
    stdlib implementations. run_image is gone from the wire (ADR-0044)."""
    driver_type(tmp_path, model='"claude-sonnet-5"')
    thin_stack(tmp_path, "implementer", "claude-dev")
    wire = load_config(tmp_path).desired_state_for("box1")["stacks"][0]
    assert "run_image" not in wire
    stack = WireStack.from_wire(wire)
    assert stack.name == "implementer" and stack.kind == "process"
    assert stack.command == "theozolith-driver builtin:implementer"
    assert stack.env["THEOZOLITH_REPO"] == "acme/sandbox"
    assert stack.env["THEOZOLITH_RUN_IMAGE"].startswith("theozolith/claude-dev:")
    assert stack.state == "running"


# -- attach argv + jobs-dir uniqueness (ADR-0019) --------------------------------


def test_attach_must_be_an_argv_array(tmp_path):
    write(
        tmp_path,
        "stacks/deck.toml",
        'kind = "container"\nnode = "box1"\nimage = "x"\n'
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
        "stacks/job.toml",
        'kind = "process"\nnode = "box1"\ncommand = "sleep 30"\n'
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


def test_default_jobs_dirs_are_per_stack_and_unique_across_resolved_workers(tmp_path):
    """Two worker Stacks with no explicit jobs dir resolve to distinct
    per-Stack defaults; the check runs on the RESOLVED (process-kind) Stacks."""
    from theozolith_control.configrepo import resolved_jobs_dir

    write(
        tmp_path,
        "worker-types/claude-dev.toml",
        f'driver = "builtin:implementer"\nworkspace = "acme/sandbox"\nbase = "{BASE}"\n',
    )
    write(
        tmp_path,
        "worker-types/claude-review.toml",
        f'driver = "builtin:reviewer"\nworkspace = "acme/sandbox"\nbase = "{BASE}"\n',
    )
    thin_stack(tmp_path, "implementer", "claude-dev")
    thin_stack(tmp_path, "reviewer", "claude-review")
    config = load_config(tmp_path)
    resolved = {resolved_jobs_dir(s) for s in config.stacks}
    assert resolved == {
        "/var/tmp/theozolith/jobs/implementer",
        "/var/tmp/theozolith/jobs/reviewer",
    }


# -- execution-equivalent argv parsing & workspace shape (ADR-0044 amendment) ----


def test_quoted_builtin_command_is_rejected(tmp_path):
    """The hard-cutover guard parses argv with shlex (execution semantics), so
    a quoted built-in cannot slip past a naive whitespace split."""
    for cmd in ("'\"theozolith-driver\" builtin:implementer'", "'\"theozolith-driver\"'"):
        write(tmp_path, "stacks/w.toml", f'kind = "process"\nnode = "box1"\ncommand = {cmd}\n')
        with pytest.raises(ConfigRepoError, match=r"invokes the driver launcher.*worker_type"):
            load_config(tmp_path)


def test_malformed_command_quoting_raises_config_repo_error(tmp_path):
    toml = 'kind = "process"\nnode = "box1"\ncommand = \'sleep "30\'\n'
    write(tmp_path, "stacks/w.toml", toml)
    with pytest.raises(ConfigRepoError, match=r"not valid shell syntax"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "bad", ["/repository", "owner/", "owner/repository/extra", "///", " / ", "owner/ "]
)
def test_malformed_workspace_is_rejected(tmp_path, bad):
    driver_type(tmp_path, workspace=f'"{bad}"')
    with pytest.raises(ConfigRepoError, match="owner/name"):
        load_config(tmp_path)


def test_normal_owner_repository_workspace_is_accepted(tmp_path):
    driver_type(tmp_path, workspace='"acme/sandbox"')
    thin_stack(tmp_path, "implementer", "claude-dev")
    assert load_config(tmp_path).worker_types["claude-dev"].workspace == "acme/sandbox"


def test_driverless_workspace_shape_is_enforced(tmp_path):
    """The two-component rule applies to the optional driverless workspace too."""
    write(tmp_path, "worker-types/fd.toml", f'base = "{BASE}"\nworkspace = "owner/repo/extra"\n')
    with pytest.raises(ConfigRepoError, match="owner/name"):
        load_config(tmp_path)


# -- config distribution (ADR-0042) ---------------------------------------------


def test_drivers_hash_rides_desired_state_and_is_empty_by_default(tmp_path):
    """No drivers/ → hash "", still always present on the wire."""
    write(tmp_path, "stacks/x.toml", 'kind = "process"\nnode = "box1"\ncommand = "sleep 1"\n')
    config = load_config(tmp_path)
    assert config.drivers_hash == ""
    assert config.desired_state_for("box1")["drivers_hash"] == ""


def test_drivers_content_produces_a_hash_on_the_wire(tmp_path):
    write(tmp_path, "drivers/custom/impl.py", "def run():\n    return 1\n")
    config = load_config(tmp_path)
    assert config.drivers_hash and len(config.drivers_hash) == 64
    assert config.desired_state_for("box1")["drivers_hash"] == config.drivers_hash


def test_folder_mode_commit_bumps_on_a_drivers_edit(tmp_path):
    """Folder mode (no .git): a drivers/*.py edit must change the commit so
    nodes see the change — the pre-ADR-0042 *.toml-only hash never did."""
    write(tmp_path, "drivers/custom/impl.py", "def run():\n    return 1\n")
    before = load_config(tmp_path).commit
    assert before.startswith("folder-")
    write(tmp_path, "drivers/custom/impl.py", "def run():\n    return 2\n")
    after = load_config(tmp_path).commit
    assert after.startswith("folder-") and after != before


def test_symlinked_drivers_root_surfaces_as_config_repo_error(tmp_path):
    """A config-distribution validation failure (a symlinked drivers root) is
    normalized to ConfigRepoError at the loading boundary, NOT a raw
    ConfigDistError — so the API turns it into the documented config-repo error
    and dispatch stays fail-open (ADR-0042)."""
    import os

    external = tmp_path / "external"
    external.mkdir()
    (external / "x.py").write_text("escape\n", encoding="utf-8")
    os.symlink(external, tmp_path / "drivers")
    with pytest.raises(ConfigRepoError, match="config distribution"):
        load_config(tmp_path)


@pytest.mark.skipif(
    getattr(__import__("os"), "geteuid", lambda: 1)() == 0, reason="root bypasses file permissions"
)
def test_unreadable_drivers_file_surfaces_as_config_repo_error(tmp_path):
    import os

    write(tmp_path, "drivers/custom/impl.py", "def run():\n    return 1\n")
    target = tmp_path / "drivers" / "custom" / "impl.py"
    os.chmod(target, 0)
    try:
        with pytest.raises(ConfigRepoError, match="config distribution"):
            load_config(tmp_path)
    finally:
        os.chmod(target, 0o644)


def test_refuse_ui_write_rejects_drivers_paths(tmp_path):
    from theozolith_control import configrepo

    for bad in (
        "drivers/x.py",
        "drivers/custom/impl.py",
        "drivers",
        "drivers/",  # trailing slash: an empty component, refused as malformed
    ):
        with pytest.raises(configrepo.ConfigRepoError):
            configrepo.refuse_ui_write(bad)


def test_refuse_ui_write_rejects_aliased_drivers_spellings(tmp_path):
    """No repository-relative spelling may resolve under drivers/ while passing
    the guard (ADR-0042 amendment): dot/dot-dot aliases, backslash variants,
    and absolute paths are all refused outright — parsed as path components,
    never matched by string prefix against a partially normalized value."""
    from theozolith_control import configrepo

    for bad in (
        "./drivers/x.py",
        "stacks/../drivers/x.py",
        "drivers/../drivers/x.py",
        "drivers\\x.py",
        ".\\drivers\\x.py",
        "stacks\\..\\drivers\\x.py",
        "/drivers/x.py",
        "\\drivers\\x.py",
        "//drivers/x.py",
        "C:\\drivers\\x.py",
        "C:/drivers/x.py",
        "/etc/passwd",  # any absolute path is malformed for a repo write
        "stacks//x.toml",  # an empty component is malformed
        "stacks/./x.toml",  # a '.' component is refused, never resolved
        "stacks/../stacks/x.toml",  # '..' is refused even when it stays outside drivers/
        "",
    ):
        with pytest.raises(configrepo.ConfigRepoError):
            configrepo.refuse_ui_write(bad)


def test_refuse_ui_write_allows_other_paths(tmp_path):
    from theozolith_control import configrepo

    # No raise for the allow-listed fixed-filename writers' targets, nor for
    # ordinary repo-relative paths a future editor would legitimately write.
    configrepo.refuse_ui_write("control.toml")
    configrepo.refuse_ui_write("product.toml")
    configrepo.refuse_ui_write("stacks/x.toml")
    configrepo.refuse_ui_write("worker-types/x.toml")
    configrepo.refuse_ui_write("stacks/drivers.toml")  # 'drivers' as a filename is fine
    configrepo.refuse_ui_write("drivers.toml")  # a top-level file merely NAMED drivers*
    configrepo.refuse_ui_write("compose/app/overlay.yaml")
