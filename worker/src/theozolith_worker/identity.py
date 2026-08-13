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
  *behaviorally*, with the Run's own credential: static re-checks of the
  image policy, a canary invocation proving an intruder ``--model`` still
  coerces to the pin (the allowlist binds), and a gated task session whose
  first turn is a no-op probe — the REAL task prompt is withheld until the
  session's init announcement and an executed probe turn (and, when effort is
  baked, the hook-captured applied effort) match the baked identity. After
  release the guard keeps watching: an identity-affecting change mid-session
  (a turn on another model, a drifted effort) kills the agent and invalidates
  the Run.

Anything unverifiable fails closed. Organization policy is never disabled,
replaced, or weakened to make a Run pass — a conflict is a failed build or a
failed preflight, with the source category named.

Everything here is deliberately value-redacting: errors and reports name
files, keys, models, efforts, and categories — never credential values,
tokens, or the contents of unrelated operator settings.
"""

from __future__ import annotations

import json
import re
import subprocess
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
# pin under provider pressure. The rest select or constrain model/effort.
IDENTITY_SETTING_KEYS = (
    "availableModels",
    "effortLevel",
    "enforceAvailableModels",
    "fallbackModel",
    "model",
    "policyHelper",
    "policyHelpers",
)

# Managed ``env`` entries that select a model or an effort.
IDENTITY_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
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
    shape of identity keys in the base managed file."""
    values: dict[str, object] = {
        "model": expected.model,
        "availableModels": [expected.model],
        "enforceAvailableModels": True,
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
    else, and any identity key in a drop-in, is still a conflict. Raises
    IdentityError on a malformed document (unknowable policy)."""
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
                    f"{path}: env '{name}' selects a model or effort over the baked identity"
                )
    return conflicts


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
    different is an image that looks pinned and binds nothing."""
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


# -- preflight ---------------------------------------------------------------


@dataclass(frozen=True)
class PreflightReport:
    """One preflight verdict: what was expected, what was proven, and — on
    failure — the mismatch category and an actionable, value-redacted detail.
    A failed preflight always means the real task prompt was never sent."""

    ok: bool
    expected_model: str
    expected_effort: str
    category: str = ""
    detail: str = ""
    cli_version: str = ""
    canary_model: str = ""  # what the widen canary actually executed

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
PREFLIGHT_SESSION_TIMEOUT = 240.0


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


def run_preflight(
    identity: BakedIdentity,
    *,
    binary: str = "claude",
    root: Path = Path("/"),
    scratch: Path,
    min_cli: tuple[int, int, int],
    run=subprocess.run,
    timeout: float = PREFLIGHT_SESSION_TIMEOUT,
) -> PreflightReport:
    """The pre-launch half of the runtime gate: static policy re-checks, the
    CLI version floor, and the widen canary. Runs with the same credential
    environment the task process will inherit (the run container's own).

    The in-session half — availability, exact/family resolution, and the
    applied effort, proven by the gated probe turn — is ``ClaudeSessionGuard``:
    only after BOTH halves pass does the harness release the real prompt."""

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

    # The CLI beside this harness must be new enough to enforce the config:
    # allowlist enforcement (2.1.175), completed family-alias substitution
    # (2.1.222), per-key managed env merge (2.1.223).
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

    # The widen canary: ask for an intruder model with the Run's own
    # credential and effective policy. Under an intact single-entry
    # allowlist every selection surface coerces to the pin (verified live);
    # an executed intruder — or anything else — means the effective policy
    # no longer binds the baked identity, whatever source changed it.
    intruder = intruder_for(identity.model)
    try:
        canary = run(
            [
                binary,
                "-p",
                CANARY_PROMPT,
                "--model",
                intruder,
                "--output-format",
                "stream-json",
                "--verbose",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(scratch),
        )
    except subprocess.TimeoutExpired:
        return failed(
            CATEGORY_TIMEOUT,
            f"the widen canary produced no verdict within {timeout:.0f}s",
            cli_version=version_raw,
        )
    except OSError as exc:
        return failed(
            CATEGORY_UNVERIFIABLE, f"cannot launch the widen canary: {exc}", cli_version=version_raw
        )
    signals = scan_stream_signals(canary.stdout or "")
    observed = signals.turn_models[-1] if signals.turn_models else ""
    if signals.is_error or canary.returncode != 0:
        note = signals.error_note or f"exit {canary.returncode}"
        return failed(
            CATEGORY_UNAVAILABLE,
            f"the canary session errored ({note}) — the baked model cannot be"
            " verified as available to this credential",
            cli_version=version_raw,
            canary_model=observed,
        )
    if not signals.turn_models:
        return failed(
            CATEGORY_UNVERIFIABLE,
            "the canary session executed no turn — the effective policy cannot be observed",
            cli_version=version_raw,
        )
    executed_intruder = any(seen == intruder for seen in signals.turn_models)
    off_pin = [seen for seen in signals.turn_models if not model_matches(identity.model, seen)]
    if executed_intruder or off_pin:
        return failed(
            CATEGORY_WIDENED if executed_intruder else CATEGORY_SUBSTITUTED,
            f"the canary asked for {intruder!r} and executed"
            f" {', '.join(signals.turn_models)} — the effective policy does"
            f" not coerce every selection to the baked {identity.model!r}"
            " (an organization/server policy, drop-in, or helper supersedes"
            " the image)",
            cli_version=version_raw,
            canary_model=observed,
        )
    return PreflightReport(
        ok=True,
        expected_model=identity.model,
        expected_effort=identity.effort,
        cli_version=version_raw,
        canary_model=observed,
    )


# -- the gated session guard --------------------------------------------------

GUARD_WAIT = "wait"
GUARD_RELEASE = "release"
GUARD_KILL = "kill"

# The in-session no-op probe turns (never the task). The tool variant makes
# the CLI fire the PostToolUse hook whose payload carries the applied effort
# — the one machine-readable observation of the effective effort in headless
# mode (an organization effort cap clamps silently in stream-json).
PROBE_PROMPT = (
    "Preflight check. Reply with exactly: OK. Do not use any tools and do not read any files."
)
PROBE_PROMPT_WITH_TOOL = (
    "Preflight check. Run the Bash tool with the exact command 'true', then"
    " reply with exactly: OK. Do not read any files."
)


@dataclass(frozen=True)
class GuardDecision:
    action: str  # GUARD_WAIT | GUARD_RELEASE | GUARD_KILL
    reason: str = ""


class ClaudeSessionGuard:
    """Line-by-line identity gate over a gated ``claude -p --input-format
    stream-json`` session.

    Before release: the init announcement must resolve to the baked model
    and the probe turn must EXECUTE on it (exact for a pinned ID, family for
    an alias); when effort is baked, the hook-captured applied effort must
    equal it. Only then does the harness send the real task prompt. After
    release: any executed turn off the baked model, or a drifted applied
    effort, kills the session — a mid-run policy change invalidates the Run
    immediately instead of being discovered post-hoc."""

    def __init__(self, identity: BakedIdentity, effort_capture: Path | None):
        self._identity = identity
        self._capture = effort_capture if identity.effort else None
        self._init_ok = False
        self._turn_ok = False
        self._violation = ""
        self.observed_model = ""
        self.observed_effort = ""

    @property
    def probe_prompt(self) -> str:
        return PROBE_PROMPT_WITH_TOOL if self._capture else PROBE_PROMPT

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

    def _fail(self, reason: str) -> None:
        if not self._violation:
            self._violation = reason

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
                        " model"
                    )
        elif event.get("type") == "assistant":
            message = event.get("message")
            seen = message.get("model") if isinstance(message, dict) else None
            if isinstance(seen, str) and seen and seen != "<synthetic>":
                if model_matches(expected, seen):
                    self.observed_model = seen
                    self._turn_ok = True
                else:
                    self._fail(f"a turn executed on {seen!r}, not the baked {expected!r}")
        elif event.get("type") == "result" and event.get("is_error") is True:
            subtype = event.get("subtype")
            note = subtype if isinstance(subtype, str) else "error"
            if not self._turn_ok:
                self._fail(
                    f"the probe turn errored ({note}) — the baked model cannot"
                    " be verified as available to this credential"
                )

    def _effort_state(self) -> str:
        """ "ok" | "wait" | a violation string, from the hook capture."""
        if self._capture is None:
            return "ok"
        try:
            data = json.loads(self._capture.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "wait"
        level = ""
        if isinstance(data, dict):
            effort = data.get("effort")
            if isinstance(effort, dict) and isinstance(effort.get("level"), str):
                level = effort["level"]
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

    def decision(self) -> GuardDecision:
        if self._violation:
            return GuardDecision(GUARD_KILL, self._violation)
        effort = self._effort_state()
        if effort not in ("ok", "wait"):
            self._fail(effort)
            return GuardDecision(GUARD_KILL, self._violation)
        if self._init_ok and self._turn_ok and effort == "ok":
            return GuardDecision(GUARD_RELEASE)
        return GuardDecision(GUARD_WAIT)
