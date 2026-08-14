"""Live proof that the baked model/effort BIND on a real Claude Code CLI
(ADR-0045). Each test materializes managed settings through the adapter's own
``materialize()`` — the same artifact a derived-image build writes (including
``forceRemoteSettingsRefresh``, so every session here also proves the
freshness key does not break startup) — installs them at the CLI's managed
path, runs a real ``claude -p`` session, and asserts the executed
model/effort from the stream (the same signals ``stream_stats`` reconciles).
The fail-closed amendment adds live proof of the drop-in merge behavior the
build gate rejects, the policyHelper preemption, the widen and same-family
canaries, the unavailable-model path, the silently-clamped effort pair, the
Stop-hook applied-effort payload the gate's effort proof rides on, the
PreToolUse release-marker denial binding under skip-permissions, and the
whole gated harness (withheld task → neutral preflight → gated session →
release → task turn) driven through the REAL ``run_harness`` against the
real CLI. The pre-verification-boundary completion adds the task-session
isolation proof — a checkout booby-trapped with env.ANTHROPIC_BASE_URL, a
model selector, a SessionStart hook, and a CLAUDE.md never reaches the
gated task session (with a positive control proving the same trap fires
without ``--setting-sources ""``) — and pins the per-turn Stop boundary:
exactly one probe and one task record in the turn journal end to end.

Deliberately opt-in (``THEOZOLITH_LIVE_CLAUDE=1``): the suite spends real
tokens (~35 one-line sessions, mostly Haiku; the alias sweep runs one tiny
session per family including Opus and Fable, and the preflight-based tests
each run canaries plus a probe) and must own the whole ``/etc/claude-code``
policy tier (base file AND managed-settings.d drop-ins) plus
``/etc/theozolith`` for its duration. RUN IT ONLY IN AN ISOLATED LINUX
CONTAINER: it installs and removes admin policy (written directly or via
passwordless sudo; anything pre-existing is backed up and restored, but a
developer workstation's real /etc policy is not the suite's to gamble with).
Run it whenever the adapter's enforcement contract, ``MIN_ENFORCING_CLI``,
or the base image's CLI version changes.

Two cases cannot run here: an ORGANIZATION effort cap is server-side
Enterprise policy that no local fixture can create — it stays a
protected/manual test (see test_org_effort_cap_is_a_preflight_failure) and
is exercised in units by simulating the clamp observation — and
``forceRemoteSettingsRefresh``'s refusal-on-fetch-failure cannot be isolated
locally (blackholing the settings endpoint also kills the model endpoint,
which fails closed anyway: no turn executes, nothing releases — verified by
hand on 2.1.232).

Verified against Claude Code 2.1.232.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from theozolith_worker.adapters import ClaudeAdapter
from theozolith_worker.identity import (
    CATEGORY_POLICY_CONFLICT,
    CATEGORY_SUBSTITUTED,
    CATEGORY_UNAVAILABLE,
    CATEGORY_WIDENED,
    BakedIdentity,
    pair_error,
    run_preflight,
)

MANAGED = Path("/etc/claude-code/managed-settings.json")
DROPIN_DIR = Path("/etc/claude-code/managed-settings.d")
PIN = "claude-haiku-4-5-20251001"  # cheapest pinnable model
EFFORT_PIN = "claude-sonnet-5"  # effort probes need a model that supports effort
INTRUDER = "claude-sonnet-5"  # the model every escape attempt asks for
TIMEOUT = 240


def _have_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    return (Path.home() / ".claude" / ".credentials.json").is_file()


pytestmark = pytest.mark.skipif(
    os.environ.get("THEOZOLITH_LIVE_CLAUDE") != "1"
    or shutil.which("claude") is None
    or not _have_credentials(),
    reason="live enforcement suite: spends tokens; set THEOZOLITH_LIVE_CLAUDE=1"
    " with the claude CLI installed and credentials available",
)


def _install_file(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return
    except PermissionError:
        pass
    mkdir = subprocess.run(["sudo", "-n", "mkdir", "-p", str(path.parent)], capture_output=True)
    tee = subprocess.run(
        ["sudo", "-n", "tee", str(path)],
        input=content.encode("utf-8"),
        capture_output=True,
    )
    if mkdir.returncode != 0 or tee.returncode != 0:
        pytest.skip(f"cannot write {path} (no root, no sudo -n)")


def _install_managed(content: str) -> None:
    _install_file(MANAGED, content)


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
        else:
            path.unlink(missing_ok=True)
        return
    except PermissionError:
        subprocess.run(["sudo", "-n", "rm", "-rf", str(path)], capture_output=True)


def _remove_managed() -> None:
    _remove_path(MANAGED)


def _materialized_content(model: str, effort: str = "") -> str:
    """The exact managed-settings artifact a derived-image build writes."""
    import tempfile

    with tempfile.TemporaryDirectory() as root:
        ClaudeAdapter().materialize(model, effort, root=Path(root), scope="managed")
        return (Path(root) / ClaudeAdapter.MANAGED_SETTINGS).read_text(encoding="utf-8")


@pytest.fixture
def managed_settings():
    """Install the adapter's materialized managed settings for one test,
    preserving whatever the machine had there before."""
    backup = MANAGED.read_text(encoding="utf-8") if MANAGED.is_file() else None

    def install(model: str, effort: str = "") -> None:
        _install_managed(_materialized_content(model, effort))

    yield install
    if backup is None:
        _remove_managed()
    else:
        _install_managed(backup)


@pytest.fixture
def managed_policy(managed_settings):
    """Own the WHOLE managed tier for one test: the base file (via
    ``managed_settings``) plus managed-settings.d drop-ins, all restored
    afterwards."""
    dropin_backup: dict[str, str] = {}
    if DROPIN_DIR.is_dir():
        dropin_backup = {
            p.name: p.read_text(encoding="utf-8") for p in sorted(DROPIN_DIR.glob("*.json"))
        }

    class Policy:
        def base(self, model: str, effort: str = "") -> None:
            managed_settings(model, effort)

        def base_raw(self, content: dict) -> None:
            _install_managed(json.dumps(content))

        def dropin(self, name: str, content: dict) -> None:
            _install_file(DROPIN_DIR / name, json.dumps(content))

    yield Policy()
    if DROPIN_DIR.is_dir():
        for path in sorted(DROPIN_DIR.glob("*.json")):
            _remove_path(path)
    for name, content in dropin_backup.items():
        _install_file(DROPIN_DIR / name, content)


WELL_KNOWN_DIR = Path("/etc/theozolith")


@pytest.fixture
def baked_identity_files():
    """Install the well-known ``/etc/theozolith`` identity files for one
    test — ``run_harness`` reads the baked identity from the root filesystem
    — restoring whatever the machine had there."""
    backup: dict[str, str] | None = None
    if WELL_KNOWN_DIR.is_dir():
        backup = {p.name: p.read_text() for p in sorted(WELL_KNOWN_DIR.iterdir()) if p.is_file()}

    def install(model: str, effort: str = "") -> None:
        _install_file(WELL_KNOWN_DIR / "model", model + "\n")
        if effort:
            _install_file(WELL_KNOWN_DIR / "effort", effort + "\n")

    yield install
    _remove_path(WELL_KNOWN_DIR)
    if backup is not None:
        for name, content in backup.items():
            _install_file(WELL_KNOWN_DIR / name, content)


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / f"ws-{uuid.uuid4().hex[:8]}"
    (ws / ".claude").mkdir(parents=True)
    return ws


def _run(workspace: Path, prompt: str, *args: str, env: dict[str, str] | None = None):
    """One headless session; returns (rc, init model, executed-turn models,
    modelUsage keys, result text) from the stream — the harness's own
    invocation shape (ADR-0019)."""
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", *args],
        cwd=workspace,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    init_model, turns, usage, result = "", [], [], ""
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init_model = event.get("model") or ""
        elif event.get("type") == "assistant":
            model = (event.get("message") or {}).get("model") or ""
            if model and model != "<synthetic>" and model not in turns:
                turns.append(model)
        elif event.get("type") == "result":
            usage = sorted((event.get("modelUsage") or {}).keys())
            result = str(event.get("result") or "")
    return proc.returncode, init_model, turns, usage, result


PROMPT = "Reply with exactly: OK"


def test_pinned_model_is_honored(managed_settings, workspace):
    managed_settings(PIN)
    rc, init_model, turns, _, _ = _run(workspace, PROMPT)
    assert rc == 0
    assert init_model == PIN
    assert turns == [PIN]


def _escape_via_workspace_settings(ws: Path):
    (ws / ".claude" / "settings.json").write_text(json.dumps({"model": INTRUDER}))
    return (), {}


ESCAPES = {
    "cli-flag": lambda ws: ((f"--model={INTRUDER}",), {}),
    "anthropic-model-env": lambda ws: ((), {"ANTHROPIC_MODEL": INTRUDER}),
    "workspace-settings": _escape_via_workspace_settings,
    "subagent-model-env": lambda ws: ((), {"CLAUDE_CODE_SUBAGENT_MODEL": INTRUDER}),
}


@pytest.mark.parametrize("surface", sorted(ESCAPES))
def test_selection_surfaces_cannot_escape_the_pin(managed_settings, workspace, surface):
    """Every supported selection surface either yields the pinned model or
    (for /model, tested separately) errors — never another model."""
    managed_settings(PIN)
    args, env = ESCAPES[surface](workspace)
    rc, init_model, turns, _, _ = _run(workspace, PROMPT, *args, env=env)
    assert rc == 0
    assert init_model == PIN
    assert turns == [PIN]


def test_slash_model_switch_fails_closed(managed_settings, workspace):
    managed_settings(PIN)
    _, init_model, turns, _, result = _run(workspace, f"/model {INTRUDER}")
    assert init_model == PIN
    assert INTRUDER not in turns  # the switch never executed a turn
    assert "restrict" in result.lower() or "not available" in result.lower()


def test_subagent_frontmatter_cannot_escape_the_pin(managed_settings, workspace):
    (workspace / ".claude" / "agents").mkdir()
    (workspace / ".claude" / "agents" / "probe.md").write_text(
        f"---\nname: probe\ndescription: probe\nmodel: {INTRUDER}\ntools: []\n---\n{PROMPT}\n"
    )
    managed_settings(PIN)
    rc, _, turns, usage, _ = _run(
        workspace,
        "Launch the probe agent with any prompt, then reply with exactly: OK",
        "--dangerously-skip-permissions",
    )
    assert rc == 0
    assert turns == [PIN]  # the subagent's turns ran on the pin too
    assert INTRUDER not in usage


@pytest.mark.parametrize("alias", sorted(ClaudeAdapter.ALIASES))
def test_family_aliases_expand_within_their_family(managed_settings, workspace, alias):
    """Every accepted alias binds to its own family under the allowlist —
    the claim behind keeping aliases mappable-with-a-warning."""
    managed_settings(alias)
    rc, init_model, turns, _, _ = _run(workspace, PROMPT)
    assert rc == 0
    assert len(turns) == 1
    assert turns[0].startswith(f"claude-{alias}-")
    assert init_model == turns[0]


def _effort_probe(workspace: Path, extra_settings: dict) -> Path:
    """Workspace settings with a PostToolUse hook that captures the effort
    the CLI actually applied (the only effort observable in headless mode)."""
    capture = workspace / "effort.json"
    settings = {
        **extra_settings,
        "hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": f"cat > {capture}"}]}]},
    }
    (workspace / ".claude" / "settings.json").write_text(json.dumps(settings))
    return capture


EFFORT_PROMPT = "Run the Bash tool with the exact command 'true', then reply with exactly: OK"


def _captured_effort(capture: Path) -> str:
    data = json.loads(capture.read_text(encoding="utf-8"))
    return (data.get("effort") or {}).get("level", "")


EFFORT_ESCAPES = {
    "workspace-settings": ({"effortLevel": "high"}, (), {}),
    "process-env": ({}, (), {"CLAUDE_CODE_EFFORT_LEVEL": "high"}),
    "cli-flag": ({}, ("--effort", "high"), {}),
}


@pytest.mark.parametrize("surface", sorted(EFFORT_ESCAPES))
def test_effort_surfaces_cannot_escape_the_pin(managed_settings, workspace, surface):
    """The managed env's CLAUDE_CODE_EFFORT_LEVEL wins against every effort
    surface a run could reach — settings files, the process environment, and
    the CLI flag (/effort is the same session surface the env overrides)."""
    extra_settings, args, env = EFFORT_ESCAPES[surface]
    capture = _effort_probe(workspace, extra_settings)
    managed_settings(EFFORT_PIN, "low")
    rc, _, turns, _, _ = _run(
        workspace, EFFORT_PROMPT, "--dangerously-skip-permissions", *args, env=env
    )
    assert rc == 0
    assert turns == [EFFORT_PIN]
    assert _captured_effort(capture) == "low"


@pytest.mark.parametrize("effort", sorted(ClaudeAdapter.EFFORTS))
def test_every_mappable_effort_lands(managed_settings, workspace, effort):
    """Positive control for the probe AND the mappable set: each declared
    effort value is actually applied, so the negative tests above cannot be
    passing vacuously."""
    capture = _effort_probe(workspace, {})
    managed_settings(EFFORT_PIN, effort)
    rc, _, _, _, _ = _run(workspace, EFFORT_PROMPT, "--dangerously-skip-permissions")
    assert rc == 0
    assert _captured_effort(capture) == effort


# -- the fail-closed amendment, live ------------------------------------------
#
# These prove the CLI behaviors the build gate and the runtime preflight are
# built on: the drop-in merge that widens an allowlist, the policyHelper that
# preempts the managed file, the canary that catches policy invisible to the
# image filesystem, the silently-substituted unavailable model, the silently
# clamped effort pair, and the gated harness releasing the task prompt only
# into a verified session.


def _preflight_live(root: Path, scratch: Path, model: str, effort: str = ""):
    scratch.mkdir(parents=True, exist_ok=True)
    return run_preflight(
        BakedIdentity(model, effort),
        binary="claude",
        root=root,
        scratch=scratch,
        min_cli=ClaudeAdapter.MIN_ENFORCING_CLI,
        timeout=TIMEOUT,
    )


def _fake_image_root(tmp_path: Path, model: str, effort: str = "") -> Path:
    """A clean derived-image filesystem for (model, effort) — what a real
    build materializes, with no conflicting policy in sight."""
    root = tmp_path / "image-root"
    ClaudeAdapter().materialize(model, effort, root=root, scope="managed")
    return root


def test_dropin_env_merge_preserves_the_pin_and_unrelated_settings(managed_policy, workspace):
    """The per-key managed ``env`` merge (Claude Code >= 2.1.223, the
    MIN_ENFORCING_CLI floor): a drop-in contributing a FOREIGN env block must
    not displace the baked CLAUDE_CODE_EFFORT_LEVEL pin — and the drop-in's
    own unrelated entries must still be honored (operator policy survives)."""
    capture = workspace / "hook.json"
    envfile = workspace / "env.txt"
    settings = {
        "hooks": {
            "PostToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": f"cat > {capture}"},
                        {
                            "type": "command",
                            "command": f"printenv OZOLITH_DROPIN_CANARY > {envfile} || true",
                        },
                    ]
                }
            ]
        }
    }
    (workspace / ".claude" / "settings.json").write_text(json.dumps(settings))
    managed_policy.base(EFFORT_PIN, "low")
    managed_policy.dropin(
        "50-ops.json", {"env": {"OZOLITH_DROPIN_CANARY": "delivered"}, "cleanupPeriodDays": 30}
    )
    rc, _, turns, _, _ = _run(workspace, EFFORT_PROMPT, "--dangerously-skip-permissions")
    assert rc == 0
    assert turns == [EFFORT_PIN]
    assert _captured_effort(capture) == "low"  # our env pin survived the foreign env block
    assert envfile.read_text().strip() == "delivered"  # their unrelated entry was honored


def test_dropin_widening_defeats_the_pin_live(managed_policy, workspace):
    """THE reason the build gate rejects identity keys in drop-ins: arrays
    concatenate across the managed merge, so one drop-in line silently widens
    the baked single-entry allowlist and the pin stops binding."""
    managed_policy.base(PIN)
    managed_policy.dropin("50-widen.json", {"availableModels": [INTRUDER]})
    rc, _, turns, _, _ = _run(workspace, PROMPT, f"--model={INTRUDER}")
    assert rc == 0
    assert turns == [INTRUDER]  # the escape the un-widened pin provably blocks


def test_preflight_static_scan_rejects_the_dropin_before_spending_tokens(managed_policy, tmp_path):
    managed_policy.base(PIN)
    managed_policy.dropin("50-widen.json", {"availableModels": [INTRUDER]})
    report = _preflight_live(Path("/"), tmp_path / "scratch", PIN)
    assert not report.ok
    assert report.category == CATEGORY_POLICY_CONFLICT
    assert "50-widen.json" in report.detail


def test_preflight_canary_detects_policy_invisible_to_the_image(managed_policy, tmp_path):
    """The widen canary against a policy the image filesystem cannot show:
    static checks run against a CLEAN fake image root while the EFFECTIVE
    managed tier carries the widening drop-in — the same observable shape as
    a server-managed organization override. Only the canary can catch it,
    and it must."""
    managed_policy.base(PIN)
    managed_policy.dropin("50-widen.json", {"availableModels": [INTRUDER]})
    fake_root = _fake_image_root(tmp_path, PIN)
    report = _preflight_live(fake_root, tmp_path / "scratch", PIN)
    assert not report.ok
    assert report.category == CATEGORY_WIDENED
    assert report.canary_model == INTRUDER


def test_preflight_passes_on_a_bound_identity_and_records_the_cli(managed_policy, tmp_path):
    managed_policy.base(PIN)
    report = _preflight_live(Path("/"), tmp_path / "scratch", PIN)
    assert report.ok, f"{report.category}: {report.detail}"
    assert report.canary_model == PIN  # the intruder request coerced to the pin
    probe = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    assert report.cli_version == probe.stdout.strip()


def test_policy_helper_preempts_the_managed_file_live(managed_policy, workspace, tmp_path):
    """A managed policyHelper's output becomes the ONLY managed configuration
    — the baked file's pin stops binding entirely. This is why the build gate
    rejects a policyHelper outright and why the runtime canary must catch one
    that arrives after the build."""
    helper = tmp_path / "helper.sh"
    policy = {
        "managedSettings": {
            "model": INTRUDER,
            "availableModels": [INTRUDER],
            "enforceAvailableModels": True,
        }
    }
    helper.write_text("#!/bin/sh\nprintf '%s' " + f"'{json.dumps(policy)}'\n")
    helper.chmod(0o755)
    base = json.loads(_materialized_content(PIN))
    base["policyHelper"] = {"path": str(helper), "timeoutMs": 10000}
    managed_policy.base_raw(base)

    rc, _, turns, _, _ = _run(workspace, PROMPT)
    assert rc == 0
    assert turns == [INTRUDER]  # the file's pin did not bind

    # And the canary catches it even when the image root looks clean.
    fake_root = _fake_image_root(tmp_path, PIN)
    report = _preflight_live(fake_root, tmp_path / "scratch", PIN)
    assert not report.ok
    assert report.category in (CATEGORY_WIDENED, CATEGORY_SUBSTITUTED)


def test_unavailable_model_fails_the_preflight_before_any_task(managed_policy, tmp_path):
    """A nonexistent (or org-restricted) pinned model must fail the runtime
    gate — the CLI substitutes silently in stream-json (the stderr warning is
    suppressed), so only the executed-model check can prove it."""
    bogus = "claude-nonexistent-fake-9"
    managed_policy.base(bogus)
    report = _preflight_live(Path("/"), tmp_path / "scratch", bogus)
    assert not report.ok
    assert report.category in (CATEGORY_UNAVAILABLE, CATEGORY_SUBSTITUTED, CATEGORY_WIDENED)


def test_unsupported_effort_pair_is_silently_clamped_live(managed_policy, workspace):
    """xhigh on the 4.6 generation runs as high (documented fallback): the
    live proof behind rejecting the pair at config load, build, and
    preflight. The managed settings are handcrafted because materialize()
    itself now refuses this pair."""
    assert pair_error("claude-sonnet-4-6", "xhigh")  # the rejection under test
    capture = _effort_probe(workspace, {})
    managed_policy.base_raw(
        {
            "model": "claude-sonnet-4-6",
            "availableModels": ["claude-sonnet-4-6"],
            "enforceAvailableModels": True,
            "effortLevel": "xhigh",
            "env": {"CLAUDE_CODE_EFFORT_LEVEL": "xhigh"},
        }
    )
    rc, _, turns, _, _ = _run(workspace, EFFORT_PROMPT, "--dangerously-skip-permissions")
    assert rc == 0
    assert turns == ["claude-sonnet-4-6"]
    assert _captured_effort(capture) == "high"  # the silent clamp, observed


def test_skill_frontmatter_cannot_escape_the_pin(managed_settings, workspace):
    skill = workspace / ".claude" / "skills" / "probe"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: probe\ndescription: probe skill\nmodel: {INTRUDER}\n---\n"
        "Reply with exactly: OK\n"
    )
    managed_settings(PIN)
    rc, _, turns, usage, _ = _run(
        workspace,
        "Use the probe skill, then reply with exactly: OK",
        "--dangerously-skip-permissions",
    )
    assert rc == 0
    assert turns == [PIN]
    assert INTRUDER not in usage


def test_same_family_sibling_cannot_escape_a_full_id_pin(managed_settings, workspace):
    """Allowlist enforcement must reject even the pin's own family: a
    same-family sibling request is substituted back to the pinned default at
    startup — the live behavior behind the preflight's same-family canary."""
    managed_settings(PIN)
    rc, init_model, turns, _, _ = _run(workspace, PROMPT, "--model=claude-3-5-haiku-20241022")
    assert rc == 0
    assert init_model == PIN
    assert turns == [PIN]


