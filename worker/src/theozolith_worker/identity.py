"""Baked-identity enforcement for the Claude adapter (ADR-0045, fail closed).

ADR-0045 bakes a worker type's model/effort into the derived image as
ENFORCEMENT. This module is the machinery that makes the enforcement
*provable* instead of assumed, across two gates:

- The **build gate** (``scan_managed_conflicts`` with no expected identity,
  called from ``ClaudeAdapter.materialize``): the materialized identity keys
  only bind if nothing else in the image's managed tier can supersede them.
  Claude Code merges ``/etc/claude-code/managed-settings.json`` with every
  ``managed-settings.d/*.json`` drop-in (alphabetical, scalars override,
  arrays concatenate, objects deep-merge), and a managed ``policyHelper``
  preempts the whole managed tier. Any identity-affecting key in any of
  those sources — or a malformed source — fails the build with the file and
  key named, never silently overwritten.

- The **runtime gate** (``run_preflight`` + ``ClaudeSessionGuard``, driven by
  the harness): server-managed organization settings outrank the baked file
  inside the managed tier and Claude Code exposes no machine-readable dump of
  the effective post-merge policy, so the effective identity is proven
  *behaviorally*, with the Run's own credential — and the proof runs inside a
  real pre-verification boundary. Every verification session (the widen and
  same-family canaries and the identity probe) executes in a NEUTRAL scratch
  directory outside the job mount, with all built-in tools removed
  (``--tools ""``), permission prompts auto-denied, no user/project/local
  settings (``--setting-sources ""``; the managed tier always applies), and
  no non-managed MCP servers — an unverified model cannot read the task,
  touch the checkout, load project CLAUDE.md/hooks/skills, or execute any
  tool. The applied effort is observed through the ``Stop`` hook payload
  (fires after a plain no-tool turn and reports the post-clamp value —
  verified live), so no tool ever runs for the effort proof. The real task
  prompt is withheld — the task file is not even present on disk — until the
  full preflight passes AND the gated task session's own announced and
  executed identity match; after release the guard keeps watching and an
  identity-affecting change (a turn on another model, a drifted applied
  effort, a ``ConfigChange`` hook firing on an identity-shaped settings
  change) kills the agent and invalidates the Run.

Anything unverifiable fails closed. Organization policy is never disabled,
replaced, weakened, or blocked to make a Run pass — a conflict is a failed
build or a failed preflight, with the source category named.

Everything here is deliberately value-redacting: errors and reports name
files, keys, models, efforts, and categories — never credential values,
tokens, or the contents of unrelated operator settings.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class IdentityError(RuntimeError):
    """A baked identity is inconsistent, unenforceable, or unverifiable."""


# The adapter-independent well-known identity files (ADR-0045).
WELL_KNOWN_MODEL_FILE = "etc/theozolith/model"
WELL_KNOWN_EFFORT_FILE = "etc/theozolith/effort"

# Claude Code's managed (admin) policy tier on Linux: the base file plus the
# drop-in directory, merged base-first then alphabetically (documented; the
# per-key managed ``env`` merge this module relies on shipped in 2.1.223).
MANAGED_SETTINGS_FILE = "etc/claude-code/managed-settings.json"
MANAGED_DROPIN_DIR = "etc/claude-code/managed-settings.d"

# Top-level managed keys that can change, widen, or supersede the baked
# identity. policyHelper/policyHelpers preempt the entire managed tier (their
# output becomes the only managed configuration), so they are never
# tolerable beside a baked identity. fallbackModel moves a session off the
# pin under provider pressure. modelOverrides remaps what actually SERVES a
# model ID (provider inference profiles/deployments) while the allowlist
# still sees the Anthropic ID — an identity change invisible to the model
# name. The rest select or constrain model/effort.
IDENTITY_SETTING_KEYS = (
    "availableModels",
    "effortLevel",
    "enforceAvailableModels",
    "fallbackModel",
    "model",
    "modelOverrides",
    "policyHelper",
    "policyHelpers",
)

# The managed key that guarantees fresh server-managed policy at startup:
# Claude Code documents it as "block startup until remote settings are
# freshly fetched; exit on fetch failure". materialize() writes it as
# literally true; any other value anywhere in the managed tier defeats the
# guarantee and is a conflict. (Live-verifiable half: startup works with the
# key and a dead settings endpoint also kills the model endpoint, so no turn
# ever executes — the refusal-on-fetch-failure half is documented behavior.)
FRESHNESS_KEY = "forceRemoteSettingsRefresh"

# Managed ``env`` entries (and forbidden process-environment variables) that
# select a model or an effort, or that repoint the CLI at a different
# provider/endpoint — behind a foreign base URL or provider switch, the
# stream's model names are whatever the endpoint claims, so the identity is
# unprovable and the only safe answer is to fail closed.
IDENTITY_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


def _is_identity_env_key(name: str) -> bool:
    """Exact identity env keys plus the ANTHROPIC_DEFAULT_*_MODEL overrides."""
    return name in IDENTITY_ENV_KEYS or (
        name.startswith("ANTHROPIC_DEFAULT_") and name.endswith("_MODEL")
    )


# Marker prefix for identity failures crossing the status.json channel: the
# driver classifies these as failure_class "identity", distinct from plain
# harness breakage.
IDENTITY_ERROR_PREFIX = "identity: "

# Preflight/gate failure categories — the "observed mismatch category" of the
# diagnostic contract. Stable strings: they land in evidence.
CATEGORY_POLICY_CONFLICT = "policy-conflict"
CATEGORY_INCONSISTENT = "identity-inconsistent"
CATEGORY_PAIR_INVALID = "pair-invalid"
CATEGORY_CLI_TOO_OLD = "cli-too-old"
CATEGORY_UNAVAILABLE = "unavailable"
CATEGORY_SUBSTITUTED = "substituted"
CATEGORY_WIDENED = "policy-widened"
CATEGORY_EFFORT_CLAMPED = "effort-clamped"
CATEGORY_UNVERIFIABLE = "unverifiable"
CATEGORY_TIMEOUT = "preflight-timeout"
# Post-gate categories (the release protocol and the released session).
CATEGORY_CONFIG_CHANGED = "config-changed"
CATEGORY_TASK_DELIVERY = "task-delivery"
CATEGORY_STDIN_CLOSE = "stdin-close"
CATEGORY_TASK_UNPROCESSED = "task-unprocessed"


@dataclass(frozen=True)
class BakedIdentity:
    """What the image was built to run: the well-known files' content."""

    model: str
    effort: str = ""  # "" = the model's own default; nothing to verify


