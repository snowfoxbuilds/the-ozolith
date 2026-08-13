"""Agent adapters: the vendor-specific slice of a run-container image (ADR-0044).

A product component **beside** the harness (NODE-SUBSTRATE Components), not
under it: the harness imports adapters downward, and the drivers read
counters through this module without reaching into ``harness/`` (ADR-0020
layering). An adapter is the per-worker-type variable the immutable harness
invokes: it knows three things about its agent CLI — the headless one-shot
argv to invoke (a constant-size pointer prompt at invocation — the task
content stays in the mounted job directory; completion is process exit,
ADR-0019 as amended), how to read counters and token usage out of the
structured output stream, and which session outputs to copy into the job
directory. One adapter ships per image; M2 ships Claude only.

ADR-0045 adds the capability half: an adapter declares which models and
reasoning-effort values it can map, and materializes them into its agent
CLI's native configuration at derived-image build time (the
``theozolith-adapter`` console script, invoked by the setup instruction the
Control Node synthesizes into the wire recipe). Model and effort are never
selected at invocation time and never delivered through the run container's
environment (the managed-settings ``env`` block the materializer writes is
image bytes — part of the baked identity, not an invocation surface).
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from theozolith_worker import identity as identity_mod
from theozolith_worker.identity import (
    BakedIdentity,
    ClaudeSessionGuard,
    IdentityError,
    PreflightReport,
)
from theozolith_worker.jobdir import MODE_REVIEW, VERDICT_FILE, Manifest


class AgentAdapterError(RuntimeError):
    """No adapter for the requested agent."""


# Model classification (ADR-0045). Shape-based, not a closed list: the agent
# CLI passes full model IDs through to the provider unchecked, so a closed
# list would need a product release per model launch. ``alias`` is mappable
# but linted control-side (pin the most-dated provider ID over floating
# aliases); ``unmappable`` fails the config load and the derived-image build.
MODEL_PINNED = "pinned"
MODEL_ALIAS = "alias"
MODEL_UNMAPPABLE = "unmappable"

# Materialization scopes (ADR-0045). ``managed`` is the driver run image:
# native config lands where nothing in a workspace checkout can override it.
# ``interactive`` is the driverless Flight Deck image: only the well-known
# plain-text files are written — never anything under /home/ozolith/.claude,
# which the per-instance claude-state volume shadows on first mount
# (ADR-0043) — and the baked start command consumes them.
SCOPE_MANAGED = "managed"
SCOPE_INTERACTIVE = "interactive"
MATERIALIZE_SCOPES = (SCOPE_MANAGED, SCOPE_INTERACTIVE)

# The adapter-independent inspection surface: what any derived image was
# baked with, readable via ``docker run --rm <tag> cat /etc/theozolith/model``.
# Root-owned on purpose — a session can read its baked default, never
# rewrite it. At runtime these same files are the harness's declaration of
# the identity the preflight gate must prove effective (identity.py).
WELL_KNOWN_MODEL_FILE = identity_mod.WELL_KNOWN_MODEL_FILE
WELL_KNOWN_EFFORT_FILE = identity_mod.WELL_KNOWN_EFFORT_FILE


@dataclass(frozen=True)
class StreamStats:
    """Counters read from the structured output stream (ADR-0019).

    ``tokens`` is None until the stream carries usage — this is how an
    adapter that reports usage closes ADR-0018's null-token gap.

    ``model`` is the *observed* model — what the session actually executed,
    reconciled from every model signal the stream carries (the session-init
    announcement, the models on real assistant turns, and the final usage
    records). With selection baked into the image (ADR-0045) this is
    telemetry's model source; the config-time value is recoverable from the
    run-image tag.

    ``model_note`` is "" when every signal agrees on one executed model.
    Anything else — the session drifting off its announced model, multiple
    models executing turns, usage records that contradict the turns, or a
    stream with no turn-level signal at all — is stated here instead of being
    flattened into a single misleading (or silently empty) ``model`` value.
    """

    tool_calls: int = 0
    tokens: int | None = None
    model: str = ""
    model_note: str = ""


class AgentAdapter(Protocol):
    name: str
    # One human-readable line describing what classify_model accepts — quoted
    # in the "cannot map model" errors control and the build emit (ADR-0045).
    model_shapes: str

    def command(self, manifest: Manifest, prompt: str) -> list[str]:
        """The headless one-shot argv; ``prompt`` is the harness's
        constant-size pointer at the mounted task file, never the task."""
        ...

    def prepare(self, workdir: Path, job: Path) -> dict[str, str]:
        """Extra environment for the agent process."""
        ...

    def collect(self, workdir: Path, job: Path, mode: str) -> None:
        """Copy mode-specific session outputs into ``output/``."""
        ...

    def stream_stats(self, transcript: Path) -> StreamStats:
        """Tool-call and token counters from the structured output stream."""
        ...

    def classify_model(self, model: str) -> str:
        """``MODEL_PINNED`` | ``MODEL_ALIAS`` | ``MODEL_UNMAPPABLE`` for a
        worker-type ``model`` value (ADR-0045)."""
        ...

    def mappable_efforts(self) -> frozenset[str]:
        """The reasoning-effort values this adapter can materialize into
        native config (ADR-0045); empty when the agent CLI has none."""
        ...

    def pair_error(self, model: str, effort: str) -> str:
        """Why ``(model, effort)`` is not an enforceable pair, or "" when it
        is (ADR-0045 pair validation): effort must be provably honored by the
        specific model — a value the agent CLI silently clamps or ignores on
        that model is rejected, as is any effort on a model whose capability
        is unknown. effort "" (the model's own default) is always valid."""
        ...

    def materialize(self, model: str, effort: str, *, root: Path, scope: str) -> list[Path]:
        """Write the native configuration for ``model``/``effort`` under
        ``root`` (the image filesystem; ``/`` in a real build) and return the
        paths written. Values must already be mappable — the CLI validates
        before calling."""
        ...

    def verify_enforceable(self) -> str:
        """Prove the agent CLI installed *beside this adapter* actually
        enforces what ``materialize`` writes, returning the CLI version for
        the build log. Raises ``AgentAdapterError`` when it cannot — a config
        the CLI would silently ignore is a baked identity that does not bind,
        so the build must fail, not proceed (ADR-0045)."""
        ...

    # -- the runtime gate (ADR-0045 fail-closed amendment) -------------------

    def baked_identity(self, root: Path) -> BakedIdentity | None:
        """The identity this filesystem was baked with, or None when it
        carries none (a model-less worker type runs ungated). Raises
        ``AgentAdapterError`` on an inconsistent or corrupt declaration."""
        ...

    def preflight(self, identity: BakedIdentity, *, root: Path, scratch: Path) -> PreflightReport:
        """The pre-launch runtime gate, with the Run's own credential and
        effective policy. A failed report means the real task prompt must
        never be sent."""
        ...

    def guarded_command(self, manifest: Manifest, effort_capture: Path | None) -> list[str]:
        """The gated session argv: input arrives over stdin so the harness
        can withhold the real task prompt until the session's identity is
        verified in-process."""
        ...

    def session_guard(self, identity: BakedIdentity, effort_capture: Path | None):
        """The line-by-line gate + monitor for a gated session (see
        ``identity.ClaudeSessionGuard`` for the contract)."""
        ...


class ClaudeAdapter:
    """Drives the Claude Code CLI. All Claude-specific mechanics live here.

    The session runs headless: ``claude -p`` with the pointer prompt as the
    argument and ``--output-format stream-json`` (which requires
    ``--verbose`` in one-shot mode), so stdout is a line-per-event JSON
    stream. That stream is the transcript, and its usage records supply the
    token counts progress telemetry carries.
    """

    name = "claude"

    # The model-family aliases the availableModels allowlist can hold: each
    # expands to the newest model of exactly one family, so a single-entry
    # allowlist still binds the image to that family (verified live, 2.1.231).
    # Floating, though — the control-side lint warns on them (ADR-0045).
    # "default" and "opusplan" are deliberately NOT here, although the CLI
    # accepts both as selections: "default" floats with the account tier and
    # a session pinned to it fails outright under an allowlist; "opusplan" is
    # a two-model mode that degrades to plain Sonnet under enforcement — both
    # verified live. Neither can name the single model ADR-0045 bakes, so
    # both classify unmappable and fail the config load and the build.
    ALIASES = frozenset({"sonnet", "opus", "haiku", "fable"})
    # A full provider model ID (dated or not: current-generation IDs ship
    # without a dated variant). Claude Code passes these through unchecked.
    _PINNED = re.compile(r"claude-[a-z0-9.-]+")
    # The effort values CLAUDE_CODE_EFFORT_LEVEL (and settings "effortLevel")
    # accept; session-only levels (e.g. max) are deliberately absent — a
    # build asking for one fails, which is ADR-0045's contract.
    EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
    # Managed settings: the CLI's admin policy layer. A bare "model" here is
    # only the session default — enforcement is the availableModels allowlist
    # with enforceAvailableModels, which constrains every selection surface
    # (--model, /model, ANTHROPIC_MODEL, settings files, subagent frontmatter,
    # CLAUDE_CODE_SUBAGENT_MODEL), and the managed "env" block, whose
    # CLAUDE_CODE_EFFORT_LEVEL overrides /effort, --effort, the process
    # environment, and any settings-file effortLevel (all verified live).
    MANAGED_SETTINGS = identity_mod.MANAGED_SETTINGS_FILE
    # The oldest Claude Code whose exact documented behavior this adapter
    # relies on: availableModels/enforceAvailableModels shipped in 2.1.175,
    # family-alias substitution completed in 2.1.222, and the per-key managed
    # ``env`` merge — without which a server-managed org env block would
    # silently displace the baked CLAUDE_CODE_EFFORT_LEVEL pin — shipped in
    # 2.1.223. Behavior verified live on 2.1.231. An older CLI would silently
    # ignore or lose parts of the baked identity — the exact hole
    # verify_enforceable() and the runtime preflight exist to close.
    MIN_ENFORCING_CLI = (2, 1, 223)
    model_shapes = "full claude-* model IDs, or the family aliases fable/haiku/opus/sonnet"

    def __init__(self, binary: str = "claude"):
        self._binary = binary

    def classify_model(self, model: str) -> str:
        if model in self.ALIASES:
            return MODEL_ALIAS
        if self._PINNED.fullmatch(model):
            return MODEL_PINNED
        return MODEL_UNMAPPABLE

    def mappable_efforts(self) -> frozenset[str]:
        return self.EFFORTS

    def pair_error(self, model: str, effort: str) -> str:
        return identity_mod.pair_error(model, effort)

    def _cli_version(self) -> str:
        """``claude --version`` output from the CLI beside this adapter."""
        try:
            probe = subprocess.run(
                [self._binary, "--version"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentAdapterError(
                f"cannot run '{self._binary} --version': {exc} — the base image"
                " must ship the Claude Code CLI beside theozolith-adapter (ADR-0045)"
            ) from exc
        if probe.returncode != 0:
            raise AgentAdapterError(
                f"'{self._binary} --version' exited {probe.returncode}:"
                f" {probe.stderr.strip() or probe.stdout.strip()}"
            )
        return probe.stdout.strip()

    def verify_enforceable(self) -> str:
        """Fail unless the in-image CLI is new enough to ENFORCE the managed
        config materialize() writes. availableModels/enforceAvailableModels
        are ignored by CLIs older than MIN_ENFORCING_CLI — the baked identity
        would look pinned and bind nothing, the one failure mode worse than a
        failed build."""
        raw = self._cli_version()
        match = re.match(r"(\d+)\.(\d+)\.(\d+)", raw)
        if not match:
            raise AgentAdapterError(
                f"cannot parse a version from '{self._binary} --version' output {raw!r}"
            )
        version = tuple(int(part) for part in match.groups())
        if version < self.MIN_ENFORCING_CLI:
            floor = ".".join(str(part) for part in self.MIN_ENFORCING_CLI)
            raise AgentAdapterError(
                f"Claude Code {raw} predates the model-enforcement settings"
                f" (availableModels/enforceAvailableModels, CLI >= {floor}) — it"
                " would silently ignore the baked restriction; bump the worker"
                " type's base to a release with a newer CLI (ADR-0045)"
            )
        return raw

    def materialize(self, model: str, effort: str, *, root: Path, scope: str) -> list[Path]:
        """Bake ``model``/``effort`` into the image filesystem under ``root``.

        ``managed`` (driver run images) first proves the managed tier under
        ``root`` cannot supersede what it is about to write: the base
        ``managed-settings.json`` and every ``managed-settings.d/*.json``
        drop-in are inspected in Claude Code's documented merge order, and a
        malformed document, a ``policyHelper``/``policyHelpers``, or any
        identity-affecting key (model, availableModels,
        enforceAvailableModels, fallbackModel, effortLevel, or a
        model/effort-selecting ``env`` entry) FAILS THE BUILD naming the file
        and key — operator policy is never silently deleted or overwritten
        (ADR-0045 fail-closed amendment). It then writes the well-known files
        and merges the enforcement keys into the base settings JSON:
        ``model`` is only the session default, so the identity is held by a
        single-entry ``availableModels`` allowlist with
        ``enforceAvailableModels`` (constraining every model-selection
        surface) and, for effort, ``env.CLAUDE_CODE_EFFORT_LEVEL`` (which
        overrides /effort, --effort, the process environment, and any
        settings-file effortLevel) beside the ``effortLevel`` default.
        Unrelated operator keys — including foreign ``env`` entries — survive
        the merge untouched.

        ``interactive`` (driverless Flight Deck images) writes ONLY
        ``/etc/theozolith/model``: no managed settings (the deck may switch
        models in-session, ADR-0045 §Flight Deck), nothing under
        ``/home/ozolith/.claude`` (state-volume shadowing, ADR-0043), and no
        effort — driverless effort is rejected upstream until a runtime
        consumer exists, and this refuses it too."""
        if scope == SCOPE_INTERACTIVE and effort:
            raise AgentAdapterError(
                "effort is not materializable at interactive scope — driverless"
                " (Flight Deck) worker types have no effort consumer (ADR-0045)"
            )
        pair = identity_mod.pair_error(model, effort)
        if pair:
            raise AgentAdapterError(pair)
        if scope == SCOPE_MANAGED:
            try:
                conflicts = identity_mod.scan_managed_conflicts(root, expected=None)
            except IdentityError as exc:
                raise AgentAdapterError(str(exc)) from exc
            if conflicts:
                raise AgentAdapterError(
                    "existing managed settings would supersede the baked"
                    " identity — refusing to overwrite operator policy;"
                    " remove or relocate the conflicting keys: " + "; ".join(conflicts)
                )
        written: list[Path] = []
        pairs = [(WELL_KNOWN_MODEL_FILE, model), (WELL_KNOWN_EFFORT_FILE, effort)]
        for relpath, value in pairs:
            if not value:
                continue
            target = root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value + "\n", encoding="utf-8")
            written.append(target)
        if scope == SCOPE_MANAGED:
            settings = root / self.MANAGED_SETTINGS
            settings.parent.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if settings.exists():
                # The conflict scan above already proved this parses to an
                # identity-free object; the merge preserves it verbatim.
                existing = json.loads(settings.read_text(encoding="utf-8"))
            if model:
                existing["model"] = model
                existing["availableModels"] = [model]
                existing["enforceAvailableModels"] = True
            if effort:
                existing["effortLevel"] = effort
                env = existing.setdefault("env", {})
                env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
            settings.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", "utf-8")
            written.append(settings)
        return written

    # -- the runtime gate (ADR-0045 fail-closed amendment) --------------------

    def baked_identity(self, root: Path) -> BakedIdentity | None:
        try:
            return identity_mod.read_baked_identity(root)
        except IdentityError as exc:
            raise AgentAdapterError(str(exc)) from exc

    def preflight(
        self, identity: BakedIdentity, *, root: Path, scratch: Path, run=subprocess.run
    ) -> PreflightReport:
        scratch.mkdir(parents=True, exist_ok=True)
        return identity_mod.run_preflight(
            identity,
            binary=self._binary,
            root=root,
            scratch=scratch,
            min_cli=self.MIN_ENFORCING_CLI,
            run=run,
        )

    def guarded_command(self, manifest: Manifest, effort_capture: Path | None) -> list[str]:
        # The gated variant of command(): input arrives as stream-json user
        # messages over stdin, so the harness can run the no-op probe turn and
        # verify the session's identity before the real task prompt enters
        # the process. When effort is baked, a PostToolUse hook (added via
        # --settings, a source managed settings always outrank) captures the
        # APPLIED effort — the one machine-readable observation of an
        # organization effort cap, which clamps silently in stream-json.
        argv = [
            self._binary,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if effort_capture is not None:
            hook = {
                "hooks": {
                    "PostToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"cat > {shlex.quote(str(effort_capture))}",
                                }
                            ]
                        }
                    ]
                }
            }
            argv += ["--settings", json.dumps(hook)]
        return argv

    def session_guard(
        self, identity: BakedIdentity, effort_capture: Path | None
    ) -> ClaudeSessionGuard:
        return ClaudeSessionGuard(identity, effort_capture)

    def command(self, manifest: Manifest, prompt: str) -> list[str]:
        # Headless on purpose: completion is process exit (ADR-0019), and
        # the structured stream is both transcript and usage source. No
        # --model (ADR-0045): the CLI reads the model/effort baked into the
        # image's managed settings — nothing at invocation selects one. Used
        # only when the image bakes no identity; a baked identity launches
        # through guarded_command() so the prompt can be gated.
        return [
            self._binary,
            "-p",
            prompt,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
        ]

    def prepare(self, workdir: Path, job: Path) -> dict[str, str]:
        return {}

    def collect(self, workdir: Path, job: Path, mode: str) -> None:
        if mode == MODE_REVIEW:
            verdict = workdir / "verdict.json"
            if verdict.is_file():
                target = job / VERDICT_FILE
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(verdict, target)

    def stream_stats(self, transcript: Path) -> StreamStats:
        """Scan the stream-json transcript for tool calls, token usage, and
        every model signal the stream carries.

        The final ``result`` event carries the session's cumulative usage;
        if the session was killed before emitting one, per-call assistant
        usage records are summed instead. Unparseable lines are skipped —
        the stream is agent output and never trusted to be well-formed.

        Three independent model signals feed ``_reconcile_models``: the
        ``system``/``init`` announcement, the model on each real assistant
        turn (the CLI stamps synthetic error notices ``<synthetic>`` — those
        are not executions and are dropped), and the ``result`` event's
        ``modelUsage`` keys. modelUsage alone is NOT identity: it also bills
        the CLI's background helper models (verified live), so it only
        cross-checks the turn-level signals.
        """
        tool_calls = 0
        summed: int | None = None
        final: int | None = None
        init_model = ""
        turn_models: list[str] = []  # unique, in first-seen order
        usage_models: list[str] = []
        try:
            with transcript.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
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
                            if (
                                isinstance(seen, str)
                                and seen
                                and seen != "<synthetic>"
                                and seen not in turn_models
                            ):
                                turn_models.append(seen)
                            content = message.get("content")
                            if isinstance(content, list):
                                tool_calls += sum(
                                    1
                                    for block in content
                                    if isinstance(block, dict) and block.get("type") == "tool_use"
                                )
                            call = _usage_total(message.get("usage"))
                            if call is not None:
                                summed = (summed or 0) + call
                    elif event.get("type") == "result":
                        total = _usage_total(event.get("usage"))
                        if total is not None:
                            final = total
                        usage = event.get("modelUsage")
                        if isinstance(usage, dict):
                            usage_models = [key for key in usage if isinstance(key, str)]
        except OSError:
            return StreamStats()
        model, note = _reconcile_models(init_model, turn_models, usage_models)
        return StreamStats(
            tool_calls=tool_calls,
            tokens=final if final is not None else summed,
            model=model,
            model_note=note,
        )


