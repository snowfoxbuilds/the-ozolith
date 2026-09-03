"""Config Repo parsing: worker-type + thin-Stack format (ADR-0044),
deterministic tags, scoping, hard-cutover rejections, and the wire-model
handshake with the Node Daemon."""

from __future__ import annotations

from pathlib import Path

import pytest
from theozolith_control.configrepo import ConfigRepoError, load_config
from theozolith_nodedaemon.stacks import WireStack

DIGEST = "0" * 64
BASE = f"ghcr.io/snowfoxbuilds/theozolith-run-claude:1.2@sha256:{DIGEST}"


def write(tmp_path, relpath: str, text: str) -> None:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def driver_type(tmp_path, name: str = "claude-dev", secrets: dict | None = None, **fields) -> None:
    """A minimal valid driver (pipeline) worker type. ``model`` is required
    with a driver (ADR-0045); pass ``model=None`` to omit it. ``secrets``
    becomes the type's ``[secrets]`` table (slot -> stored name)."""
    body = {
        "driver": '"builtin:implementer"',
        "adapter": '"claude"',
        "model": '"claude-sonnet-5"',
        "workspace": '"acme/sandbox"',
        "base": f'"{BASE}"',
        **{k: v for k, v in fields.items()},
    }
    lines = [f"{k} = {v}" for k, v in body.items() if v is not None]
    if secrets is not None:
        lines.append("[secrets]")
        lines.extend(f'{key} = "{value}"' for key, value in secrets.items())
    write(tmp_path, f"worker-types/{name}.toml", "\n".join(lines) + "\n")


def thin_stack(tmp_path, name: str, worker_type: str, **fields) -> None:
    lines = [f'worker_type = "{worker_type}"', 'node = "box1"']
    for key, value in fields.items():
        lines.append(f"{key} = {value}")
    write(tmp_path, f"stacks/{name}.toml", "\n".join(lines) + "\n")


def write_pins(
    tmp_path,
    *,
    knowledge: dict[str, str] | None = None,
    base: dict[str, str] | None = None,
    policy: dict[str, str] | None = None,
    cli: dict[str, dict] | None = None,
) -> None:
    """A machine-shaped pins.toml (ADR-0048/0055): what `theozolith config
    ingest` would have written for the given knowledge trees / base
    resolutions / policy trees / CLI pins."""
    lines = ['[source]\ncommit = "cafe1234"']
    if base:
        lines.append("[base]")
        lines.extend(f'"{ref}" = "{digest}"' for ref, digest in base.items())
    if knowledge:
        lines.append("[knowledge]")
        lines.extend(f'"{name}" = "{tree_hash}"' for name, tree_hash in knowledge.items())
    if policy:
        lines.append("[policy]")
        lines.extend(f'"{name}" = "{tree_hash}"' for name, tree_hash in policy.items())
    for key, pin in (cli or {}).items():
        lines.append(f'[cli."{key}"]\nversion = "{pin["version"]}"')
        lines.append(f'[cli."{key}".platforms]')
        lines.extend(
            f'"{tuple_key}" = {{ ' + ", ".join(f'{k} = "{v}"' for k, v in entry.items()) + " }"
            for tuple_key, entry in pin["platforms"].items()
        )
    write(tmp_path, "pins.toml", "\n".join(lines) + "\n")


def cli_pin(version: str = "2.1.257", tuples: tuple[str, ...] | None = None) -> dict:
    """An ingest-shaped CLI pin record (ADR-0055)."""
    tuples = tuples or ("linux-x64-glibc", "linux-arm64-glibc", "linux-x64-musl")
    return {
        "version": version,
        "platforms": {
            key: {"package": f"@anthropic-ai/claude-code-{key}", "integrity": "sha512-" + "A" * 96}
            for key in tuples
        },
    }


def deck_type(tmp_path, name: str = "flightdeck", **fields) -> None:
    """A minimal driverless (Flight Deck) worker type."""
    body = {"base": f'"{BASE}"', "command": '"flightdeck-start"', **fields}
    lines = [f"{k} = {v}" for k, v in body.items() if v is not None]
    write(tmp_path, f"worker-types/{name}.toml", "\n".join(lines) + "\n")


def write_knowledge_tree(tmp_path, name: str, files: dict[str, str] | None = None) -> None:
    """A compiled knowledge tree in the pinned build (ADR-0048)."""
    for relpath, text in (files or {"CLAUDE.md": "# k\n"}).items():
        write(tmp_path, f"knowledge/{name}/{relpath}", text)


POLICY_DOC = '{"attribution": {"sessionUrl": false}}\n'


def write_policy_tree(tmp_path, name: str, files: dict[str, str] | None = None) -> None:
    """An Agent Policy tree in the pinned build (ADR-0055)."""
    for relpath, text in (files or {"attribution.json": POLICY_DOC}).items():
        write(tmp_path, f"policy/{name}/{relpath}", text)


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
    # No THEOZOLITH_MODEL: the model is baked into the run image (ADR-0045),
    # never delivered as env — a model change rolls the fleet via the tag.
    assert stack.env == {
        "THEOZOLITH_REPO": "acme/sandbox",
        "THEOZOLITH_ADAPTER": "claude",
        "THEOZOLITH_RUN_IMAGE": config.worker_types["claude-dev"].tag,
    }
    assert stack.secrets == {
        "GITHUB_TOKEN": "github-implementer",
        "ANTHROPIC_API_KEY": "anthropic-api-key",
    }
    # Type-owned secrets are node-scoped through the resolved Stack, plus the
    # running type's registry pull credential (ADR-0049).
    assert config.secret_names_for("box1") == {
        "github-implementer",
        "anthropic-api-key",
        "registry:ghcr.io",
    }


def test_stack_env_overrides_injected_worker_type_env(tmp_path):
    driver_type(tmp_path)
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\n'
        '[env]\nWORKER_ID = "w1"\nTHEOZOLITH_REPO = "acme/other"\n',
    )
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "implementer")
    assert stack.env["WORKER_ID"] == "w1"
    assert stack.env["THEOZOLITH_REPO"] == "acme/other"  # Stack still wins


def test_stack_env_model_and_adapter_overrides_are_rejected(tmp_path):
    """ADR-0045: with the model baked into the image, a Stack [env] override
    would be silently inert — rejected by exact name instead. Other env stays
    free (per-placement overrides are the point of Stack [env])."""
    driver_type(tmp_path)
    for key in ("THEOZOLITH_MODEL", "THEOZOLITH_ADAPTER"):
        write(
            tmp_path,
            "stacks/implementer.toml",
            f'worker_type = "claude-dev"\nnode = "box1"\n[env]\n{key} = "claude-opus-5"\n',
        )
        with pytest.raises(ConfigRepoError, match=f"{key} is gone.*ADR-0045"):
            load_config(tmp_path)


def test_stack_env_run_image_override_is_rejected(tmp_path):
    """ADR-0045: the run-image tag IS the model — a per-placement
    THEOZOLITH_RUN_IMAGE override would silently run a different identity
    than the worker-type definition declares (the dry-run would validate the
    substituted image's own baked identity and pass)."""
    driver_type(tmp_path)
    for key in ("THEOZOLITH_RUN_IMAGE", "THEOZOLITH_RUN_IMAGE_FILE"):
        write(
            tmp_path,
            "stacks/implementer.toml",
            f'worker_type = "claude-dev"\nnode = "box1"\n'
            f'[env]\n{key} = "theozolith/claude-dev:0.3.0-oldhash"\n',
        )
        with pytest.raises(ConfigRepoError, match=f"{key} cannot be overridden"):
            load_config(tmp_path)


def test_generic_stack_env_keeps_model_keys_free(tmp_path):
    """The rejection is worker-type-Stack-only: a plain generic Stack's env
    is workload-owned and the substrate does not police its names."""
    write(
        tmp_path,
        "stacks/plainproc.toml",
        'kind = "process"\nnode = "box1"\ncommand = "run-thing"\n'
        '[env]\nTHEOZOLITH_MODEL = "whatever"\n',
    )
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "plainproc")
    assert stack.env["THEOZOLITH_MODEL"] == "whatever"


def test_model_required_when_driver_is_set(tmp_path):
    driver_type(tmp_path, model=None)
    with pytest.raises(ConfigRepoError, match="'model' is required when a driver"):
        load_config(tmp_path)


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


def test_stack_placeholder_substituted_in_worker_type_volumes(tmp_path):
    """ADR-0043/0048: {stack} in a driverless type's volume names resolves to
    the Stack name (per-Flight-Deck state/tailnet identity), while an entry
    without the placeholder — the read-only knowledge bind — is left literal
    (shared across siblings of the type). Two same-type Flight Decks on one
    node therefore get distinct state/tailscale volumes and the same knowledge
    mount."""
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "/usr/local/bin/flightdeck-start"\n'
        "volumes = [\n"
        '    "{stack}-logs:/var/log/flightdeck",\n'
        '    "{stack}-claude-state:/home/ozolith/.claude",\n'
        '    "/var/lib/theozolith/knowledge:/var/lib/theozolith/knowledge:ro",\n'
        '    "{stack}-tailscale-state:/var/lib/tailscale",\n'
        "]\n",
    )
    thin_stack(tmp_path, "flightdeck-box1", "flightdeck")
    thin_stack(tmp_path, "flightdeck-box2", "flightdeck")
    config = load_config(tmp_path)

    box1 = next(s for s in config.stacks if s.name == "flightdeck-box1")
    box2 = next(s for s in config.stacks if s.name == "flightdeck-box2")
    assert box1.volumes == (
        "flightdeck-box1-logs:/var/log/flightdeck",
        "flightdeck-box1-claude-state:/home/ozolith/.claude",
        # shared, not substituted; the :ro suffix survives untouched
        "/var/lib/theozolith/knowledge:/var/lib/theozolith/knowledge:ro",
        "flightdeck-box1-tailscale-state:/var/lib/tailscale",
    )
    # Distinct per-instance state/tailnet volumes; identical shared knowledge.
    state1 = {v for v in box1.volumes if "claude-state" in v or "tailscale-state" in v}
    state2 = {v for v in box2.volumes if "claude-state" in v or "tailscale-state" in v}
    assert state1.isdisjoint(state2)
    knowledge = "/var/lib/theozolith/knowledge:/var/lib/theozolith/knowledge:ro"
    assert knowledge in box1.volumes and knowledge in box2.volumes