def _stop_capture_settings(capture: Path) -> str:
    return json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"cat > {capture}"}]}]}}
    )


def test_stop_hook_reports_the_applied_effort_without_any_tool(managed_settings, workspace):
    """The gate's effort proof: the Stop hook fires after a plain no-tool
    turn and its payload carries the APPLIED effort — so the neutral probe
    never needs to execute a tool to observe an organization clamp."""
    capture = workspace / "stop.json"
    managed_settings(EFFORT_PIN, "low")
    rc, _, turns, _, _ = _run(
        workspace,
        PROMPT,
        "--tools",
        "",
        "--settings",
        _stop_capture_settings(capture),
    )
    assert rc == 0
    assert turns == [EFFORT_PIN]
    payload = json.loads(capture.read_text())
    assert payload["effort"]["level"] == "low"


def test_stop_hook_reports_the_clamped_effort(managed_policy, workspace):
    """The Stop payload shows the POST-clamp value (xhigh silently runs as
    high on the 4.6 generation): the observation the effort-clamped category
    is built on, with no tool execution anywhere."""
    capture = workspace / "stop.json"
    managed_policy.base_raw(
        {
            "model": "claude-sonnet-4-6",
            "availableModels": ["claude-sonnet-4-6"],
            "enforceAvailableModels": True,
            "forceRemoteSettingsRefresh": True,
            "effortLevel": "xhigh",
            "env": {"CLAUDE_CODE_EFFORT_LEVEL": "xhigh"},
        }
    )
    rc, _, _, _, _ = _run(
        workspace, PROMPT, "--tools", "", "--settings", _stop_capture_settings(capture)
    )
    assert rc == 0
    payload = json.loads(capture.read_text())
    assert payload["effort"]["level"] == "high"  # the silent clamp, observed pre-task