def _reconcile_models(
    init_model: str, turn_models: list[str], usage_models: list[str]
) -> tuple[str, str]:
    """Deterministically reconcile the stream's model signals into
    ``(observed model, note)``.

    Real assistant turns are authoritative — they are what actually executed.
    The init announcement and the usage records only corroborate: init drift
    means the session was remapped or fell back after announcing itself, and
    a turn model absent from the usage records means the two halves of the
    stream disagree. Every disagreement lands in the note; extra usage-only
    models are expected (background helpers) and ignored.
    """
    if not turn_models:
        if init_model:
            return init_model, (
                "no assistant turns in the stream; model is the session-init announcement only"
            )
        if usage_models:
            return "", (
                "no assistant turns or session init in the stream; usage records"
                f" name {', '.join(sorted(usage_models))} but attribute no turn"
            )
        return "", "the stream carried no model signal"
    notes = []
    if len(turn_models) > 1:
        # Multiple models executed turns: report the one the session ended on,
        # but never as the whole story.
        notes.append(f"multiple models executed turns: {', '.join(turn_models)}")
    model = turn_models[-1]
    if init_model and init_model not in turn_models:
        notes.append(f"session initialized as {init_model} but executed {model}")
    if usage_models and not any(turn in usage_models for turn in turn_models):
        notes.append(
            f"usage records ({', '.join(sorted(usage_models))}) do not include any executed model"
        )
    return model, "; ".join(notes)