def test_stack_placeholder_is_not_substituted_on_generic_stack_volumes(tmp_path):
    """A plain generic Stack owns its volumes verbatim — {stack} is a
    worker-type resolution placeholder only, never touched on generic Stacks."""
    write(
        tmp_path,
        "stacks/plain.toml",
        'kind = "container"\nnode = "box1"\nimage = "busybox"\nvolumes = ["{stack}-data:/data"]\n',
    )
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "plain")
    assert stack.volumes == ("{stack}-data:/data",)  # literal, unresolved


# -- tmpfs Stack field (#109, grilling 2026-09-02) -------------------------------


def test_tmpfs_on_a_driverless_worker_type_resolves_verbatim(tmp_path):
    """A driverless (Flight Deck) type's tmpfs rides through to the resolved
    Stack with NO {stack} substitution — tmpfs entries are container paths only,
    unlike volumes (per-instance named-volume identity)."""
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "sleep 30"\nworkspace = "acme/sandbox"\n'
        'tmpfs = ["/tmp:size=8g", "/scratch"]\n',
    )
    thin_stack(tmp_path, "flightdeck", "flightdeck")
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "flightdeck")
    assert stack.tmpfs == ("/tmp:size=8g", "/scratch")


def test_tmpfs_on_a_generic_container_stack_parses(tmp_path):
    write(
        tmp_path,
        "stacks/plain.toml",
        'kind = "container"\nnode = "box1"\nimage = "busybox"\ntmpfs = ["/tmp:size=1g"]\n',
    )
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "plain")
    assert stack.tmpfs == ("/tmp:size=1g",)


def test_tmpfs_rejected_on_a_thin_worker_type_stack(tmp_path):
    """A thin worker-type Stack declaring tmpfs is rejected by name, exactly
    like volumes — the field belongs on the worker-type definition."""
    driver_type(tmp_path, "claude-dev")
    thin_stack(tmp_path, "impl", "claude-dev", tmpfs='["/tmp:size=1g"]')
    with pytest.raises(ConfigRepoError, match=r"'tmpfs' moved to worker-types/claude-dev.toml"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "entry",
    [
        "tmp:size=1g",  # relative path (no leading /)
        "/tmp:",  # empty opts suffix
        "/tmp:size 1g",  # whitespace in opts
    ],
)
def test_tmpfs_malformed_entry_fails_naming_the_file(tmp_path, entry):
    write(
        tmp_path,
        "stacks/plain.toml",
        f'kind = "container"\nnode = "box1"\nimage = "busybox"\ntmpfs = ["{entry}"]\n',
    )
    with pytest.raises(ConfigRepoError, match=r"stacks/plain.toml"):
        load_config(tmp_path)


def test_tmpfs_travels_the_wire(tmp_path):
    """StackDef.as_wire -> WireStack.from_wire carries tmpfs; a Stack without it
    round-trips to an empty tuple (absent key -> empty, advisory skew)."""
    write(
        tmp_path,
        "stacks/deck.toml",
        'kind = "container"\nnode = "box1"\nimage = "busybox"\ntmpfs = ["/tmp:size=2g"]\n',
    )
    write(tmp_path, "stacks/plain.toml", 'kind = "container"\nnode = "box1"\nimage = "busybox"\n')
    wire = load_config(tmp_path).desired_state_for("box1")["stacks"]
    by_name = {s["name"]: s for s in wire}
    assert by_name["deck"]["tmpfs"] == ["/tmp:size=2g"]
    assert WireStack.from_wire(by_name["deck"]).tmpfs == ("/tmp:size=2g",)
    # Absent on the source Stack -> empty on the wire and in the daemon model.
    assert by_name["plain"]["tmpfs"] == []
    assert WireStack.from_wire(by_name["plain"]).tmpfs == ()
    # And a wire dict with no tmpfs key at all still degrades to empty.
    no_key = {k: v for k, v in by_name["plain"].items() if k != "tmpfs"}
    assert WireStack.from_wire(no_key).tmpfs == ()


# -- per-Stack bindings (ADR-0047) -----------------------------------------------


def test_stack_secrets_rebind_a_worker_type_slot(tmp_path):
    """Two Stacks of one type act as distinct identities by rebinding a slot
    to different stored secret names — the whole point of ADR-0047."""
    driver_type(
        tmp_path,
        secrets={"GITHUB_TOKEN": "github-implementer", "ANTHROPIC_API_KEY": "anthropic-api-key"},
    )
    for name, binding in (("impl-a", "github-impl-a"), ("impl-b", "github-impl-b")):
        write(
            tmp_path,
            f"stacks/{name}.toml",
            f'worker_type = "claude-dev"\nnode = "box1"\n[secrets]\nGITHUB_TOKEN = "{binding}"\n',
        )
    config = load_config(tmp_path)
    by_name = {s.name: s for s in config.stacks}
    # Rebound slot per Stack; the untouched slot inherits the type default.
    assert by_name["impl-a"].secrets == {
        "GITHUB_TOKEN": "github-impl-a",
        "ANTHROPIC_API_KEY": "anthropic-api-key",
    }
    assert by_name["impl-b"].secrets == {
        "GITHUB_TOKEN": "github-impl-b",
        "ANTHROPIC_API_KEY": "anthropic-api-key",
    }
    # Node scoping unions the RESOLVED bindings (issue #60 invariant #3),
    # plus the running type's registry pull credential (ADR-0049).
    assert config.secret_names_for("box1") == {
        "github-impl-a",
        "github-impl-b",
        "anthropic-api-key",
        "registry:ghcr.io",
    }


def test_stack_secrets_augment_with_a_new_slot(tmp_path):
    driver_type(tmp_path, secrets={"GITHUB_TOKEN": "github-implementer"})
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\n[secrets]\nEXTRA_TOKEN = "extra"\n',
    )
    config = load_config(tmp_path)
    stack = next(s for s in config.stacks if s.name == "implementer")
    assert stack.secrets == {"GITHUB_TOKEN": "github-implementer", "EXTRA_TOKEN": "extra"}
    assert config.secret_names_for("box1") == {
        "github-implementer",
        "extra",
        "registry:ghcr.io",  # running type's base pull credential (ADR-0049)
    }


def test_type_empty_secret_declares_a_required_slot(tmp_path):
    """Type ``ENV = ""`` = a required slot with no default: every
    instantiating Stack must bind it, and the failure is at config load on
    the Control Node — never a silent 404 at deploy time."""
    driver_type(tmp_path, secrets={"GITHUB_TOKEN": ""})
    write(
        tmp_path,
        "stacks/bound.toml",
        'worker_type = "claude-dev"\nnode = "box1"\n[secrets]\nGITHUB_TOKEN = "github-impl-a"\n',
    )
    config = load_config(tmp_path)
    stack = next(s for s in config.stacks if s.name == "bound")
    assert stack.secrets == {"GITHUB_TOKEN": "github-impl-a"}

    thin_stack(tmp_path, "unbound", "claude-dev")
    with pytest.raises(
        ConfigRepoError, match=r"stacks/unbound\.toml.*GITHUB_TOKEN is a required slot"
    ):
        load_config(tmp_path)


def test_stack_cannot_unbind_a_required_slot(tmp_path):
    driver_type(tmp_path, secrets={"GITHUB_TOKEN": ""})
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\n[secrets]\nGITHUB_TOKEN = ""\n',
    )
    with pytest.raises(ConfigRepoError, match=r"GITHUB_TOKEN is a required slot"):
        load_config(tmp_path)


def test_stack_empty_secret_unbinds_an_inherited_default(tmp_path):
    """Stack ``ENV = ""`` = deliberate per-placement unbind of a type
    default — e.g. dropping TS_AUTHKEY for one enrolled Flight Deck while
    its siblings keep enrolling."""
    driver_type(
        tmp_path,
        secrets={"GITHUB_TOKEN": "github-implementer", "TS_AUTHKEY": "enroll-key"},
    )
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\n[secrets]\nTS_AUTHKEY = ""\n',
    )
    config = load_config(tmp_path)
    stack = next(s for s in config.stacks if s.name == "implementer")
    assert stack.secrets == {"GITHUB_TOKEN": "github-implementer"}
    assert config.secret_names_for("box1") == {"github-implementer", "registry:ghcr.io"}


def test_stack_empty_secret_for_undeclared_slot_is_rejected(tmp_path):
    """An empty binding for a slot the type never declares is a typo, not an
    unbind — fail loud with the slot name."""
    driver_type(tmp_path, secrets={"GITHUB_TOKEN": "github-implementer"})
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\n[secrets]\nGITHUB_TOKN = ""\n',
    )
    with pytest.raises(ConfigRepoError, match=r"GITHUB_TOKN.*does not declare"):
        load_config(tmp_path)


def test_dormant_type_with_required_slots_loads(tmp_path):
    """A required slot is only decidable per instantiation — a type with no
    Stacks is a legitimate template and loads."""
    driver_type(tmp_path, secrets={"GITHUB_TOKEN": ""})
    assert "claude-dev" in load_config(tmp_path).worker_types


@pytest.mark.parametrize(
    "slot",
    ["THEOZOLITH_MODEL", "THEOZOLITH_ADAPTER", "THEOZOLITH_RUN_IMAGE", "THEOZOLITH_RUN_IMAGE_FILE"],
)
@pytest.mark.parametrize("site", ["worker-type", "stack"])
def test_reserved_secret_slot_names_are_rejected(tmp_path, site, slot):
    """A secret slot materializes <slot>_FILE, which the worker reads FIRST —
    a slot named after a baked identity field is the same hijack the [env]
    guard rejects (ADR-0045), at either declaration site."""
    if site == "worker-type":
        driver_type(tmp_path, secrets={slot: "some-name"})
        thin_stack(tmp_path, "implementer", "claude-dev")
    else:
        driver_type(tmp_path)
        write(
            tmp_path,
            "stacks/implementer.toml",
            f'worker_type = "claude-dev"\nnode = "box1"\n[secrets]\n{slot} = "some-name"\n',
        )
    with pytest.raises(ConfigRepoError, match=rf"\[secrets\] {slot} is reserved.*_FILE"):
        load_config(tmp_path)


def test_stack_workspace_overrides_the_type(tmp_path):
    """The target repo is a per-placement binding too (ADR-0047): one type,
    N repos, each Stack picking its own."""
    driver_type(tmp_path)  # workspace = "acme/sandbox" default
    thin_stack(tmp_path, "impl-other", "claude-dev", workspace='"acme/other"')
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "impl-other")
    assert stack.env["THEOZOLITH_REPO"] == "acme/other"


