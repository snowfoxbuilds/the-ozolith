"""Live proof that the baked model/effort BIND on a real Claude Code CLI
(ADR-0045). Each test materializes managed settings through the adapter's own
``materialize()`` — the same artifact a derived-image build writes — installs
them at the CLI's managed path, runs a real ``claude -p`` session, and asserts
the executed model/effort from the stream (the same signals ``stream_stats``
reconciles).

Deliberately opt-in (``THEOZOLITH_LIVE_CLAUDE=1``): the suite spends real
tokens (~15 one-line sessions, mostly Haiku; the alias sweep runs one tiny
session per family including Opus and Fable) and must own
``/etc/claude-code/managed-settings.json`` for its duration (written directly
or via passwordless sudo; any pre-existing file is backed up and restored).
Run it on the supported Linux runner whenever the adapter's enforcement
contract, ``MIN_ENFORCING_CLI``, or the base image's CLI version changes.

Verified against Claude Code 2.1.231.
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

MANAGED = Path("/etc/claude-code/managed-settings.json")
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


def _install_managed(content: str) -> None:
    try:
        MANAGED.parent.mkdir(parents=True, exist_ok=True)
        MANAGED.write_text(content, encoding="utf-8")
        return
    except PermissionError:
        pass
    mkdir = subprocess.run(["sudo", "-n", "mkdir", "-p", str(MANAGED.parent)], capture_output=True)
    tee = subprocess.run(
        ["sudo", "-n", "tee", str(MANAGED)],
        input=content.encode("utf-8"),
        capture_output=True,
    )
    if mkdir.returncode != 0 or tee.returncode != 0:
        pytest.skip("cannot write /etc/claude-code/managed-settings.json (no root, no sudo -n)")


def _remove_managed() -> None:
    try:
        MANAGED.unlink(missing_ok=True)
        return
    except PermissionError:
        subprocess.run(["sudo", "-n", "rm", "-f", str(MANAGED)], capture_output=True)


@pytest.fixture
def managed_settings():
    """Install the adapter's materialized managed settings for one test,
    preserving whatever the machine had there before."""
    backup = MANAGED.read_text(encoding="utf-8") if MANAGED.is_file() else None

    def install(model: str, effort: str = "") -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            ClaudeAdapter().materialize(model, effort, root=Path(root), scope="managed")
            content = (Path(root) / ClaudeAdapter.MANAGED_SETTINGS).read_text(encoding="utf-8")
        _install_managed(content)

    yield install
    if backup is None:
        _remove_managed()
    else:
        _install_managed(backup)


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
