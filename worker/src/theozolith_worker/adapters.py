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

import contextlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol

from theozolith_worker import codexidentity
from theozolith_worker import identity as identity_mod
from theozolith_worker import policy as policy_mod
from theozolith_worker.identity import (
    BakedIdentity,
    ClaudeSessionMonitor,
    IdentityError,
    MonitorHooks,
    PreflightReport,
)
from theozolith_worker.jobdir import Manifest


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
# the identity the static checks and the fail-loud monitor hold the Run
# to, by best effort (identity.py).
WELL_KNOWN_MODEL_FILE = identity_mod.WELL_KNOWN_MODEL_FILE
WELL_KNOWN_EFFORT_FILE = identity_mod.WELL_KNOWN_EFFORT_FILE


# -- symlink-safe atomic baking (the image-identity writes) -------------------
#
# The well-known files and the managed settings ARE the baked identity, so the
# writes that produce them get the trusted-descriptor treatment control's TLS
# writer established (the OZ-01/OZ-02 doctrine): every directory component
# below the materialize root is opened O_NOFOLLOW and fstat-verified — a
# symlink planted anywhere on the route (a hostile base layer, debris from an
# earlier setup step) fails the build instead of steering the write into an
# arbitrary tree — and each file lands via a fresh O_EXCL temp plus a
# same-directory atomic replace, so a pre-existing destination symlink's
# target is never written through (the destination shape is refused loudly)
# and no failure leaves a temp artifact or a half-written identity behind.
_BAKE_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Baked identity is world-READABLE by design (a session may inspect its own
# default) and writable by nobody but root: 0644 files in 0755 directories,
# root-owned when the build runs as root (a real image build does; tests
# exercise the same path against a tmp root as an ordinary user).
_BAKE_FILE_MODE = 0o644
_BAKE_DIR_MODE = 0o755


@contextlib.contextmanager
def _bake_dir(root: Path, parts: tuple[str, ...]) -> Iterator[int]:
    """A verified descriptor for the product-owned directory ``root/<parts>``.

    ``root`` is caller territory (``/`` in a real image build, a tmp dir in
    tests) and may legitimately sit behind symlinks; every component BELOW it
    is opened O_NOFOLLOW|O_DIRECTORY (created 0755 when missing) and
    fstat-verified before it is trusted. The final, product-owned directory
    is then normalized — never group/world-writable, root-owned when the
    build runs as root — so an unprivileged runtime user cannot unlink and
    replace the baked files through a writable or wrongly-owned parent."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for depth, name in enumerate(parts):
            where = root.joinpath(*parts[: depth + 1])
            try:
                child = os.open(name, _BAKE_DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, _BAKE_DIR_MODE, dir_fd=fd)
                try:
                    child = os.open(name, _BAKE_DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    raise AgentAdapterError(
                        f"refusing to traverse {where}: not a real directory ({exc.strerror})"
                    ) from exc
            except OSError as exc:
                raise AgentAdapterError(
                    f"refusing to traverse {where}: it is a symlink or not a"
                    f" directory ({exc.strerror})"
                ) from exc
            os.close(fd)
            fd = child
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise AgentAdapterError(
                    f"refusing to traverse {where}: opened object is not a directory"
                )
        os.fchmod(fd, _BAKE_DIR_MODE)
        if os.geteuid() == 0:
            os.fchown(fd, 0, 0)
        yield fd
    finally:
        os.close(fd)


def _refuse_irregular_destination(dir_fd: int, name: str, where: Path) -> None:
    """A destination that already exists must be a regular file: a symlink is
    never followed (its target is never overwritten) and a special file is
    never replaced — both fail the build loudly instead."""
    try:
        existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(existing.st_mode):
        raise AgentAdapterError(
            f"refusing to write {where}: destination is not a regular file —"
            " a planted symlink or special file is never followed or replaced"
        )


def _read_regular_at(dir_fd: int, name: str) -> str | None:
    """The current content of an already-shape-checked destination, read
    without ever following a symlink; None when absent."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as handle:
        return handle.read()


def _replace_file_at(dir_fd: int, name: str, data: bytes, *, where: Path) -> None:
    """Land one baked file atomically relative to a verified descriptor: a
    fresh unguessably-named O_EXCL|O_NOFOLLOW temp in the same directory,
    explicit 0644 on the descriptor (umask-independent) and root ownership
    when the build runs as root, then a dirfd-relative replace. Failure
    unlinks the temp — the destination is the old bytes or the new, never a
    torn write, and nothing orphaned survives."""
    tmp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(fd, _BAKE_FILE_MODE)
            if os.geteuid() == 0:
                os.fchown(fd, 0, 0)
            handle.write(data)
            handle.flush()
            os.fsync(fd)
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=dir_fd)
        raise


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

    def prepare(
        self, workdir: Path, job: Path, *, identity_root: Path = Path("/")
    ) -> dict[str, str]:
        """Extra environment for the agent process. ``identity_root`` is the
        filesystem the baked identity lives under (``/`` in a real
        container; a tmp root in tests) — the codex adapter copies its
        baked config out of it into the per-Run CODEX_HOME."""
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

    def materialize_hooks(self, scratch: Path, workdir: Path) -> MonitorHooks:
        """The observation-hook filesystem contract for a monitored task
        session, materialized under ``scratch`` (outside the job mount):
        the MonitorHooks paths plus whatever hook scripts and launch-time
        baselines this adapter's monitor consumes — an adapter with no hook
        surface returns the paths and writes nothing. May raise OSError;
        the harness converts it into an identity failure (the observation
        channel would be silently absent)."""
        ...

    def monitored_command(self, manifest: Manifest, prompt: str, hooks: MonitorHooks) -> list[str]:
        """The ordinary one-shot argv plus this adapter's observation hooks
        (for Claude: the Stop applied-effort journal and the ConfigChange
        recorder riding ``--settings``) — the session keeps its full normal
        capabilities, checkout instruction files and skills included."""
        ...

    def session_monitor(self, identity: BakedIdentity, hooks: MonitorHooks):
        """The stream watcher for a monitored task session: fail-loud for
        Claude (see ``identity.ClaudeSessionMonitor`` for the duck-typed
        contract), a benign evidence observer for codex (ADR-0052's
        PROBE + STATIC doctrine). A monitor exposing an ``observed_effort``
        attribute owns the applied-effort observation channel (codex: the
        rollout turn_context) — the harness records it as the authoritative
        evidence and never consults the Claude Stop-hook journal for that
        session."""
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
    # The behaviors this adapter relies on: the managed-over-project settings
    # precedence for the model default, the per-key managed ``env`` merge
    # (2.1.223 — without it a server-delivered org env block displaces the
    # baked effort pin), the Stop-hook payload carrying the applied
    # (post-clamp) effort, the ConfigChange hook, hooks riding ``--settings``,
    # and the dry-run probe's isolation flags (``--tools ""``,
    # ``--permission-mode dontAsk``, ``--setting-sources ""``) — the whole set
    # verified live on 2.1.232 (the behavior baseline). The floor below is set
    # higher, to 2.1.260, as an operator minimum-version requirement (issue
    # #128); a classification review should re-confirm the behavior set against
    # 2.1.260 before relying on it. An older CLI would either fail every Run
    # with a confusing error (unknown flag) or silently void an observation
    # channel, and the floor turns both into cli-too-old.
    MIN_ENFORCING_CLI = (2, 1, 260)
    # The Agent Policy validator this adapter owns (ADR-0055): the safe-key
    # allowlist in ``theozolith_worker.policy`` admits verbatim managed-
    # settings drop-ins, and it advances ONLY through the same deliberate
    # classification review that moves MIN_ENFORCING_CLI — the two are one
    # review of what this adapter's validated CLI set actually does with a
    # key.
    POLICY_MODULE = policy_mod
    # The product-supported CLI platform tuples (ADR-0055): the platforms the
    # Node Daemon itself supports, keyed "<os>-<arch>-<libc>". Each maps to
    # the npm platform package publishing that tuple's binary (the wrapper
    # package's version document enumerates them under optionalDependencies —
    # names verified against the registry 2026-09-02); ingest resolves EVERY
    # entry and fails when the registry cannot supply one, so a wrong name
    # here can never ship silently. Adapter-owned like MIN_ENFORCING_CLI: the
    # daemon never consults this table — it detects its own tuple and looks
    # it up in the wire-delivered pinned map (a contract test pins the key
    # spelling between the two sides).
    CLI_PLATFORM_PACKAGES: ClassVar[dict[str, str]] = {
        "linux-x64-glibc": "@anthropic-ai/claude-code-linux-x64",
        "linux-arm64-glibc": "@anthropic-ai/claude-code-linux-arm64",
        "linux-x64-musl": "@anthropic-ai/claude-code-linux-x64-musl",
        "linux-arm64-musl": "@anthropic-ai/claude-code-linux-arm64-musl",
    }
    # The npm wrapper package the CLI Pin's version/dist-tag resolves against.
    CLI_WRAPPER_PACKAGE = "@anthropic-ai/claude-code"
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
        consumer exists, and this refuses it too.

        Both scopes share one write discipline (see ``_bake_dir`` /
        ``_replace_file_at``): every destination is resolved through
        O_NOFOLLOW-verified descriptors and shape-checked before anything is
        written, then landed by same-directory atomic replace as a 0644
        regular file (root-owned, in a normalized product-owned directory,
        when the build runs as root). The baked identity is image bytes — a
        steered, torn, or runtime-user-writable write is a build failure,
        never a quiet variance."""
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
        plan: list[tuple[str, str]] = []
        for relpath, value in ((WELL_KNOWN_MODEL_FILE, model), (WELL_KNOWN_EFFORT_FILE, effort)):
            if value:
                plan.append((relpath, value + "\n"))
        if scope == SCOPE_MANAGED:
            plan.append((self.MANAGED_SETTINGS, ""))  # content computed below
        written: list[Path] = []
        with contextlib.ExitStack() as stack:
            # Every destination is resolved and shape-checked BEFORE any
            # write, so a refused path (planted symlink, special file,
            # unsafe parent) fails the build with no file materialized.
            resolved: list[tuple[int, str, Path, str]] = []
            for relpath, content in plan:
                parts = Path(relpath).parts
                dir_fd = stack.enter_context(_bake_dir(root, parts[:-1]))
                _refuse_irregular_destination(dir_fd, parts[-1], root / relpath)
                resolved.append((dir_fd, parts[-1], root / relpath, content))
            if scope == SCOPE_MANAGED:
                dir_fd, name, target, _ = resolved[-1]
                raw = _read_regular_at(dir_fd, name)
                # The conflict scan above already proved this parses to an
                # identity-free object; the merge preserves it verbatim.
                existing: dict = json.loads(raw) if raw is not None else {}
                if model:
                    existing["model"] = model
                if effort:
                    existing["effortLevel"] = effort
                    env = existing.setdefault("env", {})
                    env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
                content = json.dumps(existing, indent=2, sort_keys=True) + "\n"
                resolved[-1] = (dir_fd, name, target, content)
            for dir_fd, name, target, content in resolved:
                _replace_file_at(dir_fd, name, content.encode("utf-8"), where=target)
                written.append(target)
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

    def materialize_hooks(self, scratch: Path, workdir: Path) -> MonitorHooks:
        # The Claude observation channel: the two hook helper scripts plus
        # the launch-time baseline of the checkout's identity-shaped
        # settings keys (so the ConfigChange hook reports only keys a
        # mid-session change ADDED — a checkout that legitimately ships an
        # inert identity key is not killed for a benign edit).
        hooks = MonitorHooks(
            stop_capture=scratch / "stop.jsonl",
            config_capture=scratch / "config-change.jsonl",
            config_baseline=scratch / "config-baseline.json",
            config_hook_script=scratch / "configchange_hook.py",
            stop_hook_script=scratch / "stop_hook.py",
        )
        hooks.config_hook_script.write_text(
            identity_mod.CONFIG_CHANGE_HOOK_SOURCE, encoding="utf-8"
        )
        hooks.stop_hook_script.write_text(identity_mod.STOP_HOOK_SOURCE, encoding="utf-8")
        hooks.config_baseline.write_text(
            json.dumps(identity_mod.project_settings_baseline(workdir)), encoding="utf-8"
        )
        return hooks

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
                                f"{shlex.quote(str(hooks.config_capture))} "
                                f"{shlex.quote(str(hooks.config_baseline))}",
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

    def prepare(
        self, workdir: Path, job: Path, *, identity_root: Path = Path("/")
    ) -> dict[str, str]:
        return {}

    def collect(self, workdir: Path, job: Path, mode: str) -> None:
        # Nothing to copy out (ADR-0046): every session output the driver
        # applies is the Output Proposal, which the format-output CLI writes
        # directly into the job dir's output/ — the hand-written review-mode
        # verdict.json this step used to ferry is retired.
        return

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
        turn_models: list[str] = []  # every main-agent turn, in stream order
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


class CodexAdapter:
    """Drives the OpenAI Codex CLI (ADR-0052). All codex-specific mechanics
    live here and in ``codexidentity``.

    The session runs headless: ``codex exec`` with the pointer prompt as
    the argument and ``--json``, so stdout is a line-per-event JSONL stream
    (thread.started / turn.started / item.* / turn.completed / turn.failed
    / error — verified against codex-cli 0.150.0). That stream is the
    transcript and the usage source; it carries NO model signal — the
    observed model comes from the session rollout journal in the throwaway
    per-Run ``CODEX_HOME`` (see codexidentity). ``--sandbox
    danger-full-access`` is the codex spelling of Claude's
    ``--dangerously-skip-permissions``: the run container IS the sandbox
    (ADR-0013), and codex's own Landlock sandbox is unavailable inside it.

    Identity doctrine is PROBE + STATIC only (weaker than Claude's by the
    CLI's construction — there is no managed-policy tier and no hook
    surface): baked config + one driver-boot probe + per-Run static checks
    + a benign evidence observer. No live mid-run kill; undetected
    steering is a recorded gap (ADR-0052)."""

    name = "codex"

    # Full OpenAI model IDs, shape-checked (the CLI passes them through to
    # the provider). No hand-curated family aliases like Claude's — but the
    # provider's ``-latest`` IDs (e.g. codex-mini-latest) are moving
    # pointers, so they classify as floating aliases: mappable, and the
    # control-side lint warns exactly as it does for Claude's (ADR-0045).
    _PINNED = re.compile(r"gpt-[a-z0-9.-]+|o[0-9][a-z0-9.-]*|codex-[a-z0-9.-]+")
    # The vocabulary config.toml's model_reasoning_effort accepts. Which
    # models provably HONOR a level is the capability table's question
    # (codexidentity — empty until spike #76 S7 proves entries), so today
    # every nonempty effort fails the pair gate with an actionable message.
    EFFORTS = codexidentity.CODEX_EFFORT_VOCABULARY
    # Items in the event stream that represent executed tool-ish work.
    _TOOL_ITEM_TYPES = frozenset(
        {"command_execution", "mcp_tool_call", "web_search", "file_change"}
    )
    # The oldest codex CLI whose exact behavior this adapter relies on: the
    # --json v2 event schema the parsers speak, the rollout turn_context
    # journal carrying (model, effort), config-key binding in exec mode,
    # and the sandbox/skip-git-repo-check flags — all verified live on
    # 0.150.0, which is therefore the floor (and the base image pins the
    # same version: the schema is experimental, the pin is the policy).
    MIN_ENFORCING_CLI = (0, 150, 0)
    model_shapes = "full OpenAI model IDs (gpt-*, o*, codex-*); '-latest' IDs float"

    def __init__(self, binary: str = "codex"):
        self._binary = binary
        self._home: Path | None = None

    def classify_model(self, model: str) -> str:
        if not self._PINNED.fullmatch(model):
            return MODEL_UNMAPPABLE
        if model.endswith("-latest"):
            return MODEL_ALIAS
        return MODEL_PINNED

    def mappable_efforts(self) -> frozenset[str]:
        return self.EFFORTS

    def pair_error(self, model: str, effort: str) -> str:
        return codexidentity.pair_error(model, effort)

    def _cli_version(self) -> str:
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
                " must ship the Codex CLI beside theozolith-adapter (ADR-0052)"
            ) from exc
        if probe.returncode != 0:
            raise AgentAdapterError(
                f"'{self._binary} --version' exited {probe.returncode}:"
                f" {probe.stderr.strip() or probe.stdout.strip()}"
            )
        return probe.stdout.strip()

    def verify_enforceable(self) -> str:
        raw = self._cli_version()
        match = re.match(r"codex-cli (\d+)\.(\d+)\.(\d+)", raw)
        if not match:
            raise AgentAdapterError(
                f"cannot parse a version from '{self._binary} --version' output {raw!r}"
            )
        version = tuple(int(part) for part in match.groups())
        if version < self.MIN_ENFORCING_CLI:
            floor = ".".join(str(part) for part in self.MIN_ENFORCING_CLI)
            raise AgentAdapterError(
                f"codex {raw} predates the behavior this machinery relies on"
                " (the --json event schema, the rollout turn_context journal,"
                f" config-key binding in exec mode; CLI >= {floor}) — the"
                " baked identity would not bind; bump the worker type's base"
                " to a release with a newer CLI (ADR-0052)"
            )
        return raw

    def materialize(self, model: str, effort: str, *, root: Path, scope: str) -> list[Path]:
        """Bake ``model``/``effort`` into the image filesystem under
        ``root``: the well-known files plus the theozolith-owned
        ``etc/theozolith/codex/config.toml`` every session's throwaway
        ``CODEX_HOME`` receives a copy of.

        ``interactive`` scope is REJECTED outright: no codex Flight Deck
        worker type exists (the deck machinery is Claude-shaped, ADR-0043),
        and control refuses the config upstream — this is the in-image
        backstop. A pre-existing baked config is conflict-scanned: any
        selection key fails the build naming it (operator content is never
        overwritten), and so does any instruction-discovery key
        (``project_doc_fallback_filenames`` — the judge-isolation name set
        is fixed, #82); identity keys are then emitted as a header block
        PREPENDED before the preserved operator bytes, and the merged text
        is re-parsed before landing. Same symlink-safe atomic write
        discipline as the Claude bake."""
        if scope == SCOPE_INTERACTIVE:
            raise AgentAdapterError(
                "the codex adapter has no interactive-scope materialization —"
                " no codex Flight Deck worker type exists (ADR-0052)"
            )
        pair = codexidentity.pair_error(model, effort)
        if pair:
            raise AgentAdapterError(pair)
        if not model:
            raise AgentAdapterError(
                "the codex adapter materializes nothing without a model —"
                " effort alone is not an identity (ADR-0045)"
            )
        config_parts = Path(codexidentity.BAKED_CONFIG_FILE).parts
        plan: list[tuple[str, str]] = []
        for relpath, value in ((WELL_KNOWN_MODEL_FILE, model), (WELL_KNOWN_EFFORT_FILE, effort)):
            if value:
                plan.append((relpath, value + "\n"))
        written: list[Path] = []
        with contextlib.ExitStack() as stack:
            resolved: list[tuple[int, str, Path, str]] = []
            for relpath, content in plan:
                parts = Path(relpath).parts
                dir_fd = stack.enter_context(_bake_dir(root, parts[:-1]))
                _refuse_irregular_destination(dir_fd, parts[-1], root / relpath)
                resolved.append((dir_fd, parts[-1], root / relpath, content))
            config_fd = stack.enter_context(_bake_dir(root, config_parts[:-1]))
            config_target = root / codexidentity.BAKED_CONFIG_FILE
            _refuse_irregular_destination(config_fd, config_parts[-1], config_target)
            existing = _read_regular_at(config_fd, config_parts[-1]) or ""
            if existing:
                try:
                    document = tomllib.loads(existing)
                except ValueError as exc:
                    raise AgentAdapterError(
                        f"{config_target}: existing baked config is not valid"
                        f" TOML ({exc}) — refusing to merge into an unknowable document"
                    ) from exc
                conflicts = [
                    key for key in codexidentity.CODEX_CONFIG_IDENTITY_KEYS if key in document
                ]
                if conflicts:
                    raise AgentAdapterError(
                        "existing baked codex config would supersede the baked"
                        " identity — refusing to overwrite operator content;"
                        f" remove the conflicting keys from {config_target}:"
                        f" {', '.join(conflicts)}"
                    )
                instruction = [
                    key for key in codexidentity.CODEX_CONFIG_INSTRUCTION_KEYS if key in document
                ]
                if instruction:
                    # The judge-isolation boundary (#82) removes a fixed
                    # instruction-file name set from review checkouts; a
                    # baked config extending the CLI's project-doc
                    # discovery would reopen that channel under a name the
                    # driver does not know, so the key is refused for every
                    # codex image — the pipeline's canonical instruction
                    # file is AGENTS.md, never a configured alias.
                    raise AgentAdapterError(
                        "existing baked codex config redefines instruction-file"
                        " discovery — the review judge-isolation boundary"
                        " depends on a fixed name set (#82); remove from"
                        f" {config_target}: {', '.join(instruction)}"
                    )
            header = [
                "# Materialized by theozolith-adapter (ADR-0052) — the baked",
                "# identity every session's CODEX_HOME receives. Never hand-edit.",
                f'model = "{model}"',
            ]
            if effort:
                header.append(f'model_reasoning_effort = "{effort}"')
            merged = "\n".join(header) + "\n" + (("\n" + existing) if existing else "")
            try:
                tomllib.loads(merged)
            except ValueError as exc:  # pragma: no cover - defense in depth
                raise AgentAdapterError(
                    f"{config_target}: merged baked config does not parse ({exc})"
                ) from exc
            resolved.append((config_fd, config_parts[-1], config_target, merged))
            for dir_fd, name, target, content in resolved:
                _replace_file_at(dir_fd, name, content.encode("utf-8"), where=target)
                written.append(target)
        return written

    # -- the identity machinery (ADR-0052, PROBE + STATIC doctrine) -----------

    def baked_identity(self, root: Path) -> BakedIdentity | None:
        try:
            return codexidentity.read_baked_identity(root)
        except IdentityError as exc:
            raise AgentAdapterError(str(exc)) from exc

    def static_checks(self, identity: BakedIdentity, *, root: Path) -> PreflightReport:
        return codexidentity.static_identity_report(identity, root=root)

    def preflight(
        self, identity: BakedIdentity, *, root: Path, scratch: Path, run=subprocess.run
    ) -> PreflightReport:
        scratch.mkdir(parents=True, exist_ok=True)
        return codexidentity.run_preflight(
            identity,
            binary=self._binary,
            root=root,
            scratch=scratch,
            min_cli=self.MIN_ENFORCING_CLI,
            run=run,
        )

    def materialize_hooks(self, scratch: Path, workdir: Path) -> MonitorHooks:
        # No hook surface: the paths exist to satisfy the contract, nothing
        # is written, and no capture file ever appears — the harness's
        # missing-observation paths record gaps, which is the doctrine.
        return MonitorHooks(
            stop_capture=scratch / "stop.jsonl",
            config_capture=scratch / "config-change.jsonl",
            config_baseline=scratch / "config-baseline.json",
            config_hook_script=scratch / "configchange_hook.py",
            stop_hook_script=scratch / "stop_hook.py",
        )

    def monitored_command(self, manifest: Manifest, prompt: str, hooks: MonitorHooks) -> list[str]:
        # No observation hooks to ride: the monitored launch IS the
        # ordinary launch (the benign observer reads the rollout journal
        # after exit instead).
        return self.command(manifest, prompt)

    def session_monitor(
        self, identity: BakedIdentity, hooks: MonitorHooks
    ) -> codexidentity.CodexSessionMonitor:
        return codexidentity.CodexSessionMonitor(identity, hooks, self._home)

    def command(self, manifest: Manifest, prompt: str) -> list[str]:
        # Headless on purpose (ADR-0019). No -m/-c/--profile (ADR-0045:
        # nothing at invocation selects a model — the baked config in the
        # prepared CODEX_HOME does). --skip-git-repo-check is belt and
        # braces for non-checkout working directories.
        return [
            self._binary,
            "exec",
            prompt,
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
        ]

    def prepare(
        self, workdir: Path, job: Path, *, identity_root: Path = Path("/")
    ) -> dict[str, str]:
        """Assemble the throwaway per-Run ``CODEX_HOME`` (baked config +
        0600 auth.json from the delivered ``CODEX_AUTH_JSON``) and point the
        agent process at it. The only side-effecting ``prepare`` in the
        codebase — codex reads config only from ``CODEX_HOME``, which must
        be writable (the CLI stores session rollouts and state there; the
        temp home dies with the container). The raw credential value also
        remains visible in the agent process env — equivalent exposure to
        the auth file inside the same container (ADR-0013's rotatable
        spend-credential posture)."""
        home = Path(tempfile.mkdtemp(prefix="theozolith-codex-home-"))
        try:
            codexidentity.assemble_codex_home(home, root=identity_root, environ=os.environ)
        except IdentityError as exc:
            raise AgentAdapterError(str(exc)) from exc
        self._home = home
        return {"CODEX_HOME": str(home)}

    def collect(self, workdir: Path, job: Path, mode: str) -> None:
        return

    def stream_stats(self, transcript: Path) -> StreamStats:
        """Scan the codex JSONL transcript: executed tool-ish items
        (command_execution / mcp_tool_call / web_search / file_change on
        item.completed) and per-turn usage summed across turn.completed
        events. The stream carries no model signal — the observed model
        reaches evidence through the session monitor's rollout read, so
        ``model`` is empty here with the note saying why."""
        tool_calls = 0
        tokens: int | None = None
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
                    if event.get("type") == "item.completed":
                        item = event.get("item")
                        if isinstance(item, dict) and item.get("type") in self._TOOL_ITEM_TYPES:
                            tool_calls += 1
                    elif event.get("type") == "turn.completed":
                        turn = _usage_total(event.get("usage"))
                        if turn is not None:
                            tokens = (tokens or 0) + turn
        except OSError:
            return StreamStats()
        return StreamStats(
            tool_calls=tool_calls,
            tokens=tokens,
            model="",
            model_note=(
                "codex exec --json carries no model signal; the observed model"
                " comes from the session rollout journal"
            ),
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
    unique = list(dict.fromkeys(turn_models))  # first-seen order, for the note
    if len(unique) > 1:
        # Multiple models executed turns: report the one the session ended on
        # (turn_models carries EVERY turn in stream order, so [-1] really is
        # the last executed — a drift-and-recovery session ends where it
        # ended), but never as the whole story.
        notes.append(f"multiple models executed turns: {', '.join(unique)}")
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
    if name == "codex":
        return CodexAdapter()
    raise AgentAdapterError(f"unknown Agent adapter {name!r} (known: claude, codex)")


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