def test_workspaceless_driver_type_requires_a_stack_workspace(tmp_path):
    """A driver type with no default workspace is a multi-repo template —
    every instantiating Stack must bind the repo, fail-loud at load."""
    driver_type(tmp_path, workspace=None)
    thin_stack(tmp_path, "bound", "claude-dev", workspace='"acme/other"')
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "bound")
    assert stack.env["THEOZOLITH_REPO"] == "acme/other"

    thin_stack(tmp_path, "unbound", "claude-dev")
    with pytest.raises(ConfigRepoError, match=r"stacks/unbound\.toml.*driver but no workspace"):
        load_config(tmp_path)


def test_dormant_workspaceless_driver_type_loads(tmp_path):
    driver_type(tmp_path, workspace=None)
    assert "claude-dev" in load_config(tmp_path).worker_types


def test_stack_workspace_shape_is_validated(tmp_path):
    driver_type(tmp_path)
    thin_stack(tmp_path, "implementer", "claude-dev", workspace='"justname"')
    with pytest.raises(ConfigRepoError, match=r"stacks/implementer\.toml.*must be owner/name"):
        load_config(tmp_path)


def test_driverless_stack_workspace_override(tmp_path):
    """Uniform binding: a driverless (Flight Deck) Stack overrides the repo
    the same way; with neither side set, THEOZOLITH_REPO stays absent."""
    write(tmp_path, "worker-types/flightdeck.toml", f'base = "{BASE}"\n')
    thin_stack(tmp_path, "deck-a", "flightdeck", workspace='"acme/other"')
    thin_stack(tmp_path, "deck-b", "flightdeck")
    config = load_config(tmp_path)
    by_name = {s.name: s for s in config.stacks}
    assert by_name["deck-a"].env == {"THEOZOLITH_REPO": "acme/other"}
    assert "THEOZOLITH_REPO" not in by_name["deck-b"].env


def test_stack_env_repo_still_wins_over_stack_workspace(tmp_path):
    """[env] stays the last-word expert channel (unchanged by ADR-0047):
    the typed workspace binding seeds THEOZOLITH_REPO, [env] overrides it."""
    driver_type(tmp_path)
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\nworkspace = "acme/other"\n'
        '[env]\nTHEOZOLITH_REPO = "acme/expert"\n',
    )
    stack = next(s for s in load_config(tmp_path).stacks if s.name == "implementer")
    assert stack.env["THEOZOLITH_REPO"] == "acme/expert"


# -- Bound Workspaces (ADR-0056) -------------------------------------------------


def test_bound_repos_lists_driver_bearing_stacks_sorted_and_deduped(tmp_path):
    """The Bound Workspaces are the sorted, de-duplicated repos of resolved
    driver-bearing (process-kind) Stacks — a stopped worker's repo included
    (its zombie claims still need the janitor), a Flight Deck's and a generic
    Stack's excluded."""
    driver_type(tmp_path)  # workspace default acme/sandbox
    thin_stack(tmp_path, "impl-sandbox", "claude-dev")  # running -> acme/sandbox
    thin_stack(tmp_path, "impl-sandbox-2", "claude-dev")  # same repo, one entry
    thin_stack(tmp_path, "impl-zeta", "claude-dev", workspace='"acme/zeta"', state='"stopped"')
    # A driverless Flight Deck bound to a repo — container kind, never bound.
    write(tmp_path, "worker-types/flightdeck.toml", f'base = "{BASE}"\n')
    thin_stack(tmp_path, "deck", "flightdeck", workspace='"acme/deck"')
    # A generic (worker_type-less) process Stack — never a Bound Workspace.
    write(
        tmp_path, "stacks/generic.toml", 'kind = "process"\nnode = "box1"\ncommand = "run-thing"\n'
    )
    assert load_config(tmp_path).bound_repos() == ["acme/sandbox", "acme/zeta"]


def test_bound_repos_reports_the_env_override(tmp_path):
    """The env value is what the Driver checks out, so an [env] override is
    the bound repo — not the typed workspace it overrode (ADR-0056)."""
    driver_type(tmp_path)
    write(
        tmp_path,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\nworkspace = "acme/other"\n'
        '[env]\nTHEOZOLITH_REPO = "acme/expert"\n',
    )
    assert load_config(tmp_path).bound_repos() == ["acme/expert"]


def test_bound_repos_empty_with_no_driver_stacks(tmp_path):
    driver_type(tmp_path)  # a dormant type, no Stack instantiating it
    assert load_config(tmp_path).bound_repos() == []


# -- CRITICAL: derived-image identity (ADR-0044 as amended by ADR-0045) ----------

GOLDEN_BASE = "ghcr.io/snowfoxbuilds/theozolith-run-claude:0.3.0@sha256:" + "a" * 64
GOLDEN_IMAGE_FIELDS = (
    f'base = "{GOLDEN_BASE}"\n'
    f'setup = ["apt-get update && apt-get install -y ripgrep"]\n'
    f'knowledge = "knowledge/gold"\n'
)
GOLDEN_KNOWLEDGE = {"CLAUDE.md": "# golden knowledge\n"}
# sha256-manifest hash of GOLDEN_KNOWLEDGE as configdist.knowledge_tree_hash
# computes it — a fixed literal so the golden below is a full-config golden.
GOLDEN_KNOWLEDGE_PIN = "1a7090eb2377900ecf12c6d8ccd2d44ad9e843bd2e4fc660b80f332d1fc77a05"


def _golden_repo(tmp_path):
    write_knowledge_tree(tmp_path, "gold", GOLDEN_KNOWLEDGE)
    write_pins(tmp_path, knowledge={"gold": GOLDEN_KNOWLEDGE_PIN})


def test_tag_without_model_is_golden_stable(tmp_path):
    """A driverless type with NO model/effort has no materialize step — and
    since ADR-0048 it can carry no knowledge either (a deck's ~/.claude is
    volume-shadowed), so its identity is base + setup with EMPTY knowledge
    fields. GOLDEN: the ADR-0048 identity formula (canonical JSON over base,
    materialized setup, knowledge reference, per-tree pin). The key set
    changed deliberately with ADR-0048 (knowledge_source -> knowledge), a
    one-time fleet re-tag; from here the literal moves only when the hash
    formula changes again — a deliberate act, never drift."""
    write(
        tmp_path,
        "worker-types/goldtype.toml",
        f'base = "{GOLDEN_BASE}"\n'
        'setup = ["apt-get update && apt-get install -y ripgrep"]\n'
        # Per-type fields outside the identity: still excluded from the hash.
        'workspace = "acme/sandbox"\n[secrets]\nGITHUB_TOKEN = "github-implementer"\n',
    )
    wt = load_config(tmp_path).worker_types["goldtype"]
    assert wt.knowledge == "" and wt.knowledge_pin == ""
    assert wt.tag == "theozolith/goldtype:0.3.0-fbd115afa5ef"
    assert wt.instruction_hash == "fbd115afa5ef8c384c27b2d32325de43dac5fc88c275da7d1f3bc810728eea4a"


def test_tag_with_model_is_golden_over_the_materialized_setup(tmp_path):
    """With a model set, the synthesized materialize instruction enters the
    hash (ADR-0045): same image fields as the golden above, different tag.
    GOLDEN: pins hash-over-materialized-setup end to end — it moves only when
    the renderer format or the hash formula changes, both deliberate acts."""
    _golden_repo(tmp_path)
    write(
        tmp_path,
        "worker-types/goldtype.toml",
        'driver = "builtin:implementer"\nworkspace = "acme/sandbox"\n'
        + GOLDEN_IMAGE_FIELDS
        + 'adapter = "claude"\nmodel = "claude-sonnet-5"\n'
        '[secrets]\nGITHUB_TOKEN = "github-implementer"\n',
    )
    wt = load_config(tmp_path).worker_types["goldtype"]
    assert wt.materialized_setup[-1] == (
        "theozolith-adapter materialize --adapter claude --model claude-sonnet-5 --scope managed"
    )
    assert wt.tag == "theozolith/goldtype:0.3.0-a3f5923671c0"
    assert wt.instruction_hash == (
        "a3f5923671c0705065a7076b662ead480d86de61d1f7241aca7523a378014fcf"
    )


# The content pin of the standard example drop-in tree ({"attribution":
# {"sessionUrl": false}} in attribution.json) as configdist.policy_tree_hash
# computes it — a fixed literal so the golden below is a full-config golden.
GOLDEN_POLICY_PIN = "96a9ffe39ded28373fbd24fda24379d2d4309844ca7abbba16050f473058e7a6"


def test_tag_with_policy_is_golden_over_the_conditional_keys(tmp_path):
    """A DRIVER type with an Agent Policy gains exactly the conditional
    policy/policy_pin identity keys (ADR-0055): same image fields as the
    model golden above plus the policy reference — a different, stable hash.
    GOLDEN: it moves only when the identity formula changes again, a
    deliberate act. The two policy-less goldens above passing UNCHANGED is
    the byte-identical proof for every pre-ADR-0055 identity."""
    _golden_repo(tmp_path)
    write_policy_tree(tmp_path, "gold")
    write_pins(
        tmp_path,
        knowledge={"gold": GOLDEN_KNOWLEDGE_PIN},
        policy={"gold": GOLDEN_POLICY_PIN},
    )
    write(
        tmp_path,
        "worker-types/goldtype.toml",
        'driver = "builtin:implementer"\nworkspace = "acme/sandbox"\n'
        + GOLDEN_IMAGE_FIELDS
        + 'adapter = "claude"\nmodel = "claude-sonnet-5"\npolicy = "policy/gold"\n',
    )
    wt = load_config(tmp_path).worker_types["goldtype"]
    assert wt.baked_policy == "policy/gold" and wt.baked_policy_pin == GOLDEN_POLICY_PIN
    recipe = wt.recipe_wire()
    assert recipe["policy"] == "policy/gold" and recipe["policy_pin"] == GOLDEN_POLICY_PIN
    assert wt.tag == "theozolith/goldtype:0.3.0-0a3a3fc6d5c8"
    assert wt.instruction_hash == (
        "0a3a3fc6d5c8e14573cb93061a92406cb3340016fd3869dcf0e2c4c60c6367ea"
    )


