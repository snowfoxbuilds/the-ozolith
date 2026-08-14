"""Baked-identity machinery for the Claude adapter (ADR-0045, best effort).

ADR-0045 bakes a worker type's model/effort into the derived image and holds
Runs to it **by best effort**: the materialized managed settings make the
right identity happen (a managed ``model`` session default the checkout's
own settings cannot outrank, plus a managed-env effort pin), the harness
**fails loud** whenever it detects the effective identity is wrong, and a
gap that prevents detection is recorded as a gap — never silently, and
never by withholding or gating the work. Enforcement is scoped to the MAIN
agent only: subagents and the CLI's background helpers may run other models
(their stream events carry ``parent_tool_use_id`` and are deliberately
ignored by every identity check).

Three layers:

- The **build gate** (``scan_managed_conflicts`` with no expected identity,
  called from ``ClaudeAdapter.materialize``): the materialized keys only
  mean anything if nothing else in the image's managed tier supersedes
  them. Claude Code merges ``/etc/claude-code/managed-settings.json`` with
  every ``managed-settings.d/*.json`` drop-in (alphabetical, scalars
  override, arrays concatenate, objects deep-merge), and a managed
  ``policyHelper`` preempts the whole managed tier. Any identity-affecting
  key in any of those sources — or a malformed source — fails the build
  with the file and key named; operator policy is never silently deleted
  or overwritten.

- The **setup dry-run** (``run_preflight``, driven once per driver process
  through the harness's identity-dryrun mode): the static checks, the CLI
  version floor, and ONE neutral no-tool probe session that must announce
  and execute the baked model — and, when effort is baked, report the baked
  level as the applied (post-clamp) effort via the ``Stop`` hook payload.
  A broken image/credential/policy combination fails loud in seconds at
  worker setup, before any issue or claim is spent. The dry-run is strict
  on purpose: a probe that shows no signal is a broken observation channel,
  and setup is the time to learn that.

- The **per-Run fail-loud monitor** (``static_identity_report`` +
  ``ClaudeSessionMonitor``): zero-cost static re-checks before launch (file
  reads only — no sessions, no tokens), then an ordinary one-shot task
  launch watched by a monitor that kills the session on a DETECTED
  off-identity main-agent turn or a recorded identity-relevant mid-session
  settings change (the ConfigChange helper records, never blocks —
  organization policy is never resisted), plus a post-exit applied-effort
  check from the Stop-hook journal. Only positive detections fail; a
  missing observation is a recorded gap (ADR-0045: gaps can happen).

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

# Top-level managed keys that can change or supersede the baked identity.
# policyHelper/policyHelpers preempt the entire managed tier (their output
# becomes the only managed configuration), so they are never tolerable
# beside a baked identity. fallbackModel moves a session off the default
# under provider pressure. modelOverrides remaps what actually SERVES a
# model ID while the stream still shows the Anthropic ID. The rest select
# or constrain model/effort. These are the build gate's refusal list: the
# build never overwrites operator policy, it stops and names it.
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

# Managed ``env`` entries (and forbidden process-environment variables) that
# select a model or an effort, or that repoint the CLI at a different
# provider/endpoint. The credential contract (ADR-0045) delivers only the
# API/OAuth secret — one of these reaching the run container means some
# layer outside the image is steering the session.
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


def identity_error_detail(message: str) -> str | None:
    """The detail of an identity failure crossing the status.json channel,
    or None when ``message`` is not one.

    Anchored on purpose: the marker must BEGIN the harness's status error
    (optionally behind the session layer's ``harness failed: `` wrapper) —
    a message that merely CONTAINS the marker somewhere (an agent echoing
    it, a path fragment, a nested quote) is not an identity verdict."""
    text = message.removeprefix("harness failed: ")
    if text.startswith(IDENTITY_ERROR_PREFIX):
        return text[len(IDENTITY_ERROR_PREFIX) :]
    return None


# Failure categories — stable strings, they land in evidence.
CATEGORY_POLICY_CONFLICT = "policy-conflict"
CATEGORY_INCONSISTENT = "identity-inconsistent"
CATEGORY_PAIR_INVALID = "pair-invalid"
CATEGORY_CLI_TOO_OLD = "cli-too-old"
CATEGORY_UNAVAILABLE = "unavailable"
CATEGORY_SUBSTITUTED = "substituted"
CATEGORY_EFFORT_CLAMPED = "effort-clamped"
CATEGORY_UNVERIFIABLE = "unverifiable"
CATEGORY_TIMEOUT = "preflight-timeout"
CATEGORY_CONFIG_CHANGED = "config-changed"


@dataclass(frozen=True)
class BakedIdentity:
    """What the image was built to run: the well-known files' content."""

    model: str
    effort: str = ""  # "" = the model's own default; nothing to check


# -- (model, effort) pair capability ------------------------------------------

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
    build CLI, and the runtime checks (ADR-0045)."""
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


def _required_base_values(expected: BakedIdentity) -> dict[str, object]:
    """What materialize() writes for ``expected`` — a managed ``model``
    session default (main-agent selection: the managed tier outranks the
    checkout's settings for the same key, and the harness passes no
    ``--model``), plus the effort default when one is baked. Deliberately
    NOT an allowlist: subagents and background helpers are free to run
    other models (ADR-0045, main-agent-only enforcement)."""
    values: dict[str, object] = {"model": expected.model}
    if expected.effort:
        values["effortLevel"] = expected.effort
    return values


def _tolerated_base_values(expected: BakedIdentity) -> dict[str, object]:
    """Everything the base managed file may carry for ``expected`` without
    being a conflict: the required values plus the LEGACY enforcement keys
    earlier ADR-0045 builds wrote (single-entry allowlist + enforcement
    flag). Legacy images are stricter — they still pin subagents to the
    model — not conflicting; they relax to main-agent-only on rebuild."""
    values = _required_base_values(expected)
    values["availableModels"] = [expected.model]
    values["enforceAvailableModels"] = True
    return values


def scan_managed_conflicts(root: Path, expected: BakedIdentity | None = None) -> list[str]:
    """Identity-affecting managed policy under ``root``, as conflict strings
    naming the file and key.

    ``expected=None`` is the build gate: ANY identity key anywhere is a
    conflict (operator policy is never overwritten — the build stops and
    names it). With an expected identity (the runtime re-check) the base
    file may carry exactly the values ``materialize`` wrote for it — or the
    legacy allowlist shape older builds wrote — anything else, and any
    identity key in a drop-in, is still a conflict. Raises IdentityError on
    a malformed document (unknowable policy)."""
    conflicts: list[str] = []
    base = root / MANAGED_SETTINGS_FILE
    for path, document in managed_policy_documents(root):
        own = path == base and expected is not None
        tolerable = _tolerated_base_values(expected) if own else {}
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
            conflicts.append(f"{path}: '{key}' can change or supersede the baked model/effort")
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
    environment means some layer outside the image is steering the session.
    Fail loud, name the variable."""
    return [
        f"process environment variable '{name}' selects a model, effort, or"
        " provider endpoint — it would steer the session over the baked identity"
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
    half-declared identity must never silently run unchecked."""
    model_file = root / WELL_KNOWN_MODEL_FILE
    effort_file = root / WELL_KNOWN_EFFORT_FILE
    try:
        model = model_file.read_text(encoding="utf-8").strip() if model_file.is_file() else ""
        effort = effort_file.read_text(encoding="utf-8").strip() if effort_file.is_file() else ""
    except OSError as exc:
        # An unreadable declaration is an identity failure, not generic
        # harness breakage — it must reach the identity-inconsistent lane.
        raise IdentityError(f"unreadable baked-identity declaration: {exc}") from exc
    if model_file.is_file() and not model:
        raise IdentityError(f"{model_file} exists but is empty — corrupt baked identity")
    if effort and not model:
        raise IdentityError(
            f"{effort_file} declares effort {effort!r} but {model_file} declares"
            " no model — effort without a model is not an identity"
        )
    if not model:
        # No well-known identity. Managed identity keys without it would be
        # a selection config nothing declared — refuse to run under it.
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
    """Why the base managed file does not carry ``expected``; "" when it
    does. The well-known files declare the identity; the managed settings
    are what SELECT it (the managed ``model`` default outranks checkout and
    user settings). A declared identity whose selection keys are missing or
    different is an image that looks configured and selects nothing."""
    base = root / MANAGED_SETTINGS_FILE
    if not base.is_file():
        return (
            f"{base} is missing — the baked identity {expected.model!r} has no"
            " selection configuration"
        )
    try:
        document = _load_settings_document(base)
    except IdentityError as exc:
        return str(exc)
    for key, value in _required_base_values(expected).items():
        if document.get(key) != value:
            return (
                f"{base}: '{key}' does not carry the baked identity"
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

# The CLI decorates an announced model with a bracketed context tag when a
# long-context variant is active (e.g. "claude-opus-5[1m]" in the init event
# and modelUsage while the executed turns say "claude-opus-5" — observed live
# on 2.1.232): the same model identity with a larger context window, not a
# substitution. Matching strips the decoration; evidence keeps the raw string.
_MODEL_DECORATION = re.compile(r"\[[^\[\]]+\]$")


def normalize_model(seen: str) -> str:
    """An observed model signal with the CLI's context-window decoration
    stripped — the form identity comparisons run on."""
    return _MODEL_DECORATION.sub("", seen)


def model_matches(expected: str, seen: str) -> bool:
    """Does an observed executed model satisfy the baked expectation?

    A family alias accepts any model of exactly that family; a pinned/full
    ID requires an exact resolved match — an undated pin that the provider
    resolves to a dated ID fails, and the fix is to pin the dated ID (the
    control-side lint already points there). The observed side is
    normalized (``normalize_model``) so a context-window decoration never
    reads as a different model."""
    if not seen:
        return False
    seen = normalize_model(seen)
    if expected in _ALIASES:
        return seen.startswith(f"claude-{expected}-")
    return seen == expected


# -- reports, prompts, stream scanning -----------------------------------------


@dataclass(frozen=True)
class PreflightReport:
    """One identity-check verdict: what was expected, what was observed,
    and — on failure — the category and an actionable, value-redacted
    detail. Produced by the free per-Run static checks and by the setup
    dry-run (which adds the CLI floor and the probe observations)."""

    ok: bool
    expected_model: str
    expected_effort: str
    category: str = ""
    detail: str = ""
    cli_version: str = ""
    probe_model: str = ""  # what the dry-run probe actually executed
    probe_effort: str = ""  # the applied effort the probe's Stop hook reported

    def describe(self) -> str:
        effort = self.expected_effort or "(model default)"
        if self.ok:
            return f"identity checks passed for model {self.expected_model!r}, effort {effort}"
        return (
            f"identity checks failed [{self.category}] for model"
            f" {self.expected_model!r}, effort {effort}: {self.detail}"
        )


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# The dry-run probe's one turn. No tool: the applied effort is observed via
# the Stop hook payload, which fires after a plain turn and reports the
# post-clamp value (verified live on 2.1.232).
PROBE_PROMPT = (
    "Preflight check. Reply with exactly: OK. Do not use any tools and do not read any files."
)
PREFLIGHT_SESSION_TIMEOUT = 240.0

# The dry-run probe's invocation flags: no built-in tools, permission
# prompts auto-denied, no user/project/local settings tiers (managed policy
# always applies — that is the configuration under test), no non-managed MCP
# servers. The probe is a throwaway diagnostic session; keeping it hermetic
# keeps it deterministic. The TASK session shares none of this: it runs
# with its normal capabilities, settings sources included (ADR-0045 —
# checkout CLAUDE.md and skills belong to the work).
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
    """The MAIN-agent model signals of one probe stream (subprocess stdout)."""

    init_model: str = ""
    turn_models: tuple[str, ...] = ()
    is_error: bool = False
    error_note: str = ""


def scan_stream_signals(text: str) -> StreamSignals:
    """Parse a stream-json transcript for the identity signals: the init
    announcement, executed (non-synthetic) MAIN-agent assistant turns —
    subagent events carry ``parent_tool_use_id`` and are ignored — and
    result errors. Unparseable lines are skipped."""
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
            if event.get("parent_tool_use_id"):
                continue  # a subagent turn: free to be another model
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
    """The applied effort from a single Stop-hook payload capture; "" when
    the file is absent, partial, or carries no effort field (a hook
    mid-write is not an observation). Used by the dry-run probe."""
    try:
        data = json.loads(capture.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        effort = data.get("effort")
        if isinstance(effort, dict) and isinstance(effort.get("level"), str):
            return effort["level"]
    return ""


def read_last_journal_effort(capture: Path) -> str:
    """The most recent applied-effort observation in a Stop-hook journal
    (one JSON record per completed turn, appended by the materialized
    helper); "" when the journal is absent or carries no observation — a
    gap, which the caller records rather than fails (ADR-0045)."""
    try:
        text = capture.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in reversed(text.split("\n")[:-1]):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("effort"), str) and record["effort"]:
            return record["effort"]
    return ""


# -- the free static checks (every Run) ----------------------------------------


def static_identity_report(
    identity: BakedIdentity,
    *,
    root: Path = Path("/"),
    environ: Mapping[str, str] | None = None,
) -> PreflightReport:
    """The zero-cost identity checks the harness runs before every launch:
    the managed tier is still free of superseding policy (catches anything
    mounted over the image), the managed selection matches the well-known
    declaration, the pair is valid, and the process environment carries no
    steering variable. File reads only — no sessions, no tokens."""
    environ = os.environ if environ is None else environ

    def failed(category: str, detail: str) -> PreflightReport:
        return PreflightReport(
            ok=False,
            expected_model=identity.model,
            expected_effort=identity.effort,
            category=category,
            detail=detail,
        )

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
    return PreflightReport(ok=True, expected_model=identity.model, expected_effort=identity.effort)


# -- the setup dry-run ----------------------------------------------------------


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
    """The setup dry-run (ADR-0045): the static checks, the CLI version
    floor, and one neutral probe session with the Run credential — the
    session the effective configuration picks by itself must announce AND
    execute the baked model and, when effort is baked, report the baked
    level as the applied (post-clamp) effort via the Stop hook payload.

    Run ONCE per driver process per run image (the harness's
    identity-dryrun mode), never per Run: a broken image/credential/policy
    combination fails loud at worker setup instead of burning claims. The
    dry-run is strict where the per-Run monitor is lenient — a probe with
    no signal means the observation channel itself is broken, and setup is
    the time to learn that."""
    report = static_identity_report(identity, root=root, environ=environ)
    if not report.ok:
        return report

    def failed(category: str, detail: str, **extra) -> PreflightReport:
        return PreflightReport(
            ok=False,
            expected_model=identity.model,
            expected_effort=identity.effort,
            category=category,
            detail=detail,
            **extra,
        )

    # The CLI beside this harness must be new enough for every behavior the
    # identity machinery relies on (see ClaudeAdapter.MIN_ENFORCING_CLI).
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
            f"Claude Code {version_raw} predates the identity behavior this"
            f" machinery relies on (needs >= {floor})",
            cli_version=version_raw,
        )

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
    argv = [binary, "-p", PROBE_PROMPT, *NEUTRAL_SESSION_ARGS, *probe_args]
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
        return failed(
            CATEGORY_TIMEOUT,
            f"the identity probe produced no verdict within {timeout:.0f}s",
            cli_version=version_raw,
        )
    except OSError as exc:
        return failed(
            CATEGORY_UNVERIFIABLE,
            f"cannot launch the identity probe: {exc}",
            cli_version=version_raw,
        )
    signals = scan_stream_signals(proc.stdout or "")
    if signals.is_error or proc.returncode != 0:
        note = signals.error_note or f"exit {proc.returncode}"
        return failed(
            CATEGORY_UNAVAILABLE,
            f"the identity probe session errored ({note}) — the baked model"
            " cannot be verified as available to this credential",
            cli_version=version_raw,
        )
    if not signals.turn_models:
        return failed(
            CATEGORY_UNVERIFIABLE,
            "the identity probe executed no turn — the observation channel"
            " the per-Run monitor relies on shows nothing",
            cli_version=version_raw,
        )
    if not signals.init_model:
        return failed(
            CATEGORY_UNVERIFIABLE,
            "the identity probe announced no session identity (no init event)",
            cli_version=version_raw,
        )
    probe_model = signals.turn_models[-1]
    off_pin = [seen for seen in signals.turn_models if not model_matches(identity.model, seen)]
    init_off = not model_matches(identity.model, signals.init_model)
    if off_pin or init_off:
        drifted = ", ".join(off_pin) or signals.init_model
        return failed(
            CATEGORY_SUBSTITUTED,
            f"the identity probe ran on {drifted!r} (announced"
            f" {signals.init_model!r}), not the baked {identity.model!r} — the"
            " effective configuration selects a different model",
            cli_version=version_raw,
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
                " so the baked effort cannot be checked",
                cli_version=version_raw,
                probe_model=probe_model,
            )
        if probe_effort != identity.effort:
            return failed(
                CATEGORY_EFFORT_CLAMPED,
                f"the identity probe applied effort {probe_effort!r}, not the"
                f" baked {identity.effort!r} — an effort surface or organization"
                " cap supersedes the pin",
                cli_version=version_raw,
                probe_model=probe_model,
                probe_effort=probe_effort,
            )
    return PreflightReport(
        ok=True,
        expected_model=identity.model,
        expected_effort=identity.effort,
        cli_version=version_raw,
        probe_model=probe_model,
        probe_effort=probe_effort,
    )


def _shquote(path: Path) -> str:
    return shlex.quote(str(path))


# -- the monitored task session -------------------------------------------------


@dataclass(frozen=True)
class MonitorHooks:
    """The filesystem contract between the harness and the task session's
    observation hooks. Every path lives in a harness scratch OUTSIDE the
    job mount, where nothing in the checkout can name or reach it by a
    job-relative path.

    - ``stop_capture``: the applied-effort journal — the Stop hook helper
      appends one value-redacted record per completed turn; the harness
      checks the last observation after exit (a detected clamp fails loud;
      a missing observation is a recorded gap).
    - ``config_capture``: the ConfigChange hook helper appends one
      value-redacted JSON line per identity-relevant mid-session settings
      change; any line kills the Run.
    - ``config_hook_script`` / ``stop_hook_script``: the helpers the
      harness materializes (``CONFIG_CHANGE_HOOK_SOURCE`` /
      ``STOP_HOOK_SOURCE``)."""

    stop_capture: Path
    config_capture: Path
    config_hook_script: Path
    stop_hook_script: Path


# The ConfigChange hook helper (written by the harness into the scratch).
# It NEVER blocks a settings change — organization policy is never resisted
# (ADR-0045 doctrine); an identity-relevant change is recorded and the
# harness kills the Run instead. project_settings changes are filtered
# STRUCTURALLY: the task may legitimately edit the checkout's settings, so
# the changed file is parsed and only top-level identity keys or steering
# ``env`` entries (or an unreadable/unparseable file — unknowable is
# recorded) are noted; a nested "model" in an unrelated object or an env
# credential must not kill a legitimate Run. skills changes are ignored.
# Everything else (policy/user/local settings, unknown future sources) is
# recorded unconditionally. Records carry source, path, and key names only.
CONFIG_CHANGE_HOOK_SOURCE = '''\
"""theozolith ConfigChange hook (ADR-0045): record identity-relevant
mid-session settings changes for the harness session monitor. Never blocks."""
import json
import sys

# Mirrors identity.py's IDENTITY_SETTING_KEYS (plus forceRemoteSettingsRefresh,
# which re-fetches remote policy mid-session) and its identity env predicate —
# the hook is deliberately self-contained: nothing theozolith is importable
# from inside the session.
IDENTITY_KEYS = {
    "availableModels",
    "effortLevel",
    "enforceAvailableModels",
    "fallbackModel",
    "forceRemoteSettingsRefresh",
    "model",
    "modelOverrides",
    "policyHelper",
    "policyHelpers",
}
IDENTITY_ENV_KEYS = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
}


def identity_keys_in(document) -> list:
    """The identity-shaped keys of one PARSED settings document: top-level
    selection keys plus model/effort/endpoint-steering "env" entries. A
    nested "model" in an unrelated object (a statusline block, an MCP server
    config) or a credential env entry is not identity-shaped."""
    keys = set()
    for key in document:
        if isinstance(key, str) and key in IDENTITY_KEYS:
            keys.add(key)
    env = document.get("env")
    if isinstance(env, dict):
        for name in env:
            if not isinstance(name, str):
                continue
            if name in IDENTITY_ENV_KEYS or (
                name.startswith("ANTHROPIC_DEFAULT_") and name.endswith("_MODEL")
            ):
                keys.add(name)
    return sorted(keys)


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
    keys = []
    if source == "project_settings":
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except Exception:
            document = None  # unreadable or unparseable: unknowable, record
        if isinstance(document, dict):
            keys = identity_keys_in(document)
            if not keys:
                return 0
    with open(capture, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"source": source, "file_path": path, "keys": keys}) + "\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# The Stop hook helper (written by the harness into the scratch). The Stop
# hook fires once per completed main-agent turn (verified live on 2.1.232);
# the helper appends exactly one value-redacting record per firing — the
# applied effort level and nothing else — giving the harness a
# machine-readable applied-effort observation an organization cap would
# otherwise clamp silently in stream-json.
STOP_HOOK_SOURCE = '''\
"""theozolith Stop hook (ADR-0045): append one value-redacted applied-effort
record per completed turn for the harness session monitor."""
import json
import sys


def main() -> int:
    capture = sys.argv[1]
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = None
    record = {}
    if isinstance(payload, dict):
        effort = payload.get("effort")
        if isinstance(effort, dict) and isinstance(effort.get("level"), str):
            record["effort"] = effort["level"]
    with open(capture, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


class ClaudeSessionMonitor:
    """Fail-loud identity watcher over the ordinary one-shot task session
    (ADR-0045, best-effort doctrine).

    The task is never withheld and the session is never gated: the harness
    launches ``claude -p`` with the pointer prompt as always and feeds the
    growing transcript to this monitor, which reports a violation on a
    POSITIVE detection only —

    - a MAIN-agent assistant turn executing off the baked model (exact for
      a pinned ID, family for an alias; subagent turns carry
      ``parent_tool_use_id`` and are deliberately free — enforcement is
      main-agent-only), or an init announcement resolving off it;
    - an identity-relevant mid-session settings change recorded by the
      ConfigChange helper (the helper never blocks the change; the Run
      dies instead — organization policy is never resisted).

    Anything merely absent — no init event, no turn signal, a Stop hook
    that never fired — is a gap the harness records in evidence, never a
    failure (ADR-0045: gaps can happen). Known edge, documented: a SKILL
    that pins a different model runs on the main thread and will therefore
    fail the Run — route cheap/heavy work through subagents instead.

    Trust note: the capture files are same-user filesystem state and are
    therefore a fail-loud channel only — a forged record can only fail the
    forger's own Run, never make a detected mismatch pass."""

    def __init__(self, identity: BakedIdentity, hooks: MonitorHooks):
        self._identity = identity
        self._hooks = hooks
        self._violation = ""
        self._category = ""
        self.observed_model = ""

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
            if isinstance(seen, str) and seen and not model_matches(expected, seen):
                self._fail(
                    f"session initialized on {seen!r}, not the baked"
                    f" {expected!r} — the effective configuration selects a"
                    " different model",
                    CATEGORY_SUBSTITUTED,
                )
        elif event.get("type") == "assistant":
            if event.get("parent_tool_use_id"):
                return  # a subagent turn: free to run another model
            message = event.get("message")
            seen = message.get("model") if isinstance(message, dict) else None
            if isinstance(seen, str) and seen and seen != "<synthetic>":
                if model_matches(expected, seen):
                    self.observed_model = seen
                else:
                    self._fail(
                        f"a main-agent turn executed on {seen!r}, not the baked {expected!r}",
                        CATEGORY_SUBSTITUTED,
                    )

    def _config_change(self) -> str:
        """A violation string when the ConfigChange helper recorded an
        identity-relevant mid-session settings change; "" otherwise."""
        try:
            text = self._hooks.config_capture.read_text(encoding="utf-8")
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
                " longer runs under the checked configuration"
            )
        return ""

    def violation(self) -> tuple[str, str]:
        """(reason, category) once a positive detection exists; ("", "")
        while the session is clean."""
        if not self._violation:
            config = self._config_change()
            if config:
                self._fail(config, CATEGORY_CONFIG_CHANGED)
        return self._violation, self._category