# -- (model, effort) pair capability (ADR-0045 amendment C) -------------------

# Efforts every current-generation family supports through the settings
# layer, and the reduced set of the 4.6 generation (Claude Code documents
# that an unsupported level silently "falls back to the highest supported
# level at or below" — e.g. xhigh runs as high on Opus 4.6 — which is a
# silent identity change, so those pairs are rejected outright).
_EFFORTS_FULL = frozenset({"low", "medium", "high", "xhigh"})
_EFFORTS_NO_XHIGH = frozenset({"low", "medium", "high"})
_EFFORTS_NONE: frozenset[str] = frozenset()

# Longest-prefix capability table for pinned model IDs. A prefix matches the
# exact ID or the ID extended with "-..." (a dated variant), never a lexical
# accident (claude-opus-5 does not match claude-opus-55). Families that are
# positively known NOT to support the effort setting map to the empty set;
# anything absent is UNKNOWN and rejects any nonempty effort — a future
# model's effort capability must be positively known before it is baked.
_PINNED_EFFORT_CAPABILITY: tuple[tuple[str, frozenset[str]], ...] = (
    ("claude-fable-5", _EFFORTS_FULL),
    ("claude-mythos-5", _EFFORTS_FULL),
    ("claude-opus-5", _EFFORTS_FULL),
    ("claude-opus-4-8", _EFFORTS_FULL),
    ("claude-opus-4-7", _EFFORTS_FULL),
    ("claude-opus-4-6", _EFFORTS_NO_XHIGH),
    ("claude-sonnet-5", _EFFORTS_FULL),
    ("claude-sonnet-4-6", _EFFORTS_NO_XHIGH),
    # Positively known to have no effort setting at all.
    ("claude-sonnet-4-5", _EFFORTS_NONE),
    ("claude-sonnet-4-0", _EFFORTS_NONE),
    ("claude-sonnet-4", _EFFORTS_NONE),
    ("claude-haiku-4-5", _EFFORTS_NONE),
    ("claude-3", _EFFORTS_NONE),
)

# The family aliases: valid with an effort only when every model the alias
# can float to has stable, proven support for it. fable/opus/sonnet float to
# current-generation models (full set); haiku has no effort support.
_ALIAS_EFFORT_CAPABILITY: dict[str, frozenset[str]] = {
    "fable": _EFFORTS_FULL,
    "opus": _EFFORTS_FULL,
    "sonnet": _EFFORTS_FULL,
    "haiku": _EFFORTS_NONE,
}


def effort_capability(model: str) -> frozenset[str] | None:
    """The effort values ``model`` provably honors through the settings
    layer; the empty set when the model has none; None when unknown."""
    if model in _ALIAS_EFFORT_CAPABILITY:
        return _ALIAS_EFFORT_CAPABILITY[model]
    for prefix, capability in _PINNED_EFFORT_CAPABILITY:
        if model == prefix or model.startswith(prefix + "-"):
            return capability
    return None


def pair_error(model: str, effort: str) -> str:
    """Why ``(model, effort)`` is not an enforceable pair; "" when it is.

    effort "" is always valid — it means "the model's own default" and pins
    nothing. The same wording is used by control's config load, the in-image
    build CLI, and the runtime preflight (ADR-0045)."""
    if not effort:
        return ""
    if not model:
        return (
            f"effort {effort!r} without a model is not an enforceable pair —"
            " effort binds to the model it runs on (ADR-0045)"
        )
    capability = effort_capability(model)
    if capability is None:
        return (
            f"model {model!r} has no known effort capability — an unknown"
            f" model paired with effort {effort!r} cannot be proven"
            ' enforceable; bake the model alone (effort = "") or upgrade to'
            " a theozolith release that knows this model (ADR-0045)"
        )
    if not capability:
        return (
            f"model {model!r} does not support the effort setting — Claude"
            f" Code would silently ignore effort {effort!r} (ADR-0045)"
        )
    if effort not in capability:
        known = ", ".join(sorted(capability))
        return (
            f"model {model!r} does not support effort {effort!r} — Claude"
            " Code silently runs the highest supported level at or below it"
            f" (supported: {known}), which is an unenforced identity"
            " (ADR-0045)"
        )
    return ""