def test_driverless_knowledge_selects_the_mount_and_stays_out_of_the_image(tmp_path):
    """ADR-0048 amendment: a Flight Deck may declare knowledge =
    "knowledge/<name>". The reference is validated exactly like a driver's
    (ingested pin joined, compiled tree present) but never bakes: the wire
    recipe carries EMPTY knowledge fields (the node must not bake under the
    volume-shadowed ~/.claude), the image identity ignores reference and pin
    (a content edit redistributes live, rebuilding and recreating nothing),
    and the resolved Stack carries the selection as THEOZOLITH_KNOWLEDGE_TREE
    — part of the container spec, so changing the SELECTED TREE recreates the
    deck."""
    write_knowledge_tree(tmp_path, "dev")
    write_knowledge_tree(tmp_path, "ops")
    write_pins(tmp_path, knowledge={"dev": "a" * 64, "ops": "b" * 64})
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\nknowledge = "knowledge/dev"\ncommand = "flightdeck-start"\n',
    )
    thin_stack(tmp_path, "deck", "flightdeck")
    config = load_config(tmp_path)
    wt = config.worker_types["flightdeck"]
    assert wt.knowledge == "knowledge/dev" and wt.knowledge_pin == "a" * 64
    recipe = wt.recipe_wire()
    assert recipe["knowledge"] == "" and recipe["knowledge_pin"] == ""
    stack = next(s for s in config.stacks if s.name == "deck")
    assert stack.env["THEOZOLITH_KNOWLEDGE_TREE"] == "dev"

    # Content edit: the pin moves, the tag does not — live redistribution.
    write_pins(tmp_path, knowledge={"dev": "c" * 64, "ops": "b" * 64})
    after_content = load_config(tmp_path)
    assert after_content.worker_types["flightdeck"].tag == wt.tag
    deck = next(s for s in after_content.stacks if s.name == "deck")
    assert deck.env["THEOZOLITH_KNOWLEDGE_TREE"] == "dev"  # spec unchanged: no recreate

    # Selection edit: the tag still does not move (no image bytes changed),
    # but the injected env does — the container spec changes and the deck is
    # recreated on the new tree.
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\nknowledge = "knowledge/ops"\ncommand = "flightdeck-start"\n',
    )
    after_selection = load_config(tmp_path)
    assert after_selection.worker_types["flightdeck"].tag == wt.tag
    deck = next(s for s in after_selection.stacks if s.name == "deck")
    assert deck.env["THEOZOLITH_KNOWLEDGE_TREE"] == "ops"


def test_driverless_knowledge_validation_is_not_weakened(tmp_path):
    """A deck's knowledge reference fails loud exactly like a driver's: no
    ingested pin, or no compiled tree in the pinned build, refuses the load —
    unavailable knowledge is never silently skipped (ADR-0048)."""
    write_knowledge_tree(tmp_path, "dev")
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\nknowledge = "knowledge/dev"\n',
    )
    with pytest.raises(ConfigRepoError, match="no ingest-computed pin"):
        load_config(tmp_path)
    write_pins(tmp_path, knowledge={"missing": "a" * 64})
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\nknowledge = "knowledge/missing"\n',
    )
    with pytest.raises(ConfigRepoError, match="no compiled claude tree in the pinned build"):
        load_config(tmp_path)


def test_stack_env_knowledge_tree_override_is_rejected(tmp_path):
    """Per-Stack knowledge is rejected (ADR-0048): the selection is worker-type
    identity, so an [env] override on a worker-type Stack fails the load."""
    write_knowledge_tree(tmp_path, "dev")
    write_pins(tmp_path, knowledge={"dev": "a" * 64})
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\nknowledge = "knowledge/dev"\n',
    )
    thin_stack(tmp_path, "deck", "flightdeck")
    write(
        tmp_path,
        "stacks/deck.toml",
        'worker_type = "flightdeck"\nnode = "box1"\n[env]\nTHEOZOLITH_KNOWLEDGE_TREE = "other"\n',
    )
    with pytest.raises(ConfigRepoError, match="THEOZOLITH_KNOWLEDGE_TREE"):
        load_config(tmp_path)


def test_legacy_knowledge_fields_are_rejected_with_the_new_home(tmp_path):
    driver_type(tmp_path, knowledge_source='"https://github.com/acme/k.git"')
    with pytest.raises(ConfigRepoError, match=r"retired \(ADR-0048\)"):
        load_config(tmp_path)
    driver_type(tmp_path, knowledge_pin='"abc123"')
    with pytest.raises(ConfigRepoError, match=r"retired \(ADR-0048\)"):
        load_config(tmp_path)


def test_knowledge_reference_requires_an_ingested_pin(tmp_path):
    write_knowledge_tree(tmp_path, "dev")
    driver_type(tmp_path, knowledge='"knowledge/dev"')
    with pytest.raises(ConfigRepoError, match="no ingest-computed pin"):
        load_config(tmp_path)


def test_knowledge_reference_requires_the_compiled_tree(tmp_path):
    write_pins(tmp_path, knowledge={"dev": "c" * 64})
    driver_type(tmp_path, knowledge='"knowledge/dev"')
    with pytest.raises(ConfigRepoError, match="no compiled claude tree in the pinned build"):
        load_config(tmp_path)


# -- Agent Policy (ADR-0055) -----------------------------------------------------


def _policy_repo(tmp_path, name: str = "gold", pin: str = "d" * 64) -> None:
    write_policy_tree(tmp_path, name)
    write_pins(tmp_path, policy={name: pin})


def test_policy_field_parses_and_joins_the_ingested_pin(tmp_path):
    _policy_repo(tmp_path)
    driver_type(tmp_path, policy='"policy/gold"')
    wt = load_config(tmp_path).worker_types["claude-dev"]
    assert wt.policy == "policy/gold" and wt.policy_pin == "d" * 64
    assert wt.policy_tree == "gold"
    assert wt.baked_policy == "policy/gold" and wt.baked_policy_pin == "d" * 64


def test_policy_reference_shape_is_validated(tmp_path):
    _policy_repo(tmp_path)
    for bad in ('"gold"', '"policy/"', '"policy/has/slash"', '"policy/.dot"'):
        driver_type(tmp_path, policy=bad)
        with pytest.raises(ConfigRepoError, match=r'must be\s+"policy/<name>"'):
            load_config(tmp_path)


def test_hand_authored_policy_pin_is_refused(tmp_path):
    _policy_repo(tmp_path)
    driver_type(tmp_path, policy='"policy/gold"', policy_pin=f'"{"d" * 64}"')
    with pytest.raises(ConfigRepoError, match="ingest-computed, never authored"):
        load_config(tmp_path)


def test_policy_reference_requires_an_ingested_pin(tmp_path):
    write_policy_tree(tmp_path, "gold")
    driver_type(tmp_path, policy='"policy/gold"')
    with pytest.raises(ConfigRepoError, match="no ingest-computed pin for 'policy/gold'"):
        load_config(tmp_path)


def test_dangling_policy_reference_fails_load(tmp_path):
    """The pin proves a tree was ingested; the tree must still be present to
    distribute — a dormant type with a dangling reference breaks at configure
    time (ADR-0055), never later."""
    write_pins(tmp_path, policy={"gone": "d" * 64})
    driver_type(tmp_path, policy='"policy/gone"')
    with pytest.raises(ConfigRepoError, match="has no tree in the pinned build"):
        load_config(tmp_path)


def test_policy_is_refused_on_codex_types_in_both_shapes(tmp_path):
    """claude-only in v1, driver AND driverless alike (ADR-0055 §7): codex
    has no managed-settings tier."""
    _policy_repo(tmp_path)
    driver_type(
        tmp_path,
        adapter='"codex"',
        model='"gpt-5.2-codex"',
        policy='"policy/gold"',
        base='"ghcr.io/acme/run-codex:1.0@sha256:' + "0" * 64 + '"',
    )
    with pytest.raises(ConfigRepoError, match="cannot declare an Agent Policy"):
        load_config(tmp_path)
    write(
        tmp_path,
        "worker-types/claude-dev.toml",
        f'base = "{BASE}"\nadapter = "codex"\npolicy = "policy/gold"\n',
    )
    with pytest.raises(ConfigRepoError, match="cannot declare an Agent Policy"):
        load_config(tmp_path)


def test_load_validates_every_policy_tree_against_the_allowlist(tmp_path):
    """Config load re-runs the safe-key allowlist over every tree in the
    pinned build (ADR-0055) — referenced or not — so a hand edit or restore
    can never deliver an unadmitted key."""
    write_policy_tree(tmp_path, "rogue", {"steer.json": '{"model": "claude-opus-5"}\n'})
    with pytest.raises(ConfigRepoError, match=r"policy/rogue/steer\.json.*'model'"):
        load_config(tmp_path)


def test_stack_env_policy_tree_override_is_rejected(tmp_path):
    """Per-Stack policy does not exist (ADR-0055): the selection is
    worker-type declared, so an [env] override on a worker-type Stack fails
    the load."""
    _policy_repo(tmp_path)
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\npolicy = "policy/gold"\n',
    )
    write(
        tmp_path,
        "stacks/deck.toml",
        'worker_type = "flightdeck"\nnode = "box1"\n[env]\nTHEOZOLITH_POLICY_TREE = "other"\n',
    )
    with pytest.raises(ConfigRepoError, match="THEOZOLITH_POLICY_TREE"):
        load_config(tmp_path)


def test_driverless_policy_selects_the_mount_and_stays_out_of_the_image(tmp_path):
    """ADR-0055: a Flight Deck's policy reference never bakes — the wire
    recipe carries empty policy fields, hash/tag/recipe are byte-identical to
    the policy-less twin, and the resolved Stack carries the selection as
    THEOZOLITH_POLICY_TREE (spec input: reselection recreates once)."""
    write_policy_tree(tmp_path, "gold")
    write_policy_tree(tmp_path, "other")
    write_pins(tmp_path, policy={"gold": "d" * 64, "other": "e" * 64})
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "flightdeck-start"\npolicy = "policy/gold"\n',
    )
    thin_stack(tmp_path, "deck", "flightdeck")
    config = load_config(tmp_path)
    wt = config.worker_types["flightdeck"]
    assert wt.policy == "policy/gold" and wt.policy_pin == "d" * 64
    assert wt.baked_policy == "" and wt.baked_policy_pin == ""
    recipe = wt.recipe_wire()
    assert recipe["policy"] == "" and recipe["policy_pin"] == ""
    stack = next(s for s in config.stacks if s.name == "deck")
    assert stack.env["THEOZOLITH_POLICY_TREE"] == "gold"

    # The policy-less twin: identical hash, tag, and recipe wire — only the
    # resolved Stack env differs (policy trees are never identity-bearing on
    # decks).
    write(
        tmp_path,
        "worker-types/twin.toml",
        f'base = "{BASE}"\ncommand = "flightdeck-start"\n',
    )
    twin = load_config(tmp_path).worker_types["twin"]
    assert twin.instruction_hash == wt.instruction_hash
    assert twin.tag.split(":", 1)[1] == wt.tag.split(":", 1)[1]
    # The full wire recipe is byte-identical up to the name-derived fields.
    assert {k: v for k, v in twin.recipe_wire().items() if k not in ("name", "tag")} == {
        k: v for k, v in recipe.items() if k not in ("name", "tag")
    }

    # Content edit: the pin moves, nothing else does — live redistribution.
    write_pins(tmp_path, policy={"gold": "f" * 64, "other": "e" * 64})
    after_content = load_config(tmp_path)
    assert after_content.worker_types["flightdeck"].tag == wt.tag
    deck = next(s for s in after_content.stacks if s.name == "deck")
    assert deck.env["THEOZOLITH_POLICY_TREE"] == "gold"  # spec unchanged: no recreate

    # Selection edit: the tag still does not move, the injected env does —
    # the container spec changes and the deck is recreated on the new tree.
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "flightdeck-start"\npolicy = "policy/other"\n',
    )
    after_selection = load_config(tmp_path)
    assert after_selection.worker_types["flightdeck"].tag == wt.tag
    deck = next(s for s in after_selection.stacks if s.name == "deck")
    assert deck.env["THEOZOLITH_POLICY_TREE"] == "other"


