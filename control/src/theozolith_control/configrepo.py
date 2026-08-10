"""The Config Repo: TOML on the Control Node, JSON desired state on the wire.

ADR-0006/0015: the git-backed folder (default ~/.theozolith/configs) is
parsed only here; each node receives its own desired-state document over the
heartbeat channel and caches it for degraded mode. The channel carries
declarations and references — compose/overlay text is inlined (declarative
topology is desired state), secret and image *values* never are.

Layout::

    stacks/<name>.toml        one Stack per file. A worker-type Stack is thin
                              (worker_type + node + state + env + attach); a
                              plain generic Stack carries the substrate format
                              (kind, node, state, env, secrets, command |
                              image/compose+overlays)
    worker-types/<name>.toml  the complete customization unit for one worker
                              (ADR-0044): driver/adapter/model/workspace/secrets
                              plus the derived-image recipe (digest-pinned base,
                              setup, optional Knowledge Source). Driverless
                              types are Flight Decks (interactive containers).
    product.toml              optional [product] version pin for the update command

An empty or missing repo is a legal deployment (the deletion test): every
node's desired state is simply empty.

Worker-type Stacks are resolved into concrete generic ``StackDef``s at
``load_config`` time, so every downstream consumer (secret scoping, jobs-dir
checks, desired state, the wire) works on the substrate format and the daemon
never special-cases workers (ADR-0044). The wire keys stay ``stacks`` and
``images``: a resolved worker-type Stack is an ordinary Stack, and a worker
type's derived-image recipe rides in ``images`` via ``recipe_wire()``.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STACK_KINDS = ("process", "container")
DESIRED_STATES = ("running", "stopped")

# The built-in drivers a worker type may name (ADR-0044/ADR-0020) and the
# supervised command each resolves to control-side. Every builtin runs through
# the one generic launcher (`theozolith-driver <ref>`, ADR-0020), so control is
# the single place that names the command; the custom-driver sub-issue
# (ADR-0042) turns `drivers/<name>` refs into real resolution. The keys must
# match theozolith_worker.drivercli.BUILTIN_WORKERS (pinned by contract test).
BUILTIN_DRIVERS = {
    "builtin:implementer": "theozolith-driver builtin:implementer",
    "builtin:reviewer": "theozolith-driver builtin:reviewer",
}

# The launcher command every built-in driver resolves through; a plain process
# Stack invoking it directly is rejected (the runner only works with the env a
# worker type injects — see _parse_stack's guard).
DRIVER_LAUNCHER = "theozolith-driver"

# Where a node keeps per-Stack jobs directories unless a Stack's env says
# otherwise — must match nodedaemon.daemon.DEFAULT_JOBS_BASE. The daemon
# injects <base>/<stack-name> per process Stack; this module enforces that
# the resolved paths are unique per node (queue-behind ownership, M5).
DEFAULT_JOBS_BASE = "/var/tmp/theozolith/jobs"

# The attach-template placeholders, named once so the parser's embedded-use
# rejection and the PTY bridge's substitution (web/terminal.py) share one
# source and cannot drift (ADR-0019).
ATTACH_HOST = "{host}"
ATTACH_CONTAINER = "{container}"
ATTACH_PLACEHOLDERS = (ATTACH_HOST, ATTACH_CONTAINER)


class ConfigRepoError(RuntimeError):
    """A Config Repo file does not parse or violates the format."""


@dataclass(frozen=True)
class WorkerTypeDef:
    """The complete customization unit for one worker (worker-types/<name>.toml,
    ADR-0044): the run-image recipe plus the per-type variables — driver,
    Agent adapter, model, workspace, and secrets. A strict superset of the
    old ImageDef. Absence of ``driver`` is the discriminator: a driverless
    type is a Flight Deck (interactive container) and may carry ``command``
    and ``volumes``; a driver type is a pipeline worker (process kind).

    The derived-image identity (``instruction_hash`` and ``tag``) is computed
    over the image fields ONLY — driver/adapter/model/workspace/secrets do
    not change image bytes, so renaming images/<name>.toml into worker-types/
    with unchanged image fields yields the identical tag and rebuilds nothing
    (ADR-0044 CRITICAL)."""

    name: str
    base: str  # full ref, pinned by digest
    setup: tuple[str, ...] = ()
    knowledge_source: str = ""
    knowledge_pin: str = ""
    # -- per-type variables (excluded from the image identity) --
    driver: str = ""  # "" (Flight Deck) | "builtin:<name>" | "drivers/<name>"
    adapter: str = "claude"  # Agent adapter the harness invokes
    model: str = ""  # "" = the driver's shipped default
    workspace: str = ""  # target repo, owner/name (required with driver)
    secrets: dict[str, str] = field(default_factory=dict)  # ENV_NAME -> secret name
    command: str = ""  # driverless only: container start command
    volumes: tuple[str, ...] = ()  # driverless only

    @property
    def is_driver(self) -> bool:
        return bool(self.driver)

    @property
    def base_digest(self) -> str:
        return self.base.rsplit("@", 1)[1]

    @property
    def base_tag(self) -> str:
        """The tag component of the base ref ("latest" when bare)."""
        prefix = self.base.rsplit("@", 1)[0]
        last = prefix.rsplit("/", 1)[-1]
        return last.partition(":")[2] or "latest"

    @property
    def instruction_hash(self) -> str:
        # CRITICAL (ADR-0044): exactly these four image fields, canonical JSON
        # with sort_keys. Adding driver/adapter/model/workspace/secrets here
        # would rehash every fleet image and trigger a rebuild storm.
        canonical = json.dumps(
            {
                "base": self.base,
                "setup": list(self.setup),
                "knowledge_source": self.knowledge_source,
                "knowledge_pin": self.knowledge_pin,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def tag(self) -> str:
        """Deterministic: base tag + instruction hash (NODE-SUBSTRATE.md)."""
        return f"theozolith/{self.name}:{self.base_tag}-{self.instruction_hash[:12]}"

    def recipe_wire(self) -> dict[str, Any]:
        """The derived-image recipe as it rides in the ``images`` wire list —
        byte-identical to the pre-ADR-0044 ImageDef.as_wire() so the daemon
        needs no new keys and degraded-mode caches stay shape-compatible."""
        return {
            "name": self.name,
            "base": self.base,
            "setup": list(self.setup),
            "knowledge_source": self.knowledge_source,
            "knowledge_pin": self.knowledge_pin,
            "tag": self.tag,
            "base_digest": self.base_digest,
            "instruction_hash": self.instruction_hash,
        }


@dataclass(frozen=True)
class StackDef:
    """One declarative Stack (stacks/<name>.toml). Built-in and user-defined
    Stacks share this format; the substrate has no workload knowledge."""

    name: str
    kind: str  # "process" | "container"
    node: str  # placement: exact node name
    state: str = "running"  # desired: "running" | "stopped"
    # The worker type this Stack resolved from (ADR-0044); "" for a plain
    # generic Stack. Kept populated after resolution for display only — every
    # other field is already concrete, so consumers never special-case it.
    worker_type: str = ""
    env: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)  # ENV_NAME -> secret name
    # process kind: the supervised argv string. Container kind (single-image
    # form only): an optional docker-run command — how the Flight Deck
    # starts its named tmux session (ADR-0019).
    command: str = ""
    image: str = ""  # container kind, single-image form
    ports: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    compose: str = ""  # container kind, compose form (repo-relative path)
    overlays: tuple[str, ...] = ()
    # Web-terminal attach command as a structured argv array; ``{host}`` and
    # ``{container}`` are permitted only as complete elements, substituted
    # (after validation) by the Control Node's PTY bridge. Empty = no
    # terminal exposed for this Stack (NODE-SUBSTRATE: dashboard and
    # operator access). Container-kind Stacks only (ADR-0019: run containers
    # are headless and never attach targets — the Flight Deck and other
    # configured container Stacks are the terminal's world). Consumed
    # control-side only; it never travels to nodes. Free-form command
    # strings are rejected (M5 hardening).
    attach: tuple[str, ...] = ()

    def as_wire(self, compose_files: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "node": self.node,
            "state": self.state,
            "env": dict(self.env),
            "secrets": dict(self.secrets),
            "command": self.command,
            "image": self.image,
            "ports": list(self.ports),
            "volumes": list(self.volumes),
            "compose_files": compose_files,  # [{name, content}] base first
        }


@dataclass(frozen=True)
class DeployConfig:
    commit: str
    stacks: tuple[StackDef, ...]  # worker-type Stacks already resolved to concrete
    worker_types: dict[str, WorkerTypeDef]
    product_version: str = ""
    repo_dir: Path | None = None

    def stacks_for(self, node: str) -> list[StackDef]:
        return [stack for stack in self.stacks if stack.node == node]

    def secret_names_for(self, node: str) -> set[str]:
        """The node-scoping rule: only secrets referenced by Stacks placed
        on the node may be pulled by it (NODE-SUBSTRATE.md)."""
        names: set[str] = set()
        for stack in self.stacks_for(node):
            names.update(stack.secrets.values())
        return names

    def _compose_files(self, stack: StackDef) -> list[dict[str, str]]:
        files = []
        for relpath in ((stack.compose,) if stack.compose else ()) + stack.overlays:
            if self.repo_dir is None:
                raise ConfigRepoError(f"stack {stack.name!r} references {relpath!r} with no repo")
            path = (self.repo_dir / relpath).resolve()
            if self.repo_dir.resolve() not in path.parents:
                raise ConfigRepoError(f"stack {stack.name!r}: {relpath!r} escapes the Config Repo")
            try:
                files.append({"name": relpath, "content": path.read_text(encoding="utf-8")})
            except OSError as exc:
                raise ConfigRepoError(
                    f"stack {stack.name!r}: cannot read {relpath!r}: {exc}"
                ) from exc
        return files

    def desired_state_for(self, node: str) -> dict[str, Any]:
        """The one JSON document a node reconciles from (and caches).

        Image recipes ride only for Stacks whose desired state is running
        (ADR-0037 stage-don't-deploy): a stopped Stack deploys and builds
        nothing — flipping it to running is the single act that starts the
        build-and-run sequence, and a scaffolded placeholder digest can
        never fail a build on a box that was born misconfigured."""
        stacks = self.stacks_for(node)
        # Recipes ride for running Stacks of BOTH kinds (ADR-0044): the Flight
        # Deck's derived image builds through the same list as a driver's.
        recipe_names = {
            stack.worker_type for stack in stacks if stack.worker_type and stack.state == "running"
        }
        return {
            "commit": self.commit,
            "product_version": self.product_version,
            "stacks": [stack.as_wire(self._compose_files(stack)) for stack in stacks],
            "images": [
                self.worker_types[name].recipe_wire()
                for name in sorted(recipe_names)
                if name in self.worker_types
            ],
        }


def _require_str(data: dict, key: str, context: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or (default is None and not value):
        raise ConfigRepoError(f"{context}: {key!r} must be a non-empty string")
    return value


def _str_list(data: dict, key: str, context: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigRepoError(f"{context}: {key!r} must be a list of strings")
    return tuple(value)


def _parse_attach(data: dict, context: str) -> tuple[str, ...]:
    """The attach argv: a list of strings in which ``{host}``/``{container}``
    appear only as complete elements. The untrusted identifiers can then
    never splice into trusted command structure — the free-form string-plus-
    shlex form is rejected outright (M5 hardening)."""
    value = data.get("attach", [])
    if isinstance(value, str):
        raise ConfigRepoError(
            f"{context}: 'attach' must be an argv array of strings"
            " (free-form command strings are rejected; write"
            ' e.g. attach = ["ssh", "{{host}}", "-t", ...])'
        )
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigRepoError(f"{context}: 'attach' must be a list of strings")
    for element in value:
        for placeholder in ATTACH_PLACEHOLDERS:
            if placeholder in element and element != placeholder:
                raise ConfigRepoError(
                    f"{context}: attach element {element!r} embeds {placeholder} —"
                    " placeholders are permitted only as complete arguments"
                )
    return tuple(value)


def resolved_jobs_dir(stack: StackDef) -> str:
    """The jobs directory this process Stack will actually use: its explicit
    THEOZOLITH_JOBS_DIR, else the per-Stack default the daemon injects."""
    explicit = stack.env.get("THEOZOLITH_JOBS_DIR", "")
    return posixpath.normpath(explicit or f"{DEFAULT_JOBS_BASE}/{stack.name}")


def _check_jobs_dirs(stacks: tuple[StackDef, ...]) -> None:
    """Queue-behind ownership (M5): every process Stack on a node owns a
    distinct jobs directory, so the in-flight signal for one Stack never
    observes another Stack's Runs. The failed-push parking sibling
    (``<jobs>-pending``, see worker sweep) is part of the claim."""
    claims: dict[tuple[str, str], str] = {}
    for stack in stacks:
        if stack.kind != "process":
            continue
        jobs = resolved_jobs_dir(stack)
        for path in (jobs, f"{jobs}-pending"):
            other = claims.get((stack.node, path))
            if other is not None:
                raise ConfigRepoError(
                    f"stacks/{stack.name}.toml: jobs directory {jobs!r} collides with"
                    f" stack {other!r} on node {stack.node!r} — every process Stack"
                    " needs a distinct resolved THEOZOLITH_JOBS_DIR"
                )
            claims[(stack.node, path)] = stack.name


def _valid_workspace(value: str) -> bool:
    """The documented workspace shape: exactly two non-empty path components
    (``owner/name``). Deliberately NOT GitHub's full naming policy — only the
    two-component promise the schema makes. Rejects ``/repo``, ``owner/``,
    ``owner/repo/extra``, ``///``, and any empty or whitespace-only
    component (ADR-0044 amendment)."""
    parts = value.split("/")
    return len(parts) == 2 and all(part.strip() for part in parts)


def _str_map(data: dict, key: str, context: str) -> dict[str, str]:
    value = data.get(key, {})
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise ConfigRepoError(f"{context}: {key!r} must be a table of strings")
    return dict(value)


# Fields that a fat pre-ADR-0044 Stack carried but that now live in the worker
# type. A thin worker-type Stack that still declares one is rejected by name.
_MOVED_TO_WORKER_TYPE = (
    "kind",
    "command",
    "image",
    "compose",
    "overlays",
    "ports",
    "volumes",
    "secrets",
)
_WORKER_TYPE_STACK_KEYS = ("worker_type", "node", "state", "env", "attach")


def _parse_stack(name: str, data: dict[str, Any]) -> StackDef:
    context = f"stacks/{name}.toml"
    if name == "control":
        raise ConfigRepoError(
            f"{context}: the control Stack is deleted — the substrate never"
            " supervises its own control plane (ADR-0035); the Control Node"
            " always runs as its own systemd unit ('theozolith serve') on its"
            " host, on every deployment shape"
        )
    if "run_image" in data:
        raise ConfigRepoError(
            f"{context}: run_image is gone — declare a worker type and set"
            " worker_type on the Stack (ADR-0044)"
        )
    if "worker_type" in data:
        return _parse_worker_type_stack(name, data, context)
    return _parse_generic_stack(name, data, context)


def _parse_worker_type_stack(name: str, data: dict[str, Any], context: str) -> StackDef:
    """A thin worker-type Stack (ADR-0044): worker_type + placement + desired
    state, nothing else. kind/command/image/... all moved into the worker
    type; the concrete StackDef is produced later at resolution."""
    worker_type = _require_str(data, "worker_type", context)
    for key in data:
        if key in _MOVED_TO_WORKER_TYPE:
            raise ConfigRepoError(
                f"{context}: {key!r} moved to worker-types/{worker_type}.toml"
                " (ADR-0044) — a worker-type Stack declares only worker_type,"
                " node, state, env, attach"
            )
        if key not in _WORKER_TYPE_STACK_KEYS:
            raise ConfigRepoError(
                f"{context}: unknown key {key!r} on a worker-type Stack"
                " (allowed: worker_type, node, state, env, attach)"
            )
    state = _require_str(data, "state", context, default="running")
    if state not in DESIRED_STATES:
        raise ConfigRepoError(f"{context}: state must be one of {DESIRED_STATES}, got {state!r}")
    return StackDef(
        name=name,
        kind="",  # derived at resolution: driver type -> process, else container
        node=_require_str(data, "node", context),
        state=state,
        worker_type=worker_type,
        env=_str_map(data, "env", context),
        attach=_parse_attach(data, context),
    )


def _parse_generic_stack(name: str, data: dict[str, Any], context: str) -> StackDef:
    """A plain substrate Stack (no worker_type): the workload-agnostic format
    the substrate keeps for arbitrary process/container workloads."""
    kind = _require_str(data, "kind", context)
    if kind not in STACK_KINDS:
        raise ConfigRepoError(f"{context}: kind must be one of {STACK_KINDS}, got {kind!r}")
    state = _require_str(data, "state", context, default="running")
    if state not in DESIRED_STATES:
        raise ConfigRepoError(f"{context}: state must be one of {DESIRED_STATES}, got {state!r}")
    stack = StackDef(
        name=name,
        kind=kind,
        node=_require_str(data, "node", context),
        state=state,
        env=_str_map(data, "env", context),
        secrets=_str_map(data, "secrets", context),
        command=_require_str(data, "command", context, default=""),
        image=_require_str(data, "image", context, default=""),
        ports=_str_list(data, "ports", context),
        volumes=_str_list(data, "volumes", context),
        compose=_require_str(data, "compose", context, default=""),
        overlays=_str_list(data, "overlays", context),
        attach=_parse_attach(data, context),
    )
    if stack.kind == "process" and not stack.command:
        raise ConfigRepoError(f"{context}: process Stacks require 'command'")
    # Parse the command with the SAME argv semantics the supervisor executes
    # with (shlex.split, not str.split), so a quoted launcher — command =
    # '"theozolith-driver" builtin:implementer' — is recognized here and
    # rejected, instead of sailing past a naive whitespace split and launching
    # at run time.
    # Malformed shell quoting becomes a clear ConfigRepoError, never an
    # uncaught exception or a command that validates now and fails at launch.
    if stack.command:
        try:
            argv = shlex.split(stack.command)
        except ValueError as exc:
            raise ConfigRepoError(
                f"{context}: 'command' is not valid shell syntax ({exc}) — fix the quoting"
            ) from exc
    else:
        argv = []
    # The built-in drivers only work with the environment a worker type
    # injects (THEOZOLITH_REPO/ADAPTER/MODEL/RUN_IMAGE): a plain Stack that
    # invokes the generic launcher directly — any ref, builtin:* or drivers/* —
    # is the old fat-Stack form and is rejected (ADR-0044/ADR-0020). Matching
    # the launcher (not the two-token resolved commands) keeps the guard firing
    # now that BUILTIN_DRIVERS values start with `theozolith-driver`.
    argv0 = argv[0] if argv else ""
    if stack.kind == "process" and argv0 == DRIVER_LAUNCHER:
        raise ConfigRepoError(
            f"{context}: command {stack.command!r} invokes the driver launcher"
            " directly — declare a worker type (worker-types/<name>.toml) and set"
            " worker_type on the Stack instead (ADR-0044); the driver only runs"
            " with the env a worker type injects"
        )
    if stack.kind == "container" and bool(stack.image) == bool(stack.compose):
        raise ConfigRepoError(f"{context}: container Stacks declare exactly one of image/compose")
    if stack.attach and stack.kind != "container":
        raise ConfigRepoError(
            f"{context}: 'attach' is only valid on container-kind Stacks — run"
            " containers are headless and never attach targets (ADR-0019); the"
            " web terminal reaches the Flight Deck and other container Stacks"
        )
    if stack.kind == "container" and stack.command and stack.compose:
        raise ConfigRepoError(
            f"{context}: 'command' applies to the single-image container form"
            " only (compose services declare their own commands)"
        )
    return stack


def _parse_worker_type(name: str, data: dict[str, Any]) -> WorkerTypeDef:
    context = f"worker-types/{name}.toml"
    base = _require_str(data, "base", context)
    if "@sha256:" not in base:
        raise ConfigRepoError(f"{context}: base must be pinned by digest (ADR-0006)")
    driver = _require_str(data, "driver", context, default="")
    workspace = _require_str(data, "workspace", context, default="")
    command = _require_str(data, "command", context, default="")
    volumes = _str_list(data, "volumes", context)
    if driver:
        is_custom = driver.startswith("drivers/") and len(driver) > len("drivers/")
        if driver not in BUILTIN_DRIVERS and not is_custom:
            if driver.startswith("builtin:"):
                known = ", ".join(sorted(BUILTIN_DRIVERS))
                raise ConfigRepoError(
                    f"{context}: unknown built-in driver {driver!r} (known: {known})"
                )
            raise ConfigRepoError(
                f"{context}: driver must be 'builtin:<name>' or 'drivers/<name>'"
                f" (ADR-0042), got {driver!r}"
            )
        if not workspace:
            raise ConfigRepoError(
                f"{context}: 'workspace' (owner/name) is required when a driver is set"
            )
        for field_name, present in (("command", command), ("volumes", volumes)):
            if present:
                raise ConfigRepoError(
                    f"{context}: {field_name!r} is a driverless (Flight Deck) field"
                    " and is rejected when a driver is set (ADR-0044)"
                )
    if workspace and not _valid_workspace(workspace):
        raise ConfigRepoError(
            f"{context}: 'workspace' must be owner/name — exactly two non-empty"
            f" path components — got {workspace!r}"
        )
    return WorkerTypeDef(
        name=name,
        base=base,
        setup=_str_list(data, "setup", context),
        knowledge_source=_require_str(data, "knowledge_source", context, default=""),
        knowledge_pin=_require_str(data, "knowledge_pin", context, default=""),
        driver=driver,
        adapter=_require_str(data, "adapter", context, default="claude"),
        model=_require_str(data, "model", context, default=""),
        workspace=workspace,
        secrets=_str_map(data, "secrets", context),
        command=command,
        volumes=volumes,
    )


def _resolve_worker_stack(stack: StackDef, wt: WorkerTypeDef) -> StackDef:
    """Turn a thin worker-type Stack into a concrete generic StackDef (ADR-0044).

    Env injection happens control-side (the daemon's run_image linkage is
    gone): the type supplies THEOZOLITH_REPO/ADAPTER/MODEL/RUN_IMAGE, then the
    Stack's own [env] overrides. The kind is derived from the type's driver."""
    context = f"stacks/{stack.name}.toml"
    if wt.is_driver:
        if wt.driver.startswith("drivers/"):
            raise ConfigRepoError(
                f"worker-types/{wt.name}.toml: config-distribution driver delivery"
                " is not yet implemented (ADR-0042)"
            )
        if stack.attach:
            raise ConfigRepoError(
                f"{context}: 'attach' is only valid on container-kind Stacks — worker"
                f" type {wt.name!r} has a driver, so the Stack resolves to process"
                " kind (ADR-0019/ADR-0044)"
            )
        env = {
            "THEOZOLITH_REPO": wt.workspace,
            "THEOZOLITH_ADAPTER": wt.adapter,
            "THEOZOLITH_RUN_IMAGE": wt.tag,
        }
        if wt.model:
            env["THEOZOLITH_MODEL"] = wt.model
        env.update(stack.env)
        return StackDef(
            name=stack.name,
            kind="process",
            node=stack.node,
            state=stack.state,
            worker_type=wt.name,
            env=env,
            secrets=dict(wt.secrets),
            command=BUILTIN_DRIVERS[wt.driver],
        )
    # Driverless: an interactive Flight Deck container running the derived tag.
    env: dict[str, str] = {}
    if wt.workspace:
        env["THEOZOLITH_REPO"] = wt.workspace
    env.update(stack.env)
    return StackDef(
        name=stack.name,
        kind="container",
        node=stack.node,
        state=stack.state,
        worker_type=wt.name,
        env=env,
        secrets=dict(wt.secrets),
        command=wt.command,
        image=wt.tag,
        volumes=wt.volumes,
        attach=stack.attach,
    )


def _resolve_stacks(
    stacks: tuple[StackDef, ...], worker_types: dict[str, WorkerTypeDef]
) -> tuple[StackDef, ...]:
    resolved: list[StackDef] = []
    for stack in stacks:
        if not stack.worker_type:
            resolved.append(stack)
            continue
        wt = worker_types.get(stack.worker_type)
        if wt is None:
            raise ConfigRepoError(
                f"stacks/{stack.name}.toml: worker_type {stack.worker_type!r} has no"
                f" worker-types/{stack.worker_type}.toml"
            )
        resolved.append(_resolve_worker_stack(stack, wt))
    return tuple(resolved)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigRepoError(f"{path.name}: {exc}") from exc


def _commit(repo_dir: Path) -> str:
    """The repo's git HEAD; a content hash when it is a plain folder."""
    if (repo_dir / ".git").exists():
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    digest = hashlib.sha256()
    for path in sorted(repo_dir.rglob("*.toml")):
        digest.update(path.as_posix().encode())
        digest.update(path.read_bytes())
    return f"folder-{digest.hexdigest()[:12]}"


def load_config(repo_dir: Path) -> DeployConfig:
    """Parse the Config Repo; a missing repo is an empty deployment."""
    if not repo_dir.is_dir():
        return DeployConfig(commit="", stacks=(), worker_types={})
    if any((repo_dir / "images").glob("*.toml")):
        raise ConfigRepoError(
            "images/ is gone — rename each images/<name>.toml into"
            " worker-types/<name>.toml and add the worker-type fields"
            " (driver/adapter/model/workspace/secrets) (ADR-0044)"
        )
    worker_types = {
        path.stem: _parse_worker_type(path.stem, _load_toml(path))
        for path in sorted((repo_dir / "worker-types").glob("*.toml"))
    }
    stacks = _resolve_stacks(
        tuple(
            _parse_stack(path.stem, _load_toml(path))
            for path in sorted((repo_dir / "stacks").glob("*.toml"))
        ),
        worker_types,
    )
    _check_jobs_dirs(stacks)
    product_version = ""
    product = repo_dir / "product.toml"
    if product.is_file():
        table = _load_toml(product).get("product", {})
        if isinstance(table, dict):
            version = table.get("version", "")
            if isinstance(version, str):
                product_version = version
    return DeployConfig(
        commit=_commit(repo_dir),
        stacks=stacks,
        worker_types=worker_types,
        product_version=product_version,
        repo_dir=repo_dir,
    )