# -- managed-tier policy scanning (build gate + runtime re-check) -------------


def _load_settings_document(path: Path) -> dict:
    """One managed-tier JSON document, or IdentityError naming the file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IdentityError(f"{path}: unreadable managed settings ({exc})") from exc
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IdentityError(
            f"{path}: not valid JSON ({exc}) — a malformed managed-tier"
            " document makes the effective policy unknowable"
        ) from exc
    if not isinstance(loaded, dict):
        raise IdentityError(
            f"{path}: managed settings must be a JSON object, not {type(loaded).__name__}"
        )
    return loaded


def managed_policy_documents(root: Path) -> list[tuple[Path, dict]]:
    """Every managed-tier document under ``root`` in Claude Code's merge
    order: the base file first, then ``managed-settings.d/*.json``
    alphabetically (dotfiles ignored, as the CLI ignores them). Raises
    IdentityError on a malformed document."""
    documents: list[tuple[Path, dict]] = []
    base = root / MANAGED_SETTINGS_FILE
    if base.is_file():
        documents.append((base, _load_settings_document(base)))
    dropins = root / MANAGED_DROPIN_DIR
    if dropins.is_dir():
        for path in sorted(dropins.glob("*.json")):
            if path.name.startswith("."):
                continue
            documents.append((path, _load_settings_document(path)))
    return documents


def _expected_base_values(expected: BakedIdentity) -> dict[str, object]:
    """Exactly what materialize() wrote for ``expected`` — the one tolerable
    shape of identity keys in the base managed file. The freshness key is
    part of the artifact: an image without it cannot guarantee the canaries,
    the probe, and the task all start on freshly fetched server policy."""
    values: dict[str, object] = {
        "model": expected.model,
        "availableModels": [expected.model],
        "enforceAvailableModels": True,
        FRESHNESS_KEY: True,
    }
    if expected.effort:
        values["effortLevel"] = expected.effort
    return values


def scan_managed_conflicts(root: Path, expected: BakedIdentity | None = None) -> list[str]:
    """Identity-affecting managed policy under ``root``, as conflict strings
    naming the file and key.

    ``expected=None`` is the build gate: ANY identity key anywhere is a
    conflict. With an expected identity (the runtime re-check) the base file
    may carry exactly the values ``materialize`` wrote for it — anything
    else, and any identity key in a drop-in, is still a conflict. The
    freshness key is special-cased in both modes: literally ``true`` is the
    one tolerable value anywhere (it is what materialize writes and what an
    operator setting it themselves means); anything else defeats
    fresh-policy startup. Raises IdentityError on a malformed document
    (unknowable policy)."""
    conflicts: list[str] = []
    base = root / MANAGED_SETTINGS_FILE
    for path, document in managed_policy_documents(root):
        own = path == base and expected is not None
        tolerable = _expected_base_values(expected) if own else {}
        for key in IDENTITY_SETTING_KEYS:
            if key not in document:
                continue
            if key in ("policyHelper", "policyHelpers"):
                conflicts.append(
                    f"{path}: '{key}' — a managed policy helper preempts every"
                    " other managed source, so the baked identity would not"
                    " bind"
                )
                continue
            if own and key in tolerable and document[key] == tolerable[key]:
                continue
            conflicts.append(f"{path}: '{key}' can change or widen the baked model/effort identity")
        if FRESHNESS_KEY in document and document[FRESHNESS_KEY] is not True:
            conflicts.append(
                f"{path}: '{FRESHNESS_KEY}' is not literally true — fresh"
                " server-managed policy at session startup is no longer"
                " guaranteed"
            )
        env = document.get("env")
        if env is not None and not isinstance(env, dict):
            conflicts.append(
                f"{path}: 'env' is {type(env).__name__}, not an object — the"
                " merged managed environment is unknowable"
            )
        elif isinstance(env, dict):
            for name in sorted(env):
                if not isinstance(name, str) or not _is_identity_env_key(name):
                    continue
                if (
                    own
                    and name == "CLAUDE_CODE_EFFORT_LEVEL"
                    and expected is not None
                    and expected.effort
                    and env[name] == expected.effort
                ):
                    continue
                conflicts.append(
                    f"{path}: env '{name}' selects a model, effort, or provider"
                    " endpoint over the baked identity"
                )
    return conflicts


def scan_process_environment(environ: Mapping[str, str]) -> list[str]:
    """Identity-affecting variables in the RUN CONTAINER's own process
    environment, as conflict strings naming the variable (never the value).

    The credential contract (ADR-0045) delivers only the API/OAuth secret —
    a model, effort, or provider-endpoint variable reaching the harness
    environment means some layer outside the image is steering the session,
    and everything the canaries and probe observe would be relative to that
    steering. Fail closed, name the variable."""
    return [
        f"process environment variable '{name}' selects a model, effort, or"
        " provider endpoint — the baked identity cannot be proven under it"
        for name in sorted(environ)
        if _is_identity_env_key(name)
    ]


# -- the baked identity, as an image (or run container) declares it -----------


def read_baked_identity(root: Path) -> BakedIdentity | None:
    """The identity this filesystem was baked with, or None when it carries
    none (a model-less worker type: no well-known files, no managed identity
    keys). An inconsistent declaration — effort without model, empty or
    unparseable files, managed identity keys without the well-known files, or
    a managed pin that does not match them — raises IdentityError: a
    half-declared identity must never silently run unenforced."""
    model_file = root / WELL_KNOWN_MODEL_FILE
    effort_file = root / WELL_KNOWN_EFFORT_FILE
    model = model_file.read_text(encoding="utf-8").strip() if model_file.is_file() else ""
    effort = effort_file.read_text(encoding="utf-8").strip() if effort_file.is_file() else ""
    if model_file.is_file() and not model:
        raise IdentityError(f"{model_file} exists but is empty — corrupt baked identity")
    if effort and not model:
        raise IdentityError(
            f"{effort_file} declares effort {effort!r} but {model_file} declares"
            " no model — effort without a model is not an identity"
        )
    if not model:
        # No well-known identity. Managed identity keys without it would be
        # an enforcement config nothing verifies — refuse to run under it.
        conflicts = scan_managed_conflicts(root, expected=None)
        if conflicts:
            raise IdentityError(
                "managed settings carry identity keys but the image declares no"
                " baked identity (missing " + WELL_KNOWN_MODEL_FILE + "): " + "; ".join(conflicts)
            )
        return None
    if "\n" in model or "\n" in effort:
        raise IdentityError(
            f"{model_file} / {effort_file} carry multiple lines — corrupt baked identity"
        )
    return BakedIdentity(model=model, effort=effort)


def verify_managed_pin(root: Path, expected: BakedIdentity) -> str:
    """Why the base managed file does not pin ``expected``; "" when it does.

    The well-known files declare the identity; the managed settings are what
    ENFORCE it. A declared identity whose enforcement keys are missing or
    different is an image that looks pinned and binds nothing. The freshness
    key is required with the same force: without it the canaries and the
    probe may run under stale server policy and prove nothing about what the
    task session would start under (images built before this amendment fail
    here and need a rebuild — the missing key is named)."""
    base = root / MANAGED_SETTINGS_FILE
    if not base.is_file():
        return (
            f"{base} is missing — the baked identity {expected.model!r} has no"
            " enforcement configuration"
        )
    try:
        document = _load_settings_document(base)
    except IdentityError as exc:
        return str(exc)
    for key, value in _expected_base_values(expected).items():
        if document.get(key) != value:
            return (
                f"{base}: '{key}' does not pin the baked identity"
                f" (expected the materialized value for {expected.model!r})"
            )
    if expected.effort:
        env = document.get("env")
        if not isinstance(env, dict) or env.get("CLAUDE_CODE_EFFORT_LEVEL") != expected.effort:
            return (
                f"{base}: env CLAUDE_CODE_EFFORT_LEVEL does not pin the baked"
                f" effort {expected.effort!r}"
            )
    return ""


# -- expectation matching -----------------------------------------------------

_ALIASES = frozenset(_ALIAS_EFFORT_CAPABILITY)


def model_matches(expected: str, seen: str) -> bool:
    """Does an observed executed model satisfy the baked expectation?

    A family alias accepts any model of exactly that family (the allowlist
    semantics verified live); a pinned/full ID requires an exact resolved
    match — an undated pin that the provider resolves to a dated ID fails,
    and the fix is to pin the dated ID (the control-side lint already points
    there)."""
    if not seen:
        return False
    if expected in _ALIASES:
        return seen.startswith(f"claude-{expected}-")
    return seen == expected


def intruder_for(expected: str) -> str:
    """A cheap, always-available model of a DIFFERENT family than the pin,
    for the widen canary."""
    return "claude-sonnet-5" if "haiku" in expected else "claude-haiku-4-5"


# Known same-family alternatives for the second canary. Ordered so the first
# candidate is never one the provider would merely resolve TO the pin (an
# undated alias of the pinned ID would execute the pin and prove nothing).
# Families with a single known member (fable, mythos) have no sibling: the
# same-family canary is skipped there and the different-family canary plus
# the in-session guard remain the proof.
_FAMILY_SIBLINGS: dict[str, tuple[str, ...]] = {
    "haiku": ("claude-3-5-haiku-20241022", "claude-haiku-4-5"),
    "opus": ("claude-opus-4-6", "claude-opus-5"),
    "sonnet": ("claude-sonnet-4-6", "claude-sonnet-5"),
}


def sibling_for(expected: str) -> str:
    """A same-family model ID different from a FULL-ID pin, for the
    same-family widen canary; "" for aliases (family members are expected to
    pass) and for families with no known second member.

    A full-ID pin must reject even its own family: allowlist enforcement
    that quietly matched at family granularity would satisfy the
    different-family canary and still run the wrong model. The sibling need
    not be servable — under intact enforcement it never reaches the
    provider, and under broken enforcement an unavailable sibling errors the
    canary, which also fails closed."""
    if expected in _ALIASES:
        return ""
    for family, candidates in _FAMILY_SIBLINGS.items():
        if family in expected:
            for candidate in candidates:
                if candidate != expected and not model_matches(expected, candidate):
                    return candidate
    return ""


# -- preflight ---------------------------------------------------------------


@dataclass(frozen=True)
class PreflightReport:
    """One preflight verdict: what was expected, what was proven, and — on
    failure — the mismatch category and an actionable, value-redacted detail.
    A failed preflight always means the real task prompt was never sent (the
    task file was not even on disk while the preflight ran)."""

    ok: bool
    expected_model: str
    expected_effort: str
    category: str = ""
    detail: str = ""
    cli_version: str = ""
    canary_model: str = ""  # what the widen canary actually executed
    probe_model: str = ""  # what the neutral identity probe executed
    probe_effort: str = ""  # the applied effort the probe's Stop hook reported

    def describe(self) -> str:
        effort = self.expected_effort or "(model default)"
        if self.ok:
            return f"preflight passed for model {self.expected_model!r}, effort {effort}"
        return (
            f"preflight failed [{self.category}] for model"
            f" {self.expected_model!r}, effort {effort}: {self.detail}"
            " (the real task prompt was not sent)"
        )


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# One tiny turn: enough to observe the executed model, never the task.
CANARY_PROMPT = "Reply with exactly: OK"
# The neutral identity probe's turn (never the task). No tool: the applied
# effort is observed via the Stop hook payload, which fires after a plain
# turn and reports the post-clamp value (verified live on 2.1.232).
PROBE_PROMPT = (
    "Preflight check. Reply with exactly: OK. Do not use any tools and do not read any files."
)
PREFLIGHT_SESSION_TIMEOUT = 240.0

# The pre-verification boundary, as invocation flags: every verification
# session runs with NO built-in tools, permission prompts auto-denied (an
# adversarially substituted model that emits a tool call gets a denial, not
# an execution), no user/project/local settings tiers (managed policy always
# applies — that is the policy under test), and no non-managed MCP servers.
# The sessions also run cwd-neutral (the preflight scratch, outside the job
# mount) so no project CLAUDE.md, settings, hooks, or skills exist to load.
NEUTRAL_SESSION_ARGS = (
    "--output-format",
    "stream-json",
    "--verbose",
    "--tools",
    "",
    "--permission-mode",
    "dontAsk",
    "--setting-sources",
    "",
    "--strict-mcp-config",
)


@dataclass(frozen=True)
class StreamSignals:
    """The model signals of one probe/canary stream (subprocess stdout)."""

    init_model: str = ""
    turn_models: tuple[str, ...] = ()
    is_error: bool = False
    error_note: str = ""


def scan_stream_signals(text: str) -> StreamSignals:
    """Parse a stream-json transcript for the identity signals: the init
    announcement, executed (non-synthetic) assistant turns, and result
    errors. Unparseable lines are skipped — agent output is never trusted."""
    init_model = ""
    turns: list[str] = []
    is_error = False
    error_note = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            seen = event.get("model")
            if isinstance(seen, str) and not init_model:
                init_model = seen
        elif event.get("type") == "assistant":
            message = event.get("message")
            if isinstance(message, dict):
                seen = message.get("model")
                if isinstance(seen, str) and seen and seen != "<synthetic>" and seen not in turns:
                    turns.append(seen)
        elif event.get("type") == "result":
            if event.get("is_error") is True:
                is_error = True
                subtype = event.get("subtype")
                error_note = subtype if isinstance(subtype, str) else "error"
    return StreamSignals(
        init_model=init_model, turn_models=tuple(turns), is_error=is_error, error_note=error_note
    )


def read_effort_capture(capture: Path) -> str:
    """The applied effort from a Stop-hook capture file; "" when the file is
    absent, partial, or carries no effort field (a hook mid-write is not an
    observation)."""
    try:
        data = json.loads(capture.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        effort = data.get("effort")
        if isinstance(effort, dict) and isinstance(effort.get("level"), str):
            return effort["level"]
    return ""


def run_preflight(
    identity: BakedIdentity,
    *,
    binary: str = "claude",
    root: Path = Path("/"),
    scratch: Path,
    min_cli: tuple[int, int, int],
    run=subprocess.run,
    environ: Mapping[str, str] | None = None,
    timeout: float = PREFLIGHT_SESSION_TIMEOUT,
) -> PreflightReport:
    """The pre-launch runtime gate, entirely inside the pre-verification
    boundary: static policy re-checks, the process-environment audit, the
    CLI version floor, the widen canary, the same-family canary (full-ID
    pins), and the neutral identity probe that proves an executed turn and —
    when effort is baked — the applied (post-clamp) effort, all with the
    Run's own credential and effective policy, all in the neutral scratch,
    all with tools and project sources removed.

    Only after this passes does the harness even LAUNCH the task session,
    and the real prompt is still withheld until that session's own announced
    and executed identity match (``ClaudeSessionGuard``)."""
    environ = os.environ if environ is None else environ

    def failed(category: str, detail: str, **extra) -> PreflightReport:
        return PreflightReport(
            ok=False,
            expected_model=identity.model,
            expected_effort=identity.effort,
            category=category,
            detail=detail,
            **extra,
        )

    # Static: the image (or anything mounted over it) must still be free of
    # superseding managed policy, and must actually pin what it declares.
    try:
        conflicts = scan_managed_conflicts(root, expected=identity)
    except IdentityError as exc:
        return failed(CATEGORY_POLICY_CONFLICT, str(exc))
    if conflicts:
        return failed(CATEGORY_POLICY_CONFLICT, "; ".join(conflicts))
    inconsistency = verify_managed_pin(root, identity)
    if inconsistency:
        return failed(CATEGORY_INCONSISTENT, inconsistency)
    pair = pair_error(identity.model, identity.effort)
    if pair:
        return failed(CATEGORY_PAIR_INVALID, pair)
    env_conflicts = scan_process_environment(environ)
    if env_conflicts:
        return failed(CATEGORY_POLICY_CONFLICT, "; ".join(env_conflicts))

    # The CLI beside this harness must be new enough for every behavior the
    # gate relies on (see ClaudeAdapter.MIN_ENFORCING_CLI for the ledger).
    try:
        probe = run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return failed(CATEGORY_UNVERIFIABLE, f"cannot run '{binary} --version': {exc}")
    version_raw = (probe.stdout or "").strip()
    match = _VERSION_RE.match(version_raw)
    if probe.returncode != 0 or not match:
        return failed(
            CATEGORY_UNVERIFIABLE,
            f"'{binary} --version' gave no parseable version (exit {probe.returncode})",
        )
    if tuple(int(part) for part in match.groups()) < min_cli:
        floor = ".".join(str(part) for part in min_cli)
        return failed(
            CATEGORY_CLI_TOO_OLD,
            f"Claude Code {version_raw} predates the enforcement behavior this"
            f" identity relies on (needs >= {floor})",
            cli_version=version_raw,
        )

    def neutral_session(prompt: str, *extra: str) -> tuple[str, str, StreamSignals | None]:
        """One neutral one-shot verification session in the scratch;
        (error category, error detail, signals) — category "" on a run that
        executed at least one turn without a session error."""
        argv = [binary, "-p", prompt, *NEUTRAL_SESSION_ARGS, *extra]
        try:
            proc = run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=str(scratch),
            )
        except subprocess.TimeoutExpired:
            return CATEGORY_TIMEOUT, f"no verdict within {timeout:.0f}s", None
        except OSError as exc:
            return CATEGORY_UNVERIFIABLE, f"cannot launch the session: {exc}", None
        signals = scan_stream_signals(proc.stdout or "")
        if signals.is_error or proc.returncode != 0:
            note = signals.error_note or f"exit {proc.returncode}"
            return CATEGORY_UNAVAILABLE, note, signals
        if not signals.turn_models:
            return CATEGORY_UNVERIFIABLE, "the session executed no turn", signals
        return "", "", signals

    # The widen canaries: ask for an intruder model with the Run's own
    # credential and effective policy. Under an intact single-entry
    # allowlist every selection surface coerces to the pin (verified live);
    # an executed intruder — or anything else — means the effective policy
    # no longer binds the baked identity, whatever source changed it. A
    # full-ID pin gets a second canary with a SAME-family sibling: family-
    # granular enforcement would pass the different-family canary and still
    # run the wrong model. Neither canary proves the absence of conditional
    # fallback or of server-side overrides that only trigger later — that
    # residual is what the in-session guard's drift kill is for.
    canaries = [("the widen canary", intruder_for(identity.model))]
    sibling = sibling_for(identity.model)
    if sibling:
        canaries.append(("the same-family canary", sibling))
    canary_observed = ""
    for label, intruder in canaries:
        category, note, signals = neutral_session(CANARY_PROMPT, "--model", intruder)
        if category == CATEGORY_TIMEOUT:
            return failed(
                CATEGORY_TIMEOUT,
                f"{label} produced no verdict within {timeout:.0f}s",
                cli_version=version_raw,
            )
        if category == CATEGORY_UNAVAILABLE:
            return failed(
                CATEGORY_UNAVAILABLE,
                f"{label} session errored ({note}) — the baked model cannot be"
                " verified as available to this credential",
                cli_version=version_raw,
                canary_model=(signals.turn_models[-1] if signals and signals.turn_models else ""),
            )
        if category:
            return failed(
                CATEGORY_UNVERIFIABLE,
                f"{label} could not observe the effective policy: {note}",
                cli_version=version_raw,
            )
        assert signals is not None
        observed = signals.turn_models[-1]
        if label.startswith("the widen"):
            canary_observed = observed
        executed_intruder = any(seen == intruder for seen in signals.turn_models)
        off_pin = [seen for seen in signals.turn_models if not model_matches(identity.model, seen)]
        if executed_intruder or off_pin:
            return failed(
                CATEGORY_WIDENED if executed_intruder else CATEGORY_SUBSTITUTED,
                f"{label} asked for {intruder!r} and executed"
                f" {', '.join(signals.turn_models)} — the effective policy does"
                f" not coerce every selection to the baked {identity.model!r}"
                " (an organization/server policy, drop-in, or helper supersedes"
                " the image)",
                cli_version=version_raw,
                canary_model=observed,
            )

    # The neutral identity probe: no --model, no tools — the session the
    # effective policy chooses by itself must announce AND execute the baked
    # model, and, when effort is baked, the Stop hook must report the baked
    # level as the applied one (an organization cap clamps silently in the
    # stream; the hook payload is the machine-readable observation).
    probe_args: list[str] = []
    effort_capture = scratch / "preflight-effort.json"
    if identity.effort:
        hook = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": f"cat > {_shquote(effort_capture)}"}]}
                ]
            }
        }
        probe_args = ["--settings", json.dumps(hook)]
    category, note, signals = neutral_session(PROBE_PROMPT, *probe_args)
    if category == CATEGORY_TIMEOUT:
        return failed(
            CATEGORY_TIMEOUT,
            f"the identity probe produced no verdict within {timeout:.0f}s",
            cli_version=version_raw,
            canary_model=canary_observed,
        )
    if category == CATEGORY_UNAVAILABLE:
        return failed(
            CATEGORY_UNAVAILABLE,
            f"the identity probe session errored ({note}) — the baked model"
            " cannot be verified as available to this credential",
            cli_version=version_raw,
            canary_model=canary_observed,
        )
    if category:
        return failed(
            CATEGORY_UNVERIFIABLE,
            f"the identity probe could not observe the session identity: {note}",
            cli_version=version_raw,
            canary_model=canary_observed,
        )
    assert signals is not None
    probe_model = signals.turn_models[-1]
    off_pin = [seen for seen in signals.turn_models if not model_matches(identity.model, seen)]
    init_off = signals.init_model and not model_matches(identity.model, signals.init_model)
    if off_pin or init_off:
        drifted = ", ".join(off_pin) or signals.init_model
        return failed(
            CATEGORY_SUBSTITUTED,
            f"the identity probe ran on {drifted!r}, not the baked"
            f" {identity.model!r} — the effective policy substituted the model",
            cli_version=version_raw,
            canary_model=canary_observed,
            probe_model=probe_model,
        )
    probe_effort = ""
    if identity.effort:
        probe_effort = read_effort_capture(effort_capture)
        if not probe_effort:
            return failed(
                CATEGORY_UNVERIFIABLE,
                "the identity probe exposed no applied effort — the CLI beside"
                " this harness does not report effort in the Stop hook payload,"
                " so the baked effort cannot be proven",
                cli_version=version_raw,
                canary_model=canary_observed,
                probe_model=probe_model,
            )
        if probe_effort != identity.effort:
            return failed(
                CATEGORY_EFFORT_CLAMPED,
                f"the identity probe applied effort {probe_effort!r}, not the"
                f" baked {identity.effort!r} — an effort surface or organization"
                " cap supersedes the pin",
                cli_version=version_raw,
                canary_model=canary_observed,
                probe_model=probe_model,
                probe_effort=probe_effort,
            )
    return PreflightReport(
        ok=True,
        expected_model=identity.model,
        expected_effort=identity.effort,
        cli_version=version_raw,
        canary_model=canary_observed,
        probe_model=probe_model,
        probe_effort=probe_effort,
    )


def _shquote(path: Path) -> str:
    return shlex.quote(str(path))


# -- the gated task session ---------------------------------------------------

GUARD_WAIT = "wait"
GUARD_RELEASE = "release"
GUARD_KILL = "kill"


@dataclass(frozen=True)
class TaskGate:
    """The filesystem contract between the harness and the gated task
    session's hooks. Every path lives in the harness's neutral scratch,
    OUTSIDE the job mount.

    - ``release_marker``: created by the harness at release; until it
      exists, the PreToolUse gate hook denies every tool call in the task
      session (verified live: the denial binds even under
      ``--dangerously-skip-permissions``).
    - ``effort_capture``: the Stop hook's payload file (None when no effort
      is baked) — the pre-release proof channel and the post-release drift
      monitor.
    - ``config_capture``: the ConfigChange hook helper appends one
      value-redacted JSON line per identity-relevant mid-session settings
      change; any line kills the Run.
    - ``config_hook_script``: the helper the harness materializes (see
      ``CONFIG_CHANGE_HOOK_SOURCE``)."""

    release_marker: Path
    effort_capture: Path | None
    config_capture: Path
    config_hook_script: Path


# The ConfigChange hook helper (written by the harness into the gate
# scratch). It NEVER blocks a settings change — organization policy is never
# resisted (ADR-0045 doctrine); an identity-relevant change is recorded and
# the harness kills the Run instead. project_settings changes are filtered
# by content: the task may legitimately edit the checkout's settings, so
# only identity-shaped keys (or an unreadable changed file — unknowable is
# fail-closed) are recorded. skills changes are ignored: skill model
# frontmatter is bound by the enforced allowlist (verified live). Everything
# else (policy/user/local settings, unknown future sources) is recorded
# unconditionally. Records carry source, path, and matched key names only.
CONFIG_CHANGE_HOOK_SOURCE = '''\
"""theozolith ConfigChange hook (ADR-0045): record identity-relevant
mid-session settings changes for the harness session guard. Never blocks."""
import json
import re
import sys

IDENTITY_PATTERN = re.compile(
    r\'"(model|availableModels|enforceAvailableModels|fallbackModel|effortLevel\'
    r"|modelOverrides|policyHelper|policyHelpers|forceRemoteSettingsRefresh"
    r"|ANTHROPIC_[A-Z_]+|CLAUDE_CODE_EFFORT_LEVEL|CLAUDE_CODE_SUBAGENT_MODEL"
    r\'|CLAUDE_CODE_USE_BEDROCK|CLAUDE_CODE_USE_VERTEX)"\\s*:\'
)


def main() -> int:
    capture = sys.argv[1]
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    if not isinstance(event, dict):
        event = {}
    source = event.get("source") if isinstance(event.get("source"), str) else ""
    path = event.get("file_path") if isinstance(event.get("file_path"), str) else ""
    if source == "skills":
        return 0
    keys: list[str] = []
    if source == "project_settings":
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            text = None
        if text is not None:
            keys = sorted({match.group(1) for match in IDENTITY_PATTERN.finditer(text)})
            if not keys:
                return 0
    with open(capture, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"source": source, "file_path": path, "keys": keys}) + "\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


@dataclass(frozen=True)
class GuardDecision:
    action: str  # GUARD_WAIT | GUARD_RELEASE | GUARD_KILL
    reason: str = ""
    category: str = ""  # a CATEGORY_* string on GUARD_KILL


class ClaudeSessionGuard:
    """Line-by-line identity gate over the gated ``claude -p --input-format
    stream-json`` task session.

    The task session is only ever LAUNCHED after ``run_preflight`` proved
    the effective policy with the Run's own credential. This guard is the
    in-process re-verification on top: before release, the session's init
    announcement must resolve to the baked model, the probe turn must
    EXECUTE on it (exact for a pinned ID, family for an alias), and — when
    effort is baked — the Stop-hook capture must report the baked level as
    applied. Until the harness releases, the session's tools are denied by
    the PreToolUse gate hook and the task file is not on disk. After release
    (``mark_released``): any executed turn off the baked model, a drifted
    applied effort, or a recorded identity-relevant ConfigChange kills the
    session — a mid-run policy change invalidates the Run immediately
    instead of being discovered post-hoc. Probe and task turns are counted
    separately so an exit that never processed the task is detectable.

    Trust note: transcript events are authored by the CLI and gate the
    release; the capture files are same-user filesystem state and are
    therefore a fail-closed channel only — a forged "matching" capture
    changes nothing (release still required the transcript proof and the
    preflight), and a forged mismatch only fails the forger's own Run."""

    def __init__(self, identity: BakedIdentity, gate: TaskGate):
        self._identity = identity
        self._gate = gate
        self._init_ok = False
        self._turn_ok = False
        self._violation = ""
        self._category = ""
        self._released = False
        self.observed_model = ""
        self.observed_effort = ""
        self.probe_turns = 0
        self.task_turns = 0

    @property
    def probe_prompt(self) -> str:
        return PROBE_PROMPT

    def probe_input(self) -> str:
        return self.render_input(self.probe_prompt)

    def render_input(self, prompt: str) -> str:
        """One stream-json input line carrying a user message."""
        return json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            }
        )

    def mark_released(self) -> None:
        """The task prompt has entered the process: turns observed from here
        are task turns, and the guard is a drift monitor from here on."""
        self._released = True

    @property
    def released(self) -> bool:
        return self._released

    def _fail(self, reason: str, category: str) -> None:
        if not self._violation:
            self._violation = reason
            self._category = category

    def observe(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        expected = self._identity.model
        if event.get("type") == "system" and event.get("subtype") == "init":
            seen = event.get("model")
            if isinstance(seen, str) and seen:
                if model_matches(expected, seen):
                    self._init_ok = True
                else:
                    self._fail(
                        f"session initialized on {seen!r}, not the baked"
                        f" {expected!r} — the effective policy substituted the"
                        " model",
                        CATEGORY_SUBSTITUTED,
                    )
        elif event.get("type") == "assistant":
            message = event.get("message")
            seen = message.get("model") if isinstance(message, dict) else None
            if isinstance(seen, str) and seen and seen != "<synthetic>":
                if self._released:
                    self.task_turns += 1
                else:
                    self.probe_turns += 1
                if model_matches(expected, seen):
                    self.observed_model = seen
                    self._turn_ok = True
                else:
                    self._fail(
                        f"a turn executed on {seen!r}, not the baked {expected!r}",
                        CATEGORY_SUBSTITUTED,
                    )
        elif event.get("type") == "result" and event.get("is_error") is True:
            subtype = event.get("subtype")
            note = subtype if isinstance(subtype, str) else "error"
            if not self._turn_ok:
                self._fail(
                    f"the probe turn errored ({note}) — the baked model cannot"
                    " be verified as available to this credential",
                    CATEGORY_UNAVAILABLE,
                )

    def _effort_state(self) -> str:
        """ "ok" | "wait" | a violation string, from the Stop-hook capture."""
        if self._gate.effort_capture is None:
            return "ok"
        level = read_effort_capture(self._gate.effort_capture)
        if not level:
            return "wait"
        self.observed_effort = level
        if level != self._identity.effort:
            return (
                f"the session applies effort {level!r}, not the baked"
                f" {self._identity.effort!r} — an effort surface or"
                " organization cap superseded the pin"
            )
        return "ok"

    def _config_change(self) -> str:
        """A violation string when the ConfigChange helper recorded an
        identity-relevant mid-session settings change; "" otherwise."""
        try:
            text = self._gate.config_capture.read_text(encoding="utf-8")
        except OSError:
            return ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            source, keys = "", ""
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                record = None
            if isinstance(record, dict):
                source = str(record.get("source", ""))
                key_list = record.get("keys")
                if isinstance(key_list, list):
                    keys = ", ".join(str(key) for key in key_list)
            detail = f" ({keys})" if keys else ""
            return (
                "an identity-affecting settings change was applied mid-session"
                f" (source: {source or 'unknown'}{detail}) — the session no"
                " longer runs under the proven configuration"
            )
        return ""

    def decision(self) -> GuardDecision:
        if self._violation:
            return GuardDecision(GUARD_KILL, self._violation, self._category)
        config = self._config_change()
        if config:
            self._fail(config, CATEGORY_CONFIG_CHANGED)
            return GuardDecision(GUARD_KILL, self._violation, self._category)
        effort = self._effort_state()
        if effort not in ("ok", "wait"):
            self._fail(effort, CATEGORY_EFFORT_CLAMPED)
            return GuardDecision(GUARD_KILL, self._violation, self._category)
        if not self._released and self._init_ok and self._turn_ok and effort == "ok":
            return GuardDecision(GUARD_RELEASE)
        return GuardDecision(GUARD_WAIT)