def test_pins_policy_table_shape_is_validated(tmp_path):
    write(tmp_path, "pins.toml", '[source]\ncommit = "c"\n[policy]\ngold = "nothex"\n')
    with pytest.raises(ConfigRepoError, match=r"\[policy\] 'gold' must map to a 64-hex"):
        load_config(tmp_path)
    write(tmp_path, "pins.toml", f'[source]\ncommit = "c"\n[policy]\n".bad" = "{"d" * 64}"\n')
    with pytest.raises(ConfigRepoError, match=r"\[policy\] key '\.bad' must be a plain"):
        load_config(tmp_path)


def test_refuse_ui_write_covers_policy(tmp_path):
    from theozolith_control.configrepo import refuse_ui_write

    with pytest.raises(ConfigRepoError, match="git-native-only"):
        refuse_ui_write("policy/gold/attribution.json")


# -- the CLI Pin (ADR-0055) ---------------------------------------------------------


def test_cli_pin_field_parses_and_joins_the_ingest_pin(tmp_path):
    write_pins(tmp_path, cli={"claude/2.1.257": cli_pin()})
    deck_type(tmp_path, cli='"2.1.257"')
    wt = load_config(tmp_path).worker_types["flightdeck"]
    assert wt.cli == "2.1.257"
    assert wt.cli_version == "2.1.257"
    assert set(wt.cli_platforms) == {"linux-x64-glibc", "linux-arm64-glibc", "linux-x64-musl"}
    assert wt.cli_platforms["linux-x64-glibc"]["integrity"].startswith("sha512-")
    recipe = wt.recipe_wire()
    assert recipe["cli_tool"] == "claude"
    assert recipe["cli_version"] == "2.1.257"
    assert recipe["cli_platforms"] == wt.cli_platforms


def test_cli_with_a_driver_is_refused(tmp_path):
    """Driverless-only in v1 (ADR-0055 §7): a driver type keeps the base
    image's CLI as identity bytes — and the refusal fires with its precise
    message even though ingest resolved no pin for the definition."""
    driver_type(tmp_path, cli='"2.1.257"')
    with pytest.raises(ConfigRepoError, match="driverless-only in v1"):
        load_config(tmp_path)


def test_cli_on_a_codex_adapter_is_refused(tmp_path):
    deck_type(tmp_path, adapter='"codex"', cli='"0.150.0"')
    with pytest.raises(ConfigRepoError, match="cannot declare a CLI Pin"):
        load_config(tmp_path)


def test_cli_without_an_ingest_pin_is_refused(tmp_path):
    deck_type(tmp_path, cli='"2.1.257"')
    with pytest.raises(ConfigRepoError, match="re-run `theozolith config ingest`"):
        load_config(tmp_path)


def test_cli_declared_shape_is_validated(tmp_path):
    for bad in ("2.1.257 beta", "a/b", ".hidden", ""):
        deck_type(tmp_path, cli=f'"{bad}"' if bad else None)
        if not bad:
            load_config(tmp_path)  # absent field: a pinless deck is legal
            continue
        with pytest.raises(ConfigRepoError, match="cli"):
            load_config(tmp_path)


def test_cli_resolved_fields_cannot_be_authored(tmp_path):
    deck_type(tmp_path, cli_version='"2.1.257"')
    with pytest.raises(ConfigRepoError, match="ingest-resolved, never authored"):
        load_config(tmp_path)


def test_cli_below_the_adapter_floor_is_refused_at_load(tmp_path):
    """The pinned build may predate a floor bump: the load re-checks the
    joined version against MIN_ENFORCING_CLI (the lint site is
    configrepo/ingest, never the image build or the deck launch)."""
    write_pins(tmp_path, cli={"claude/2.0.0": cli_pin("2.0.0")})
    deck_type(tmp_path, cli='"2.0.0"')
    with pytest.raises(ConfigRepoError, match="below the claude adapter's enforcement floor"):
        load_config(tmp_path)


def test_cli_is_never_identity_bearing(tmp_path):
    """GOLDEN (ADR-0055): a cli-bearing driverless type's instruction_hash
    and tag are byte-identical to the same type without cli — declared,
    fleet-visible, never identity — and the wire recipes differ ONLY in the
    three cli keys."""
    write_pins(tmp_path, cli={"claude/2.1.257": cli_pin()})
    deck_type(tmp_path, cli='"2.1.257"')
    deck_type(tmp_path, name="twin")
    config = load_config(tmp_path)
    pinned, twin = config.worker_types["flightdeck"], config.worker_types["twin"]
    assert pinned.instruction_hash == twin.instruction_hash
    assert pinned.tag.split(":", 1)[1] == twin.tag.split(":", 1)[1]
    cli_keys = ("cli_tool", "cli_version", "cli_platforms")
    pinned_wire, twin_wire = pinned.recipe_wire(), twin.recipe_wire()
    assert {k: v for k, v in pinned_wire.items() if k not in ("name", "tag", *cli_keys)} == {
        k: v for k, v in twin_wire.items() if k not in ("name", "tag", *cli_keys)
    }
    assert (twin_wire["cli_tool"], twin_wire["cli_version"], twin_wire["cli_platforms"]) == (
        "",
        "",
        {},
    )


def test_cli_lifecycle_rides_the_injected_env(tmp_path):
    """ADR-0055 lifecycle at the config layer: ADOPTING or DROPPING cli
    changes the resolved Stack env (THEOZOLITH_WORKER_TYPE appears or goes —
    a container-fingerprint delta, one recreate); a VERSION bump changes no
    container-spec input at all (env, image, command, volumes identical) —
    it redistributes live through the wire recipe only."""
    deck_type(tmp_path)
    thin_stack(tmp_path, "deck", "flightdeck")
    bare = next(s for s in load_config(tmp_path).stacks if s.name == "deck")
    assert "THEOZOLITH_WORKER_TYPE" not in bare.env

    write_pins(tmp_path, cli={"claude/pinned": cli_pin("2.1.257")})
    deck_type(tmp_path, cli='"pinned"')
    adopted = next(s for s in load_config(tmp_path).stacks if s.name == "deck")
    assert adopted.env["THEOZOLITH_WORKER_TYPE"] == "flightdeck"
    assert adopted.image == bare.image  # never identity-bearing

    write_pins(tmp_path, cli={"claude/pinned": cli_pin("2.1.258")})
    bumped = next(s for s in load_config(tmp_path).stacks if s.name == "deck")
    for field in ("env", "image", "command", "volumes", "ports", "secrets"):
        assert getattr(bumped, field) == getattr(adopted, field)
    wt = load_config(tmp_path).worker_types["flightdeck"]
    assert wt.cli_version == "2.1.258"  # the wire recipe moved; the spec did not


def test_stack_env_worker_type_override_is_rejected(tmp_path):
    write_pins(tmp_path, cli={"claude/2.1.257": cli_pin()})
    deck_type(tmp_path, cli='"2.1.257"')
    write(
        tmp_path,
        "stacks/deck.toml",
        'worker_type = "flightdeck"\nnode = "box1"\n[env]\nTHEOZOLITH_WORKER_TYPE = "other"\n',
    )
    with pytest.raises(ConfigRepoError, match="THEOZOLITH_WORKER_TYPE"):
        load_config(tmp_path)


def test_pins_cli_table_shape_is_validated(tmp_path):
    """The [cli] table is machine-written: any deviation is corruption."""
    good = cli_pin()
    for mutate, needle in (
        (lambda p: {"claude": p}, r"\[cli\] key 'claude' must be"),
        (lambda p: {"claude/x": {**p, "version": "latest"}}, "exact"),
        (lambda p: {"claude/x": {"version": p["version"], "platforms": {}}}, "non-empty"),
        (
            lambda p: {
                "claude/x": {
                    "version": p["version"],
                    "platforms": {"linux-x64-glibc": {"package": "p", "integrity": "sha256-x"}},
                }
            },
            "sha512",
        ),
        (
            lambda p: {
                "claude/x": {
                    "version": p["version"],
                    "platforms": {"linux-x64-glibc": {"integrity": "sha512-" + "A" * 96}},
                }
            },
            "package",
        ),
    ):
        write_pins(tmp_path, cli=mutate(dict(good)))
        with pytest.raises(ConfigRepoError, match=needle):
            load_config(tmp_path)


def test_knowledge_reference_shape_is_validated(tmp_path):
    for bad in ("dev", "knowledge/", "knowledge/../x", "knowledge/.dot", "knowledge/a/b"):
        driver_type(tmp_path, knowledge=f'"{bad}"')
        with pytest.raises(ConfigRepoError, match="knowledge reference"):
            load_config(tmp_path)


def test_pins_join_supplies_pin_and_tag_moves_with_the_tree_hash(tmp_path):
    """Selective rebuild (ADR-0048): the per-tree pin is identity, so bumping
    ONE tree's pin re-tags exactly the types that reference that tree."""
    write_knowledge_tree(tmp_path, "dev")
    write_knowledge_tree(tmp_path, "review")
    write_pins(tmp_path, knowledge={"dev": "a" * 64, "review": "b" * 64})
    driver_type(tmp_path, name="dev-type", knowledge='"knowledge/dev"')
    driver_type(tmp_path, name="review-type", knowledge='"knowledge/review"')
    before = {n: wt.tag for n, wt in load_config(tmp_path).worker_types.items()}
    write_pins(tmp_path, knowledge={"dev": "c" * 64, "review": "b" * 64})
    after = {n: wt.tag for n, wt in load_config(tmp_path).worker_types.items()}
    assert before["dev-type"] != after["dev-type"]
    assert before["review-type"] == after["review-type"]
    assert load_config(tmp_path).worker_types["dev-type"].knowledge_pin == "c" * 64


