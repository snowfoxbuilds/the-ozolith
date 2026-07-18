"""The Config Repo: TOML on the Control Node, JSON desired state on the wire.

ADR-0006/0015: the git-backed folder (default ~/.theozolith/configs) is
parsed only here; each node receives its own desired-state document over the
heartbeat channel and caches it for degraded mode. The channel carries
declarations and references — compose/overlay text is inlined (declarative
topology is desired state), secret and image *values* never are.

Layout::

    stacks/<name>.toml   one Stack per file (kind, node, state, env, secrets,
                         command/run_image | image/compose+overlays)
    images/<name>.toml   derived-image recipes (digest-pinned base, setup,
                         optional Knowledge Source)
    product.toml         optional [product] version pin for the update command

An empty or missing repo is a legal deployment (the deletion test): every
node's desired state is simply empty.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STACK_KINDS = ("process", "container")
DESIRED_STATES = ("running", "stopped")

# Where a node keeps per-Stack jobs directories unless a Stack's env says
# otherwise — must match nodedaemon.daemon.DEFAULT_JOBS_BASE. The daemon
# injects <base>/<stack-name> per process Stack; this module enforces that
# the resolved paths are unique per node (queue-behind ownership, M5).
DEFAULT_JOBS_BASE = "/var/tmp/theozolith/jobs"

ATTACH_PLACEHOLDERS = ("{host}", "{container}")


class ConfigRepoError(RuntimeError):
    """A Config Repo file does not parse or violates the format."""


@dataclass(frozen=True)
class ImageDef:
    """One derived-image recipe (images/<name>.toml)."""

    name: str
    base: str  # full ref, pinned by digest
    setup: tuple[str, ...] = ()
    knowledge_source: str = ""
    knowledge_pin: str = ""

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

    def as_wire(self) -> dict[str, Any]:
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
    env: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)  # ENV_NAME -> secret name
    command: str = ""  # process kind
    run_image: str = ""  # process kind: images/<name> the driver launches
    image: str = ""  # container kind, single-image form
    ports: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    compose: str = ""  # container kind, compose form (repo-relative path)
    overlays: tuple[str, ...] = ()
    # Web-terminal attach command as a structured argv array; ``{host}`` and
    # ``{container}`` are permitted only as complete elements, substituted
    # (after validation) by the Control Node's PTY bridge. Empty = no
    # terminal exposed for this Stack (NODE-SUBSTRATE: dashboard and
    # operator access). Consumed control-side only; it never travels to
    # nodes. Free-form command strings are rejected (M5 hardening).
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
            "run_image": self.run_image,
            "image": self.image,
            "ports": list(self.ports),
            "volumes": list(self.volumes),
            "compose_files": compose_files,  # [{name, content}] base first
        }


@dataclass(frozen=True)
class DeployConfig:
    commit: str
    stacks: tuple[StackDef, ...]
    images: dict[str, ImageDef]
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
        """The one JSON document a node reconciles from (and caches)."""
        stacks = self.stacks_for(node)
        image_names = {stack.run_image for stack in stacks if stack.run_image}
        return {
            "commit": self.commit,
            "product_version": self.product_version,
            "stacks": [stack.as_wire(self._compose_files(stack)) for stack in stacks],
            "images": [
                self.images[name].as_wire() for name in sorted(image_names) if name in self.images
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


def _str_map(data: dict, key: str, context: str) -> dict[str, str]:
    value = data.get(key, {})
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise ConfigRepoError(f"{context}: {key!r} must be a table of strings")
    return dict(value)


def _parse_stack(name: str, data: dict[str, Any]) -> StackDef:
    context = f"stacks/{name}.toml"
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
        run_image=_require_str(data, "run_image", context, default=""),
        image=_require_str(data, "image", context, default=""),
        ports=_str_list(data, "ports", context),
        volumes=_str_list(data, "volumes", context),
        compose=_require_str(data, "compose", context, default=""),
        overlays=_str_list(data, "overlays", context),
        attach=_parse_attach(data, context),
    )
    if stack.kind == "process" and not stack.command:
        raise ConfigRepoError(f"{context}: process Stacks require 'command'")
    if stack.kind == "container" and bool(stack.image) == bool(stack.compose):
        raise ConfigRepoError(f"{context}: container Stacks declare exactly one of image/compose")
    return stack


def _parse_image(name: str, data: dict[str, Any]) -> ImageDef:
    context = f"images/{name}.toml"
    base = _require_str(data, "base", context)
    if "@sha256:" not in base:
        raise ConfigRepoError(f"{context}: base must be pinned by digest (ADR-0006)")
    return ImageDef(
        name=name,
        base=base,
        setup=_str_list(data, "setup", context),
        knowledge_source=_require_str(data, "knowledge_source", context, default=""),
        knowledge_pin=_require_str(data, "knowledge_pin", context, default=""),
    )


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
        return DeployConfig(commit="", stacks=(), images={})
    stacks = tuple(
        _parse_stack(path.stem, _load_toml(path))
        for path in sorted((repo_dir / "stacks").glob("*.toml"))
    )
    _check_jobs_dirs(stacks)
    images = {
        path.stem: _parse_image(path.stem, _load_toml(path))
        for path in sorted((repo_dir / "images").glob("*.toml"))
    }
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
        images=images,
        product_version=product_version,
        repo_dir=repo_dir,
    )