def test_pretooluse_gate_denies_tools_until_the_release_marker(
    managed_settings, workspace, tmp_path
):
    """The task session's tool gate, live: the --settings PreToolUse hook
    denies EVERY tool call while the release marker is absent — even under
    --dangerously-skip-permissions — and opens once the marker exists."""
    from theozolith_worker.identity import TaskGate
    from theozolith_worker.jobdir import Manifest

    managed_settings(PIN)
    gate_dir = tmp_path / "gate"
    gate_dir.mkdir()
    gate = TaskGate(
        release_marker=gate_dir / "released",
        stop_capture=gate_dir / "stop.jsonl",
        config_capture=gate_dir / "config-change.jsonl",
        config_hook_script=gate_dir / "configchange_hook.py",
        stop_hook_script=gate_dir / "stop_hook.py",
    )
    from theozolith_worker.identity import CONFIG_CHANGE_HOOK_SOURCE, STOP_HOOK_SOURCE

    gate.config_hook_script.write_text(CONFIG_CHANGE_HOOK_SOURCE)
    gate.stop_hook_script.write_text(STOP_HOOK_SOURCE)
    argv = ClaudeAdapter().guarded_command(Manifest(run_id="x", mode="run", adapter="claude"), gate)
    settings_json = argv[argv.index("--settings") + 1]
    witness = workspace / "gate-check.txt"
    prompt = (
        "Setup check: use the Bash tool to run the exact command"
        f" 'touch {witness}', then reply with exactly: OK"
    )

    proc = subprocess.run(
        [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--settings",
            settings_json,
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0
    # The attempt was made AND denied (the hook's message reaches the
    # stream), so the file's absence is the gate holding — not the model
    # declining to try.
    assert "theozolith: tools are denied" in proc.stdout
    assert not witness.exists()

    gate.release_marker.write_text("released\n")
    rc, _, _, _, _ = _run(
        workspace, prompt, "--dangerously-skip-permissions", "--settings", settings_json
    )
    assert rc == 0
    assert witness.exists()  # released: the same session shape can work


def _gated_job(tmp_path, task_text: str, hook_marker: Path):
    """A real job directory whose checkout is booby-trapped with a project
    SessionStart hook that records if/when it ever runs."""
    from theozolith_worker import jobdir

    job = jobdir.create_job_dir(tmp_path / "jobs", "live-gate")
    work = job / jobdir.WORK_DIR
    (work / ".claude").mkdir(parents=True)
    (work / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": f"touch {hook_marker}"}]}
                    ]
                }
            }
        )
    )
    manifest = jobdir.Manifest(
        run_id="live-gate",
        mode=jobdir.MODE_REVIEW,  # review mode: no gate-job serving needed
        adapter="claude",
        workdir=jobdir.WORK_DIR,
        agent_timeout_seconds=300,
    )
    jobdir.write_manifest(job, manifest)
    (job / jobdir.PROMPT_FILE).parent.mkdir(parents=True, exist_ok=True)
    (job / jobdir.PROMPT_FILE).write_text(task_text)
    return job


