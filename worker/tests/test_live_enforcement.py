"""Live proof that the baked model/effort machinery behaves as claimed on a
real Claude Code CLI (ADR-0045, best-effort doctrine). Each test
materializes managed settings through the adapter's own ``materialize()`` —
the same artifact a derived-image build writes — installs them at the CLI's
managed path, runs a real ``claude -p`` session, and asserts the executed
model/effort from the stream (the same signals ``stream_stats`` and the
session monitor read).

What the suite pins, post-consolidation:

- the managed ``model`` DEFAULT binds the main-agent session and outranks a
  checkout's ``.claude/settings.json``/``settings.local.json`` model (the
  selection mechanism, no allowlist);
- ``--model`` DOES escape the default — the documented gap: the harness
  never passes it, and a wrong main-agent turn is the monitor's fail-loud
  territory;
- subagents run their own frontmatter models while the main agent stays on
  the default (main-agent-only enforcement — the deliberately FLIPPED
  claim);
- the managed-env effort pin still beats every effort surface, the per-key
  managed env merge keeps it alive beside foreign drop-in env blocks, and
  the Stop-hook payload reports the applied (post-clamp) effort after a
  plain no-tool turn;
- the setup dry-run (``run_preflight`` and the harness identity-dryrun
  mode) passes on a healthy image and fails loud on a bogus model;
- the monitored ``run_harness`` runs the task NORMALLY: checkout CLAUDE.md
  reaches the session (the restored capability), checkout hooks fire, and
  the identity record lands with the observed model and applied effort.

Deliberately opt-in (``THEOZOLITH_LIVE_CLAUDE=1``): the suite spends real
tokens (~22 sessions, mostly Haiku) and must own the whole
``/etc/claude-code`` policy tier (base file AND managed-settings.d
drop-ins) plus ``/etc/theozolith`` for its duration. RUN IT ONLY IN AN
ISOLATED LINUX CONTAINER: it installs and removes admin policy (written
directly or via passwordless sudo; anything pre-existing is backed up and
restored, but a developer workstation's real /etc policy is not the
suite's to gamble with). Run it whenever the adapter's identity contract,
``MIN_ENFORCING_CLI``, or the base image's CLI version changes.

One case cannot run here: an ORGANIZATION effort cap is server-side
Enterprise policy that no local fixture can create — it stays a
protected/manual test (see test_org_effort_cap_fails_the_dry_run) and is
exercised in units by simulating the clamp observation.

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
    CATEGORY_SUBSTITUTED,
    CATEGORY_UNAVAILABLE,
    BakedIdentity,
    pair_error,
    run_preflight,
)

MANAGED = Path("/etc/claude-code/managed-settings.json")
DROPIN_DIR = Path("/etc/claude-code/managed-settings.d")
PIN = "claude-haiku-4-5-20251001"  # cheapest pinnable model
EFFORT_PIN = "claude-sonnet-5"  # effort probes need a model that supports effort
OTHER = "claude-sonnet-5"  # the model selectors and subagents ask for
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
    """One headless session; returns (rc, init model, MAIN-agent turn
    models, subagent turn models, modelUsage keys, result text) from the
    stream — the harness's own invocation shape (ADR-0019), with the
    main/subagent split the identity machinery relies on."""
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", *args],
        cwd=workspace,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    init_model, main_turns, sub_turns, usage, result = "", [], [], [], ""
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
            if not model or model == "<synthetic>":
                continue
            bucket = sub_turns if event.get("parent_tool_use_id") else main_turns
            if model not in bucket:
                bucket.append(model)
        elif event.get("type") == "result":
            usage = sorted((event.get("modelUsage") or {}).keys())
            result = str(event.get("result") or "")
    return proc.returncode, init_model, main_turns, sub_turns, usage, result


PROMPT = "Reply with exactly: OK"


# -- the selection mechanism: a managed model DEFAULT ---------------------------


def test_managed_default_binds_the_main_session(managed_settings, workspace):
    managed_settings(PIN)
    rc, init_model, main_turns, _, _, _ = _run(workspace, PROMPT)
    assert rc == 0
    assert init_model == PIN
    assert main_turns == [PIN]


def test_workspace_settings_cannot_displace_the_managed_default(managed_settings, workspace):
    """THE precedence claim the selection mechanism rests on (verified here,
    not assumed): the managed tier outranks the checkout's project AND local
    settings for the same key — a checked-in model selector does not move
    the main agent."""
    (workspace / ".claude" / "settings.json").write_text(json.dumps({"model": OTHER}))
    (workspace / ".claude" / "settings.local.json").write_text(json.dumps({"model": OTHER}))
    managed_settings(PIN)
    rc, init_model, main_turns, _, _, _ = _run(workspace, PROMPT)
    assert rc == 0
    assert init_model == PIN
    assert main_turns == [PIN]


def test_cli_flag_escapes_the_default_by_design(managed_settings, workspace):
    """The documented gap, pinned honestly: --model DOES beat the managed
    default. The harness never passes it (every invocation surface was
    removed), and a main-agent turn off the baked model is exactly what the
    fail-loud monitor kills — this is where best effort ends and detection
    takes over."""
    managed_settings(PIN)
    rc, _, main_turns, _, _, _ = _run(workspace, PROMPT, f"--model={OTHER}")
    assert rc == 0
    assert main_turns == [OTHER]


def test_subagent_frontmatter_uses_its_own_model(managed_settings, workspace):
    """Main-agent-only enforcement, the deliberately FLIPPED claim: a
    subagent declaring its own model RUNS on it (a capability, not an
    escape) while the main agent stays on the managed default."""
    (workspace / ".claude" / "agents").mkdir()
    (workspace / ".claude" / "agents" / "probe.md").write_text(
        f"---\nname: probe\ndescription: probe\nmodel: {OTHER}\ntools: []\n---\n{PROMPT}\n"
    )
    managed_settings(PIN)
    rc, _, main_turns, sub_turns, usage, _ = _run(
        workspace,
        "Launch the probe agent with any prompt, then reply with exactly: OK",
        "--dangerously-skip-permissions",
    )
    assert rc == 0
    assert main_turns == [PIN]  # the main agent held the default
    assert any(model.startswith(OTHER) for model in sub_turns + usage)


@pytest.mark.parametrize("alias", sorted(ClaudeAdapter.ALIASES))
def test_family_aliases_expand_within_their_family(managed_settings, workspace, alias):
    """Every accepted alias resolves the default to its own family — the
    claim behind keeping aliases mappable-with-a-warning."""
    managed_settings(alias)
    rc, _, main_turns, _, _, _ = _run(workspace, PROMPT)
    assert rc == 0
    assert len(main_turns) == 1
    assert main_turns[0].startswith(f"claude-{alias}-")


# -- the effort pin (still enforcement: managed env wins) -----------------------


def _effort_probe(workspace: Path, extra_settings: dict) -> Path:
    """Workspace settings with a PostToolUse hook that captures the effort
    the CLI actually applied."""
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
    rc, _, main_turns, _, _, _ = _run(
        workspace, EFFORT_PROMPT, "--dangerously-skip-permissions", *args, env=env
    )
    assert rc == 0
    assert main_turns == [EFFORT_PIN]
    assert _captured_effort(capture) == "low"


@pytest.mark.parametrize("effort", sorted(ClaudeAdapter.EFFORTS))
def test_every_mappable_effort_lands(managed_settings, workspace, effort):
    """Positive control for the probe AND the mappable set: each declared
    effort value is actually applied, so the negative tests above cannot be
    passing vacuously."""
    capture = _effort_probe(workspace, {})
    managed_settings(EFFORT_PIN, effort)
    rc, _, _, _, _, _ = _run(workspace, EFFORT_PROMPT, "--dangerously-skip-permissions")
    assert rc == 0
    assert _captured_effort(capture) == effort


def test_dropin_env_merge_preserves_the_pin_and_unrelated_settings(managed_policy, workspace):
    """The per-key managed ``env`` merge (Claude Code >= 2.1.223): a drop-in
    contributing a FOREIGN env block must not displace the baked
    CLAUDE_CODE_EFFORT_LEVEL pin — and the drop-in's own unrelated entries
    must still be honored (operator policy survives)."""
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
    rc, _, main_turns, _, _, _ = _run(workspace, EFFORT_PROMPT, "--dangerously-skip-permissions")
    assert rc == 0
    assert main_turns == [EFFORT_PIN]
    assert _captured_effort(capture) == "low"  # our env pin survived the foreign env block
    assert envfile.read_text().strip() == "delivered"  # their unrelated entry was honored


def _stop_capture_settings(capture: Path) -> str:
    return json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"cat > {capture}"}]}]}}
    )


def test_stop_hook_reports_the_applied_effort_without_any_tool(managed_settings, workspace):
    """The applied-effort observation channel: the Stop hook fires after a
    plain no-tool turn and its payload carries the APPLIED effort — the
    dry-run probe and the per-Run journal both ride this."""
    capture = workspace / "stop.json"
    managed_settings(EFFORT_PIN, "low")
    rc, _, main_turns, _, _, _ = _run(
        workspace,
        PROMPT,
        "--tools",
        "",
        "--settings",
        _stop_capture_settings(capture),
    )
    assert rc == 0
    assert main_turns == [EFFORT_PIN]
    payload = json.loads(capture.read_text())
    assert payload["effort"]["level"] == "low"


def test_stop_hook_reports_the_clamped_effort(managed_policy, workspace):
    """The Stop payload shows the POST-clamp value (xhigh silently runs as
    high on the 4.6 generation): the observation the effort-clamped category
    is built on, with no tool execution anywhere."""
    capture = workspace / "stop.json"
    assert pair_error("claude-sonnet-4-6", "xhigh")  # the rejection under test
    managed_policy.base_raw(
        {
            "model": "claude-sonnet-4-6",
            "effortLevel": "xhigh",
            "env": {"CLAUDE_CODE_EFFORT_LEVEL": "xhigh"},
        }
    )
    rc, _, _, _, _, _ = _run(
        workspace, PROMPT, "--tools", "", "--settings", _stop_capture_settings(capture)
    )
    assert rc == 0
    payload = json.loads(capture.read_text())
    assert payload["effort"]["level"] == "high"  # the silent clamp, observed


# -- the setup dry-run -----------------------------------------------------------


def _fake_image_root(tmp_path: Path, model: str, effort: str = "") -> Path:
    """A clean derived-image filesystem for (model, effort) — what a real
    build materializes."""
    root = tmp_path / "image-root"
    ClaudeAdapter().materialize(model, effort, root=root, scope="managed")
    return root


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


def test_dry_run_passes_on_a_healthy_image_and_records_the_cli(managed_policy, tmp_path):
    managed_policy.base(PIN)
    report = _preflight_live(Path("/"), tmp_path / "scratch", PIN)
    assert report.ok, f"{report.category}: {report.detail}"
    assert report.probe_model == PIN
    probe = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    assert report.cli_version == probe.stdout.strip()


def test_dry_run_fails_loud_on_a_bogus_model(managed_policy, tmp_path):
    """A nonexistent (or org-restricted) model must fail the dry-run at
    worker setup — before any issue or claim is spent."""
    bogus = "claude-nonexistent-fake-9"
    managed_policy.base(bogus)
    report = _preflight_live(Path("/"), tmp_path / "scratch", bogus)
    assert not report.ok
    assert report.category in (CATEGORY_UNAVAILABLE, CATEGORY_SUBSTITUTED)


def test_harness_dryrun_mode_live(managed_policy, baked_identity_files, tmp_path):
    """The identity-dryrun manifest mode end to end: manifest in, one probe
    session, identity.json + done status out — the exact container the
    driver commissions once per boot."""
    from theozolith_worker import jobdir
    from theozolith_worker.harness.main import run_harness

    managed_policy.base(PIN)
    baked_identity_files(PIN)
    job = jobdir.create_job_dir(tmp_path / "jobs", "dryrun")
    jobdir.write_manifest(
        job, jobdir.Manifest(run_id="dryrun", mode=jobdir.MODE_DRYRUN, adapter="claude")
    )

    code = run_harness(job, identity_root=Path("/"), scratch_root=tmp_path / "scratch")

    assert code == 0
    ident = jobdir.read_identity(job)
    assert ident["dry_run"] == "passed"
    assert ident["expected_model"] == PIN and ident["probe_model"] == PIN


# -- the monitored task session, end to end ---------------------------------------


def _monitored_job(tmp_path, task_text: str, hook_marker: Path):
    """A real job directory whose checkout carries a CLAUDE.md and a project
    SessionStart hook — capabilities the task session now KEEPS."""
    from theozolith_worker import jobdir

    job = jobdir.create_job_dir(tmp_path / "jobs", "live-monitor")
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
    (work / "CLAUDE.md").write_text(
        "# Project\nThe magic word is ZEBRAFISH42. When asked for the magic"
        " word, reply with exactly it.\n"
    )
    manifest = jobdir.Manifest(
        run_id="live-monitor",
        mode=jobdir.MODE_REVIEW,  # review mode: no gate-job serving needed
        adapter="claude",
        workdir=jobdir.WORK_DIR,
        agent_timeout_seconds=300,
    )
    jobdir.write_manifest(job, manifest)
    (job / jobdir.PROMPT_FILE).parent.mkdir(parents=True, exist_ok=True)
    (job / jobdir.PROMPT_FILE).write_text(task_text)
    return job


def test_monitored_harness_end_to_end_live(managed_policy, baked_identity_files, tmp_path):
    """THE live integration: the real ``run_harness`` against the real CLI —
    an ordinary launch (task in the argv pointer, nothing withheld), the
    checkout's CLAUDE.md REACHING the session (the capability the
    consolidation restored), checkout hooks firing, the applied effort
    journaled by the Stop hook, and a clean identity record."""
    from theozolith_worker import jobdir
    from theozolith_worker.harness.main import run_harness

    managed_policy.base(EFFORT_PIN, "low")
    baked_identity_files(EFFORT_PIN, "low")
    hook_marker = tmp_path / "project-hook-ran"
    task = (
        "If your project instructions state a magic word, reply with exactly"
        " that word. Otherwise reply with exactly: NOMAGIC. Do not use any"
        " tools.\n"
    )
    job = _monitored_job(tmp_path, task, hook_marker)

    code = run_harness(job, identity_root=Path("/"), scratch_root=tmp_path / "scratch")

    assert code == 0
    ident = jobdir.read_identity(job)
    assert ident["checks"] == "passed"
    assert ident["violation"] == "" and ident["category"] == ""
    assert ident["observed_model"] == EFFORT_PIN
    assert ident["observed_effort"] == "low"  # the Stop journal observation
    assert ident["notes"] == []
    transcript = (job / jobdir.TRANSCRIPT_FILE).read_text()
    # The restored capability: checkout CLAUDE.md loads in the task session.
    assert "ZEBRAFISH42" in transcript
    # And checkout hooks fire — the session keeps its normal capabilities.
    assert hook_marker.exists()
    # The task file never left the disk.
    assert (job / jobdir.PROMPT_FILE).read_text() == task


@pytest.mark.skipif(
    os.environ.get("THEOZOLITH_LIVE_CLAUDE_ORG_CAP") != "1",
    reason="protected/manual: an ORGANIZATION effort cap is server-side"
    " Enterprise policy that no local fixture can create. Run with"
    " THEOZOLITH_LIVE_CLAUDE_ORG_CAP=1 against a credential whose org caps"
    " the pinned model's effort below xhigh; the unit suite covers the clamp"
    " observation itself (test_identity.py monitor and journal tests).",
)
def test_org_effort_cap_fails_the_dry_run(managed_policy, workspace, tmp_path):
    """Against a capped org, an effort baked above the cap is applied at the
    cap (silently in stream-json) — the Stop-hook capture observes it and
    the dry-run must fail with the exact effort-clamped category, never
    accept the downgrade."""
    from theozolith_worker.identity import CATEGORY_EFFORT_CLAMPED

    capture = workspace / "stop.json"
    managed_policy.base(EFFORT_PIN, "xhigh")
    rc, _, _, _, _, _ = _run(
        workspace, PROMPT, "--tools", "", "--settings", _stop_capture_settings(capture)
    )
    assert rc == 0
    applied = json.loads(capture.read_text())["effort"]["level"]
    assert applied != "xhigh", "this organization does not cap effort; the fixture is wrong"
    report = _preflight_live(Path("/"), tmp_path / "scratch", EFFORT_PIN, "xhigh")
    assert not report.ok
    assert report.category == CATEGORY_EFFORT_CLAMPED
