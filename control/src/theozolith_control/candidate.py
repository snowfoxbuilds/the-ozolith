"""Candidate Bundles: export, verification, and the verified standalone build
(ADR-0054; docs/specs/BENCH-CONTRACT.md).

A Candidate Bundle is the self-contained export of one worker-type definition
for benchmarking: ``candidate.json`` (the resolved manifest), the compiled
knowledge tree for the candidate's adapter tool, and a Dockerfile emitted by
the Node Daemon's own codegen — building the same derived image a deployment
would run. Export consumes an already-materialized LOCAL config-repo-shaped
directory and reuses the ingest resolution machinery (per-tool knowledge
compile, base tag->digest, pins, instruction hash) with no Pinned Build
involvement; benchmark<->deployment equivalence is identity-hash equality,
never shared provenance.

Verification authenticates the COMPLETE executable build context from bundle
bytes: every byte capable of affecting the built image is either recomputed
from verified inputs or refused, and every failure precedes any Docker
invocation. The supported build is the wrapper below: stage a private
snapshot, fully verify it, ``docker build`` that same snapshot, and clean up
on every exit path — closing the verify-then-mutate race. Raw ``docker
build`` on the bundle directory stays mechanically possible but is not
identity-verified and never produces trusted benchmark evidence.

The Dockerfile codegen and knowledge staging are the Node Daemon's own
(``theozolith_nodedaemon.builds``), imported — never reimplemented — so
contract drift between bench builds and fleet builds is structurally
impossible (issue #88: share, don't duplicate).

Credentials ride the ADR-0049 Docker-compatible model: a private base digest
resolves through a caller-supplied DOCKER_CONFIG directory (static ``auths``
or a credential helper — the Fernet store is absent off the Control Node);
public bases keep the anonymous fast path. The credential reaches no bundle
byte, log, argv, or error message — failures name the registry host and the
remediation only.

Version keys (BENCH-CONTRACT.md): ``BUNDLE_FORMAT_VERSION`` owns the
``candidate.json`` schema, the allowed build-context entries, structure
verification, and verified-build behavior; ``IDENTITY_SPEC_VERSION`` owns
the canonical identity serialization and the identity-triple computation
(the production formula in ``configrepo.WorkerTypeDef.instruction_hash``,
golden vectors in docs/specs/bench-identity-vectors.json). Every breaking
change bumps its owning key and lands a Changelog entry in the spec —
visibility, not immutability.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from theozolith_nodedaemon import builds
from theozolith_nodedaemon import configdist as node_configdist
from theozolith_nodedaemon.dockerctl import DockerCtl, DockerError

from theozolith_control import __version__, configrepo, ingest

BUNDLE_FORMAT_VERSION = 1
IDENTITY_SPEC_VERSION = 1

MANIFEST_NAME = "candidate.json"
DOCKERFILE_NAME = "Dockerfile"
# The bundle IS a docker build context, so the knowledge tree lives under the
# exact directory name the daemon stages (builds._CONTEXT_KNOWLEDGE); the
# layout contract test pins the two names together.
KNOWLEDGE_SUBDIR = "knowledge"

# Worker-type names ride into the deterministic tag and the bundle manifest;
# v1 pins them to the same conservative class as knowledge tree names.
_WORKER_TYPE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_EXPORTED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

# The exact candidate.json v1 key set (BENCH-CONTRACT.md): identity-bearing
# fields plus non-identity metadata. Missing, unknown, or malformed fields
# are refused per the stamped bundle_format_version.
_MANIFEST_KEYS = (
    "adapter",
    "base",
    "base_digest",
    "bundle_format_version",
    "driver",
    "effort",
    "exported_at",
    "identity_spec_version",
    "instruction_hash",
    "knowledge",
    "knowledge_pin",
    "knowledge_target",
    "model",
    "product_version",
    "secret_slots",
    "setup",
    "worker_type",
)


class CandidateError(Exception):
    """A candidate export, verification, or verified build failed."""


@dataclass(frozen=True)
class CandidateSummary:
    """The recomputed candidate identity triple (base digest, instruction
    hash, adapter name) plus the deterministic tag it builds under."""

    worker_type: str
    adapter: str
    base_digest: str
    instruction_hash: str
    tag: str


# -- export --------------------------------------------------------------------


def export_candidate(
    source: str | Path,
    worker_type: str,
    out: str | Path,
    *,
    docker_config: Path | None = None,
    resolve_digest: Callable[[str], str] | None = None,
    now: Callable[[], str] | None = None,
) -> CandidateSummary:
    """Export one worker-type definition from a local config-repo-shaped
    directory (``worker-types/`` + ``knowledge/``) as a Candidate Bundle.

    v1 accepts nothing but a local directory: URLs and non-directory sources
    are rejected — callers clone remote repositories themselves, preferably
    at an immutable commit. Runs the ingest resolution machinery (per-tool
    knowledge compile, base tag->digest via ``resolve_digest`` or the
    DOCKER_CONFIG-credentialed registry resolver, pin computation, the full
    worker-type parse with its capability gates) and writes the bundle; no
    Pinned Build is read or written. Secret slot NAMES travel in the
    manifest; stored secret names and values never do."""
    source_dir = _source_dir(source)
    if not _WORKER_TYPE_NAME.fullmatch(worker_type):
        raise CandidateError(
            f"worker type name {worker_type!r} must match"
            " ^[A-Za-z0-9][A-Za-z0-9._-]*$ (it names the bundle's deterministic tag)"
        )
    type_path = source_dir / "worker-types" / f"{worker_type}.toml"
    if type_path.is_symlink() or not type_path.is_file():
        raise CandidateError(
            f"source has no worker-types/{worker_type}.toml (a regular file; symlinks are refused)"
        )
    try:
        data = tomllib.loads(type_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CandidateError(f"worker-types/{worker_type}.toml does not parse: {exc}") from exc

    out_dir = _prepare_out(out)
    with tempfile.TemporaryDirectory(prefix="theozolith-candidate-export-") as staging_text:
        staging = Path(staging_text)
        try:
            knowledge_pins = ingest._compile_knowledge(source_dir, staging)
        except ingest.IngestError as exc:
            raise CandidateError(str(exc)) from exc
        pins = configrepo.Pins(
            base=_resolve_base(data, docker_config, resolve_digest),
            knowledge=knowledge_pins,
        )
        try:
            wt = configrepo._parse_worker_type(worker_type, data, pins)
        except configrepo.ConfigRepoError as exc:
            raise CandidateError(str(exc)) from exc

        recipe = wt.recipe_wire()
        exported_at = now() if now else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Assemble in staging, publish to out only on full success — a failed
        # export never leaves a partial bundle behind.
        bundle = staging / "bundle"
        bundle.mkdir()
        reason = builds.stage_knowledge(recipe, staging, bundle)
        if reason:
            raise CandidateError(f"cannot stage the compiled knowledge tree: {reason}")
        (bundle / DOCKERFILE_NAME).write_text(
            builds.dockerfile_for(recipe, exported_at), encoding="utf-8"
        )
        manifest = {
            "bundle_format_version": BUNDLE_FORMAT_VERSION,
            "identity_spec_version": IDENTITY_SPEC_VERSION,
            "worker_type": wt.name,
            "adapter": wt.adapter,
            "base": wt.base,
            "base_digest": wt.base_digest,
            "setup": list(wt.setup),
            "model": wt.model,
            "effort": wt.effort,
            # The BAKED knowledge view (what the image actually carries):
            # a driverless type's reference selects a mount, not a bake,
            # and exports as empty exactly as it rides the wire recipe.
            "knowledge": wt.baked_knowledge,
            "knowledge_pin": wt.baked_knowledge_pin,
            "knowledge_target": wt.knowledge_target,
            "instruction_hash": wt.instruction_hash,
            "driver": wt.driver,
            # Slot names only — a consumer learns what to bind; the
            # deployment's stored secret names and values never travel.
            "secret_slots": sorted(wt.secrets),
            "product_version": __version__,
            "exported_at": exported_at,
        }
        (bundle / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        shutil.copytree(bundle, out_dir, dirs_exist_ok=True)
    return CandidateSummary(
        worker_type=wt.name,
        adapter=wt.adapter,
        base_digest=wt.base_digest,
        instruction_hash=wt.instruction_hash,
        tag=wt.tag,
    )


def _source_dir(source: str | Path) -> Path:
    text = str(source)
    if "://" in text or text.startswith("git@"):
        raise CandidateError(
            f"source {text!r} looks like a URL — candidate export v1 accepts only a"
            " local config-repo-shaped directory (ADR-0054): clone the repository"
            " yourself, preferably at an immutable commit, and pass the checkout path"
        )
    path = Path(source)
    if not path.exists():
        raise CandidateError(f"source directory {text!r} does not exist")
    if not path.is_dir():
        raise CandidateError(
            f"source {text!r} is not a directory — candidate export v1 accepts only a"
            " local config-repo-shaped directory (worker-types/ + knowledge/)"
        )
    return path


def _prepare_out(out: str | Path) -> Path:
    out_dir = Path(out)
    if out_dir.is_symlink():
        raise CandidateError(f"output {out_dir} is a symlink — refused")
    if out_dir.exists():
        if not out_dir.is_dir() or any(out_dir.iterdir()):
            raise CandidateError(f"output {out_dir} must be a new or empty directory")
    else:
        out_dir.mkdir(parents=True)
    return out_dir


def _resolve_base(
    data: dict,
    docker_config: Path | None,
    resolve_digest: Callable[[str], str] | None,
) -> dict[str, str]:
    """tag->digest pins for the candidate's base, mirroring ingest's
    ``_resolve_bases`` for the one ref this export needs. A base already
    pinned by digest resolves nothing (and needs no credential); anything
    malformed is left for the worker-type parse to refuse with its own
    actionable error."""
    base = data.get("base", "")
    if not isinstance(base, str) or not base or "@sha256:" in base:
        return {}
    resolve = resolve_digest
    if resolve is None:
        host = configrepo.registry_host(base)
        credential = _registry_credential(docker_config, host)
        resolve = functools.partial(
            ingest.resolve_image_digest,
            credentials={host: credential} if credential else {},
            hint=_docker_config_hint,
        )
    try:
        digest = resolve(base)
    except ingest.IngestError as exc:
        raise CandidateError(str(exc)) from exc
    except Exception as exc:
        raise CandidateError(f"cannot resolve base tag {base!r}: {exc}") from exc
    if not isinstance(digest, str) or not (
        digest.startswith("sha256:") and _HEX64.fullmatch(digest[len("sha256:") :])
    ):
        raise CandidateError(
            f"resolver returned {digest!r} for base tag {base!r} — expected 'sha256:<64 hex>'"
        )
    return {base: digest}


# -- credentials (ADR-0049 Docker-compatible model, off the Control Node) ------


def discover_docker_config(explicit: str | Path | None = None) -> Path | None:
    """The caller-supplied DOCKER_CONFIG directory: an explicit path wins,
    else the DOCKER_CONFIG environment variable, else Docker's own
    ``~/.docker`` when it exists — the Docker-compatible discovery order, so
    existing ``docker login`` state just works. ``None`` = anonymous."""
    if explicit:
        path = Path(explicit)
        if not path.is_dir():
            raise CandidateError(f"docker config {path} is not a directory")
        return path
    env = os.environ.get("DOCKER_CONFIG", "")
    if env:
        return Path(env)
    default = Path.home() / ".docker"
    return default if default.is_dir() else None


def _auth_keys_for(host: str) -> tuple[str, ...]:
    # Docker Hub credentials are conventionally stored under the legacy v1
    # index URL, not the registry host the token flow challenges with — the
    # same twin the Node Daemon writes (ADR-0049 PR2).
    if host == "registry-1.docker.io":
        return (host, "https://index.docker.io/v1/", "index.docker.io")
    return (host,)


def _registry_credential(docker_config: Path | None, host: str) -> str:
    """The ``<user>:<token>`` pull credential for ``host`` from a
    Docker-compatible config dir: static ``auths`` first, then a configured
    credential helper (``credHelpers`` beats ``credsStore``). ``""`` means
    anonymous — the absence of a credential is never an error here; a private
    base simply fails resolution later with the host and remediation named.
    No error message ever carries usernames, tokens, config contents, or
    helper output."""
    if docker_config is None:
        return ""
    path = docker_config / "config.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CandidateError(f"docker config {path} is not readable JSON") from exc
    if not isinstance(data, dict):
        raise CandidateError(f"docker config {path} is not a JSON object")
    auths = data.get("auths", {})
    if isinstance(auths, dict):
        for key in _auth_keys_for(host):
            entry = auths.get(key)
            if isinstance(entry, dict):
                credential = _static_auth(entry, path, key)
                if credential:
                    return credential
    helper = ""
    helpers = data.get("credHelpers", {})
    if isinstance(helpers, dict):
        for key in _auth_keys_for(host):
            value = helpers.get(key)
            if isinstance(value, str) and value:
                helper = value
                break
    if not helper:
        store = data.get("credsStore", "")
        helper = store if isinstance(store, str) and store else ""
    if helper:
        for key in _auth_keys_for(host):
            credential = _helper_credential(helper, key, host)
            if credential:
                return credential
    return ""


def _static_auth(entry: dict, path: Path, key: str) -> str:
    auth = entry.get("auth", "")
    if isinstance(auth, str) and auth:
        try:
            decoded = base64.b64decode(auth.encode("ascii"), validate=True).decode("utf-8")
        except Exception as exc:
            raise CandidateError(
                f"docker config {path}: auths[{key!r}].auth does not decode to"
                " base64 '<user>:<token>'"
            ) from exc
        if ":" not in decoded:
            raise CandidateError(
                f"docker config {path}: auths[{key!r}].auth does not decode to"
                " base64 '<user>:<token>'"
            )
        return decoded
    username = entry.get("username", "")
    password = entry.get("password", "")
    if isinstance(username, str) and username and isinstance(password, str) and password:
        return f"{username}:{password}"
    return ""


def _helper_credential(helper: str, key: str, host: str) -> str:
    """One ``docker-credential-<helper> get`` invocation. A nonzero exit is
    the helper's not-found convention — anonymous, never an error; helper
    output is parsed for Username/Secret and NEVER quoted in any message."""
    argv = [f"docker-credential-{helper}", "get"]
    try:
        proc = subprocess.run(
            argv, input=key, capture_output=True, text=True, timeout=30, check=False
        )
    except FileNotFoundError as exc:
        raise CandidateError(
            f"docker credential helper docker-credential-{helper} is not installed"
            f" (configured for {host} in the supplied docker config)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CandidateError(
            f"docker credential helper docker-credential-{helper} timed out for {host}"
        ) from exc
    if proc.returncode != 0:
        return ""
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise CandidateError(
            f"docker credential helper docker-credential-{helper} returned"
            f" malformed output for {host}"
        ) from exc
    if not isinstance(payload, dict):
        return ""
    username = payload.get("Username", "")
    secret = payload.get("Secret", "")
    if isinstance(username, str) and username and isinstance(secret, str) and secret:
        return f"{username}:{secret}"
    return ""


def _docker_config_hint(ref: str, registry: str, code: int, credential: str) -> str:
    """The terminal 401/403 message for export-side base resolution: names
    the host and the DOCKER_CONFIG remediation (the Fernet-store command the
    ingest hint names does not exist off the Control Node)."""
    base = f"cannot resolve base tag {ref!r}: HTTP {code}"
    if code not in (401, 403):
        return base
    if credential:
        return (
            f"{base} — the {registry} pull credential from the supplied docker"
            " config was refused; check the value and its pull scope"
        )
    return (
        f"{base} — the image may be private; supply a DOCKER_CONFIG directory whose"
        f" config.json or credential helper carries a pull credential for {registry}"
        f" (e.g. `docker login {registry}`), or pin the base by digest in the source"
    )


# -- verification --------------------------------------------------------------


def verify_bundle(bundle: str | Path) -> CandidateSummary:
    """Authenticate the complete executable build context from bundle bytes
    (BENCH-CONTRACT.md, in order): (1) strict ``candidate.json`` parse, with
    unsupported bundle/identity versions refused outright; (2) recompute the
    compiled knowledge tree's pin (an empty knowledge ref means no knowledge
    tree may be present); (3) recompute the materialized setup, instruction
    hash, base-digest consistency, adapter identity, and the identity triple
    — through the production worker-type parse, so every capability gate a
    deployment applies fires here too; (4) reconstruct the production wire
    recipe; (5) regenerate the Dockerfile through the shared production
    codegen and require an exact byte match; (6) validate the layout against
    the v1 allowlist — unexpected entries, symlinks, path traversal, and
    special files refused. Every failure precedes any Docker invocation."""
    bundle_dir = Path(bundle)
    if not bundle_dir.is_dir():
        raise CandidateError(f"bundle {bundle_dir} is not a directory")
    manifest = _load_manifest(bundle_dir)
    _verify_knowledge_tree(bundle_dir, manifest)
    wt = _reconstruct_worker_type(manifest)
    recipe = wt.recipe_wire()
    _verify_dockerfile(bundle_dir, recipe, manifest)
    _verify_layout(bundle_dir, has_knowledge=bool(manifest["knowledge"]))
    return CandidateSummary(
        worker_type=wt.name,
        adapter=wt.adapter,
        base_digest=wt.base_digest,
        instruction_hash=wt.instruction_hash,
        tag=wt.tag,
    )


def _refuse_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise CandidateError(f"{MANIFEST_NAME} carries duplicate field {key!r}")
        result[key] = value
    return result


def _load_manifest(bundle: Path) -> dict:
    path = bundle / MANIFEST_NAME
    if not path.is_file():
        raise CandidateError(f"{MANIFEST_NAME} is missing from {bundle}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_refuse_duplicate_keys)
    except (OSError, ValueError) as exc:
        raise CandidateError(f"{MANIFEST_NAME} does not parse as JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CandidateError(f"{MANIFEST_NAME} must be a JSON object")
    # Unsupported versions are refused OUTRIGHT — before strict key checks,
    # so a future format never surfaces as a pile of "unknown field" noise.
    for key, supported in (
        ("bundle_format_version", BUNDLE_FORMAT_VERSION),
        ("identity_spec_version", IDENTITY_SPEC_VERSION),
    ):
        value = raw.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise CandidateError(f"{MANIFEST_NAME}: {key} must be an integer")
        if value != supported:
            raise CandidateError(
                f"{MANIFEST_NAME}: {key} {value} is unsupported — this build"
                f" understands {key} {supported} only (re-export the candidate,"
                " or run a matching product version)"
            )
    unknown = sorted(set(raw) - set(_MANIFEST_KEYS))
    if unknown:
        raise CandidateError(
            f"{MANIFEST_NAME} carries unknown fields: {', '.join(unknown)}"
            f" (bundle_format_version {BUNDLE_FORMAT_VERSION} refuses fields it"
            " cannot verify)"
        )
    missing = sorted(set(_MANIFEST_KEYS) - set(raw))
    if missing:
        raise CandidateError(f"{MANIFEST_NAME} is missing fields: {', '.join(missing)}")
    _check_manifest_shapes(raw)
    return raw


def _require_str_field(raw: dict, name: str) -> str:
    value = raw[name]
    if not isinstance(value, str):
        raise CandidateError(f"{MANIFEST_NAME}: {name} must be a string")
    return value


def _check_manifest_shapes(raw: dict) -> None:
    for name in (
        "adapter",
        "base",
        "base_digest",
        "driver",
        "effort",
        "exported_at",
        "instruction_hash",
        "knowledge",
        "knowledge_pin",
        "knowledge_target",
        "model",
        "product_version",
        "worker_type",
    ):
        _require_str_field(raw, name)
    if not _WORKER_TYPE_NAME.fullmatch(raw["worker_type"]):
        raise CandidateError(
            f"{MANIFEST_NAME}: worker_type must match ^[A-Za-z0-9][A-Za-z0-9._-]*$"
        )
    if not raw["adapter"]:
        raise CandidateError(f"{MANIFEST_NAME}: adapter must be non-empty")
    if not raw["product_version"]:
        raise CandidateError(f"{MANIFEST_NAME}: product_version must be non-empty")
    marker = "@sha256:"
    base = raw["base"]
    if marker not in base or not _HEX64.fullmatch(base.rsplit(marker, 1)[1]):
        raise CandidateError(
            f"{MANIFEST_NAME}: base must be a digest-pinned image ref (…@sha256:<64 hex>)"
        )
    digest = raw["base_digest"]
    if not digest.startswith("sha256:") or not _HEX64.fullmatch(digest[len("sha256:") :]):
        raise CandidateError(f"{MANIFEST_NAME}: base_digest must be 'sha256:<64 hex>'")
    if not _HEX64.fullmatch(raw["instruction_hash"]):
        raise CandidateError(f"{MANIFEST_NAME}: instruction_hash must be 64 hex chars")
    if raw["knowledge_pin"] and not _HEX64.fullmatch(raw["knowledge_pin"]):
        raise CandidateError(f"{MANIFEST_NAME}: knowledge_pin must be '' or 64 hex chars")
    if not _EXPORTED_AT.fullmatch(raw["exported_at"]):
        raise CandidateError(
            f"{MANIFEST_NAME}: exported_at must be 'YYYY-MM-DDTHH:MM:SSZ'"
            " (it is a Dockerfile label byte)"
        )
    setup = raw["setup"]
    if not isinstance(setup, list) or not all(isinstance(item, str) for item in setup):
        raise CandidateError(f"{MANIFEST_NAME}: setup must be a list of strings")
    slots = raw["secret_slots"]
    if (
        not isinstance(slots, list)
        or not all(isinstance(item, str) and item for item in slots)
        or slots != sorted(set(slots))
    ):
        raise CandidateError(
            f"{MANIFEST_NAME}: secret_slots must be a sorted list of unique non-empty slot names"
        )


def _verify_knowledge_tree(bundle: Path, manifest: dict) -> None:
    """Step 2: recompute the compiled tree's pin with the published tree-hash
    function — the daemon's own fail-closed walk, so a symlink or special
    file inside the tree refuses here exactly as it would refuse a bake."""
    ref = manifest["knowledge"]
    tree = bundle / KNOWLEDGE_SUBDIR
    if not ref:
        for dependent in ("knowledge_pin", "knowledge_target"):
            if manifest[dependent]:
                raise CandidateError(
                    f"{MANIFEST_NAME}: {dependent} is set but the knowledge ref is empty"
                )
        if tree.is_symlink() or tree.exists():
            raise CandidateError(
                "bundle carries a knowledge/ entry but the manifest declares no"
                " knowledge — an empty knowledge ref means no knowledge tree may"
                " be present"
            )
        return
    if not manifest["driver"]:
        raise CandidateError(
            f"{MANIFEST_NAME}: a driverless candidate bakes no knowledge — the"
            " manifest carries the BAKED knowledge view, which is empty for a"
            " Flight Deck type"
        )
    if not manifest["knowledge_pin"]:
        raise CandidateError(f"{MANIFEST_NAME}: a knowledge ref requires its compiled-tree pin")
    if tree.is_symlink() or not tree.is_dir():
        raise CandidateError("bundle knowledge/ must be a directory (symlinks refused)")
    try:
        recomputed = node_configdist.tree_hash(tree)
    except node_configdist.ConfigDistError as exc:
        raise CandidateError(f"bundle knowledge tree failed verification: {exc}") from exc
    if not recomputed or recomputed != manifest["knowledge_pin"]:
        raise CandidateError(
            f"bundle knowledge tree hashes {recomputed[:12] or '(empty)'} but the"
            f" manifest pins {manifest['knowledge_pin'][:12]} — compiled bytes and"
            " recorded pin disagree"
        )


def _reconstruct_worker_type(manifest: dict) -> configrepo.WorkerTypeDef:
    """Step 3: rebuild the worker-type definition through the PRODUCTION
    parse (capability gates, reserved-slot guards, adapter identity and all)
    and require the recomputed identity fields to match the recorded ones —
    a recorded identity is a convenience the verifier checks, never trusted
    (ADR-0054)."""
    knowledge = manifest["knowledge"]
    pins = configrepo.Pins(
        knowledge=(
            {
                f"{knowledge[len(configrepo.KNOWLEDGE_REF_PREFIX) :]}/{manifest['adapter']}": (
                    manifest["knowledge_pin"]
                )
            }
            if knowledge
            else {}
        ),
    )
    data = {
        "base": manifest["base"],
        "setup": list(manifest["setup"]),
        "knowledge": knowledge,
        "driver": manifest["driver"],
        "adapter": manifest["adapter"],
        "model": manifest["model"],
        "effort": manifest["effort"],
        "secrets": {slot: "" for slot in manifest["secret_slots"]},
    }
    try:
        wt = configrepo._parse_worker_type(manifest["worker_type"], data, pins)
    except configrepo.ConfigRepoError as exc:
        raise CandidateError(
            f"{MANIFEST_NAME} does not reconstruct a valid worker-type definition: {exc}"
        ) from exc
    for name, recomputed in (
        ("base_digest", wt.base_digest),
        ("knowledge_target", wt.knowledge_target),
        ("instruction_hash", wt.instruction_hash),
    ):
        if manifest[name] != recomputed:
            raise CandidateError(
                f"{MANIFEST_NAME}: recorded {name} {manifest[name]!r} does not match"
                f" the recomputed {recomputed!r} — identity is recomputed from bundle"
                " bytes, never trusted (ADR-0054)"
            )
    return wt


def _verify_dockerfile(bundle: Path, recipe: dict, manifest: dict) -> None:
    """Step 5: regenerate through the shared production codegen (the Node
    Daemon's own renderer) and require an exact byte match — a smuggled RUN,
    a changed FROM or COPY target, an edited label or USER line all refuse
    here, before Docker exists."""
    path = bundle / DOCKERFILE_NAME
    if not path.is_file():
        raise CandidateError(f"bundle {DOCKERFILE_NAME} is missing")
    expected = builds.dockerfile_for(recipe, manifest["exported_at"]).encode("utf-8")
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise CandidateError(f"cannot read bundle {DOCKERFILE_NAME}: {exc}") from exc
    if actual != expected:
        raise CandidateError(
            f"bundle {DOCKERFILE_NAME} does not byte-match the production codegen"
            " for this manifest — any edit (FROM, RUN, COPY, LABEL, USER,"
            " whitespace) is refused (ADR-0054)"
        )


def _verify_layout(bundle: Path, *, has_knowledge: bool) -> None:
    """Step 6: the v1 allowlist — exactly ``candidate.json``, ``Dockerfile``,
    and (with knowledge) ``knowledge/``; every entry a regular file or
    directory. Anything that could alter or escape the build context —
    unexpected entries, symlinks, special files — refuses."""
    allowed = {MANIFEST_NAME, DOCKERFILE_NAME} | ({KNOWLEDGE_SUBDIR} if has_knowledge else set())
    unexpected = sorted(set(os.listdir(bundle)) - allowed)
    if unexpected:
        raise CandidateError(
            f"bundle carries unexpected entries: {', '.join(unexpected)} —"
            f" bundle_format_version {BUNDLE_FORMAT_VERSION} allows exactly"
            f" {MANIFEST_NAME}, {DOCKERFILE_NAME}, and (with knowledge)"
            f" {KNOWLEDGE_SUBDIR}/"
        )
    for name in (MANIFEST_NAME, DOCKERFILE_NAME):
        if not stat.S_ISREG(os.lstat(bundle / name).st_mode):
            raise CandidateError(
                f"bundle {name} must be a regular file — symlinks and special files are refused"
            )
    if has_knowledge:
        root = bundle / KNOWLEDGE_SUBDIR
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise CandidateError(f"bundle {KNOWLEDGE_SUBDIR}/ must be a directory")
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in (*dirnames, *filenames):
                entry = Path(dirpath) / name
                if not stat.S_ISREG(os.lstat(entry).st_mode) and not stat.S_ISDIR(
                    os.lstat(entry).st_mode
                ):
                    raise CandidateError(
                        f"bundle entry {entry.relative_to(bundle)} is not a regular"
                        " file or directory — symlinks, devices, and other special"
                        " files are refused"
                    )


# -- verified standalone build -------------------------------------------------


def build_candidate(
    bundle: str | Path,
    *,
    docker: DockerCtl | None = None,
    docker_config: Path | None = None,
    no_cache: bool = False,
) -> CandidateSummary:
    """The supported standalone build (ADR-0054): copy the bundle into a
    PRIVATE staged snapshot (refusing symlinks and special files at copy
    time — a followed symlink would smuggle outside-bundle bytes into the
    verified context), run the full verification on that snapshot, ``docker
    build`` that same snapshot under the deterministic tag, and clean the
    snapshot up on success, failure, and interruption alike. Nothing can
    change between what was verified and what is built. ``docker_config``
    rides to the build for a private base pull (ADR-0049); the credential
    never appears in argv."""
    bundle_dir = Path(bundle)
    if not bundle_dir.is_dir():
        raise CandidateError(f"bundle {bundle_dir} is not a directory")
    staging = Path(tempfile.mkdtemp(prefix="theozolith-candidate-build-"))
    try:
        snapshot = staging / "snapshot"
        _snapshot_bundle(bundle_dir, snapshot)
        verified = verify_bundle(snapshot)
        ctl = docker or DockerCtl()
        try:
            ctl.build(snapshot, verified.tag, no_cache=no_cache, docker_config=docker_config)
        except DockerError as exc:
            raise CandidateError(f"docker build of {verified.tag} failed: {exc}") from exc
        return verified
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _snapshot_bundle(source: Path, dest: Path) -> None:
    dest.mkdir()
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        rel = Path(dirpath).relative_to(source)
        for name in dirnames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                raise CandidateError(
                    f"bundle entry {rel / name} is a symlink — refused before staging"
                )
            (dest / rel / name).mkdir()
        for name in filenames:
            entry = Path(dirpath) / name
            if not stat.S_ISREG(os.lstat(entry).st_mode):
                raise CandidateError(
                    f"bundle entry {rel / name} is not a regular file — refused before staging"
                )
            # copy2 preserves the exec bit the knowledge pin covers.
            shutil.copy2(entry, dest / rel / name)