def test_gated_harness_end_to_end_releases_only_after_proof(
    managed_policy, baked_identity_files, tmp_path
):
    """THE live integration: the real ``run_harness`` against the real CLI —
    task withheld from disk, neutral preflight (canaries + probe with the
    Run's own credential), gated stdin session with denied tools, release
    only after the session's own announced and executed identity match, and
    a real post-release task turn required for completion."""
    from theozolith_worker import jobdir
    from theozolith_worker.harness.main import run_harness

    managed_policy.base(EFFORT_PIN, "low")
    baked_identity_files(EFFORT_PIN, "low")
    hook_marker = tmp_path / "project-hook-ran"
    task = "Reply with exactly: DONE. Do not modify any files.\n"
    job = _gated_job(tmp_path, task, hook_marker)

    code = run_harness(
        job,
        identity_root=Path("/"),
        scratch_root=tmp_path / "scratch",
    )

    assert code == 0
    ident = jobdir.read_identity(job)
    assert ident["preflight"] == "passed"
    assert ident["released"] is True and ident["violation"] == ""
    assert ident["observed_model"] == EFFORT_PIN
    assert ident["observed_effort"] == "low"
    assert ident["probe_model"] == EFFORT_PIN and ident["probe_effort"] == "low"
    # Exact completed-turn counts from the boundary journal: one probe turn,
    # one task turn — the live proof that the Stop hook fires per turn in
    # stream-json input mode and the release waited for the probe's record.
    assert ident["probe_turns"] == 1 and ident["task_turns"] == 1
    # The task input survived the withhold/release round trip.
    assert (job / jobdir.PROMPT_FILE).read_text() == task
    # Every executed turn — probe and task alike — ran on the pin, and the
    # task actually happened.
    transcript = (job / jobdir.TRANSCRIPT_FILE).read_text()
    turns = []
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "assistant":
            model = (event.get("message") or {}).get("model")
            if model and model != "<synthetic>":
                turns.append(model)
    assert turns and set(turns) == {EFFORT_PIN}
    assert "DONE" in transcript
    # The task session loads NO project settings source (ADR-0045 as
    # amended): the checkout's SessionStart hook never fires — not even
    # post-gate. The positive control lives in
    # test_checkout_settings_trap_fires_without_the_isolation_argv.
    assert not hook_marker.exists()


