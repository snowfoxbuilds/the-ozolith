"""The pinned build: TOML on the Control Node, JSON desired state on the wire.

ADR-0006/0015/0048: the git-backed folder (default ~/.theozolith/configs) is
parsed only here; each node receives its own desired-state document over the
heartbeat channel and caches it for degraded mode. The channel carries
declarations and references — compose/overlay text is inlined (declarative
topology is desired state), secret and image *values* never are. Since
ADR-0048 this tree is the machine-owned PINNED BUILD, committed only by
``theozolith config ingest`` from the human Config Repo; loading is unchanged,
but resolved pins are joined from the ingest-written ``pins.toml``.

Layout::

    stacks/<name>.toml        one Stack per file. A worker-type Stack is thin
                              (worker_type + node + state + env + attach); a
                              plain generic Stack carries the substrate format
                              (kind, node, state, env, secrets, command |
                              image/compose+overlays)
    worker-types/<name>.toml  the complete customization unit for one worker
                              (ADR-0044): driver/adapter/model/effort/workspace/
                              secrets plus the derived-image recipe (digest-
                              pinned base, setup, optional in-repo knowledge
                              reference ``knowledge = "knowledge/<name>"``,
                              ADR-0048). Driverless types are Flight Decks
                              (interactive containers). model/effort are
                              validated against the adapter and baked into the
                              image (ADR-0045).
    knowledge/<name>/<tool>/  one COMPILED knowledge tree per (name, tool)
                              (ADR-0048, per-tool since ADR-0052): each
                              registered ADR-0009 compiler's output, written
                              at ingest, distributed to nodes alongside
                              drivers/ (a pre-ADR-0052 build keeps the claude
                              compile bare under knowledge/<name>/ until the
                              next ingest migrates it)
    policy/<name>/            one Agent Policy tree per name (ADR-0055):
                              verbatim Claude managed-settings drop-ins,
                              allowlist-validated at ingest AND at load,
                              copied by ingest, distributed to nodes
                              alongside drivers/ and knowledge/ (baked into
                              driver-type images, live-mounted into decks)
    pins.toml                 machine-written by ingest: source-commit stamp,
                              base tag->digest resolutions, per-knowledge-tree
                              content-hash pins keyed "<name>/<tool>", and
                              per-policy-tree pins under [policy]
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
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The adapter capability registry (ADR-0045): control validates worker-type
# model/effort values against the SAME code the derived image runs at build
# time (theozolith-worker is a runtime dependency, ADR-0015 amendment), so an
# unmappable value fails here at config load — earlier than the in-image
# ``theozolith-adapter`` backstop — and the synthesized setup instruction is
# rendered by the one shared renderer.
# The Agent Policy validator (ADR-0055): the ONE safe-key allowlist, owned by
# the claude adapter, applied here at config load and by ingest at staging
# time. Imported as a MODULE and invoked through the attribute at every call
# site, so a single monkeypatch of theozolith_worker.policy.validate_policy_tree
# observes both sites — the provable-sharing contract the tests pin.
from theozolith_worker import policy as agentpolicy
from theozolith_worker.adapters import (
    MODEL_ALIAS,
    MODEL_UNMAPPABLE,
    SCOPE_INTERACTIVE,
    SCOPE_MANAGED,
    AgentAdapterError,
    make_agent_adapter,
    materialize_instruction,
)

from theozolith_control import configdist

STACK_KINDS = ("process", "container")
DESIRED_STATES = ("running", "stopped")

# Repo-relative prefixes that are git-native only (ADR-0042/0048/0055): a
# config write here equals code execution (drivers/), agent-instruction
# injection (knowledge/), or managed-settings injection into decks and images
# (policy/) on nodes, so none is ever touched by the web UI or any future
# config editor — drivers/ is edited in git, knowledge/ is compiled and
# policy/ validated-and-copied by ingest.
GIT_NATIVE_ONLY = ("drivers/", "knowledge/", "policy/")

# Files only `theozolith config ingest` writes (ADR-0048); refused for any
# UI/editor write alongside the git-native prefixes.
MACHINE_OWNED_FILES = ("pins.toml",)

# The ingest-written pins file at the pinned-build root (ADR-0048).
PINS_FILE = "pins.toml"

# Where a driver type's baked knowledge lands in the derived image, derived
# from the type's adapter (ADR-0052). Control computes this — the daemon
# stays adapter-blind and COPYs to the path it is told. The claude entry is
# the historical default: recipes that omit the field (and old daemons that
# ignore it) behave exactly as before, which is also why only a NON-default
# target enters the image identity (see instruction_hash).
_KNOWLEDGE_TARGETS = {
    "claude": "/home/ozolith/.claude/",
    "codex": "/home/ozolith/.codex/",
}
_DEFAULT_KNOWLEDGE_TARGET = _KNOWLEDGE_TARGETS["claude"]

# An in-repo knowledge reference is ``knowledge/<name>`` (ADR-0048). The name
# rule matches the knowledge package's entry-name rule and — by requiring a
# leading alphanumeric — can never collide with the distribution's excluded
# names (dot-prefixed components never ride the config distribution).
KNOWLEDGE_REF_PREFIX = "knowledge/"
KNOWLEDGE_TREE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# An in-repo Agent Policy reference is ``policy/<name>`` (ADR-0055); the name
# rule is the knowledge tree-name rule.
POLICY_REF_PREFIX = "policy/"

# A CLI Pin declaration (ADR-0055) is a plausible npm exact version or
# dist-tag: no whitespace, no '/'. The exact version an ingest resolves it to
# is semver with an optional prerelease suffix (mirrors ingest._CLI_VERSION).
CLI_DECLARED_NAME = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*")
CLI_VERSION = re.compile(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")

# The built-in drivers a worker type may name (ADR-0044/ADR-0020) and the
# supervised command each resolves to control-side. Every builtin runs through
# the one generic launcher (`theozolith-driver <ref>`, ADR-0020), so control is
# the single place that names the command; a custom `drivers/<name>` ref
# resolves through the same launcher (_resolve_driver_command, ADR-0042). The
# keys must match theozolith_worker.drivercli.BUILTIN_WORKERS (contract test).
BUILTIN_DRIVERS = {
    "builtin:implementer": "theozolith-driver builtin:implementer",
    "builtin:reviewer": "theozolith-driver builtin:reviewer",
}

# The launcher command every built-in driver resolves through; a plain process
# Stack invoking it directly is rejected (the runner only works with the env a
# worker type injects — see _parse_stack's guard). A custom driver resolves to
# the same launcher with a ``drivers/<name>`` ref (ADR-0042).
DRIVER_LAUNCHER = "theozolith-driver"

# A custom driver ref is ``drivers/<name>`` where ``<name>`` is a valid Python
# identifier (ADR-0042): the module is imported as ``drivers.<name>``, so dashes
# are unimportable and rejected here (the resolver) exactly as the runner rejects
# them. Same shape both sides — one source of truth for the convention.
CUSTOM_DRIVER_PREFIX = "drivers/"
CUSTOM_DRIVER_NAME = re.compile(r"[a-z_][a-z0-9_]*")

# A managed registry pull credential (ADR-0049): the stored secret named
# ``registry:<host>`` (value ``<user>:<token>``) that ingest uses to resolve a
# PRIVATE base image digest and the container-host uses to pull it. The prefix
# is reserved: a ``registry:``-prefixed name can never be a workload [secrets]
# binding value (an infra pull credential must never be routed into a Stack's
# environment by a config edit) — enforced by _guard_secret_bindings.
REGISTRY_SECRET_PREFIX = "registry:"


def registry_host(ref: str) -> str:
    """The registry host of a base image ref — the key its ``registry:<host>``
    pull credential is stored under (ADR-0049). Accepts a digest-pinned ref
    (loaded ``base`` values carry ``@sha256:…``): the digest is stripped, then
    the ingest resolver's first-component rule applies — a dotted, ported, or
    ``localhost`` first path element IS the registry; anything else is Docker
    Hub shorthand, normalized to ``registry-1.docker.io`` (the node-side
    ``config.json`` writer duplicates the Hub auth under docker's legacy key)."""
    ref = ref.split("@", 1)[0]
    head, _, rest = ref.partition("/")
    if rest and ("." in head or ":" in head or head == "localhost"):
        return head
    return "registry-1.docker.io"


def validate_registry_secret(name: str, value: str) -> None:
    """Shape-check a secret at the WRITE surface (PUT /api/v1/secrets and the
    web form, ADR-0049). Only ``registry:``-prefixed names are constrained —
    a managed registry pull credential must name a plausible host and carry a
    ``<user>:<token>`` value, or ingest and the node cannot use it. Every
    other name stays shape-blind (secret values are opaque). Raises
    ``ConfigRepoError`` with an actionable message on a malformed credential."""
    if not name.startswith(REGISTRY_SECRET_PREFIX):
        return
    host = name[len(REGISTRY_SECRET_PREFIX) :]
    if not host or "/" in host or any(ch.isspace() for ch in host):
        raise ConfigRepoError(
            f"secret name {name!r} is not a usable registry credential — "
            f"'{REGISTRY_SECRET_PREFIX}<host>' needs a plausible registry host"
            " (e.g. registry:ghcr.io, registry:localhost:5000)"
        )
    if ":" not in value:
        raise ConfigRepoError(
            f"registry credential {name!r} must be '<user>:<token>' (e.g. a"
            " GitHub username and a PAT with read:packages)"
        )


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

# The per-Stack placeholder a driverless worker type's volume names may carry
# (ADR-0043): substituted with the resolving Stack's name so two same-type
# Flight Decks on one node get distinct runtime-state and tailnet-identity
# volumes; an entry that omits it (e.g. the read-only knowledge bind, ADR-0048)
# is deliberately shared across siblings of the type. Echoes the
# {host}/{container} attach convention; it is the only volume placeholder and
# is resolved control-side — the daemon only ever receives concrete volume
# names.
VOLUME_STACK = "{stack}"


class ConfigRepoError(RuntimeError):
    """A Config Repo file does not parse or violates the format."""


def refuse_ui_write(relpath: str) -> None:
    """The git-native-only guard (ADR-0042). CONTRACT: any future config editor
    MUST call this on every repo-relative path it would write, BEFORE writing —
    a path that is, or resolves under, ``drivers/`` raises ``ConfigRepoError``.
    The web UI and any config editor never touch ``drivers/``; that code is
    edited in git only, because a Config Repo write there is code execution on
    every node. There is no general repo editor today (only fixed-filename
    allow-list writers for control.toml and product.toml), so this is the
    standing constraint the next one inherits.

    The input is parsed as a repository-relative POSIX path — separators
    normalized, then split into components — never compared by string prefix
    against a partially normalized value. Anything that is not a plain
    relative path is refused outright rather than resolved permissively: an
    absolute or drive-letter path, or any empty/``.``/``..`` component. That
    closes every aliased spelling (``./drivers/x``, ``stacks/../drivers/x``,
    ``drivers/../drivers/x``, backslash variants) without needing filesystem
    resolution — a config editor has no business writing through such
    spellings anyway."""
    normalized = relpath.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or (len(normalized) >= 2 and normalized[1] == ":")
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ConfigRepoError(
            f"{relpath!r} is not a plain repo-relative path — a config editor"
            " writes canonical repo-relative POSIX paths only (no absolute or"
            " drive-letter paths, no empty, '.' or '..' components; ADR-0042)"
        )
    for prefix in GIT_NATIVE_ONLY:
        if parts[0] == prefix.rstrip("/"):
            raise ConfigRepoError(
                f"{relpath!r} is under a git-native-only path ({prefix}) — driver"
                " code, compiled knowledge, and Agent Policy trees are never"
                " editable through the web UI or a config editor; edit the"
                " Config Repo and ingest (ADR-0042/0048/0055)"
            )
    if normalized in MACHINE_OWNED_FILES:
        raise ConfigRepoError(
            f"{relpath!r} is machine-owned — only `theozolith config ingest` writes it (ADR-0048)"
        )


_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Pins:
    """The ingest-resolved pins (pins.toml, ADR-0048): the decisions that exist
    nowhere else. ``base`` maps a tag-only base ref to its resolved
    ``sha256:<hex>`` digest; ``knowledge`` maps a per-tool compiled tree key
    ``<name>/<tool>`` to its content hash (ADR-0052 — a legacy bare
    ``<name>`` key from a pre-per-tool pinned build normalizes to
    ``<name>/claude`` at load, the only compiler that could have written it);
    ``policy`` maps an Agent Policy tree name to its content hash (ADR-0055);
    ``cli`` maps a ``<tool>/<declared>`` CLI Pin key to its ingest-resolved
    ``{"version": ..., "platforms": {"<os>-<arch>-<libc>": {"package": ...,
    "integrity": ...}}}`` record (ADR-0055); ``source_commit`` stamps the
    Config Repo commit the pinned build was ingested from."""

    source_commit: str = ""
    base: dict[str, str] = field(default_factory=dict)
    knowledge: dict[str, str] = field(default_factory=dict)
    policy: dict[str, str] = field(default_factory=dict)
    cli: dict[str, dict] = field(default_factory=dict)


def load_pins(repo_dir: Path) -> Pins:
    """Parse ``pins.toml`` at the pinned-build root; a missing file is the
    empty pin set (a pre-ingest tree, or one whose bases are digest-pinned by
    hand and which references no knowledge). Malformed shapes fail loudly —
    the file is machine-written, so any deviation is corruption or a hand
    edit, both of which the load must surface."""
    path = Path(repo_dir) / PINS_FILE
    if not path.is_file():
        return Pins()
    data = _load_toml(path)
    source = data.get("source", {})
    commit = source.get("commit", "") if isinstance(source, dict) else ""
    if not isinstance(commit, str):
        raise ConfigRepoError(f"{PINS_FILE}: [source].commit must be a string")
    base = data.get("base", {})
    knowledge = data.get("knowledge", {})
    policy = data.get("policy", {})
    cli = data.get("cli", {})
    if not all(isinstance(table, dict) for table in (base, knowledge, policy, cli)):
        raise ConfigRepoError(
            f"{PINS_FILE}: [base], [knowledge], [policy], and [cli] must be tables"
        )
    for ref, digest in base.items():
        if not isinstance(digest, str) or not (
            digest.startswith("sha256:") and _HEX64.fullmatch(digest[len("sha256:") :])
        ):
            raise ConfigRepoError(
                f"{PINS_FILE}: [base] {ref!r} must map to 'sha256:<64 hex>', got {digest!r}"
            )
    normalized: dict[str, str] = {}
    for name, tree_hash in knowledge.items():
        if not isinstance(tree_hash, str) or not _HEX64.fullmatch(tree_hash):
            raise ConfigRepoError(
                f"{PINS_FILE}: [knowledge] {name!r} must map to a 64-hex content"
                f" hash, got {tree_hash!r}"
            )
        parts = name.split("/")
        if len(parts) > 2 or not all(KNOWLEDGE_TREE_NAME.fullmatch(part) for part in parts):
            raise ConfigRepoError(
                f"{PINS_FILE}: [knowledge] key {name!r} must be '<name>/<tool>'"
                " (or a legacy bare '<name>') with slug components (ADR-0052)"
            )
        # A bare key predates per-tool compiles; only the claude compiler
        # could have written it. Unknown tool suffixes are tolerated: a
        # newer ingest may pin tools this control version has no use for.
        normalized[name if len(parts) == 2 else f"{name}/claude"] = tree_hash
    policy_pins: dict[str, str] = {}
    for name, tree_hash in policy.items():
        if not isinstance(tree_hash, str) or not _HEX64.fullmatch(tree_hash):
            raise ConfigRepoError(
                f"{PINS_FILE}: [policy] {name!r} must map to a 64-hex content"
                f" hash, got {tree_hash!r}"
            )
        if not KNOWLEDGE_TREE_NAME.fullmatch(name):
            raise ConfigRepoError(
                f"{PINS_FILE}: [policy] key {name!r} must be a plain tree name"
                " (^[A-Za-z0-9][A-Za-z0-9._-]*$) (ADR-0055)"
            )
        policy_pins[name] = tree_hash
    cli_pins: dict[str, dict] = {}
    for key, pin in cli.items():
        # The [cli] table is machine-written (ADR-0055): any shape deviation
        # is corruption or a hand edit, surfaced with the exact key.
        parts = key.split("/")
        if len(parts) != 2 or not all(KNOWLEDGE_TREE_NAME.fullmatch(part) for part in parts):
            raise ConfigRepoError(
                f"{PINS_FILE}: [cli] key {key!r} must be '<tool>/<declared>' (ADR-0055)"
            )
        version = pin.get("version", "") if isinstance(pin, dict) else ""
        if not isinstance(version, str) or not CLI_VERSION.fullmatch(version):
            raise ConfigRepoError(
                f"{PINS_FILE}: [cli] {key!r} must carry an exact"
                f" <major>.<minor>.<patch> version, got {version!r}"
            )
        platforms = pin.get("platforms")
        if not isinstance(platforms, dict) or not platforms:
            raise ConfigRepoError(
                f"{PINS_FILE}: [cli] {key!r} must carry a non-empty platforms table"
            )
        for tuple_key, entry in platforms.items():
            package = entry.get("package", "") if isinstance(entry, dict) else ""
            integrity = entry.get("integrity", "") if isinstance(entry, dict) else ""
            if (
                not isinstance(package, str)
                or not package
                or not isinstance(integrity, str)
                or not integrity.startswith("sha512-")
            ):
                raise ConfigRepoError(
                    f"{PINS_FILE}: [cli] {key!r} platform {tuple_key!r} must map"
                    " to { package = <non-empty>, integrity = 'sha512-...' }"
                )
        cli_pins[key] = {"version": version, "platforms": dict(platforms)}
    return Pins(
        source_commit=commit,
        base=dict(base),
        knowledge=normalized,
        policy=policy_pins,
        cli=cli_pins,
    )


@dataclass(frozen=True)
class WorkerTypeDef:
    """The complete customization unit for one worker (worker-types/<name>.toml,
    ADR-0044): the run-image recipe plus the per-type variables — driver,
    Agent adapter, model, workspace, and secrets. A strict superset of the
    old ImageDef. Absence of ``driver`` is the discriminator: a driverless
    type is a Flight Deck (interactive container) and may carry ``command``
    and ``volumes``; a driver type is a pipeline worker (process kind).

    The derived-image identity (``instruction_hash`` and ``tag``) is computed
    over the bytes that build the image: the image fields plus the
    materialized model/effort setup instruction (ADR-0045 amending ADR-0044).
    driver/workspace/secrets still do not change image bytes and stay outside
    the identity; a type with no model/effort has no materialize step, so its
    hash is byte-identical to the pre-ADR-0045 value and rebuilds nothing."""

    name: str
    base: str  # full ref, pinned by digest
    setup: tuple[str, ...] = ()
    # In-repo knowledge reference (ADR-0048): "" or "knowledge/<name>". The
    # pin is the ingest-computed per-tree content hash joined from pins.toml
    # at load — never authored in the worker-type TOML. DRIVER types bake the
    # tree, so both fields are image identity: editing a knowledge tree
    # re-tags exactly the types that reference it. DRIVERLESS (Flight Deck)
    # types never bake (~/.claude is volume-shadowed): the reference selects
    # which applied tree the deck's read-only mount serves, delivered as the
    # control-injected THEOZOLITH_KNOWLEDGE_TREE env — changing the SELECTION
    # changes the container spec (the deck is recreated), while a content
    # edit moves only the pin and redistributes live, rebuilding and
    # recreating nothing.
    knowledge: str = ""
    knowledge_pin: str = ""
    # In-repo Agent Policy reference (ADR-0055): "" or "policy/<name>". The
    # pin is ingest-computed and joined from pins.toml at load, never
    # authored. DRIVER types BAKE the tree into the managed drop-in dir, so
    # both fields enter the image identity — conditionally, exactly like the
    # ADR-0052 knowledge_target key, so a policy-less identity hashes
    # byte-identically to before. DRIVERLESS (Flight Deck) types never bake:
    # the reference selects which exported tree the deck's read-only mount
    # serves, delivered as the control-injected THEOZOLITH_POLICY_TREE env —
    # changing the SELECTION changes the container spec (recreate once) while
    # a content edit moves only the pin and redistributes live.
    policy: str = ""
    policy_pin: str = ""
    # The CLI Pin (ADR-0055): the declared value ("" or an exact npm version /
    # dist-tag), plus the ingest-resolved exact version and per-platform
    # {package, integrity} map joined from pins.toml at load — never authored.
    # Declared, fleet-visible, and deliberately NOT identity-bearing on a
    # driverless type: the pinned CLI is delivered LIVE through the node's
    # fail-closed install and the deck's launch-path shim, so a version bump
    # changes no image byte and recreates nothing — adopting or dropping the
    # field recreates once through the injected THEOZOLITH_WORKER_TYPE env.
    # Driverless-only and claude-only in v1 (driver types keep the base
    # image's CLI as identity bytes; codex has no consumer).
    cli: str = ""
    cli_version: str = ""
    cli_platforms: dict[str, dict[str, str]] = field(default_factory=dict)
    # -- per-type variables --
    driver: str = ""  # "" (Flight Deck) | "builtin:<name>" | "drivers/<name>"
    adapter: str = "claude"  # Agent adapter the harness invokes
    # model/effort are identity-bearing (ADR-0045): validated against the
    # adapter at parse time, baked into the image via the materialize step,
    # never delivered as env vars or invocation flags. model is required when
    # a driver is set (the drivers' shipped defaults are gone); effort "" =
    # the model's own default.
    model: str = ""
    effort: str = ""
    workspace: str = ""  # target repo, owner/name (required with driver)
    secrets: dict[str, str] = field(default_factory=dict)  # ENV_NAME -> secret name
    # driverless only: the container's FULL start command — the daemon runs
    # it via --entrypoint, overriding any ENTRYPOINT the base image carries
    # (a derived run image inherits the harness entrypoint; the Flight Deck
    # must start its own script instead, never hand it to the harness as argv)
    command: str = ""
    volumes: tuple[str, ...] = ()  # driverless only

    @property
    def is_driver(self) -> bool:
        return bool(self.driver)

    @property
    def knowledge_tree(self) -> str:
        """The bare tree name of the knowledge reference ("" when none)."""
        return self.knowledge[len(KNOWLEDGE_REF_PREFIX) :] if self.knowledge else ""

    @property
    def baked_knowledge(self) -> str:
        """The knowledge reference AS BAKED: only a driver type bakes its
        tree into the derived image (ADR-0048) — a Flight Deck's ~/.claude is
        volume-shadowed, so its reference selects the read-only mount instead
        and must stay out of the image recipe and the image identity."""
        return self.knowledge if self.is_driver else ""

    @property
    def baked_knowledge_pin(self) -> str:
        return self.knowledge_pin if self.is_driver else ""

    @property
    def policy_tree(self) -> str:
        """The bare tree name of the Agent Policy reference ("" when none)."""
        return self.policy[len(POLICY_REF_PREFIX) :] if self.policy else ""

    @property
    def baked_policy(self) -> str:
        """The policy reference AS BAKED: only a driver type bakes the tree
        into its derived image (ADR-0055) — a Flight Deck's reference selects
        the live read-only mount instead and must stay out of the image
        recipe and the image identity (a deck policy edit or reselection must
        never rebuild an image)."""
        return self.policy if self.is_driver else ""

    @property
    def baked_policy_pin(self) -> str:
        return self.policy_pin if self.is_driver else ""

    @property
    def knowledge_tool(self) -> str:
        """Which per-tool compile of the tree the node stages for the bake
        (ADR-0052): the adapter name, for a baking (driver) type only."""
        return self.adapter if self.baked_knowledge else ""

    @property
    def knowledge_target(self) -> str:
        """Where the baked tree lands in the derived image ("" when nothing
        bakes). Parse time guarantees the adapter is mapped."""
        return _KNOWLEDGE_TARGETS[self.adapter] if self.baked_knowledge else ""

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
    def materialized_setup(self) -> tuple[str, ...]:
        """The setup instructions as they ride the wire and build the image:
        the operator's setup plus, when model/effort is set, ONE synthesized
        instruction invoking the in-image ``theozolith-adapter`` CLI
        (ADR-0045). Managed scope for driver run images (native config where
        a workspace checkout cannot override it); interactive scope for
        driverless Flight Deck images (well-known files only — anything under
        /home/ozolith/.claude would be shadowed by the claude-state volume,
        ADR-0043). Appended, never written into the operator's TOML."""
        if not self.model and not self.effort:
            return self.setup
        scope = SCOPE_MANAGED if self.is_driver else SCOPE_INTERACTIVE
        return (*self.setup, materialize_instruction(self.adapter, self.model, self.effort, scope))

    @property
    def instruction_hash(self) -> str:
        # CRITICAL (ADR-0044 as amended by ADR-0045/0052/0055): exactly
        # these four keys — plus ``knowledge_target`` ONLY when it differs
        # from the historical claude default, plus ``policy``/``policy_pin``
        # ONLY when a driver type bakes a policy tree — canonical JSON with
        # sort_keys, over the MATERIALIZED setup: the hash covers the exact
        # bytes that build the image, including the baked model/effort
        # config, so candidate identity equals image identity.
        # driver/workspace/secrets never enter (they change no image bytes;
        # adding them would rebuild the fleet for nothing) — which is what
        # makes their per-Stack rebinding free (ADR-0047) — and a type with
        # no model/effort hashes byte-identically to pre-ADR-0045. The
        # knowledge and policy keys are the BAKED values: a Flight Deck's
        # reference selects a mount, changes no image bytes, and so hashes
        # as empty — a knowledge- or policy-content edit must never rebuild
        # or recreate a deck (ADR-0048/0055); the selection change recreates
        # it through the injected env instead. A non-default knowledge
        # target IS a Dockerfile byte (the COPY destination, ADR-0052) and
        # enters the hash, and so does a baked policy tree (a COPY plus its
        # content pin, ADR-0055) — both conditionally, so every pre-existing
        # identity stays byte-stable and nothing retags on upgrade. The
        # rendered materialize instruction is therefore identity-bearing:
        # its format is frozen by golden tests here and in the worker
        # package.
        identity: dict[str, object] = {
            "base": self.base,
            "setup": list(self.materialized_setup),
            "knowledge": self.baked_knowledge,
            "knowledge_pin": self.baked_knowledge_pin,
        }
        if self.knowledge_target and self.knowledge_target != _DEFAULT_KNOWLEDGE_TARGET:
            identity["knowledge_target"] = self.knowledge_target
        if self.baked_policy:
            identity["policy"] = self.baked_policy
            identity["policy_pin"] = self.baked_policy_pin
        canonical = json.dumps(identity, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def tag(self) -> str:
        """Deterministic: base tag + instruction hash (NODE-SUBSTRATE.md)."""
        return f"theozolith/{self.name}:{self.base_tag}-{self.instruction_hash[:12]}"

    def recipe_wire(self) -> dict[str, Any]:
        """The derived-image recipe as it rides in the ``images`` wire list.
        15 keys (12 before the CLI Pin, 10 pre-ADR-0055, 8 pre-ADR-0052):
        ``cli_tool``/``cli_version``/``cli_platforms`` (ADR-0055) carry the
        CLI Pin the Node Daemon converges under ``<state-dir>/cli/`` — the
        tool computed control-side from the adapter so the daemon stays
        adapter-blind, all three empty on an unpinned type, ignored by old
        daemons, and never part of the image identity. ``knowledge`` (the
        in-repo reference) and ``knowledge_pin`` (the per-tree content pin)
        replace the retired ``knowledge_source`` pair (ADR-0048) — the node
        bakes the referenced tree from its applied config-distribution tree
        after verifying the pin — and ``knowledge_tool``/``knowledge_target``
        (ADR-0052) tell the node WHICH per-tool compile of the tree to stage
        and WHERE the bake COPY lands, both computed control-side from the
        adapter so the daemon stays adapter-blind (an old daemon ignores
        them and behaves exactly as before; empty for legacy-layout dists it
        falls back to the bare tree). ``policy``/``policy_pin`` (ADR-0055)
        name the Agent Policy tree the node stages and pin-verifies into the
        managed drop-in COPY — an old daemon ignores them (its distribution
        refuses policy/ members anyway: advisory skew). The BAKED values
        ride: a driverless type's recipe carries empty knowledge AND policy
        fields, so the node never bakes under a Flight Deck's
        volume-shadowed state (the deck reads the applied trees through its
        read-only mounts instead). ``setup`` is the MATERIALIZED setup
        (ADR-0045): the daemon renders one RUN per entry exactly as
        before."""
        return {
            "name": self.name,
            "base": self.base,
            "setup": list(self.materialized_setup),
            "knowledge": self.baked_knowledge,
            "knowledge_pin": self.baked_knowledge_pin,
            "knowledge_tool": self.knowledge_tool,
            "knowledge_target": self.knowledge_target,
            "policy": self.baked_policy,
            "policy_pin": self.baked_policy_pin,
            "cli_tool": self.adapter if self.cli else "",
            "cli_version": self.cli_version,
            "cli_platforms": {k: dict(v) for k, v in self.cli_platforms.items()},
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
    # Per-placement target-repo binding (ADR-0047), worker-type Stacks only:
    # overrides the type's workspace default at resolution. The resolved
    # Stack carries the outcome in env THEOZOLITH_REPO — this field never
    # travels to nodes (not in as_wire).
    workspace: str = ""
    env: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)  # ENV_NAME -> secret name
    # process kind: the supervised argv string. Container kind (single-image
    # form only): an optional FULL start command — executed via --entrypoint,
    # replacing any ENTRYPOINT the image inherited — which is how the Flight
    # Deck starts its named tmux session (ADR-0019) despite the base run
    # image's harness entrypoint.
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
    # The recorded config-distribution hash (ADR-0042): "" = no drivers/. Only
    # this reference rides the channel; the artifact is pulled by hash.
    drivers_hash: str = ""
    repo_dir: Path | None = None
    # Non-fatal lint findings from load (ADR-0045: e.g. a floating model
    # alias where a pinned ID is the convention). Logged once at the app's
    # config boundary; surfaced to dashboards from here. Never an error — a
    # warning must not take desired state down.
    warnings: tuple[str, ...] = ()

    def stacks_for(self, node: str) -> list[StackDef]:
        return [stack for stack in self.stacks if stack.node == node]

    def secret_names_for(self, node: str) -> set[str]:
        """The node-scoping rule: only secrets referenced by Stacks placed
        on the node may be pulled by it (NODE-SUBSTRATE.md). This includes the
        managed ``registry:<host>`` pull credential (ADR-0049) for every
        worker type behind a RUNNING worker-type Stack on the node — the same
        running-recipe rule ``desired_state_for`` uses for ``images``, so a
        node may pull only the pull credential of a base it will actually
        build."""
        names: set[str] = set()
        for stack in self.stacks_for(node):
            names.update(stack.secrets.values())
        for stack in self.stacks_for(node):
            if stack.worker_type and stack.state == "running":
                wt = self.worker_types.get(stack.worker_type)
                if wt is not None:
                    names.add(REGISTRY_SECRET_PREFIX + registry_host(wt.base))
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
            # Always present; "" = no config distribution (ADR-0042). Only the
            # reference rides the channel — the node pulls the artifact by hash.
            "drivers_hash": self.drivers_hash,
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


# The identity names the worker-type-Stack [env] guard rejects (ADR-0045),
# rejected as secret SLOT names too — at both declaration sites, since the
# type-side hole is the same hole. A slot materializes <slot>_FILE and the
# worker's env reader takes the _FILE spelling FIRST, so a slot named after
# a baked identity field is the same override through a file.
_RESERVED_SECRET_SLOTS = (
    "THEOZOLITH_MODEL",
    "THEOZOLITH_ADAPTER",
    "THEOZOLITH_RUN_IMAGE",
    "THEOZOLITH_RUN_IMAGE_FILE",
)


def _guard_secret_slots(secrets: dict[str, str], context: str) -> None:
    for slot in _RESERVED_SECRET_SLOTS:
        if slot in secrets:
            raise ConfigRepoError(
                f"{context}: [secrets] {slot} is reserved — a secret slot"
                f" materializes {slot}_FILE, which the worker reads first and"
                " would steer the baked worker-type identity"
                " (ADR-0045/ADR-0047); rename the slot"
            )


def _guard_secret_bindings(secrets: dict[str, str], context: str) -> None:
    """A [secrets] binding VALUE (the stored secret name) may not name a
    managed registry pull credential (ADR-0049): ``registry:<host>`` is
    infrastructure delivered to container *builds*, never routed into workload
    env by a config edit. Guarded at both declaration sites (worker-type
    default bindings and per-Stack rebindings)."""
    for slot, stored_name in secrets.items():
        if stored_name.startswith(REGISTRY_SECRET_PREFIX):
            raise ConfigRepoError(
                f"{context}: [secrets] {slot} binds {stored_name!r} — a"
                f" '{REGISTRY_SECRET_PREFIX}' pull credential is infrastructure"
                " (ADR-0049), never a workload secret; it cannot be routed into"
                " a Stack's environment"
            )


# Fields that a fat pre-ADR-0044 Stack carried but that now live in the worker
# type. A thin worker-type Stack that still declares one is rejected by name.
# ``secrets`` moved back OUT of this list (ADR-0047): the type declares the
# slot contract, the Stack may rebind per placement.
_MOVED_TO_WORKER_TYPE = (
    "kind",
    "command",
    "image",
    "compose",
    "overlays",
    "ports",
    "volumes",
)
_WORKER_TYPE_STACK_KEYS = ("worker_type", "node", "state", "env", "attach", "workspace", "secrets")


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
    state, plus the per-placement bindings — workspace and [secrets]
    (ADR-0047). kind/command/image/... all moved into the worker type; the
    concrete StackDef is produced later at resolution."""
    worker_type = _require_str(data, "worker_type", context)
    for key in data:
        if key in _MOVED_TO_WORKER_TYPE:
            raise ConfigRepoError(
                f"{context}: {key!r} moved to worker-types/{worker_type}.toml"
                " (ADR-0044) — a worker-type Stack declares only worker_type,"
                " node, state, env, attach, workspace, secrets"
            )
        if key not in _WORKER_TYPE_STACK_KEYS:
            raise ConfigRepoError(
                f"{context}: unknown key {key!r} on a worker-type Stack"
                " (allowed: worker_type, node, state, env, attach, workspace, secrets)"
            )
    state = _require_str(data, "state", context, default="running")
    if state not in DESIRED_STATES:
        raise ConfigRepoError(f"{context}: state must be one of {DESIRED_STATES}, got {state!r}")
    # Per-placement target-repo binding (ADR-0047): overrides the type's
    # workspace default at resolution.
    workspace = _require_str(data, "workspace", context, default="")
    if workspace and not _valid_workspace(workspace):
        raise ConfigRepoError(
            f"{context}: 'workspace' must be owner/name — exactly two non-empty"
            f" path components — got {workspace!r}"
        )
    # Per-placement secret bindings (ADR-0047): slot -> stored name, merged
    # over the type's contract at resolution. Names only — values never
    # enter the Config Repo (ADR-0006).
    secrets = _str_map(data, "secrets", context)
    _guard_secret_slots(secrets, context)
    _guard_secret_bindings(secrets, context)
    env = _str_map(data, "env", context)
    # Exact names, not a *_MODEL glob: a worker-type Stack's [env] was the
    # last per-placement override of the type's identity fields; with model
    # baked into the image (ADR-0045) an override here would be silently
    # inert, so it is rejected by name. Generic Stacks and any other env
    # stay free.
    for key in ("THEOZOLITH_MODEL", "THEOZOLITH_ADAPTER"):
        if key in env:
            raise ConfigRepoError(
                f"{context}: [env] {key} is gone — model and adapter are"
                " worker-type fields baked into the derived image (ADR-0045);"
                f" edit worker-types/{worker_type}.toml"
            )
    if "THEOZOLITH_KNOWLEDGE_TREE" in env:
        # Per-Stack knowledge is rejected (ADR-0048): the tree selection is
        # worker-type identity, injected control-side from the type's
        # `knowledge` field — an [env] override here would silently point one
        # deck at different instructions than its type declares.
        raise ConfigRepoError(
            f"{context}: [env] THEOZOLITH_KNOWLEDGE_TREE cannot be set on a"
            " worker-type Stack — the knowledge selection is the worker"
            " type's `knowledge` field (per-Stack knowledge is rejected,"
            f" ADR-0048); edit worker-types/{worker_type}.toml"
        )
    if "THEOZOLITH_POLICY_TREE" in env:
        # Per-Stack policy does not exist (ADR-0055): the selection is
        # worker-type declared and injected control-side — an [env] override
        # here would silently run one deck under different managed settings
        # than its type declares.
        raise ConfigRepoError(
            f"{context}: [env] THEOZOLITH_POLICY_TREE cannot be set on a"
            " worker-type Stack — the Agent Policy selection is the worker"
            " type's `policy` field (per-Stack policy is rejected,"
            f" ADR-0055); edit worker-types/{worker_type}.toml"
        )
    if "THEOZOLITH_WORKER_TYPE" in env:
        # The CLI Pin selection key (ADR-0055): worker-type identity, injected
        # control-side when the type pins a CLI — an [env] override here
        # would point one deck's launch gate at another type's pin records.
        raise ConfigRepoError(
            f"{context}: [env] THEOZOLITH_WORKER_TYPE cannot be set on a"
            " worker-type Stack — it is injected control-side from the worker"
            " type itself (the deck's CLI Pin resolves by it, ADR-0055); edit"
            f" worker-types/{worker_type}.toml"
        )
    for key in ("THEOZOLITH_RUN_IMAGE", "THEOZOLITH_RUN_IMAGE_FILE"):
        # Not inert — the opposite: after ADR-0045 the run-image tag IS the
        # model, so a per-placement override here would silently run a
        # different identity than the worker-type definition declares (and
        # the dry-run would validate the substituted image's own identity,
        # happily). The _FILE spelling is the worker's indirection convention
        # and reads FIRST, so it is the same override through a file. The
        # type owns the image (ADR-0044 thin Stacks).
        if key in env:
            raise ConfigRepoError(
                f"{context}: [env] {key} cannot be overridden on a"
                " worker-type Stack — the run-image tag carries the baked"
                " model/effort identity (ADR-0045); change"
                f" worker-types/{worker_type}.toml instead"
            )
    return StackDef(
        name=name,
        kind="",  # derived at resolution: driver type -> process, else container
        node=_require_str(data, "node", context),
        state=state,
        worker_type=worker_type,
        workspace=workspace,
        env=env,
        secrets=secrets,
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
    secrets = _str_map(data, "secrets", context)
    _guard_secret_bindings(secrets, context)
    stack = StackDef(
        name=name,
        kind=kind,
        node=_require_str(data, "node", context),
        state=state,
        env=_str_map(data, "env", context),
        secrets=secrets,
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
    # injects (THEOZOLITH_REPO/ADAPTER/RUN_IMAGE): a plain Stack that
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


def _parse_worker_type(name: str, data: dict[str, Any], pins: Pins | None = None) -> WorkerTypeDef:
    context = f"worker-types/{name}.toml"
    base = _require_str(data, "base", context)
    if "@sha256:" not in base:
        # A tag-only base is legal exactly when ingest resolved it (ADR-0048):
        # the digest lives in pins.toml keyed by the verbatim ref. Anything
        # else keeps the ADR-0006 fail-loud rule.
        resolved = (pins.base if pins else {}).get(base, "")
        if not resolved:
            raise ConfigRepoError(
                f"{context}: base must be pinned by digest (ADR-0006) — pin it"
                " in the Config Repo, or use a tag and let `theozolith config"
                " ingest` resolve it (ADR-0048)"
            )
        base = f"{base}@{resolved}"
    for legacy in ("knowledge_source", "knowledge_pin"):
        if legacy in data:
            raise ConfigRepoError(
                f"{context}: {legacy!r} is retired (ADR-0048) — knowledge lives"
                " in the Config Repo: reference it as knowledge ="
                ' "knowledge/<name>" and let ingest compute the per-tree pin'
            )
    # The adapter selects which per-tool compile of the tree this type
    # consumes (ADR-0052), so it parses before the pin join below.
    adapter_name = _require_str(data, "adapter", context, default="claude")
    knowledge = _require_str(data, "knowledge", context, default="")
    knowledge_pin = ""
    if knowledge:
        tree = knowledge[len(KNOWLEDGE_REF_PREFIX) :]
        if not knowledge.startswith(KNOWLEDGE_REF_PREFIX) or not KNOWLEDGE_TREE_NAME.fullmatch(
            tree
        ):
            raise ConfigRepoError(
                f"{context}: knowledge reference {knowledge!r} must be"
                ' "knowledge/<name>" with a plain tree name'
                " (^[A-Za-z0-9][A-Za-z0-9._-]*$) (ADR-0048)"
            )
        knowledge_pin = (pins.knowledge if pins else {}).get(f"{tree}/{adapter_name}", "")
        if not knowledge_pin:
            raise ConfigRepoError(
                f"{context}: no ingest-computed pin for {knowledge!r} compiled"
                f" for {adapter_name!r} — either the tree has no"
                f" {adapter_name}-consumable content, or the pinned build"
                " predates per-tool compiles: re-run `theozolith config"
                f" ingest`, which records the content hash in {PINS_FILE}"
                " (ADR-0048/ADR-0052)"
            )
    if "policy_pin" in data:
        raise ConfigRepoError(
            f"{context}: 'policy_pin' is ingest-computed, never authored"
            ' (ADR-0055) — declare policy = "policy/<name>" and let'
            " `theozolith config ingest` record the content hash"
        )
    policy = _require_str(data, "policy", context, default="")
    policy_pin = ""
    if policy:
        # The Agent Policy field is claude-only in v1, driver AND driverless
        # alike (unlike knowledge, whose refusal is deck-shaped): codex has
        # no managed-settings tier, so a codex type could never consume the
        # tree — refused here, recorded as out of scope in ADR-0055 §7.
        if adapter_name != "claude":
            raise ConfigRepoError(
                f"{context}: a worker type with adapter {adapter_name!r}"
                " cannot declare an Agent Policy — policy trees are Claude"
                " managed-settings drop-ins, and no other adapter has a"
                " managed-settings tier (ADR-0055)"
            )
        policy_tree = policy[len(POLICY_REF_PREFIX) :]
        if not policy.startswith(POLICY_REF_PREFIX) or not KNOWLEDGE_TREE_NAME.fullmatch(
            policy_tree
        ):
            raise ConfigRepoError(
                f"{context}: policy reference {policy!r} must be"
                ' "policy/<name>" with a plain tree name'
                " (^[A-Za-z0-9][A-Za-z0-9._-]*$) (ADR-0055)"
            )
        policy_pin = (pins.policy if pins else {}).get(policy_tree, "")
        if not policy_pin:
            raise ConfigRepoError(
                f"{context}: no ingest-computed pin for {policy!r} — either"
                " the tree is empty or absent from the Config Repo, or the"
                " pinned build predates it: re-run `theozolith config"
                f" ingest`, which records the content hash in {PINS_FILE}"
                " (ADR-0055)"
            )
    driver = _require_str(data, "driver", context, default="")
    workspace = _require_str(data, "workspace", context, default="")
    command = _require_str(data, "command", context, default="")
    volumes = _str_list(data, "volumes", context)
    if driver:
        if driver.startswith(CUSTOM_DRIVER_PREFIX):
            custom_name = driver[len(CUSTOM_DRIVER_PREFIX) :]
            if not CUSTOM_DRIVER_NAME.fullmatch(custom_name):
                raise ConfigRepoError(
                    f"{context}: custom driver ref {driver!r} — the name after"
                    " 'drivers/' must be a valid Python identifier"
                    " (^[a-z_][a-z0-9_]*$); dashes are unimportable (ADR-0042)"
                )
            is_custom = True
        else:
            is_custom = False
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
        for field_name, present in (("command", command), ("volumes", volumes)):
            if present:
                raise ConfigRepoError(
                    f"{context}: {field_name!r} is a driverless (Flight Deck) field"
                    " and is rejected when a driver is set (ADR-0044)"
                )
    if "cli_version" in data or "cli_platforms" in data:
        raise ConfigRepoError(
            f"{context}: 'cli_version'/'cli_platforms' are ingest-resolved,"
            ' never authored (ADR-0055) — declare cli = "<version|dist-tag>"'
            " and let `theozolith config ingest` record the pinned map"
        )
    cli = _require_str(data, "cli", context, default="")
    cli_version = ""
    cli_platforms: dict[str, dict[str, str]] = {}
    if cli:
        # Field refusals fire BEFORE the pin join, so a definition ingest
        # deliberately skipped (cli with a driver, or on a non-claude
        # adapter) gets its precise error — never the missing-pin message.
        if not CLI_DECLARED_NAME.fullmatch(cli):
            raise ConfigRepoError(
                f"{context}: cli {cli!r} must be a plausible npm exact version"
                " or dist-tag (^[0-9A-Za-z][0-9A-Za-z._-]*$) (ADR-0055)"
            )
        if driver:
            raise ConfigRepoError(
                f"{context}: 'cli' is driverless-only in v1 (ADR-0055) — a"
                " driver type keeps the base image's CLI as identity bytes;"
                " remove the field or drop the driver"
            )
        if adapter_name != "claude":
            raise ConfigRepoError(
                f"{context}: a worker type with adapter {adapter_name!r}"
                " cannot declare a CLI Pin — refused until a consumer exists"
                " (ADR-0055)"
            )
        pin = (pins.cli if pins else {}).get(f"{adapter_name}/{cli}")
        if not pin:
            raise ConfigRepoError(
                f"{context}: no ingest-resolved CLI pin for {cli!r} — re-run"
                " `theozolith config ingest`, which resolves the exact version"
                f" and per-platform integrity map into {PINS_FILE} (ADR-0055)"
            )
        cli_version = pin["version"]
        cli_platforms = {k: dict(v) for k, v in pin["platforms"].items()}
        # Floor re-check at load (grilling Q14): the pinned build may predate
        # a floor bump — the lint site is configrepo/ingest, never the image
        # build or the deck launch.
        floor = make_agent_adapter(adapter_name).MIN_ENFORCING_CLI
        if _cli_version_tuple(cli_version) < tuple(floor):
            floor_text = ".".join(str(part) for part in floor)
            raise ConfigRepoError(
                f"{context}: pinned CLI version {cli_version} is below the"
                f" {adapter_name} adapter's enforcement floor {floor_text}"
                " (ADR-0055) — pin a newer version and re-run `theozolith"
                " config ingest`"
            )
    # Driverless types may declare knowledge too (ADR-0048 amendment): the
    # reference selects which applied tree the deck's read-only mount serves
    # (nothing bakes — the state volume shadows ~/.claude). The pin join and
    # the compiled-tree presence check below apply identically to both kinds.
    # The node's knowledge export serves the CLAUDE view of the tree
    # (ADR-0052), so a non-claude deck cannot be given knowledge it could
    # read — refused here, recorded as future work in ADR-0052.
    if knowledge and not driver and adapter_name != "claude":
        raise ConfigRepoError(
            f"{context}: a driverless (Flight Deck) type with adapter"
            f" {adapter_name!r} cannot declare knowledge — the node's"
            " knowledge export serves the claude view only (ADR-0052)"
        )
    if knowledge and driver and adapter_name not in _KNOWLEDGE_TARGETS:
        raise ConfigRepoError(
            f"{context}: no knowledge bake target is mapped for adapter"
            f" {adapter_name!r} — extend _KNOWLEDGE_TARGETS when adding an"
            " adapter whose worker types bake knowledge (ADR-0052)"
        )
    # A driverless type is a Flight Deck, and the deck machinery is
    # Claude-shaped end to end (ADR-0043; the interactive materialize scope
    # exists only on the Claude adapter). Refuse here rather than at the
    # image build the daemon would fail on.
    if not driver and adapter_name != "claude" and (data.get("model") or data.get("effort")):
        raise ConfigRepoError(
            f"{context}: a driverless (Flight Deck) type with adapter"
            f" {adapter_name!r} cannot bake a model/effort — no"
            f" {adapter_name} Flight Deck exists (ADR-0052)"
        )
    if workspace and not _valid_workspace(workspace):
        raise ConfigRepoError(
            f"{context}: 'workspace' must be owner/name — exactly two non-empty"
            f" path components — got {workspace!r}"
        )
    model = _require_str(data, "model", context, default="")
    effort = _require_str(data, "effort", context, default="")
    _validate_model_effort(context, adapter_name, model, effort, is_driver=bool(driver))
    # The slot contract (ADR-0047): slot -> default stored name; "" declares
    # a required slot every instantiating Stack must bind.
    secrets = _str_map(data, "secrets", context)
    _guard_secret_slots(secrets, context)
    _guard_secret_bindings(secrets, context)
    return WorkerTypeDef(
        name=name,
        base=base,
        setup=_str_list(data, "setup", context),
        knowledge=knowledge,
        knowledge_pin=knowledge_pin,
        policy=policy,
        policy_pin=policy_pin,
        cli=cli,
        cli_version=cli_version,
        cli_platforms=cli_platforms,
        driver=driver,
        adapter=adapter_name,
        model=model,
        effort=effort,
        workspace=workspace,
        secrets=secrets,
        command=command,
        volumes=volumes,
    )


def _cli_version_tuple(version: str) -> tuple[int, int, int]:
    """The comparable (major, minor, patch) of a CLI_VERSION-validated string
    (mirrors ingest._cli_version_tuple; both sides parse the machine-written
    pin, so the shapes cannot drift)."""
    core = version.split("-", 1)[0]
    major, minor, patch = core.split(".")
    return int(major), int(minor), int(patch)


def _validate_model_effort(
    context: str, adapter_name: str, model: str, effort: str, *, is_driver: bool
) -> None:
    """The adapter-capability gate (ADR-0045), at parse time so EVERY worker
    type — dormant and driverless included — breaks at configure time, never
    at build or dispatch (the dormant-driver precedent). The in-image
    ``theozolith-adapter`` CLI re-validates as the build-time backstop."""
    try:
        adapter = make_agent_adapter(adapter_name)
    except AgentAdapterError as exc:
        raise ConfigRepoError(f"{context}: {exc}") from exc
    if is_driver and not model:
        raise ConfigRepoError(
            f"{context}: 'model' is required when a driver is set (ADR-0045) —"
            " the derived image bakes the model into the adapter's native"
            " config; drivers no longer ship a default"
        )
    if effort and not is_driver:
        raise ConfigRepoError(
            f"{context}: 'effort' is rejected on driverless (Flight Deck) worker"
            " types (ADR-0045) — interactive scope bakes only the default-model"
            " file, and nothing at runtime consumes a baked effort; set effort"
            " when a consumer exists"
        )
    if model and adapter.classify_model(model) == MODEL_UNMAPPABLE:
        raise ConfigRepoError(
            f"{context}: adapter {adapter_name!r} cannot map model {model!r}"
            f" (mappable: {adapter.model_shapes}) (ADR-0045)"
        )
    if effort and effort not in adapter.mappable_efforts():
        known = ", ".join(sorted(adapter.mappable_efforts())) or "none"
        raise ConfigRepoError(
            f"{context}: adapter {adapter_name!r} cannot map effort {effort!r}"
            f" (mappable: {known}) (ADR-0045)"
        )
    # Pair-aware capability validation (ADR-0045 amendment): the effort must
    # be provably honored by THIS model. Claude Code silently runs the
    # highest supported level at or below an unsupported one (xhigh becomes
    # high on the 4.6 generation) and silently ignores effort on models
    # without the setting — a baked value the session would not actually run
    # at is a fake identity, so the pair fails the load. An unknown future
    # model paired with an effort fails too: enforceability must be
    # positively known, never assumed.
    pair = adapter.pair_error(model, effort)
    if pair:
        raise ConfigRepoError(f"{context}: {pair}")


def _resolve_volumes(volumes: tuple[str, ...], stack_name: str) -> tuple[str, ...]:
    """Substitute the ``{stack}`` placeholder in each worker-type volume entry
    with the resolving Stack's name (ADR-0043). Only the name segment (before
    the first ``:``) is rewritten — mount paths are fixed — and only the whole
    ``{stack}`` token is replaced; entries that deliberately omit it (the
    read-only knowledge bind, ADR-0048) stay shared across siblings of the
    type."""
    resolved = []
    for volume in volumes:
        name, sep, rest = volume.partition(":")
        resolved.append(f"{name.replace(VOLUME_STACK, stack_name)}{sep}{rest}")
    return tuple(resolved)


def _worker_type_warnings(wt: WorkerTypeDef) -> list[str]:
    """Non-fatal lint (ADR-0045): warn — never fail — on a floating model
    alias. Current-generation provider IDs ship without a dated variant, so
    'pin the most-dated ID' can only ever be a convention nudge; the adapter
    already classified the value as mappable."""
    warnings: list[str] = []
    if wt.model and make_agent_adapter(wt.adapter).classify_model(wt.model) == MODEL_ALIAS:
        warnings.append(
            f"worker-types/{wt.name}.toml: model {wt.model!r} is a floating"
            " alias — pin the most-dated provider model ID (ADR-0045)"
        )
    return warnings


def _custom_driver_exists(repo_dir: Path, name: str) -> bool:
    """Whether a ``drivers/<name>`` custom driver is present in the Config Repo,
    in either sanctioned form (ADR-0042): the module file ``drivers/<name>.py``
    or the package ``drivers/<name>/__init__.py``."""
    return (repo_dir / "drivers" / f"{name}.py").is_file() or (
        repo_dir / "drivers" / name / "__init__.py"
    ).is_file()


def _resolve_driver_command(wt: WorkerTypeDef, repo_dir: Path | None) -> str:
    """The supervised argv a driver worker type resolves to (ADR-0042/ADR-0020):
    every driver runs through the one generic launcher. ``builtin:*`` maps to the
    fixed command; ``drivers/<name>`` maps to ``theozolith-driver drivers/<name>``
    after VERIFYING the module exists in the Config Repo — a dangling reference
    fails loudly here at config-load time on the Control Node, never silently at
    process start on a node. The name shape was validated in _parse_worker_type."""
    if wt.driver in BUILTIN_DRIVERS:
        return BUILTIN_DRIVERS[wt.driver]
    name = wt.driver[len(CUSTOM_DRIVER_PREFIX) :]
    if repo_dir is None or not _custom_driver_exists(repo_dir, name):
        raise ConfigRepoError(
            f"worker-types/{wt.name}.toml: custom driver {wt.driver!r} has no module"
            f" in the Config Repo — expected drivers/{name}.py or"
            f" drivers/{name}/__init__.py (ADR-0042)"
        )
    return f"{DRIVER_LAUNCHER} {wt.driver}"


def _merge_secret_bindings(stack: StackDef, wt: WorkerTypeDef, context: str) -> dict[str, str]:
    """The per-placement secret binding (ADR-0047): key-wise merge, the Stack
    wins — how two Stacks of one type act as distinct identities. An empty
    name on the TYPE declares a required slot (every instantiating Stack must
    bind it — fails here at config load, never as a deploy-time 404 on the
    node); an empty name on the STACK unbinds an inherited default (the
    per-instance TS_AUTHKEY removal). Empty entries never reach the resolved
    Stack, so ``secret_names_for`` and the daemon only ever see real names."""
    resolved: dict[str, str] = {}
    for slot, name in {**wt.secrets, **stack.secrets}.items():
        if name:
            resolved[slot] = name
        elif wt.secrets.get(slot) == "":
            raise ConfigRepoError(
                f"{context}: [secrets] {slot} is a required slot on worker type"
                f" {wt.name!r} — declared with an empty name in"
                f" worker-types/{wt.name}.toml, so every instantiating Stack"
                " must bind it to a stored secret name (ADR-0047)"
            )
        elif slot not in wt.secrets:
            raise ConfigRepoError(
                f'{context}: [secrets] {slot} = "" unbinds a slot worker type'
                f" {wt.name!r} does not declare — remove the entry, or bind it"
                " to a stored secret name (ADR-0047)"
            )
        # else: a type default deliberately unbound by this Stack — dropped.
    return resolved


def _resolve_worker_stack(stack: StackDef, wt: WorkerTypeDef, repo_dir: Path | None) -> StackDef:
    """Turn a thin worker-type Stack into a concrete generic StackDef (ADR-0044).

    Env injection happens control-side (the daemon's run_image linkage is
    gone): the type supplies THEOZOLITH_REPO/ADAPTER/RUN_IMAGE, then the
    Stack's own [env] overrides. The model is deliberately NOT here: it is
    baked into the run image (ADR-0045), and a model change rolls the fleet
    through the changed RUN_IMAGE tag instead of a changed env var. The kind
    is derived from the type's driver; the supervised command is resolved
    from the driver ref (builtin or a verified drivers/<name>, ADR-0042).
    Workspace and secret bindings merge Stack-over-type here (ADR-0047) —
    the per-placement half of the split, never image-identity-bearing."""
    context = f"stacks/{stack.name}.toml"
    workspace = stack.workspace or wt.workspace
    if wt.is_driver:
        if stack.attach:
            raise ConfigRepoError(
                f"{context}: 'attach' is only valid on container-kind Stacks — worker"
                f" type {wt.name!r} has a driver, so the Stack resolves to process"
                " kind (ADR-0019/ADR-0044)"
            )
        if not workspace:
            raise ConfigRepoError(
                f"{context}: worker type {wt.name!r} has a driver but no workspace —"
                " set 'workspace' (owner/name) on this Stack or a default in"
                f" worker-types/{wt.name}.toml (ADR-0047)"
            )
        command = _resolve_driver_command(wt, repo_dir)
        env = {
            "THEOZOLITH_REPO": workspace,
            "THEOZOLITH_ADAPTER": wt.adapter,
            "THEOZOLITH_RUN_IMAGE": wt.tag,
        }
        env.update(stack.env)
        return StackDef(
            name=stack.name,
            kind="process",
            node=stack.node,
            state=stack.state,
            worker_type=wt.name,
            env=env,
            secrets=_merge_secret_bindings(stack, wt, context),
            command=command,
        )
    # Driverless: an interactive Flight Deck container running the derived tag.
    env: dict[str, str] = {}
    if workspace:
        env["THEOZOLITH_REPO"] = workspace
    if wt.knowledge:
        # The deck's knowledge SELECTION (ADR-0048 amendment): flightdeck-start
        # symlinks ~/.claude into exactly this tree under the read-only mount
        # and fails loud when it is absent. Identity-bearing per type — the
        # [env] guard above keeps it un-overridable per Stack — and part of
        # the container spec, so changing the selected tree recreates the
        # deck while content edits redistribute live.
        env["THEOZOLITH_KNOWLEDGE_TREE"] = wt.knowledge_tree
    if wt.policy:
        # The deck's Agent Policy SELECTION (ADR-0055): the start script
        # links the managed drop-in dir to exactly this tree under the
        # read-only policy mount and fails loud when it is absent. Same
        # contract as knowledge: un-overridable per Stack, part of the
        # container spec — reselection recreates the deck once, a content
        # edit redistributes live and recreates nothing.
        env["THEOZOLITH_POLICY_TREE"] = wt.policy_tree
    if wt.cli:
        # The deck's CLI Pin selection (ADR-0055): the in-container shim and
        # flightdeck-start resolve the daemon-maintained pin records by this
        # name. CONDITIONAL injection is the lifecycle mechanism: adopting or
        # dropping the field is an env delta -> container-fingerprint change
        # -> one deck recreate, while a version bump changes no container
        # spec and never recreates (the new export lands on the next launch).
        env["THEOZOLITH_WORKER_TYPE"] = wt.name
    env.update(stack.env)
    return StackDef(
        name=stack.name,
        kind="container",
        node=stack.node,
        state=stack.state,
        worker_type=wt.name,
        env=env,
        secrets=_merge_secret_bindings(stack, wt, context),
        command=wt.command,
        image=wt.tag,
        volumes=_resolve_volumes(wt.volumes, stack.name),
        attach=stack.attach,
    )


def _resolve_stacks(
    stacks: tuple[StackDef, ...],
    worker_types: dict[str, WorkerTypeDef],
    repo_dir: Path | None,
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
        resolved.append(_resolve_worker_stack(stack, wt, repo_dir))
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
    # Folder mode: hash ALL regular files, not just *.toml — a drivers/*.py edit
    # in a non-git configs folder must bump the commit so nodes see the change
    # (ADR-0042). The exclusion predicate is shared with the drivers manifest
    # (one source of truth for what counts as content), and so is the
    # normalized executable state (a chmod-only change re-distributes, so the
    # commit must move with it). Existing folder-mode commits jump once when
    # either rule lands: a harmless single ripple.
    digest = hashlib.sha256()
    for path in configdist.regular_files(repo_dir):
        digest.update(path.relative_to(repo_dir).as_posix().encode())
        digest.update(configdist.entry_mode(path.stat().st_mode).encode())
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
            " (driver/adapter/model/effort/workspace/secrets) (ADR-0044)"
        )
    pins = load_pins(repo_dir)
    worker_types = {
        path.stem: _parse_worker_type(path.stem, _load_toml(path), pins)
        for path in sorted((repo_dir / "worker-types").glob("*.toml"))
    }
    warnings = tuple(
        warning for wt in worker_types.values() for warning in _worker_type_warnings(wt)
    )
    # Config Repo validity is independent of Stack placement (ADR-0042): every
    # driver-bearing worker type resolves its command here, so a dangling
    # drivers/<name> reference fails load_config() even when no Stack
    # instantiates the type — dormant definitions break at configure time,
    # never later when a Stack first activates them. A dangling knowledge
    # reference fails the same way (ADR-0048): the pin joined above proves a
    # tree was ingested, this proves it is still present to distribute.
    # Every Agent Policy tree in the pinned build is validated against the
    # one safe-key allowlist at LOAD as well as at ingest (ADR-0055): the two
    # sites run the same theozolith_worker.policy validator, so a tree that
    # reached the pinned build outside ingest (a hand edit, a restore) can
    # never deliver an unadmitted key to a deck or an image.
    policy_root = repo_dir / configdist.POLICY_DIR
    if policy_root.is_dir() and not policy_root.is_symlink():
        for entry in sorted(policy_root.iterdir(), key=lambda p: p.name):
            if configdist.excluded_part(entry.name):
                continue
            try:
                agentpolicy.validate_policy_tree(
                    entry, label=f"{configdist.POLICY_DIR}/{entry.name}"
                )
            except agentpolicy.PolicyError as exc:
                raise ConfigRepoError(str(exc)) from exc
    for wt in worker_types.values():
        if wt.is_driver:
            _resolve_driver_command(wt, repo_dir)
        if wt.policy and not (repo_dir / wt.policy).is_dir():
            # A dangling policy reference fails load exactly as a dangling
            # knowledge reference does (ADR-0055): the pin joined at parse
            # proves a tree was ingested, this proves it is still present to
            # distribute.
            raise ConfigRepoError(
                f"worker-types/{wt.name}.toml: policy reference {wt.policy!r}"
                " has no tree in the pinned build — run `theozolith config"
                " ingest` (ADR-0055)"
            )
        if wt.knowledge:
            compiled = repo_dir / wt.knowledge / wt.adapter
            # A pre-ADR-0052 pinned build keeps the claude compile directly
            # under knowledge/<name>/ — tolerated until the next ingest
            # migrates the layout (only claude compiles could exist there).
            legacy_ok = wt.adapter == "claude" and (repo_dir / wt.knowledge).is_dir()
            if not compiled.is_dir() and not legacy_ok:
                raise ConfigRepoError(
                    f"worker-types/{wt.name}.toml: knowledge reference"
                    f" {wt.knowledge!r} has no compiled {wt.adapter} tree in"
                    " the pinned build — run `theozolith config ingest`"
                    " (ADR-0048/ADR-0052)"
                )
    stacks = _resolve_stacks(
        tuple(
            _parse_stack(path.stem, _load_toml(path))
            for path in sorted((repo_dir / "stacks").glob("*.toml"))
        ),
        worker_types,
        repo_dir,
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
    # Normalize config-distribution validation/read failures (a symlinked or
    # non-directory drivers root, an unreadable drivers/ file) into the
    # established ConfigRepoError path (ADR-0042), so the API's config boundary
    # turns them into the documented HTTP error and dispatch stays fail-open on
    # a broken repo. Only ConfigDistError is caught — a programming error must
    # not be swallowed by an overbroad except.
    try:
        drivers_hash = configdist.dist_hash(repo_dir)
    except configdist.ConfigDistError as exc:
        raise ConfigRepoError(f"config distribution: {exc}") from exc
    return DeployConfig(
        commit=_commit(repo_dir),
        stacks=stacks,
        worker_types=worker_types,
        product_version=product_version,
        drivers_hash=drivers_hash,
        repo_dir=repo_dir,
        warnings=warnings,
    )