def _usage_total(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    total = 0
    seen = False
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
            seen = True
    return total if seen else None


def make_agent_adapter(name: str) -> AgentAdapter:
    if name == "claude":
        return ClaudeAdapter()
    raise AgentAdapterError(f"unknown Agent adapter {name!r} (M2 ships: claude)")


def materialize_instruction(adapter: str, model: str, effort: str, scope: str) -> str:
    """THE renderer of the synthesized materialize setup instruction — the one
    the Control Node appends to a wire recipe when a worker type sets model or
    effort (ADR-0045). The rendered string enters the instruction hash, so the
    format is identity-bearing: any change here re-tags every model-bearing
    derived image. Deliberate (the image bytes change), but never accidental —
    a golden test pins the format."""
    if scope not in MATERIALIZE_SCOPES:
        raise AgentAdapterError(
            f"unknown materialize scope {scope!r} (known: managed, interactive)"
        )
    if not model and not effort:
        raise AgentAdapterError("materialize instruction needs a model or an effort")
    if scope == SCOPE_INTERACTIVE and effort:
        # Config-load validation already rejects driverless effort; refuse to
        # even render an instruction the in-image CLI would refuse to run.
        raise AgentAdapterError(
            "effort has no interactive-scope materialization — driverless"
            " (Flight Deck) worker types reject 'effort' (ADR-0045)"
        )
    parts = ["theozolith-adapter", "materialize", "--adapter", shlex.quote(adapter)]
    if model:
        parts += ["--model", shlex.quote(model)]
    if effort:
        parts += ["--effort", shlex.quote(effort)]
    parts += ["--scope", scope]
    return " ".join(parts)


def stream_stats(adapter_name: str, transcript: Path) -> StreamStats:
    """Counters for ``adapter_name`` over ``transcript``, empty on an unknown
    adapter. The single stream-stats seam the drivers (progress telemetry,
    token accounting) call — they never construct adapters themselves."""
    try:
        adapter = make_agent_adapter(adapter_name)
    except AgentAdapterError:
        return StreamStats()
    return adapter.stream_stats(transcript)