def test_base_tag_resolves_through_pins(tmp_path):
    """An ingest-resolved base (ADR-0048): the Config Repo names a tag, the
    pinned build's pins.toml carries the digest, load joins them."""
    ref = "ghcr.io/snowfoxbuilds/theozolith-run-claude:1.2"
    write_pins(tmp_path, base={ref: f"sha256:{'d' * 64}"})
    driver_type(tmp_path, base=f'"{ref}"')
    wt = load_config(tmp_path).worker_types["claude-dev"]
    assert wt.base == f"{ref}@sha256:{'d' * 64}"
    assert wt.base_digest == f"sha256:{'d' * 64}"
    assert wt.tag.startswith("theozolith/claude-dev:1.2-")


def test_unresolved_base_tag_names_ingest(tmp_path):
    driver_type(tmp_path, base='"ghcr.io/acme/run:1.2"')
    with pytest.raises(ConfigRepoError, match="must be pinned by digest"):
        load_config(tmp_path)


def test_malformed_pins_fail_loudly(tmp_path):
    driver_type(tmp_path)
    for body in (
        '[base]\n"ghcr.io/a:1" = "nope"\n',
        f'[knowledge]\n"dev" = "{"g" * 64}"\n',  # non-hex
        '[knowledge]\n"dev" = 7\n',
        "base = 3\n",
    ):
        write(tmp_path, "pins.toml", body)
        with pytest.raises(ConfigRepoError, match=r"pins\.toml"):
            load_config(tmp_path)


def test_model_and_effort_change_the_tag(tmp_path):
    driver_type(tmp_path, name="a")
    driver_type(tmp_path, name="b", model='"claude-opus-5"')
    driver_type(tmp_path, name="c", effort='"high"')
    types = load_config(tmp_path).worker_types
    hashes = {types[n].instruction_hash for n in ("a", "b", "c")}
    assert len(hashes) == 3  # model and effort are identity-bearing (ADR-0045)


def test_non_image_per_type_fields_still_do_not_change_the_tag(tmp_path):
    driver_type(tmp_path, name="a", workspace='"acme/sandbox"')
    driver_type(tmp_path, name="b", workspace='"acme/other"', driver='"builtin:reviewer"')
    types = load_config(tmp_path).worker_types
    # driver/workspace/secrets change no image bytes -> identical hash.
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


def test_custom_driver_ref_resolves_to_the_launcher_command(tmp_path):
    """A drivers/<name> worker type whose module is present resolves to the one
    generic launcher with a drivers/<name> ref (ADR-0042), process kind."""
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "drivers/custom"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "a/b"\nbase = "{BASE}"\n',
    )
    write(tmp_path, "drivers/custom.py", "from theozolith_worker import api\nDriver = api.Worker\n")
    thin_stack(tmp_path, "s", "i")
    config = load_config(tmp_path)
    stack = next(s for s in config.stacks if s.name == "s")
    assert stack.kind == "process"
    assert stack.command == "theozolith-driver drivers/custom"
    # The custom code rides desired state as a hash (ADR-0042); the reference,
    # not the artifact, travels the channel.
    assert config.drivers_hash and config.desired_state_for("box1")["drivers_hash"]


def test_custom_driver_package_form_resolves(tmp_path):
    """The package form drivers/<name>/__init__.py resolves too (ADR-0042)."""
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "drivers/custom"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "a/b"\nbase = "{BASE}"\n',
    )
    write(tmp_path, "drivers/custom/__init__.py", "Driver = object\n")
    thin_stack(tmp_path, "s", "i")
    config = load_config(tmp_path)
    assert next(s for s in config.stacks if s.name == "s").command == (
        "theozolith-driver drivers/custom"
    )


def test_custom_driver_dangling_reference_fails_at_load(tmp_path):
    """A drivers/<name> reference with no matching module fails loudly at
    config-load time on the Control Node, not at process start (ADR-0042)."""
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "drivers/custom"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "a/b"\nbase = "{BASE}"\n',
    )
    thin_stack(tmp_path, "s", "i")
    with pytest.raises(
        ConfigRepoError, match=r"custom driver 'drivers/custom' has no module.*ADR-0042"
    ):
        load_config(tmp_path)


def test_unused_custom_worker_type_with_missing_driver_fails_at_load(tmp_path):
    """A dangling drivers/<name> reference fails load_config() even when no
    Stack instantiates the worker type: Config Repo validity is independent of
    placement (ADR-0042), so a dormant definition breaks at configure time."""
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "drivers/missing"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "a/b"\nbase = "{BASE}"\n',
    )
    with pytest.raises(
        ConfigRepoError, match=r"custom driver 'drivers/missing' has no module.*ADR-0042"
    ):
        load_config(tmp_path)


@pytest.mark.parametrize("module_path", ["drivers/custom.py", "drivers/custom/__init__.py"])
def test_unused_custom_worker_type_with_present_driver_loads(tmp_path, module_path):
    """An unplaced custom worker type still loads when its module exists, in
    either sanctioned form (ADR-0042): the load-time validation pass accepts
    exactly what Stack resolution would."""
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "drivers/custom"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "a/b"\nbase = "{BASE}"\n',
    )
    write(tmp_path, module_path, "Driver = object\n")
    config = load_config(tmp_path)
    assert config.worker_types["i"].driver == "drivers/custom"
    assert config.stacks == ()


@pytest.mark.parametrize("bad", ["drivers/My-Name", "drivers/1abc", "drivers/a-b", "drivers/"])
def test_custom_driver_name_must_be_a_python_identifier(tmp_path, bad):
    """The name after drivers/ must be an importable identifier (^[a-z_][a-z0-9_]*$):
    dashes, a leading digit, or an empty name are rejected at parse time (ADR-0042)."""
    write(
        tmp_path, "worker-types/i.toml", f'driver = "{bad}"\nworkspace = "a/b"\nbase = "{BASE}"\n'
    )
    with pytest.raises(ConfigRepoError, match=r"(valid Python identifier|drivers/<name>)"):
        load_config(tmp_path)


def test_driver_stack_requires_a_resolved_workspace(tmp_path):
    """The driver⇒workspace requirement moved from type parse to per-Stack
    resolution (ADR-0047): a workspace-less driver type is a legal multi-repo
    template; a Stack instantiating it without binding one fails at load."""
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "builtin:implementer"\nmodel = "claude-sonnet-5"\nbase = "{BASE}"\n',
    )
    load_config(tmp_path)  # dormant: loads

    thin_stack(tmp_path, "implementer", "i")
    with pytest.raises(ConfigRepoError, match=r"driver but no workspace.*worker-types/i\.toml"):
        load_config(tmp_path)


def test_workspace_must_be_owner_name(tmp_path):
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "builtin:implementer"\nworkspace = "justname"\nbase = "{BASE}"\n',
    )
    with pytest.raises(ConfigRepoError, match="must be owner/name"):
        load_config(tmp_path)


@pytest.mark.parametrize("field", ["command", "volumes", "tmpfs"])
def test_driverless_fields_are_rejected_with_a_driver(tmp_path, field):
    # tmpfs carries a shape-valid entry so the driverless-only rejection (not the
    # tmpfs lint) is what fires — the field is driver-forbidden even when valid.
    value = {"command": '"x"', "volumes": '["v:/p"]', "tmpfs": '["/tmp:size=1g"]'}[field]
    write(
        tmp_path,
        "worker-types/i.toml",
        f'driver = "builtin:implementer"\nworkspace = "a/b"\nbase = "{BASE}"\n{field} = {value}\n',
    )
    with pytest.raises(ConfigRepoError, match=rf"'{field}' is a driverless"):
        load_config(tmp_path)


# -- adapter capability validation (ADR-0045) ------------------------------------


def test_unknown_adapter_is_rejected_at_load(tmp_path):
    """The adapter finally gets validated — against the worker package's
    registry, the same code the image runs — so adapter = "pi" fails by
    construction until a Pi adapter exists."""
    driver_type(tmp_path, adapter='"pi"')
    with pytest.raises(ConfigRepoError, match=r"unknown Agent adapter 'pi'"):
        load_config(tmp_path)


def test_unmappable_model_is_rejected_with_the_mappable_shapes(tmp_path):
    driver_type(tmp_path, model='"gpt-5"')
    with pytest.raises(ConfigRepoError, match=r"cannot map model 'gpt-5'.*claude-\*.*ADR-0045"):
        load_config(tmp_path)


def test_unmappable_effort_is_rejected_listing_the_mappable_set(tmp_path):
    driver_type(tmp_path, effort='"max"')
    with pytest.raises(ConfigRepoError, match=r"cannot map effort 'max'.*low.*xhigh"):
        load_config(tmp_path)


def test_pair_validation_rejects_a_silently_clamped_effort(tmp_path):
    """Amendment C (ADR-0045): (model, effort) validate together. Claude Code
    documents that an unsupported level silently runs as the highest
    supported level at or below it — xhigh becomes high on the 4.6
    generation — so the pair fails the load instead of baking an identity the
    session would not run at."""
    driver_type(tmp_path, model='"claude-opus-4-6"', effort='"xhigh"')
    with pytest.raises(ConfigRepoError, match=r"claude-opus-4-6.*silently runs"):
        load_config(tmp_path)


def test_pair_validation_rejects_effort_on_an_effortless_model(tmp_path):
    driver_type(tmp_path, model='"claude-haiku-4-5"', effort='"low"')
    with pytest.raises(ConfigRepoError, match=r"silently ignore"):
        load_config(tmp_path)


def test_pair_validation_rejects_effort_on_an_unknown_model(tmp_path):
    # Mappable shape, unknown capability: enforceability must be positively
    # known — bake the model alone or upgrade theozolith.
    driver_type(tmp_path, model='"claude-newfamily-1"', effort='"high"')
    with pytest.raises(ConfigRepoError, match=r"no known effort capability"):
        load_config(tmp_path)


def test_pair_validation_accepts_supported_pairs_and_model_default(tmp_path):
    driver_type(tmp_path, model='"claude-sonnet-5"', effort='"xhigh"')
    load_config(tmp_path)  # supported pair
    driver_type(tmp_path, model='"claude-opus-4-6"', effort='"high"')
    load_config(tmp_path)  # supported on the 4.6 generation
    driver_type(tmp_path, model='"claude-haiku-4-5"')  # effort "" = model default
    load_config(tmp_path)
    driver_type(tmp_path, model='"claude-newfamily-1"')  # unknown model, no effort
    load_config(tmp_path)