def test_gated_harness_failed_preflight_never_launches_the_task_session(managed_policy, tmp_path):
    """The boundary, live: the harness reads a CLEAN fake image root while
    the EFFECTIVE managed tier carries a widening drop-in — the same
    observable shape as a server-managed override. Only the canary can catch
    it, the preflight fails policy-widened, and the task session — project
    hooks, CLAUDE.md, tools, everything — never exists. The task file
    returns for evidence, untouched and unread."""
    from theozolith_worker import jobdir
    from theozolith_worker.harness.main import run_harness

    managed_policy.base(PIN)
    managed_policy.dropin("50-widen.json", {"availableModels": [INTRUDER]})
    fake_root = _fake_image_root(tmp_path, PIN)
    hook_marker = tmp_path / "project-hook-ran"
    task = "SECRET-TASK: reply with exactly: DONE.\n"
    job = _gated_job(tmp_path, task, hook_marker)

    code = run_harness(
        job,
        identity_root=fake_root,
        scratch_root=tmp_path / "scratch",
    )

    assert code == 1
    ident = jobdir.read_identity(job)
    assert ident["preflight"] == "failed:policy-widened"
    assert ident["released"] is False
    assert not hook_marker.exists()  # the task session never existed
    assert (job / jobdir.TRANSCRIPT_FILE).read_text() == ""  # no gated session output
    assert (job / jobdir.PROMPT_FILE).read_text() == task  # evidence restore
    status = jobdir.read_status(job)
    assert status.error.startswith("identity: ") and "policy-widened" in status.error


