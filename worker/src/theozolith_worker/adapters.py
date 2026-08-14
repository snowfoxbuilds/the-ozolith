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
    ClaudeSessionMonitor,
    IdentityError,
    MonitorHooks,
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

    # -- the identity machinery (ADR-0045, best-effort doctrine) -------------

    def baked_identity(self, root: Path) -> BakedIdentity | None:
        """The identity this filesystem was baked with, or None when it
        carries none (a model-less worker type runs unmonitored). Raises
        ``AgentAdapterError`` on an inconsistent or corrupt declaration."""
        ...

    def static_checks(self, identity: BakedIdentity, *, root: Path) -> PreflightReport:
        """The zero-cost per-Run identity checks (file reads only — no
        sessions, no tokens): managed selection consistent with the
        well-known declaration, no superseding policy, valid pair, clean
        process environment. A failed report fails the Run loud before
        launch."""
        ...

    def preflight(self, identity: BakedIdentity, *, root: Path, scratch: Path) -> PreflightReport:
        """The setup dry-run (run once per driver process, never per Run):
        the static checks, the CLI version floor, and one neutral probe
        session with the Run credential proving the effective configuration
        selects the baked model (and applies the baked effort)."""
        ...

    def monitored_command(self, manifest: Manifest, prompt: str, hooks: MonitorHooks) -> list[str]:
        """The ordinary one-shot argv plus the observation hooks (Stop
        applied-effort journal, ConfigChange recorder) riding ``--settings``
        — the session keeps its full normal capabilities, checkout
        CLAUDE.md and skills included."""
        ...

    def session_monitor(self, identity: BakedIdentity, hooks: MonitorHooks):
        """The fail-loud stream watcher for a monitored task session (see
        ``identity.ClaudeSessionMonitor`` for the contract)."""
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

    # The model-family aliases the managed model default can hold: each
    # expands to the newest model of exactly one family (verified live), so
    # a baked alias binds the main agent to that family. Floating, though —
    # the control-side lint warns on them (ADR-0045). "default" and
    # "opusplan" are deliberately NOT here, although the CLI accepts both as
    # selections: "default" floats with the account tier and "opusplan" is a
    # two-model mode — neither names the single checkable model ADR-0045
    # bakes, so both classify unmappable and fail the config load and the
    # build.
    ALIASES = frozenset({"sonnet", "opus", "haiku", "fable"})
    # A full provider model ID (dated or not: current-generation IDs ship
    # without a dated variant). Claude Code passes these through unchecked.
    _PINNED = re.compile(r"claude-[a-z0-9.-]+")
    # The effort values CLAUDE_CODE_EFFORT_LEVEL (and settings "effortLevel")
    # accept; session-only levels (e.g. max) are deliberately absent — a
    # build asking for one fails, which is ADR-0045's contract.
    EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
    # Managed settings: the CLI's admin policy layer. The materialized
    # "model" is the MAIN-agent session default: the managed tier outranks
    # the checkout's and home's settings files for the same key (verified
    # live), the harness passes no --model, and the env audit rejects
    # steering variables — so in the normal case the main agent runs the
    # baked model, while subagents and background helpers stay free to use
    # other models (ADR-0045, main-agent-only). The managed "env" block's
    # CLAUDE_CODE_EFFORT_LEVEL overrides /effort, --effort, the process
    # environment, and any settings-file effortLevel (verified live).
    MANAGED_SETTINGS = identity_mod.MANAGED_SETTINGS_FILE
    # The oldest Claude Code whose exact behavior this adapter relies on:
    # the managed-over-project settings precedence for the model default,
    # the per-key managed ``env`` merge (2.1.223 — without it a
    # server-delivered org env block displaces the baked effort pin), the
    # Stop-hook payload carrying the applied (post-clamp) effort, the
    # ConfigChange hook, hooks riding ``--settings``, and the dry-run
    # probe's isolation flags (``--tools ""``, ``--permission-mode
    # dontAsk``, ``--setting-sources ""``) — the whole set verified live on
    # 2.1.232, which is therefore the floor: an older CLI would either fail
    # every Run with a confusing error (unknown flag) or silently void an
    # observation channel, and the floor turns both into cli-too-old.
    MIN_ENFORCING_CLI = (2, 1, 232)
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
        """Fail unless the in-image CLI is new enough for every behavior the
        identity machinery relies on (MIN_ENFORCING_CLI): the managed
        model-default precedence over checkout settings, the per-key managed
        ``env`` merge, and the Stop/ConfigChange hook payloads. An older CLI
        would let the baked identity look pinned while binding nothing — the
        one failure mode worse than a failed build."""
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
                f"Claude Code {raw} predates the identity behavior this"
                " machinery relies on (managed model-default precedence,"
                " per-key managed env merge, Stop/ConfigChange hook payloads;"
                f" CLI >= {floor}) — the baked identity would not bind; bump"
                " the worker type's base to a release with a newer CLI"
                " (ADR-0045)"
            )
        return raw

    def materialize(self, model: str, effort: str, *, root: Path, scope: str) -> list[Path]:
        """Bake ``model``/``effort`` into the image filesystem under ``root``.

        ``managed`` (driver run images) first proves the managed tier under
        ``root`` carries nothing that would supersede what it is about to
        write: the base ``managed-settings.json`` and every
        ``managed-settings.d/*.json`` drop-in are inspected in Claude Code's
        documented merge order, and a malformed document, a
        ``policyHelper``/``policyHelpers``, or any identity-affecting key
        FAILS THE BUILD naming the file and key — operator policy is never
        silently deleted or overwritten. It then writes the well-known files
        and merges the selection keys into the base settings JSON: a managed
        ``model`` session default for the MAIN agent (the managed tier
        outranks checkout/user settings for the same key — verified live —
        and the harness passes no ``--model``), plus, for effort, the
        ``effortLevel`` default beside ``env.CLAUDE_CODE_EFFORT_LEVEL``
        (which overrides /effort, --effort, the process environment, and
        any settings-file effortLevel). Deliberately NO
        ``availableModels`` allowlist: enforcement is main-agent-only
        (ADR-0045) — subagents, skills routed through subagents, and the
        CLI's background helpers are free to run other models, and a
        detected main-agent drift fails the Run loud at runtime instead.
        Unrelated operator keys — including foreign ``env`` entries —
        survive the merge untouched.

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
            if effort:
                existing["effortLevel"] = effort
                env = existing.setdefault("env", {})
                env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
            settings.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", "utf-8")
            written.append(settings)
        return written

    # -- the identity machinery (ADR-0045, best-effort doctrine) --------------

    def baked_identity(self, root: Path) -> BakedIdentity | None:
        try:
            return identity_mod.read_baked_identity(root)
        except IdentityError as exc:
            raise AgentAdapterError(str(exc)) from exc

    def static_checks(self, identity: BakedIdentity, *, root: Path) -> PreflightReport:
        return identity_mod.static_identity_report(identity, root=root)

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

    def monitored_command(self, manifest: Manifest, prompt: str, hooks: MonitorHooks) -> list[str]:
        # The ordinary one-shot launch plus two observation hooks riding
        # --settings (managed settings outrank it, so nothing here can
        # weaken the selection). The session keeps its FULL normal
        # capabilities — checkout CLAUDE.md, skills, slash commands, and
        # settings sources all load (ADR-0045: they belong to the work; the
        # checkout is not treated as an identity threat by ruling):
        # - Stop: appends one value-redacted applied-effort record per
        #   completed turn — the observation an organization cap would
        #   clamp silently in stream-json; checked after exit, fail loud
        #   on a detected mismatch, gap recorded when absent.
        # - ConfigChange: records identity-relevant mid-session settings
        #   changes (never blocks them) for the monitor to kill on.
        settings = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 "
                                f"{shlex.quote(str(hooks.stop_hook_script))} "
                                f"{shlex.quote(str(hooks.stop_capture))}",
                            }
                        ]
                    }
                ],
                "ConfigChange": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 "
                                f"{shlex.quote(str(hooks.config_hook_script))} "
                                f"{shlex.quote(str(hooks.config_capture))}",
                            }
                        ]
                    }
                ],
            }
        }
        return [*self.command(manifest, prompt), "--settings", json.dumps(settings)]

    def session_monitor(self, identity: BakedIdentity, hooks: MonitorHooks) -> ClaudeSessionMonitor:
        return ClaudeSessionMonitor(identity, hooks)

    def command(self, manifest: Manifest, prompt: str) -> list[str]:
        # Headless on purpose: completion is process exit (ADR-0019), and
        # the structured stream is both transcript and usage source. No
        # --model (ADR-0045): the CLI reads the model/effort baked into the
        # image's managed settings — nothing at invocation selects one. A
        # baked identity launches through monitored_command(), which is this
        # argv plus the observation hooks.
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
        ``system``/``init`` announcement, the model on each real MAIN-agent
        assistant turn (subagent events carry ``parent_tool_use_id`` and are
        legitimately free to run other models — ADR-0045 main-agent-only —
        so they never feed the identity reconciliation, though their tool
        calls and usage still count; the CLI stamps synthetic error notices
        ``<synthetic>`` — those are not executions and are dropped), and the
        ``result`` event's ``modelUsage`` keys. modelUsage alone is NOT
        identity: it also bills subagents and the CLI's background helper
        models (verified live), so it only cross-checks the turn-level
        signals.
        """
        tool_calls = 0
        summed: int | None = None
        final: int | None = None
        init_model = ""
        turn_models: list[str] = []  # unique, in first-seen order; main agent only
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
                                not event.get("parent_tool_use_id")
                                and isinstance(seen, str)
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
    models are expected (background helpers) and ignored, and comparisons
    strip the CLI's context-window decoration (init and usage announce e.g.
    "claude-opus-5[1m]" while the turns execute "claude-opus-5" — the same
    model, not a disagreement).
    """
    normalize = identity_mod.normalize_model
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
    normalized_turns = {normalize(turn) for turn in turn_models}
    if init_model and normalize(init_model) not in normalized_turns:
        notes.append(f"session initialized as {init_model} but executed {model}")
    if usage_models and not normalized_turns & {normalize(used) for used in usage_models}:
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
