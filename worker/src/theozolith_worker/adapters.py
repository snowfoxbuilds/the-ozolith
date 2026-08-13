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
selected at invocation time and never delivered as env vars.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
# rewrite it.
WELL_KNOWN_MODEL_FILE = "etc/theozolith/model"
WELL_KNOWN_EFFORT_FILE = "etc/theozolith/effort"


@dataclass(frozen=True)
class StreamStats:
    """Counters read from the structured output stream (ADR-0019).

    ``tokens`` is None until the stream carries usage — this is how an
    adapter that reports usage closes ADR-0018's null-token gap.

    ``model`` is the *observed* model — what the session actually ran, read
    from the stream, "" until the stream carries it. With model selection
    baked into the image (ADR-0045) this is telemetry's model source; the
    config-time value is recoverable from the run-image tag.
    """

    tool_calls: int = 0
    tokens: int | None = None
    model: str = ""


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

    def materialize(self, model: str, effort: str, *, root: Path, scope: str) -> list[Path]:
        """Write the native configuration for ``model``/``effort`` under
        ``root`` (the image filesystem; ``/`` in a real build) and return the
        paths written. Values must already be mappable — the CLI validates
        before calling."""
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

    # Claude Code's own model shorthands. Mappable — the CLI resolves them —
    # but floating, so the control-side lint warns on them (ADR-0045).
    ALIASES = frozenset({"default", "sonnet", "opus", "haiku", "fable", "opusplan"})
    # A full provider model ID (dated or not: current-generation IDs ship
    # without a dated variant). Claude Code passes these through unchecked.
    _PINNED = re.compile(r"claude-[a-z0-9.-]+")
    # The effort values persistable in Claude Code settings as "effortLevel";
    # session-only levels (e.g. max) are deliberately absent — a build asking
    # for one fails, which is ADR-0045's contract.
    EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
    # Managed settings: the highest-precedence settings layer, above CLI args
    # and above any .claude/settings.json a workspace checkout carries — the
    # baked model is not overridable from inside a run (ADR-0045).
    MANAGED_SETTINGS = "etc/claude-code/managed-settings.json"
    model_shapes = (
        "full claude-* model IDs, or the aliases default/fable/haiku/opus/opusplan/sonnet"
    )

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

    def materialize(self, model: str, effort: str, *, root: Path, scope: str) -> list[Path]:
        """Bake ``model``/``effort`` into the image filesystem under ``root``.

        Both scopes write the plain-text well-known files. ``managed``
        additionally merges ``model``/``effortLevel`` into the managed
        settings JSON — merge, not overwrite, so an operator setup step that
        pre-wrote managed settings survives (a malformed existing file fails
        the build loudly instead of being clobbered)."""
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
                loaded = json.loads(settings.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise AgentAdapterError(
                        f"{settings} exists but is not a JSON object — refusing to"
                        " overwrite operator-written managed settings"
                    )
                existing = loaded
            if model:
                existing["model"] = model
            if effort:
                existing["effortLevel"] = effort
            settings.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", "utf-8")
            written.append(settings)
        return written

    def command(self, manifest: Manifest, prompt: str) -> list[str]:
        # Headless on purpose: completion is process exit (ADR-0019), and
        # the structured stream is both transcript and usage source. No
        # --model (ADR-0045): the CLI reads the model/effort baked into the
        # image's managed settings — nothing at invocation selects one.
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
        """Scan the stream-json transcript for tool calls and token usage.

        The final ``result`` event carries the session's cumulative usage;
        if the session was killed before emitting one, per-call assistant
        usage records are summed instead. Unparseable lines are skipped —
        the stream is agent output and never trusted to be well-formed.
        """
        tool_calls = 0
        summed: int | None = None
        final: int | None = None
        model = ""
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
                    if event.get("type") == "assistant":
                        message = event.get("message")
                        if isinstance(message, dict):
                            seen_model = message.get("model")
                            if isinstance(seen_model, str) and seen_model:
                                model = seen_model  # last one wins: the observed model
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
        except OSError:
            return StreamStats()
        return StreamStats(
            tool_calls=tool_calls,
            tokens=final if final is not None else summed,
            model=model,
        )


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