def test_pair_validation_alias_effort_follows_family_capability(tmp_path):
    driver_type(tmp_path, model='"sonnet"', effort='"xhigh"')
    load_config(tmp_path)  # the sonnet family has stable, proven support
    driver_type(tmp_path, model='"haiku"', effort='"low"')
    with pytest.raises(ConfigRepoError, match=r"silently ignore"):
        load_config(tmp_path)


def test_dormant_worker_type_model_is_validated_too(tmp_path):
    """No Stack instantiates the type: it still breaks at configure time,
    never later when a Stack first activates it (the dormant-driver rule)."""
    driver_type(tmp_path, model='"gpt-5"')  # note: no stacks/ at all
    with pytest.raises(ConfigRepoError, match="cannot map model"):
        load_config(tmp_path)


def test_driverless_model_is_validated_too(tmp_path):
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "sleep 30"\nmodel = "gpt-5"\n',
    )
    with pytest.raises(ConfigRepoError, match="cannot map model"):
        load_config(tmp_path)


def test_driverless_effort_is_rejected(tmp_path):
    """Fail closed (ADR-0045): interactive scope bakes only the default-model
    file and nothing at Flight Deck runtime consumes a baked effort — a field
    that silently binds nothing would misrepresent the deck's identity."""
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "sleep 30"\nmodel = "claude-opus-5"\neffort = "high"\n',
    )
    with pytest.raises(ConfigRepoError, match=r"'effort' is rejected on driverless"):
        load_config(tmp_path)


@pytest.mark.parametrize("value", ["default", "opusplan"])
def test_unenforceable_cli_selections_are_rejected(tmp_path, value):
    """The Claude CLI accepts these as selections, but neither names the one
    model the image bakes: "default" floats with the account tier and fails
    outright under the allowlist; "opusplan" is a two-model mode that
    degrades to plain Sonnet under enforcement (both verified live on
    2.1.231). Unenforceable = unmappable = configure-time failure."""
    driver_type(tmp_path, model=f'"{value}"')
    with pytest.raises(ConfigRepoError, match=f"cannot map model '{value}'"):
        load_config(tmp_path)


def test_alias_model_warns_but_loads(tmp_path):
    """The pin-the-dated-ID convention is a lint, never an error: current-
    generation provider IDs have no dated variant, so a warning is the
    strongest honest signal (ADR-0045)."""
    driver_type(tmp_path, model='"sonnet"')
    config = load_config(tmp_path)
    assert config.worker_types["claude-dev"].model == "sonnet"
    assert any("floating" in warning and "sonnet" in warning for warning in config.warnings)


def test_full_model_ids_produce_no_warnings(tmp_path):
    driver_type(tmp_path)  # claude-sonnet-5: a full ID, undated by the provider
    assert load_config(tmp_path).warnings == ()


# -- materialization on the wire (ADR-0045) ---------------------------------------


def test_driver_model_effort_materialize_managed_scope_on_the_wire(tmp_path):
    """The synthesized instruction rides the recipe's ``setup`` — the 15
    wire keys (ADR-0052 added knowledge_tool/knowledge_target, ADR-0055
    policy/policy_pin and cli_tool/cli_version/cli_platforms), daemon
    adapter-blind — with managed scope for driver run images."""
    driver_type(tmp_path, effort='"high"')
    thin_stack(tmp_path, "implementer", "claude-dev")
    recipe = load_config(tmp_path).desired_state_for("box1")["images"][0]
    assert recipe["setup"][-1] == (
        "theozolith-adapter materialize --adapter claude"
        " --model claude-sonnet-5 --effort high --scope managed"
    )
    assert set(recipe) == {
        "name",
        "base",
        "setup",
        "knowledge",
        "knowledge_pin",
        "knowledge_tool",
        "knowledge_target",
        "policy",
        "policy_pin",
        "cli_tool",
        "cli_version",
        "cli_platforms",
        "tag",
        "base_digest",
        "instruction_hash",
    }


def test_driverless_model_materializes_interactive_scope(tmp_path):
    """Flight Deck images get interactive scope: well-known files only, never
    managed settings (which would lock /model) and never ~/.claude (which the
    claude-state volume shadows, ADR-0043)."""
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "sleep 30"\nmodel = "claude-opus-5"\n',
    )
    thin_stack(tmp_path, "flightdeck", "flightdeck")
    recipe = load_config(tmp_path).desired_state_for("box1")["images"][0]
    assert recipe["setup"][-1] == (
        "theozolith-adapter materialize --adapter claude --model claude-opus-5 --scope interactive"
    )


def test_no_model_or_effort_means_no_materialize_step(tmp_path):
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "sleep 30"\nsetup = ["apt-get update"]\n',
    )
    thin_stack(tmp_path, "flightdeck", "flightdeck")
    recipe = load_config(tmp_path).desired_state_for("box1")["images"][0]
    assert recipe["setup"] == ["apt-get update"]  # exactly the operator setup


def test_deck_tag_moves_exactly_with_model(tmp_path):
    """Issue #39 (ADR-0045 §4): the deck's derived-image tag moves when its
    baked default model is set or changed — and ONLY then: fields outside the
    image identity (secrets) leave the tag alone, so Flight Decks rebuild
    exactly when their definition's model does."""

    def deck_tag(extra: str) -> str:
        write(
            tmp_path,
            "worker-types/flightdeck.toml",
            f'base = "{BASE}"\ncommand = "sleep 30"\n{extra}',
        )
        return load_config(tmp_path).worker_types["flightdeck"].tag

    plain = deck_tag("")
    fable = deck_tag('model = "claude-fable-5"\n')
    sonnet = deck_tag('model = "claude-sonnet-5"\n')
    assert len({plain, fable, sonnet}) == 3
    assert deck_tag('[secrets]\nGITHUB_TOKEN = "flightdeck-github-token"\n') == plain
    assert (
        deck_tag('model = "claude-fable-5"\n[secrets]\nGITHUB_TOKEN = "flightdeck-github-token"\n')
        == fable
    )


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