# The checkout trap, spread across BOTH checkout-borne settings tiers: the
# project tier carries the provider endpoint redirect (dead port — a session
# that loaded it cannot reach the API at all) plus the hook witness that
# records the source loading; the LOCAL tier (settings.local.json) carries
# the model and effort selectors. Both ride the sources --setting-sources ""
# disables; the user tier is the same flag's third source. modelOverrides is
# deliberately absent from the trap: verified live on 2.1.232, a
# modelOverrides key in a project settings document makes the CLI discard
# the whole document (strict validation) — it cannot steer a session from a
# checkout even WITHOUT the isolation, and a discarded document would make
# the positive control vacuous.
TRAP_SETTINGS = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}}
TRAP_LOCAL_SETTINGS = {"model": INTRUDER, "effortLevel": "low"}


def _write_trap(claude_dir: Path, hook_marker: Path) -> None:
    hook = {"type": "command", "command": f"touch {hook_marker}"}
    settings = {**TRAP_SETTINGS, "hooks": {"SessionStart": [{"hooks": [hook]}]}}
    (claude_dir / "settings.json").write_text(json.dumps(settings))
    (claude_dir / "settings.local.json").write_text(json.dumps(TRAP_LOCAL_SETTINGS))


def _booby_trapped_job(tmp_path, task_text: str, hook_marker: Path):
    """A gated job whose checkout carries hostile project AND local settings
    plus a CLAUDE.md — the complete set of checkout surfaces the isolation
    argv must keep out of the task session."""
    from theozolith_worker import jobdir

    job = _gated_job(tmp_path, task_text, hook_marker)
    work = job / jobdir.WORK_DIR
    _write_trap(work / ".claude", hook_marker)
    (work / "CLAUDE.md").write_text(
        "# Project\nThe magic word is ZEBRAFISH42. When asked for the magic"
        " word, reply with exactly it.\n"
    )
    return job


