"""Codex-specific identity mechanics (ADR-0052's PROBE + STATIC doctrine).

The Codex CLI has no managed-policy configuration tier: its only config is
``$CODEX_HOME/config.toml``, and the CLI *writes* into ``CODEX_HOME`` at
runtime (auth.json, sessions/, sqlite state). The identity design follows
from that single fact:

- The baked identity source of truth is theozolith-owned and root-owned:
  the adapter-independent well-known files plus
  ``etc/theozolith/codex/config.toml`` (model + model_reasoning_effort).
  Nothing is baked under ``~/.codex`` — image bytes must not be
  runtime-writable, and the CLI needs its home writable.
- Every session (probe and Run alike) gets a FRESH throwaway ``CODEX_HOME``
  assembled by ``assemble_codex_home``: the baked config copied
  byte-for-byte, the plan-auth credential written 0600 from the delivered
  ``CODEX_AUTH_JSON`` environment value. Session state dies with the
  container — no cross-Run persistence channel.
- The observation channel is the CLI's own session rollout journal
  (``CODEX_HOME/sessions/**/rollout-*.jsonl``): its ``turn_context``
  records carry the model and effort each turn ran under (verified live on
  codex-cli 0.150.0 — the baked config values flow through verbatim). The
  ``codex exec --json`` event stream itself announces NO model
  (thread.started carries only a thread id), so the rollout is the model
  check for the setup dry-run and the post-exit evidence source for Runs.
- PROBE + STATIC only (the user-decided doctrine): one dry-run probe per
  driver boot, free static checks per Run, and a BENIGN session observer —
  no live mid-run kill. Undetected steering is a recorded gap, matching
  ADR-0045's accepted-gaps pattern at codex's weaker enforcement ceiling.

Effort note: ``turn_context.effort`` echoes the configured value and is not
proven to be post-clamp, so no (model, effort) pair is positively known
enforceable yet — the capability table below is EMPTY and any nonempty
effort is rejected at config load and at build (spike #76 S7 adds entries
as models are proven). The machinery (materialize, static checks, probe,
rollout read) is effort-ready for the day the table gains its first row.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

from theozolith_worker.identity import (
    CATEGORY_CLI_TOO_OLD,
    CATEGORY_INCONSISTENT,
    CATEGORY_PAIR_INVALID,
    CATEGORY_POLICY_CONFLICT,
    CATEGORY_SUBSTITUTED,
    CATEGORY_TIMEOUT,
    CATEGORY_UNAVAILABLE,
    CATEGORY_UNVERIFIABLE,
    WELL_KNOWN_EFFORT_FILE,
    WELL_KNOWN_MODEL_FILE,
    BakedIdentity,
    IdentityError,
    MonitorHooks,
    PreflightReport,
)

# The theozolith-owned baked selection config: copied into every session's
# throwaway CODEX_HOME as config.toml. Root-owned image bytes, deliberately
# NOT under /home/ozolith/.codex (runtime-writable) and NOT /etc/codex (the
# CLI reads no such path — a name that implied it would mislead).
BAKED_CONFIG_FILE = "etc/theozolith/codex/config.toml"

# config.toml keys that select or steer the executing model. The refusal
# list for a pre-existing baked config (materialize never overwrites
# operator content) and the static checks' foreign-key scan.
CODEX_CONFIG_IDENTITY_KEYS = (
    "model",
    "model_provider",
    "model_providers",
    "model_reasoning_effort",
    "profile",
    "profiles",
)

# config.toml keys that redefine WHICH working-tree files the CLI auto-loads
# as instructions (codex's project-doc discovery: AGENTS.override.md, then
# AGENTS.md, then any configured fallback names). The Reviewer's judge
# isolation removes a FIXED name set from the review checkout
# (reviewer._neutralize_agent_config, ADR-0008/#82); a baked config
# extending that discovery would reopen the involuntary instruction channel
# under a name the driver does not know. Refused wherever the baked config
# is read — the materialize conflict scan, the per-Run static checks, and
# the identity reader (model-less images included) — so the fixed set
# provably IS the effective set.
CODEX_CONFIG_INSTRUCTION_KEYS = ("project_doc_fallback_filenames",)

# Process-environment variables that would steer a codex session over the
# baked identity: an externally-delivered CODEX_HOME points the CLI at a
# foreign config+auth tree; OPENAI_BASE_URL repoints the provider;
# OPENAI_API_KEY flips the auth mode away from the delivered plan
# credential. CODEX_AUTH_JSON (the credential itself) is deliberately NOT
# here. The adapter's own prepare()-assembled CODEX_HOME rides the agent
# process env only, applied after these checks scan the harness environment.
CODEX_IDENTITY_ENV_KEYS = (
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)

# The effort vocabulary the CLI's config accepts (model_reasoning_effort).
CODEX_EFFORT_VOCABULARY = frozenset({"minimal", "low", "medium", "high", "xhigh"})

# Longest-prefix (model -> proven efforts) capability table, mirroring
# identity.py's. EMPTY on purpose: turn_context.effort echoes the config
# rather than a proven post-clamp value, so until spike #76 S7 positively
# proves a model honors a level, no pair is enforceable and any nonempty
# effort is rejected (bake the model alone). Adding a row here is the
# whole unlock.
_PINNED_EFFORT_CAPABILITY: tuple[tuple[str, frozenset[str]], ...] = ()


def effort_capability(model: str) -> frozenset[str] | None:
    """The effort values ``model`` provably honors; None when unknown
    (which today is every model — see the table comment)."""
    for prefix, capability in _PINNED_EFFORT_CAPABILITY:
        if model == prefix or model.startswith(prefix + "-"):
            return capability
    return None


def pair_error(model: str, effort: str) -> str:
    """Why ``(model, effort)`` is not an enforceable pair; "" when it is.
    Same contract as identity.pair_error, codex capability."""
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
            f"model {model!r} has no proven effort capability for the codex"
            f" adapter — the rollout journal echoes the configured effort"
            " rather than a post-clamp value, so enforcement cannot be"
            f' proven; bake the model alone (effort = "") until spike #76 S7'
            " evidence adds this model to the capability table (ADR-0052)"
        )
    if effort not in capability:
        known = ", ".join(sorted(capability)) or "(none)"
        return (
            f"model {model!r} is not proven to honor effort {effort!r}"
            f" (proven: {known}) — an unproven level is an unenforced"
            " identity (ADR-0052)"
        )
    return ""


# -- the baked identity, as an image declares it ------------------------------


def _load_baked_config(root: Path) -> dict | None:
    """The parsed baked config.toml, None when absent, IdentityError when
    unreadable or unparseable (an unknowable selection)."""
    path = root / BAKED_CONFIG_FILE
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise IdentityError(f"{path}: unreadable baked codex config ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise IdentityError(
            f"{path}: not valid TOML ({exc}) — a malformed baked config makes"
            " the effective selection unknowable"
        ) from exc


def _instruction_key_error(path: Path, document: dict) -> str:
    """Why the baked config would widen the CLI's instruction-file
    discovery; "" when it would not. The judge-isolation boundary removes a
    fixed name set from review checkouts, so these keys are refused no
    matter what they are set to — an empty list today is a hostile or
    mistaken list after one edit, and the driver cannot see image bytes to
    follow along."""
    present = [key for key in CODEX_CONFIG_INSTRUCTION_KEYS if key in document]
    if present:
        return (
            f"{path}: redefines the CLI's instruction-file discovery"
            f" ({', '.join(present)}) — the review judge-isolation boundary"
            " removes a fixed instruction-file name set, and a configured"
            " fallback name would reopen the involuntary instruction channel"
            " (#82); remove the key"
        )
    return ""


def verify_baked_config(root: Path, expected: BakedIdentity) -> str:
    """Why the baked config does not carry ``expected``; "" when it does.
    The well-known files declare the identity; the baked config.toml is what
    SELECTS it (copied into every session's CODEX_HOME). A declared identity
    whose selection is missing or different is an image that looks
    configured and selects nothing."""
    try:
        document = _load_baked_config(root)
    except IdentityError as exc:
        return str(exc)
    path = root / BAKED_CONFIG_FILE
    if document is None:
        return (
            f"{path} is missing — the baked identity {expected.model!r} has no"
            " selection configuration"
        )
    if document.get("model") != expected.model:
        return f"{path}: 'model' does not carry the baked identity {expected.model!r}"
    if expected.effort and document.get("model_reasoning_effort") != expected.effort:
        return f"{path}: 'model_reasoning_effort' does not pin the baked effort {expected.effort!r}"
    foreign = [
        key
        for key in CODEX_CONFIG_IDENTITY_KEYS
        if key in document and key not in ("model", "model_reasoning_effort")
    ]
    if foreign:
        return (
            f"{path}: carries steering keys beyond the materialized selection"
            f" ({', '.join(foreign)}) — the effective identity is not the baked one"
        )
    instruction = _instruction_key_error(path, document)
    if instruction:
        return instruction
    if "model_reasoning_effort" in document and not expected.effort:
        return (
            f"{path}: pins an effort the well-known files do not declare — half-declared identity"
        )
    return ""


def read_baked_identity(root: Path) -> BakedIdentity | None:
    """The identity this filesystem was baked with, or None when it carries
    none. Inconsistent declarations raise IdentityError — a half-declared
    identity must never silently run unchecked (same contract as the Claude
    reader, codex selection surface)."""
    model_file = root / WELL_KNOWN_MODEL_FILE
    effort_file = root / WELL_KNOWN_EFFORT_FILE
    try:
        model = model_file.read_text(encoding="utf-8").strip() if model_file.is_file() else ""
        effort = effort_file.read_text(encoding="utf-8").strip() if effort_file.is_file() else ""
    except OSError as exc:
        raise IdentityError(f"unreadable baked-identity declaration: {exc}") from exc
    if model_file.is_file() and not model:
        raise IdentityError(f"{model_file} exists but is empty — corrupt baked identity")
    if effort and not model:
        raise IdentityError(
            f"{effort_file} declares effort {effort!r} but {model_file} declares"
            " no model — effort without a model is not an identity"
        )
    if not model:
        document = _load_baked_config(root)
        if document and any(key in document for key in CODEX_CONFIG_IDENTITY_KEYS):
            raise IdentityError(
                f"{root / BAKED_CONFIG_FILE} carries selection keys but the"
                " image declares no baked identity (missing " + WELL_KNOWN_MODEL_FILE + ")"
            )
        if document:
            # The instruction-discovery refusal holds on model-less images
            # too: this reader is the one check every baked_identity() call
            # runs, identity or not, so a config widening project-doc
            # discovery can never ride an unidentified image past it.
            instruction = _instruction_key_error(root / BAKED_CONFIG_FILE, document)
            if instruction:
                raise IdentityError(instruction)
        return None
    if "\n" in model or "\n" in effort:
        raise IdentityError(
            f"{model_file} / {effort_file} carry multiple lines — corrupt baked identity"
        )
    inconsistency = verify_baked_config(root, BakedIdentity(model=model, effort=effort))
    if inconsistency:
        raise IdentityError(inconsistency)
    return BakedIdentity(model=model, effort=effort)


# -- the per-session CODEX_HOME -----------------------------------------------


def assemble_codex_home(home: Path, *, root: Path, environ: Mapping[str, str]) -> Path:
    """The prepare()/preflight sequence: a fresh 0700 ``CODEX_HOME`` holding
    the baked config (byte-for-byte) and the plan-auth credential as a 0600
    ``auth.json`` written from the delivered ``CODEX_AUTH_JSON`` value.
    Value-free errors: the credential never appears in a message."""
    credential = environ.get("CODEX_AUTH_JSON", "")
    if not credential:
        raise IdentityError(
            "CODEX_AUTH_JSON is not set — the codex adapter needs the"
            " ChatGPT-plan auth.json document delivered through the worker"
            " type's [secrets] slot (ADR-0052)"
        )
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    baked = root / BAKED_CONFIG_FILE
    if baked.is_file():
        (home / "config.toml").write_bytes(baked.read_bytes())
    auth = home / "auth.json"
    auth.touch(mode=0o600, exist_ok=True)
    auth.chmod(0o600)
    auth.write_text(credential, encoding="utf-8")
    return home


# -- the rollout journal (the observation channel) ----------------------------


def read_rollout_turn_context(home: Path) -> tuple[str, str]:
    """The (model, effort) of the LAST ``turn_context`` record in the
    session rollout journal under ``home`` — what the session actually ran
    under (the CLI writes one per turn; verified live on 0.150.0). ("", "")
    when no journal or no record exists: a gap for the per-Run observer, a
    strict failure for the dry-run probe."""
    sessions = home / "sessions"
    model, effort = "", ""
    if not sessions.is_dir():
        return model, effort
    for path in sorted(sessions.rglob("rollout-*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "turn_context":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                seen_model = payload.get("model")
                seen_effort = payload.get("effort")
                if isinstance(seen_model, str) and seen_model:
                    model = seen_model
                    effort = seen_effort if isinstance(seen_effort, str) else ""
    return model, effort


def scan_stream_errors(text: str) -> str:
    """The first fatal signal in a ``codex exec --json`` stream: a
    ``turn.failed`` or ``error`` event's message, "" when none. The stream
    carries no model signal (the rollout journal does), so errors are all
    the probe reads from it."""
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
        if event.get("type") in ("error", "turn.failed"):
            message = event.get("message")
            if not isinstance(message, str) or not message:
                error = event.get("error")
                message = (
                    error.get("message", "") if isinstance(error, dict) else ""
                ) or event.get("type", "error")
            return str(message)
    return ""


# -- the free static checks (every Run) ---------------------------------------


def scan_process_environment(environ: Mapping[str, str]) -> list[str]:
    """Identity-affecting variables in the run container's own process
    environment, as conflict strings naming the variable (never the value)."""
    return [
        f"process environment variable '{name}' selects a provider endpoint,"
        " auth mode, or config home — it would steer the session over the"
        " baked identity"
        for name in sorted(environ)
        if name in CODEX_IDENTITY_ENV_KEYS
    ]


def static_identity_report(
    identity: BakedIdentity,
    *,
    root: Path = Path("/"),
    environ: Mapping[str, str] | None = None,
) -> PreflightReport:
    """The zero-cost identity checks before every launch: the baked config
    still selects the declared identity and carries no foreign steering
    keys, the pair is proven, and the process environment is clean. File
    reads only — no sessions, no tokens."""
    environ = os.environ if environ is None else environ

    def failed(category: str, detail: str) -> PreflightReport:
        return PreflightReport(
            ok=False,
            expected_model=identity.model,
            expected_effort=identity.effort,
            category=category,
            detail=detail,
        )

    inconsistency = verify_baked_config(root, identity)
    if inconsistency:
        return failed(CATEGORY_INCONSISTENT, inconsistency)
    pair = pair_error(identity.model, identity.effort)
    if pair:
        return failed(CATEGORY_PAIR_INVALID, pair)
    env_conflicts = scan_process_environment(environ)
    if env_conflicts:
        return failed(CATEGORY_POLICY_CONFLICT, "; ".join(env_conflicts))
    return PreflightReport(ok=True, expected_model=identity.model, expected_effort=identity.effort)


# -- the setup dry-run ---------------------------------------------------------

PROBE_PROMPT = (
    "Preflight check. Reply with exactly: OK. Do not use any tools and do not read any files."
)
PREFLIGHT_SESSION_TIMEOUT = 240.0

_VERSION_RE = re.compile(r"codex-cli (\d+)\.(\d+)\.(\d+)")


def run_preflight(
    identity: BakedIdentity,
    *,
    binary: str = "codex",
    root: Path = Path("/"),
    scratch: Path,
    min_cli: tuple[int, int, int],
    run=subprocess.run,
    environ: Mapping[str, str] | None = None,
    timeout: float = PREFLIGHT_SESSION_TIMEOUT,
) -> PreflightReport:
    """The setup dry-run (once per driver boot, never per Run): the static
    checks, the CLI version floor, then one neutral ``codex exec`` probe in
    a throwaway CODEX_HOME whose post-exit rollout journal must attribute
    the session to the baked model. Strict where the per-Run observer is
    lenient: a probe with no rollout signal means the observation channel
    itself is broken, and setup is the time to learn that."""
    environ = os.environ if environ is None else environ
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

    try:
        probe = run([binary, "--version"], capture_output=True, text=True, timeout=60, check=False)
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
            f"codex {version_raw} predates the behavior this machinery relies"
            f" on (the --json event schema, the rollout turn_context journal,"
            f" config-key binding in exec mode; needs >= {floor})",
            cli_version=version_raw,
        )

    try:
        home = assemble_codex_home(scratch / "codex-home", root=root, environ=environ)
    except (IdentityError, OSError) as exc:
        return failed(CATEGORY_UNVERIFIABLE, str(exc), cli_version=version_raw)
    argv = [
        binary,
        "exec",
        PROBE_PROMPT,
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
    ]
    try:
        proc = run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(scratch),
            env={**environ, "CODEX_HOME": str(home)},
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
    if proc.returncode != 0:
        note = scan_stream_errors(proc.stdout or "") or f"exit {proc.returncode}"
        return failed(
            CATEGORY_UNAVAILABLE,
            f"the identity probe session errored ({note}) — the baked model"
            " cannot be verified as available to this credential",
            cli_version=version_raw,
        )
    probe_model, probe_effort = read_rollout_turn_context(home)
    if not probe_model:
        return failed(
            CATEGORY_UNVERIFIABLE,
            "the identity probe left no rollout turn_context record — the"
            " observation channel the per-Run evidence relies on shows nothing",
            cli_version=version_raw,
        )
    if probe_model != identity.model:
        return failed(
            CATEGORY_SUBSTITUTED,
            f"the identity probe ran on {probe_model!r}, not the baked"
            f" {identity.model!r} — the effective configuration selects a"
            " different model",
            cli_version=version_raw,
            probe_model=probe_model,
        )
    return PreflightReport(
        ok=True,
        expected_model=identity.model,
        expected_effort=identity.effort,
        cli_version=version_raw,
        probe_model=probe_model,
        probe_effort=probe_effort if identity.effort else "",
    )


# -- the benign per-Run observer ----------------------------------------------


class CodexSessionMonitor:
    """The PROBE + STATIC doctrine's per-Run observer (ADR-0052): records
    the observed model for evidence and NEVER reports a violation — codex
    Runs are not killed mid-session, and a detected post-exit mismatch
    surfaces through the evidence record (stream_stats has no model source;
    the rollout journal read here is it).

    ``observe`` is a no-op (the --json stream carries no model signal);
    the final ``violation()`` call — the harness makes one after process
    exit — reads the session rollout journal from the throwaway CODEX_HOME
    and populates ``observed_model``/``observed_effort``. Always returns
    ("", ""): benign by doctrine, not by accident."""

    def __init__(self, identity: BakedIdentity, hooks: MonitorHooks, home: Path | None):
        self._identity = identity
        self._hooks = hooks
        self._home = home
        self.observed_model = ""
        self.observed_effort = ""

    def observe(self, line: str) -> None:
        return

    def violation(self) -> tuple[str, str]:
        if self._home is not None:
            model, effort = read_rollout_turn_context(self._home)
            if model:
                self.observed_model = model
                self.observed_effort = effort
        return "", ""
