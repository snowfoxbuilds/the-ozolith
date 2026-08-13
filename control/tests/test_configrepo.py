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


def driver_type(tmp_path, name: str = "claude-dev", **fields) -> None:
    """A minimal valid driver (pipeline) worker type. ``model`` is required
    with a driver (ADR-0045); pass ``model=None`` to omit it."""
    body = {
        "driver": '"builtin:implementer"',
        "adapter": '"claude"',
        "model": '"claude-sonnet-5"',
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
    # Type-owned secrets are node-scoped through the resolved Stack.
    assert config.secret_names_for("box1") == {"github-implementer", "anthropic-api-key"}


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
    """ADR-0043: {stack} in a driverless type's volume names resolves to the
    Stack name (per-Flight-Deck state/tailnet identity), while knowledge-* is
    left literal (shared across siblings of the type). Two same-type Flight
    Decks on one node therefore get distinct state/tailscale volumes and the
    same knowledge volume."""
    write(
        tmp_path,
        "worker-types/flightdeck.toml",
        f'base = "{BASE}"\ncommand = "/usr/local/bin/flightdeck-start"\n'
        "volumes = [\n"
        '    "{stack}-logs:/var/log/flightdeck",\n'
        '    "{stack}-claude-state:/home/ozolith/.claude",\n'
        '    "knowledge-claude-dev:/home/ozolith/knowledge",\n'
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
        "knowledge-claude-dev:/home/ozolith/knowledge",  # shared: not substituted
        "flightdeck-box1-tailscale-state:/var/lib/tailscale",
    )
    # Distinct per-instance state/tailnet volumes; identical shared knowledge.
    state1 = {v for v in box1.volumes if "claude-state" in v or "tailscale-state" in v}
    state2 = {v for v in box2.volumes if "claude-state" in v or "tailscale-state" in v}
    assert state1.isdisjoint(state2)
    knowledge = "knowledge-claude-dev:/home/ozolith/knowledge"
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


# -- CRITICAL: derived-image identity (ADR-0044 as amended by ADR-0045) ----------

GOLDEN_BASE = "ghcr.io/snowfoxbuilds/theozolith-run-claude:0.3.0@sha256:" + "a" * 64
GOLDEN_IMAGE_FIELDS = (
    f'base = "{GOLDEN_BASE}"\n'
    f'setup = ["apt-get update && apt-get install -y ripgrep"]\n'
    f'knowledge_source = "https://github.com/acme/my-knowledge.git"\n'
    f'knowledge_pin = "{"b" * 40}"\n'
)


def test_tag_without_model_is_golden_stable_across_adr_0045(tmp_path):
    """A type with NO model/effort has no materialize step, so its hash must
    be BYTE-IDENTICAL to the pre-ADR-0045 value — this literal is the old
    goldtype golden. It is what guarantees adopting this release rebuilds
    nothing until a definition actually sets a model."""
    write(
        tmp_path,
        "worker-types/goldtype.toml",
        GOLDEN_IMAGE_FIELDS
        # Per-type fields outside the identity: still excluded from the hash.
        + 'workspace = "acme/sandbox"\n[secrets]\nGITHUB_TOKEN = "github-implementer"\n',
    )
    wt = load_config(tmp_path).worker_types["goldtype"]
    assert wt.tag == "theozolith/goldtype:0.3.0-48a66bc6e009"
    assert wt.instruction_hash == (
        "48a66bc6e009a3a84ebaf7bf7d05dc2c9df09851e4fddfb9344eacffbfd59f68"
    )


def test_tag_with_model_is_golden_over_the_materialized_setup(tmp_path):
    """With a model set, the synthesized materialize instruction enters the
    hash (ADR-0045): same image fields as the golden above, different tag.
    GOLDEN: pins hash-over-materialized-setup end to end — it moves only when
    the renderer format or the hash formula changes, both deliberate acts."""
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
    assert wt.tag == "theozolith/goldtype:0.3.0-8e28b92a4665"
    assert wt.instruction_hash == (
        "8e28b92a46657896d8a6f984dccfcabd9f4ee85cba539b0b56145bbd8d069280"
    )


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
    """The synthesized instruction rides the recipe's ``setup`` — same 8 wire
    keys, daemon adapter-blind — with managed scope for driver run images."""
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
        "knowledge_source",
        "knowledge_pin",
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