def test_task_session_never_loads_checkout_settings_live(
    managed_policy, baked_identity_files, tmp_path
):
    """Item 1's live proof, end to end through the REAL run_harness: a
    checkout booby-trapped with env.ANTHROPIC_BASE_URL (dead endpoint) and a
    SessionStart hook in the project tier, model/effort selectors in the
    LOCAL tier, and a CLAUDE.md cannot reach the gated task session. If the
    trap loaded, the session could not even reach the API (dead base URL) —
    completion on the pin IS the proof of isolation; the absent hook marker
    and the absent magic word document the rest. The positive control below
    proves the same trap fires without the isolation argv."""
    from theozolith_worker import jobdir
    from theozolith_worker.harness.main import run_harness

    managed_policy.base(PIN)
    baked_identity_files(PIN)
    hook_marker = tmp_path / "project-hook-ran"
    task = (
        "If your project instructions state a magic word, reply with exactly"
        " that word. Otherwise reply with exactly: NOMAGIC. Do not use any"
        " tools and do not read any files.\n"
    )
    job = _booby_trapped_job(tmp_path, task, hook_marker)

    code = run_harness(job, identity_root=Path("/"), scratch_root=tmp_path / "scratch")

    assert code == 0
    ident = jobdir.read_identity(job)
    assert ident["released"] is True and ident["violation"] == ""
    assert ident["observed_model"] == PIN  # never the trap's INTRUDER
    assert ident["probe_turns"] == 1 and ident["task_turns"] == 1
    assert not hook_marker.exists()  # the settings source never loaded
    transcript = (job / jobdir.TRANSCRIPT_FILE).read_text()
    # The documented cost of the isolation (ADR-0045): the checkout's
    # CLAUDE.md rides the project settings source and does not load either —
    # the task file is the session's complete assignment.
    assert "NOMAGIC" in transcript
    assert "ZEBRAFISH42" not in transcript