def test_materialize_instruction_invokes_the_shipped_console_script(tmp_path):
    """The synthesized setup line control puts on the wire must invoke the
    console script the worker wheel actually installs (ADR-0045) — otherwise
    every model-bearing derived-image build dies on 'command not found'."""
    import tomllib

    driver_type(tmp_path)
    thin_stack(tmp_path, "implementer", "claude-dev")
    recipe = load_config(tmp_path).desired_state_for("box1")["images"][0]
    command = recipe["setup"][-1].split()[0]
    assert command == "theozolith-adapter"

    worker_pyproject = Path(__file__).parents[2] / "worker" / "pyproject.toml"
    scripts = tomllib.loads(worker_pyproject.read_text())["project"]["scripts"]
    assert command in scripts


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
        f'driver = "builtin:implementer"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{BASE}"\n',
    )
    write(
        tmp_path,
        "worker-types/claude-review.toml",
        f'driver = "builtin:reviewer"\nmodel = "claude-fable-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{BASE}"\n',
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
        "knowledge/dev/CLAUDE.md",  # compiled knowledge is ingest-written (ADR-0048)
        "knowledge",
        "pins.toml",  # machine-owned, ingest-only (ADR-0048)
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
    configrepo.refuse_ui_write("stacks/pins.toml")  # only the ROOT pins.toml is machine-owned
    configrepo.refuse_ui_write("compose/app/overlay.yaml")


# -- managed registry pull credentials (ADR-0049) -------------------------------


def test_registry_host_grammar():
    """The host key a `registry:<host>` credential is stored under. Accepts a
    pinned ref (loaded `base` values carry a digest) and the Docker Hub
    shorthand (normalized to registry-1.docker.io)."""
    from theozolith_control.configrepo import registry_host

    assert registry_host("ghcr.io/acme/run:1.2") == "ghcr.io"
    assert registry_host(f"ghcr.io/acme/run:1.2@sha256:{'a' * 64}") == "ghcr.io"
    assert registry_host("localhost:5000/run:1") == "localhost:5000"
    assert registry_host("ubuntu:24.04") == "registry-1.docker.io"
    assert registry_host("acme/run:1") == "registry-1.docker.io"


def test_running_worker_type_stack_scopes_its_base_pull_credential(tmp_path):
    """A running worker-type Stack scopes in `registry:<host>` for its base —
    the same running-recipe rule desired_state_for uses for images."""
    driver_type(tmp_path, secrets={"GITHUB_TOKEN": "github-implementer"})
    thin_stack(tmp_path, "implementer", "claude-dev", state='"running"')
    config = load_config(tmp_path)
    assert config.secret_names_for("box1") == {"github-implementer", "registry:ghcr.io"}


def test_stopped_worker_type_stack_scopes_no_registry_credential(tmp_path):
    """A stopped Stack builds no image (ADR-0037), so no base is pulled and its
    registry credential is NOT scoped in — the workload bindings still are."""
    driver_type(tmp_path, secrets={"GITHUB_TOKEN": "github-implementer"})
    thin_stack(tmp_path, "implementer", "claude-dev", state='"stopped"')
    config = load_config(tmp_path)
    assert config.secret_names_for("box1") == {"github-implementer"}


def test_generic_stack_node_scopes_no_registry_credential(tmp_path):
    """A node running only a plain (workerless) container Stack pulls no base
    through the substrate, so no registry credential is scoped in."""
    write(
        tmp_path,
        "stacks/web.toml",
        'kind = "container"\nnode = "box9"\nstate = "running"\n'
        'image = "ghcr.io/acme/web:1"\n[secrets]\nWEB_TOKEN = "web-token"\n',
    )
    config = load_config(tmp_path)
    assert config.secret_names_for("box9") == {"web-token"}


def test_registry_credential_is_scoped_per_node(tmp_path):
    """The pull credential rides only to nodes that run the type — a node with
    nothing placed pulls nothing."""
    driver_type(tmp_path)
    thin_stack(tmp_path, "impl-box1", "claude-dev", state='"running"')
    write(
        tmp_path,
        "stacks/impl-box2.toml",
        'worker_type = "claude-dev"\nnode = "box2"\nstate = "running"\n',
    )
    config = load_config(tmp_path)
    assert config.secret_names_for("box1") == {"registry:ghcr.io"}
    assert config.secret_names_for("box2") == {"registry:ghcr.io"}
    assert config.secret_names_for("box3") == set()


@pytest.mark.parametrize("site", ["worker-type", "stack", "generic-process", "generic-container"])
def test_registry_prefixed_binding_value_is_rejected(tmp_path, site):
    """A `registry:`-prefixed name can never be a workload [secrets] binding
    value — an infra pull credential must not be routed into a Stack's env
    (ADR-0049), at every declaration site: worker-type defaults, thin-Stack
    rebindings, and plain (workerless) process/container Stacks."""
    if site == "worker-type":
        driver_type(tmp_path, secrets={"GITHUB_TOKEN": "registry:ghcr.io"})
        thin_stack(tmp_path, "implementer", "claude-dev")
    elif site == "stack":
        driver_type(tmp_path)
        write(
            tmp_path,
            "stacks/implementer.toml",
            'worker_type = "claude-dev"\nnode = "box1"\n'
            '[secrets]\nGITHUB_TOKEN = "registry:ghcr.io"\n',
        )
    elif site == "generic-process":
        write(
            tmp_path,
            "stacks/job.toml",
            'kind = "process"\nnode = "box9"\nstate = "running"\n'
            'command = "python3 -m job"\n[secrets]\nPULL_CRED = "registry:ghcr.io"\n',
        )
    else:  # generic-container
        write(
            tmp_path,
            "stacks/web.toml",
            'kind = "container"\nnode = "box9"\nstate = "running"\n'
            'image = "ghcr.io/acme/web:1"\n[secrets]\nPULL_CRED = "registry:ghcr.io"\n',
        )
    with pytest.raises(ConfigRepoError, match="pull credential is infrastructure"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("name", "value", "ok"),
    [
        ("registry:ghcr.io", "octocat:ghp_x", True),
        ("registry:localhost:5000", "u:t", True),  # a ported host keeps its colon
        ("github-implementer", "anything-opaque", True),  # normal names shape-blind
        ("registry:", "u:t", False),  # no host
        ("registry:ghcr.io/acme", "u:t", False),  # a path is not a host
        ("registry:gh cr.io", "u:t", False),  # whitespace in host
        ("registry:ghcr.io", "no-colon-value", False),  # value must be <user>:<token>
    ],
)
def test_validate_registry_secret_shape(name, value, ok):
    from theozolith_control.configrepo import validate_registry_secret

    if ok:
        validate_registry_secret(name, value)  # no raise
    else:
        with pytest.raises(ConfigRepoError):
            validate_registry_secret(name, value)


@pytest.mark.parametrize(
    "bad", ["", ".", "..", "../evil", "a/b", "/abs", ".hidden", ".a.tmp", "with\x00nul"]
)
def test_validate_registry_secret_rejects_unsafe_stored_names(bad):
    """The WRITE surface (PUT /api/v1/secrets and the web form) refuses a stored
    name unsafe to materialize as a tmpfs leaf (#114) before it can be stored —
    the same shared validator applied at config load and in the daemon."""
    from theozolith_control.configrepo import validate_registry_secret

    with pytest.raises(ConfigRepoError):
        validate_registry_secret(bad, "some-value")


@pytest.mark.parametrize("site", ["worker-type", "stack", "generic-process", "generic-container"])
def test_invalid_stored_secret_name_binding_is_rejected(tmp_path, site):
    """A [secrets] binding VALUE unsafe to materialize is refused at config load,
    fail-loud at every declaration site (#114): worker-type defaults, thin-Stack
    rebindings, and plain (workerless) process/container Stacks — so a traversing
    name can never reach the Fernet store or the node."""
    bad = "../evil"
    if site == "worker-type":
        driver_type(tmp_path, secrets={"GITHUB_TOKEN": bad})
        thin_stack(tmp_path, "implementer", "claude-dev")
    elif site == "stack":
        driver_type(tmp_path)
        write(
            tmp_path,
            "stacks/implementer.toml",
            f'worker_type = "claude-dev"\nnode = "box1"\n[secrets]\nGITHUB_TOKEN = "{bad}"\n',
        )
    elif site == "generic-process":
        write(
            tmp_path,
            "stacks/job.toml",
            'kind = "process"\nnode = "box9"\nstate = "running"\n'
            f'command = "python3 -m job"\n[secrets]\nTOK = "{bad}"\n',
        )
    else:  # generic-container
        write(
            tmp_path,
            "stacks/web.toml",
            'kind = "container"\nnode = "box9"\nstate = "running"\n'
            f'image = "ghcr.io/acme/web:1"\n[secrets]\nTOK = "{bad}"\n',
        )
    with pytest.raises(ConfigRepoError, match="invalid stored secret name"):
        load_config(tmp_path)


# -- the codex adapter through config load (ADR-0052) ----------------------------


def test_codex_driver_type_joins_the_per_tool_pin_and_targets(tmp_path):
    """A codex worker type joins the ``<tree>/codex`` pin and derives its
    knowledge tool/target from the adapter; the wire recipe carries both so
    the daemon stays adapter-blind."""
    write(tmp_path, "knowledge/dev/codex/AGENTS.md", "# k\n")
    write_pins(tmp_path, knowledge={"dev/claude": "a" * 64, "dev/codex": "b" * 64})
    driver_type(
        tmp_path,
        name="codex-review",
        driver='"builtin:reviewer"',
        adapter='"codex"',
        model='"gpt-5.2-codex"',
        knowledge='"knowledge/dev"',
    )
    wt = load_config(tmp_path).worker_types["codex-review"]
    assert wt.knowledge_pin == "b" * 64
    assert wt.knowledge_tool == "codex"
    assert wt.knowledge_target == "/home/ozolith/.codex/"
    recipe = wt.recipe_wire()
    assert recipe["knowledge_tool"] == "codex"
    assert recipe["knowledge_target"] == "/home/ozolith/.codex/"
    assert recipe["setup"][-1] == (
        "theozolith-adapter materialize --adapter codex --model gpt-5.2-codex --scope managed"
    )


def test_claude_recipes_carry_the_default_knowledge_targets(tmp_path):
    write_knowledge_tree(tmp_path, "dev")
    write_pins(tmp_path, knowledge={"dev": "a" * 64})
    driver_type(tmp_path, knowledge='"knowledge/dev"')
    recipe = load_config(tmp_path).worker_types["claude-dev"].recipe_wire()
    assert recipe["knowledge_tool"] == "claude"
    assert recipe["knowledge_target"] == "/home/ozolith/.claude/"


def test_missing_per_tool_pin_is_actionable(tmp_path):
    """A tree pinned only for claude fails a codex type's join with the
    re-ingest pointer (either no codex-consumable content, or a pre-ADR-0052
    pinned build)."""
    write(tmp_path, "knowledge/dev/claude/CLAUDE.md", "# k\n")
    write_pins(tmp_path, knowledge={"dev/claude": "a" * 64})
    driver_type(
        tmp_path,
        name="codex-review",
        driver='"builtin:reviewer"',
        adapter='"codex"',
        model='"gpt-5.2-codex"',
        knowledge='"knowledge/dev"',
    )
    with pytest.raises(ConfigRepoError, match=r"compiled\n? ?for 'codex'"):
        load_config(tmp_path)


def test_codex_type_requires_the_compiled_codex_tree(tmp_path):
    """The presence check is per tool: a claude-only compiled tree does not
    satisfy a codex type even when the pin exists."""
    write(tmp_path, "knowledge/dev/claude/CLAUDE.md", "# k\n")
    write_pins(tmp_path, knowledge={"dev/claude": "a" * 64, "dev/codex": "b" * 64})
    driver_type(
        tmp_path,
        name="codex-review",
        driver='"builtin:reviewer"',
        adapter='"codex"',
        model='"gpt-5.2-codex"',
        knowledge='"knowledge/dev"',
    )
    with pytest.raises(ConfigRepoError, match="no compiled codex tree"):
        load_config(tmp_path)


def test_driverless_codex_knowledge_is_refused(tmp_path):
    """The node's knowledge export serves the claude view only — a codex
    Flight Deck cannot be given knowledge it could read (ADR-0052)."""
    write(tmp_path, "knowledge/dev/codex/AGENTS.md", "# k\n")
    write_pins(tmp_path, knowledge={"dev/codex": "b" * 64})
    write(
        tmp_path,
        "worker-types/codexdeck.toml",
        f'base = "{BASE}"\nadapter = "codex"\nknowledge = "knowledge/dev"\n',
    )
    with pytest.raises(ConfigRepoError, match="claude view only"):
        load_config(tmp_path)


def test_driverless_codex_model_is_refused(tmp_path):
    """No codex Flight Deck exists: a driverless codex type baking a model
    would render an interactive-scope instruction the codex adapter refuses
    at build — fail at config load instead (ADR-0052)."""
    write(
        tmp_path,
        "worker-types/codexdeck.toml",
        f'base = "{BASE}"\nadapter = "codex"\nmodel = "gpt-5.2-codex"\n',
    )
    with pytest.raises(ConfigRepoError, match="no codex Flight Deck exists"):
        load_config(tmp_path)


def test_codex_latest_model_warns_but_loads(tmp_path):
    """The provider's -latest IDs are moving pointers, not pins: the codex
    adapter classifies them as floating aliases, so config load keeps the
    exact behavior claude aliases get — a lint warning, never an error
    (ADR-0045)."""
    driver_type(
        tmp_path,
        name="codex-review",
        driver='"builtin:reviewer"',
        adapter='"codex"',
        model='"codex-mini-latest"',
    )
    config = load_config(tmp_path)
    assert config.worker_types["codex-review"].model == "codex-mini-latest"
    assert any(
        "floating" in warning and "codex-mini-latest" in warning for warning in config.warnings
    )


def test_codex_effort_is_rejected_until_proven(tmp_path):
    """The codex capability table is empty until spike #76 S7 proves a
    model honors a level — config load rejects every nonempty effort with
    the actionable message."""
    driver_type(
        tmp_path,
        name="codex-review",
        driver='"builtin:reviewer"',
        adapter='"codex"',
        model='"gpt-5.2-codex"',
        effort='"high"',
    )
    with pytest.raises(ConfigRepoError, match="no proven effort capability"):
        load_config(tmp_path)


def test_knowledge_target_enters_the_identity_only_when_non_default():
    """The no-fleet-retag contract (ADR-0052): a claude type's identity is
    byte-identical to the pre-ADR-0052 four-key formula; a codex type's
    differs exactly because the non-default COPY destination is a
    Dockerfile byte."""
    import dataclasses
    import hashlib
    import json

    from theozolith_control.configrepo import WorkerTypeDef

    claude = WorkerTypeDef(
        name="t",
        base=BASE,
        knowledge="knowledge/dev",
        knowledge_pin="c" * 64,
        driver="builtin:reviewer",
        adapter="claude",
    )
    legacy_formula = hashlib.sha256(
        json.dumps(
            {
                "base": BASE,
                "setup": [],
                "knowledge": "knowledge/dev",
                "knowledge_pin": "c" * 64,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert claude.instruction_hash == legacy_formula
    codex = dataclasses.replace(claude, adapter="codex")
    assert codex.instruction_hash != legacy_formula