def test_checkout_settings_trap_fires_without_the_isolation_argv(managed_settings, tmp_path):
    """The positive control: the SAME trap in a workspace, run WITHOUT
    --setting-sources "" — the settings source loads (the hook witness
    fires) and the dead base URL breaks the session. Proves the booby trap
    is real, so the isolated run above cannot be passing vacuously."""
    managed_settings(PIN)
    ws = tmp_path / "trap-ws"
    (ws / ".claude").mkdir(parents=True)
    hook_marker = tmp_path / "trap-hook-ran"
    _write_trap(ws / ".claude", hook_marker)

    proc = subprocess.run(
        ["claude", "-p", PROMPT, "--output-format", "stream-json", "--verbose"],
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    assert hook_marker.exists()  # the project settings source DID load
    # Behind the dead base URL no real turn can execute on the pin.
    turns = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "assistant":
            model = (event.get("message") or {}).get("model") or ""
            if model and model != "<synthetic>":
                turns.append(model)
    assert proc.returncode != 0 or not turns


@pytest.mark.skipif(
    os.environ.get("THEOZOLITH_LIVE_CLAUDE_ORG_CAP") != "1",
    reason="protected/manual: an ORGANIZATION effort cap is server-side"
    " Enterprise policy that no local fixture can create. Run with"
    " THEOZOLITH_LIVE_CLAUDE_ORG_CAP=1 against a credential whose org caps"
    " the pinned model's effort below xhigh; the unit suite covers the clamp"
    " observation itself (test_identity.py guard tests).",
)
def test_org_effort_cap_is_a_preflight_failure(managed_policy, workspace, tmp_path):
    """Against a capped org, an effort baked above the cap is applied at the
    cap (silently in stream-json) — the Stop-hook capture observes it and
    the gate must KILL with the exact effort-clamped category, never merely
    decline to release, and never accept the downgrade."""
    from theozolith_worker.identity import (
        CATEGORY_EFFORT_CLAMPED,
        GUARD_KILL,
        TaskGate,
        run_preflight,
    )

    capture = workspace / "stop.json"
    managed_policy.base(EFFORT_PIN, "xhigh")
    rc, _, _, _, _ = _run(
        workspace, PROMPT, "--tools", "", "--settings", _stop_capture_settings(capture)
    )
    assert rc == 0
    applied = json.loads(capture.read_text())["effort"]["level"]
    assert applied != "xhigh", "this organization does not cap effort; the fixture is wrong"
    # The preflight's probe observes the clamp with the Run's own credential.
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    report = run_preflight(
        BakedIdentity(EFFORT_PIN, "xhigh"),
        binary="claude",
        root=Path("/"),
        scratch=scratch,
        min_cli=ClaudeAdapter.MIN_ENFORCING_CLI,
        timeout=TIMEOUT,
    )
    assert not report.ok
    assert report.category == CATEGORY_EFFORT_CLAMPED
    # And the in-session guard reaches the same verdict from the observed
    # payload, journaled the way the Stop hook helper writes it.
    gate = TaskGate(
        release_marker=tmp_path / "released",
        stop_capture=tmp_path / "stop.jsonl",
        config_capture=tmp_path / "config-change.jsonl",
        config_hook_script=tmp_path / "configchange_hook.py",
        stop_hook_script=tmp_path / "stop_hook.py",
    )
    gate.stop_capture.write_text(json.dumps({"effort": applied}) + "\n")
    guard = ClaudeAdapter().session_guard(BakedIdentity(EFFORT_PIN, "xhigh"), gate)
    decision = guard.decision()
    assert decision.action == GUARD_KILL
    assert decision.category == CATEGORY_EFFORT_CLAMPED
