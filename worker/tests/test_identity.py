"""The baked-identity machinery (ADR-0045, best effort).

Unit tests for identity.py: the managed-tier conflict scan the build gate
runs (and its runtime re-check with legacy-image tolerance), the
process-environment audit, the pair-aware (model, effort) capability table,
the baked-identity reader, the free per-Run static checks, the setup
dry-run with a scripted subprocess runner, the hook helpers, and the
fail-loud session monitor. Live proof that the real CLI behaves as these
tests assume is test_live_enforcement.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from theozolith_worker.identity import (
    CATEGORY_CLI_TOO_OLD,
    CATEGORY_CONFIG_CHANGED,
    CATEGORY_EFFORT_CLAMPED,
    CATEGORY_INCONSISTENT,
    CATEGORY_PAIR_INVALID,
    CATEGORY_POLICY_CONFLICT,
    CATEGORY_SUBSTITUTED,
    CATEGORY_TIMEOUT,
    CATEGORY_UNAVAILABLE,
    CATEGORY_UNVERIFIABLE,
    CONFIG_CHANGE_HOOK_SOURCE,
    PROBE_PROMPT,
    STOP_HOOK_SOURCE,
    BakedIdentity,
    ClaudeSessionMonitor,
    IdentityError,
    MonitorHooks,
    effort_capability,
    identity_error_detail,
    model_matches,
    normalize_model,
    pair_error,
    read_baked_identity,
    read_last_journal_effort,
    run_preflight,
    scan_managed_conflicts,
    scan_process_environment,
    static_identity_report,
)

MANAGED = "etc/claude-code/managed-settings.json"
DROPINS = "etc/claude-code/managed-settings.d"
MIN_CLI = (2, 1, 232)


def _write(root: Path, relpath: str, data) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def _bake(root: Path, model: str, effort: str = "") -> None:
    """The exact artifact materialize() writes for (model, effort): a
    managed model DEFAULT (main-agent selection), no allowlist."""
    _write(root, "etc/theozolith/model", model + "\n")
    settings: dict = {"model": model}
    if effort:
        _write(root, "etc/theozolith/effort", effort + "\n")
        settings["effortLevel"] = effort
        settings["env"] = {"CLAUDE_CODE_EFFORT_LEVEL": effort}
    _write(root, MANAGED, settings)


def _bake_legacy(root: Path, model: str, effort: str = "") -> None:
    """The artifact PRE-consolidation builds wrote: the single-entry
    allowlist plus the freshness key. Tolerated (stricter, not conflicting)
    until the image is rebuilt."""
    _bake(root, model, effort)
    document = json.loads((root / MANAGED).read_text())
    document["availableModels"] = [model]
    document["enforceAvailableModels"] = True
    document["forceRemoteSettingsRefresh"] = True
    _write(root, MANAGED, document)


# -- the managed-tier conflict scan (build gate) ------------------------------


def test_scan_is_empty_on_a_clean_tree(tmp_path):
    assert scan_managed_conflicts(tmp_path) == []
    _write(tmp_path, MANAGED, {"permissions": {"deny": ["WebSearch"]}, "env": {"FOO": "1"}})
    _write(tmp_path, f"{DROPINS}/10-telemetry.json", {"cleanupPeriodDays": 30})
    assert scan_managed_conflicts(tmp_path) == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("model", "claude-opus-5"),
        ("availableModels", ["claude-opus-5"]),
        ("enforceAvailableModels", True),
        ("fallbackModel", ["claude-sonnet-5"]),
        ("effortLevel", "high"),
        ("modelOverrides", {"claude-opus-5": "arn:aws:bedrock:us-east-1:1:profile/x"}),
        ("policyHelper", {"path": "/usr/local/bin/helper"}),
        ("policyHelpers", {"linux": {"path": "/usr/local/bin/helper"}}),
    ],
)
def test_scan_flags_every_identity_key_in_the_base_file(tmp_path, key, value):
    _write(tmp_path, MANAGED, {key: value})
    conflicts = scan_managed_conflicts(tmp_path)
    assert len(conflicts) == 1
    # The diagnostic contract: the file and the key, never the value.
    assert MANAGED in conflicts[0] and f"'{key}'" in conflicts[0]
    if isinstance(value, str):
        assert value not in conflicts[0]


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ],
)
def test_scan_flags_identity_env_keys(tmp_path, name):
    # Model/effort selectors AND provider/endpoint redirects in the MANAGED
    # tier would steer every session in the image — the build refuses to
    # bake an identity on top of them.
    _write(tmp_path, f"{DROPINS}/50-ops.json", {"env": {name: "anything", "UNRELATED": "ok"}})
    conflicts = scan_managed_conflicts(tmp_path)
    assert len(conflicts) == 1
    assert f"'{name}'" in conflicts[0] and "50-ops.json" in conflicts[0]
    assert "anything" not in conflicts[0]  # values never leak


def test_scan_covers_dropins_in_merge_order(tmp_path):
    # Claude Code merges the base file first, then *.json alphabetically.
    # Every fragment is scanned; conflicts come back in merge order so the
    # first-named file is the first the CLI would apply.
    _write(tmp_path, MANAGED, {"model": "claude-opus-5"})
    _write(tmp_path, f"{DROPINS}/20-widen.json", {"availableModels": ["claude-sonnet-5"]})
    _write(tmp_path, f"{DROPINS}/10-effort.json", {"effortLevel": "low"})
    _write(tmp_path, f"{DROPINS}/.hidden.json", {"model": "ignored-like-the-cli-does"})
    _write(tmp_path, f"{DROPINS}/notes.txt", "not json, not merged")
    conflicts = scan_managed_conflicts(tmp_path)
    assert [c.split(":")[0].rsplit("/", 1)[-1] for c in conflicts] == [
        "managed-settings.json",
        "10-effort.json",
        "20-widen.json",
    ]


def test_scan_rejects_malformed_documents(tmp_path):
    _write(tmp_path, f"{DROPINS}/10-bad.json", '{"model": ')
    with pytest.raises(IdentityError, match="not valid JSON"):
        scan_managed_conflicts(tmp_path)
    _write(tmp_path, f"{DROPINS}/10-bad.json", "[]")
    with pytest.raises(IdentityError, match="JSON object"):
        scan_managed_conflicts(tmp_path)


def test_scan_flags_non_object_env(tmp_path):
    _write(tmp_path, MANAGED, {"env": "PATH=/bin"})
    conflicts = scan_managed_conflicts(tmp_path)
    assert len(conflicts) == 1 and "'env' is str" in conflicts[0]


def test_scan_ignores_the_freshness_key(tmp_path):
    # forceRemoteSettingsRefresh is no longer part of the artifact (the
    # best-effort doctrine dropped it: it added per-session startup latency
    # and a hard availability coupling to the settings endpoint). Whatever
    # an operator sets it to is their business — it does not select an
    # identity.
    _write(tmp_path, MANAGED, {"forceRemoteSettingsRefresh": True})
    _write(tmp_path, f"{DROPINS}/50-stale.json", {"forceRemoteSettingsRefresh": False})
    assert scan_managed_conflicts(tmp_path) == []


def test_scan_with_expected_identity_tolerates_the_materialized_shape(tmp_path):
    # The runtime re-check: the base file may carry exactly what materialize
    # wrote — anything else is still a conflict, and a drop-in never gets
    # the exemption even with matching values.
    expected = BakedIdentity(model="claude-sonnet-5", effort="low")
    _bake(tmp_path, "claude-sonnet-5", "low")
    assert scan_managed_conflicts(tmp_path, expected) == []
    _write(tmp_path, f"{DROPINS}/90-copy.json", {"effortLevel": "low"})
    conflicts = scan_managed_conflicts(tmp_path, expected)
    assert len(conflicts) == 1 and "90-copy.json" in conflicts[0]


def test_scan_with_expected_identity_tolerates_the_legacy_allowlist(tmp_path):
    # Images built before the consolidation carry the single-entry
    # allowlist and the freshness key: stricter (they still pin subagents),
    # not conflicting — they keep running until rebuilt.
    expected = BakedIdentity(model="claude-sonnet-5", effort="low")
    _bake_legacy(tmp_path, "claude-sonnet-5", "low")
    assert scan_managed_conflicts(tmp_path, expected) == []


def test_scan_with_expected_identity_flags_a_different_pin(tmp_path):
    _bake_legacy(tmp_path, "claude-sonnet-5", "low")
    expected = BakedIdentity(model="claude-opus-5", effort="low")
    conflicts = scan_managed_conflicts(tmp_path, expected)
    assert any("'model'" in c for c in conflicts)
    assert any("'availableModels'" in c for c in conflicts)


def test_scan_with_expected_identity_flags_a_local_modeloverrides(tmp_path):
    # modelOverrides remaps what actually SERVES a model ID while the
    # stream still shows the Anthropic ID — never tolerable, base file or
    # drop-in, even when the identity otherwise matches.
    _bake(tmp_path, "claude-sonnet-5")
    document = json.loads((tmp_path / MANAGED).read_text())
    document["modelOverrides"] = {"claude-sonnet-5": "arn:aws:bedrock:us-east-1:1:profile/x"}
    _write(tmp_path, MANAGED, document)
    conflicts = scan_managed_conflicts(tmp_path, BakedIdentity(model="claude-sonnet-5"))
    assert len(conflicts) == 1 and "'modelOverrides'" in conflicts[0]


# -- the process-environment audit --------------------------------------------


def test_process_environment_scan_flags_identity_variables():
    conflicts = scan_process_environment(
        {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "credential-stays",  # the credential is NOT identity
            "ANTHROPIC_MODEL": "claude-opus-5",
            "ANTHROPIC_BASE_URL": "https://proxy.example",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
        }
    )
    assert len(conflicts) == 3
    text = "\n".join(conflicts)
    assert "'ANTHROPIC_MODEL'" in text
    assert "'ANTHROPIC_BASE_URL'" in text
    assert "'ANTHROPIC_DEFAULT_SONNET_MODEL'" in text
    # Names only — never the values.
    assert "claude-opus-5" not in text and "proxy.example" not in text


def test_process_environment_scan_passes_the_credential_contract():
    # Exactly what ADR-0045 delivers to a run container: the secret, nothing
    # that selects a model, effort, or endpoint.
    assert scan_process_environment({"ANTHROPIC_API_KEY": "x", "THEOZOLITH_JOB": "/job"}) == []
    assert scan_process_environment({"CLAUDE_CODE_OAUTH_TOKEN": "x"}) == []


# -- pair-aware capability ------------------------------------------------------


def test_effort_capability_current_generation_supports_all_four():
    for model in (
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
    ):
        assert effort_capability(model) == frozenset({"low", "medium", "high", "xhigh"})


def test_effort_capability_46_generation_lacks_xhigh():
    # Claude Code documents that an unsupported level silently runs as the
    # highest supported level at or below it — xhigh becomes high on the 4.6
    # generation. The capability table therefore excludes it there.
    for model in ("claude-opus-4-6", "claude-sonnet-4-6"):
        assert effort_capability(model) == frozenset({"low", "medium", "high"})


def test_effort_capability_effortless_models_and_unknowns():
    for model in ("claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-4-5"):
        assert effort_capability(model) == frozenset()
    # Unknown ≠ effortless: an unknown future model returns None, and a
    # dated variant extends its family prefix while a longer version number
    # does not accidentally match a shorter one.
    assert effort_capability("claude-sonnet-5-20270101") == frozenset(
        {"low", "medium", "high", "xhigh"}
    )
    assert effort_capability("claude-sonnet-55") is None
    assert effort_capability("claude-newfamily-1") is None


def test_effort_capability_aliases():
    for alias in ("fable", "opus", "sonnet"):
        assert effort_capability(alias) == frozenset({"low", "medium", "high", "xhigh"})
    assert effort_capability("haiku") == frozenset()


def test_pair_error_matrix():
    assert pair_error("claude-sonnet-5", "") == ""  # "" = model default, always valid
    assert pair_error("claude-sonnet-5", "xhigh") == ""
    assert pair_error("sonnet", "high") == ""
    assert "silently runs" in pair_error("claude-opus-4-6", "xhigh")
    assert "silently ignore" in pair_error("claude-haiku-4-5", "low")
    assert "silently ignore" in pair_error("haiku", "low")
    assert "no known effort capability" in pair_error("claude-newfamily-1", "high")
    assert "without a model" in pair_error("", "high")
    # max stays outside the mappable set upstream; the pair table never
    # legitimizes it either.
    assert pair_error("claude-opus-5", "max") != ""


# -- the baked-identity reader ------------------------------------------------


def test_read_baked_identity_absent_everywhere_is_none(tmp_path):
    assert read_baked_identity(tmp_path) is None
    _write(tmp_path, MANAGED, {"permissions": {}})  # unrelated settings only
    assert read_baked_identity(tmp_path) is None


def test_read_baked_identity_reads_the_well_known_files(tmp_path):
    _bake(tmp_path, "claude-sonnet-5", "low")
    assert read_baked_identity(tmp_path) == BakedIdentity(model="claude-sonnet-5", effort="low")
    _bake(tmp_path, "claude-opus-5")
    (tmp_path / "etc/theozolith/effort").unlink()
    assert read_baked_identity(tmp_path) == BakedIdentity(model="claude-opus-5", effort="")


def test_read_baked_identity_fails_on_corruption(tmp_path):
    _write(tmp_path, "etc/theozolith/model", "")
    with pytest.raises(IdentityError, match="empty"):
        read_baked_identity(tmp_path)
    _write(tmp_path, "etc/theozolith/model", "a\nb\n")
    with pytest.raises(IdentityError, match="multiple lines"):
        read_baked_identity(tmp_path)


def test_read_baked_identity_fails_on_effort_without_model(tmp_path):
    _write(tmp_path, "etc/theozolith/effort", "high\n")
    with pytest.raises(IdentityError, match="effort without a model"):
        read_baked_identity(tmp_path)


def test_read_baked_identity_unreadable_declaration_is_an_identity_error(tmp_path):
    # An unreadable well-known file must take the identity-inconsistent lane
    # (IdentityError), never escape as generic harness breakage.
    if os.geteuid() == 0:
        pytest.skip("permission-based failure injection is a no-op as root")
    path = _write(tmp_path, "etc/theozolith/model", "claude-sonnet-5\n")
    path.chmod(0o000)
    try:
        with pytest.raises(IdentityError, match="unreadable baked-identity"):
            read_baked_identity(tmp_path)
    finally:
        path.chmod(0o644)


def test_read_baked_identity_fails_on_selection_without_declaration(tmp_path):
    # Managed identity keys with no well-known model file: a selection
    # config nothing declares — never silently run under it.
    _write(tmp_path, MANAGED, {"availableModels": ["claude-opus-5"]})
    with pytest.raises(IdentityError, match="no baked identity"):
        read_baked_identity(tmp_path)


# -- the free per-Run static checks --------------------------------------------


def test_static_checks_pass_on_the_materialized_shapes(tmp_path):
    _bake(tmp_path, "claude-sonnet-5", "low")
    identity = BakedIdentity("claude-sonnet-5", "low")
    report = static_identity_report(identity, root=tmp_path, environ={})
    assert report.ok, report.detail

    legacy = tmp_path / "legacy"
    _bake_legacy(legacy, "claude-sonnet-5", "low")
    report = static_identity_report(identity, root=legacy, environ={})
    assert report.ok, report.detail


def test_static_checks_fail_on_a_missing_or_drifted_managed_selection(tmp_path):
    _write(tmp_path, "etc/theozolith/model", "claude-sonnet-5\n")  # no managed file
    identity = BakedIdentity("claude-sonnet-5")
    report = static_identity_report(identity, root=tmp_path, environ={})
    assert not report.ok and report.category == CATEGORY_INCONSISTENT
    assert "no selection configuration" in report.detail

    _bake(tmp_path, "claude-sonnet-5", "low")
    document = json.loads((tmp_path / MANAGED).read_text())
    document["env"] = {"CLAUDE_CODE_EFFORT_LEVEL": "high"}  # drifted pin
    _write(tmp_path, MANAGED, document)
    report = static_identity_report(
        BakedIdentity("claude-sonnet-5", "low"), root=tmp_path, environ={}
    )
    # A PRESENT-but-different value is caught by the conflict scan (it is
    # policy that would supersede the identity); a MISSING key is the
    # inconsistent case above.
    assert not report.ok and report.category == CATEGORY_POLICY_CONFLICT
    assert "CLAUDE_CODE_EFFORT_LEVEL" in report.detail


def test_static_checks_fail_on_superseding_policy(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    _write(tmp_path, f"{DROPINS}/50-steer.json", {"model": "claude-opus-5"})
    report = static_identity_report(BakedIdentity("claude-sonnet-5"), root=tmp_path, environ={})
    assert not report.ok and report.category == CATEGORY_POLICY_CONFLICT
    assert "50-steer.json" in report.detail


def test_static_checks_fail_on_an_invalid_pair(tmp_path):
    _bake(tmp_path, "claude-sonnet-4-6", "xhigh")  # silently clamped pair
    report = static_identity_report(
        BakedIdentity("claude-sonnet-4-6", "xhigh"), root=tmp_path, environ={}
    )
    assert not report.ok and report.category == CATEGORY_PAIR_INVALID


def test_static_checks_fail_on_a_process_environment_conflict(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    report = static_identity_report(
        BakedIdentity("claude-sonnet-5"),
        root=tmp_path,
        environ={"ANTHROPIC_BASE_URL": "https://proxy.example"},
    )
    assert not report.ok and report.category == CATEGORY_POLICY_CONFLICT
    assert "'ANTHROPIC_BASE_URL'" in report.detail
    assert "proxy.example" not in report.detail  # value redacted


# -- the setup dry-run (scripted subprocess runner) -----------------------------


def _stream_json(*events) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _session_ok(model: str) -> str:
    return _stream_json(
        {"type": "system", "subtype": "init", "model": model},
        {"type": "assistant", "message": {"model": model, "content": []}},
        {"type": "result", "subtype": "success", "is_error": False},
    )


class ScriptedRunner:
    """Answers ``claude --version`` and the neutral identity probe from a
    script. ``on_probe`` runs when the probe is invoked — tests use it to
    simulate the Stop hook writing the effort capture."""

    def __init__(
        self,
        version="2.1.232 (Claude Code)",
        probe_stdout="",
        probe_rc=0,
        on_probe=None,
    ):
        self.version = version
        self.probe_stdout = probe_stdout
        self.probe_rc = probe_rc
        self.on_probe = on_probe
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=self.version, stderr="")
        if self.on_probe is not None:
            self.on_probe()
        return subprocess.CompletedProcess(argv, self.probe_rc, stdout=self.probe_stdout, stderr="")

    @property
    def probe_calls(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls if "--version" not in call["argv"]]


def _preflight(tmp_path, identity_obj, runner, **kwargs):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return run_preflight(
        identity_obj,
        binary="claude",
        root=tmp_path,
        scratch=scratch,
        min_cli=MIN_CLI,
        run=runner,
        environ={},  # the process-environment audit is tested explicitly
        **kwargs,
    )


def test_preflight_passes_and_asks_for_no_model(tmp_path):
    _bake(tmp_path, "claude-sonnet-5", "low")
    capture = tmp_path / "scratch" / "preflight-stop.jsonl"
    runner = ScriptedRunner(
        probe_stdout=_session_ok("claude-sonnet-5"),
        on_probe=lambda: _write(tmp_path, "scratch/preflight-stop.jsonl", '{"effort": "low"}\n'),
    )
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "low"), runner)
    assert report.ok, report.detail
    assert report.cli_version.startswith("2.1.232")
    assert report.probe_model == "claude-sonnet-5"
    assert report.probe_effort == "low"
    assert capture.is_file()
    # ONE probe session, no canaries, and no --model anywhere: the session
    # the effective configuration picks by itself is the observation.
    (probe,) = runner.probe_calls
    assert "--model" not in probe
    assert len(runner.calls) == 2  # version + probe, nothing else


def test_preflight_probe_runs_hermetic(tmp_path):
    """The dry-run probe is a throwaway diagnostic: no tools, no permission
    prompts, no user/project/local settings, no non-managed MCP, cwd in the
    scratch. (The TASK session shares none of this — it keeps its full
    normal capabilities, checkout CLAUDE.md and skills included.)"""
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(probe_stdout=_session_ok("claude-sonnet-5"))
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert report.ok, report.detail
    (probe,) = runner.probe_calls
    assert probe[probe.index("--tools") + 1] == ""
    assert probe[probe.index("--permission-mode") + 1] == "dontAsk"
    assert probe[probe.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in probe
    assert "--dangerously-skip-permissions" not in probe
    (call,) = [c for c in runner.calls if "--version" not in c["argv"]]
    assert call["cwd"] == str(tmp_path / "scratch")


def test_preflight_static_failure_spends_no_subprocess(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    _write(tmp_path, f"{DROPINS}/50-steer.json", {"model": "claude-opus-5"})
    runner = ScriptedRunner()
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_POLICY_CONFLICT
    assert runner.calls == []


def test_preflight_fails_on_a_pre_enforcement_cli(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(version="2.1.222 (Claude Code)")
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_CLI_TOO_OLD
    assert "2.1.232" in report.detail


def test_preflight_probe_substitution_fails(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(probe_stdout=_session_ok("claude-opus-5"))
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_SUBSTITUTED
    assert "claude-opus-5" in report.detail


def test_preflight_probe_error_means_unavailable(tmp_path):
    _bake(tmp_path, "claude-nonexistent-9")
    stdout = _stream_json(
        {"type": "system", "subtype": "init", "model": "claude-nonexistent-9"},
        {"type": "result", "subtype": "error_during_execution", "is_error": True},
    )
    runner = ScriptedRunner(probe_stdout=stdout, probe_rc=1)
    report = _preflight(tmp_path, BakedIdentity("claude-nonexistent-9"), runner)
    assert not report.ok and report.category == CATEGORY_UNAVAILABLE


def test_preflight_no_signal_is_unverifiable(tmp_path):
    # The dry-run is strict where the per-Run monitor is lenient: a probe
    # with no signal means the observation channel itself is broken, and
    # setup is the time to learn that.
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(probe_stdout="")
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_UNVERIFIABLE

    no_init = _stream_json(
        {"type": "assistant", "message": {"model": "claude-sonnet-5", "content": []}},
        {"type": "result", "subtype": "success", "is_error": False},
    )
    runner = ScriptedRunner(probe_stdout=no_init)
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_UNVERIFIABLE
    assert "no init event" in report.detail


def test_preflight_accepts_a_decorated_init_announcement(tmp_path):
    _bake(tmp_path, "claude-opus-5")
    stdout = _stream_json(
        {"type": "system", "subtype": "init", "model": "claude-opus-5[1m]"},
        {"type": "assistant", "message": {"model": "claude-opus-5", "content": []}},
        {"type": "result", "subtype": "success", "is_error": False},
    )
    runner = ScriptedRunner(probe_stdout=stdout)
    report = _preflight(tmp_path, BakedIdentity("claude-opus-5"), runner)
    assert report.ok, f"{report.category}: {report.detail}"


def test_preflight_ignores_subagent_turns(tmp_path):
    # Subagent events carry parent_tool_use_id and are free to run other
    # models — main-agent-only enforcement (ADR-0045).
    _bake(tmp_path, "claude-sonnet-5")
    stdout = _stream_json(
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
        {
            "type": "assistant",
            "message": {"model": "claude-haiku-4-5", "content": []},
            "parent_tool_use_id": "toolu_01",
        },
        {"type": "assistant", "message": {"model": "claude-sonnet-5", "content": []}},
        {"type": "result", "subtype": "success", "is_error": False},
    )
    runner = ScriptedRunner(probe_stdout=stdout)
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert report.ok, f"{report.category}: {report.detail}"


def test_preflight_timeout_fails_closed(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")

    def hanging(argv, **kwargs):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="2.1.232", stderr="")
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), hanging)
    assert not report.ok and report.category == CATEGORY_TIMEOUT


def test_preflight_effort_clamp_is_detected_by_the_probe(tmp_path):
    # An organization effort cap clamps silently in the stream — the Stop
    # hook capture is the observation, and a clamp is a dry-run failure,
    # never an accepted downgrade.
    _bake(tmp_path, "claude-sonnet-5", "xhigh")
    runner = ScriptedRunner(
        probe_stdout=_session_ok("claude-sonnet-5"),
        on_probe=lambda: _write(tmp_path, "scratch/preflight-stop.jsonl", '{"effort": "high"}\n'),
    )
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "xhigh"), runner)
    assert not report.ok and report.category == CATEGORY_EFFORT_CLAMPED
    assert "'high'" in report.detail and "'xhigh'" in report.detail
    assert report.probe_effort == "high"


def test_preflight_missing_effort_signal_is_unverifiable(tmp_path):
    _bake(tmp_path, "claude-sonnet-5", "low")
    runner = ScriptedRunner(probe_stdout=_session_ok("claude-sonnet-5"))
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "low"), runner)
    assert not report.ok and report.category == CATEGORY_UNVERIFIABLE
    assert "applied effort" in report.detail


def test_preflight_effort_probe_carries_the_stop_hook(tmp_path):
    _bake(tmp_path, "claude-sonnet-5", "low")
    runner = ScriptedRunner(
        probe_stdout=_session_ok("claude-sonnet-5"),
        on_probe=lambda: _write(tmp_path, "scratch/preflight-stop.jsonl", '{"effort": "low"}\n'),
    )
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "low"), runner)
    assert report.ok, report.detail
    (probe,) = runner.probe_calls
    settings_json = probe[probe.index("--settings") + 1]
    # The SAME channel every Run rides: the python3 Stop-hook helper writing
    # the journal — the dry-run proves python3-in-hook-shell + script +
    # journal shape, not a lookalike capture.
    assert "Stop" in settings_json and "preflight-stop.jsonl" in settings_json
    assert "python3" in settings_json and "stop_hook.py" in settings_json
    assert (tmp_path / "scratch" / "stop_hook.py").is_file()
    assert "PostToolUse" not in settings_json
    assert PROBE_PROMPT in probe
    assert "tool" in PROBE_PROMPT  # the prompt itself forbids tool use


def test_preflight_alias_pin_accepts_its_family(tmp_path):
    _bake(tmp_path, "sonnet")
    runner = ScriptedRunner(probe_stdout=_session_ok("claude-sonnet-5"))
    report = _preflight(tmp_path, BakedIdentity("sonnet"), runner)
    assert report.ok, report.detail
    runner = ScriptedRunner(probe_stdout=_session_ok("claude-haiku-4-5"))
    report = _preflight(tmp_path, BakedIdentity("sonnet"), runner)
    assert not report.ok and report.category == CATEGORY_SUBSTITUTED


def test_preflight_report_describe_names_the_category(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    report = _preflight(
        tmp_path,
        BakedIdentity("claude-sonnet-5"),
        ScriptedRunner(probe_stdout=_session_ok("claude-opus-5")),
    )
    text = report.describe()
    assert "claude-sonnet-5" in text and report.category in text


# -- expectation matching -----------------------------------------------------


def test_model_matches_pinned_is_exact():
    assert model_matches("claude-sonnet-5", "claude-sonnet-5")
    # A dated resolution of an undated pin is NOT a match: pin the dated ID.
    assert not model_matches("claude-sonnet-4-5", "claude-sonnet-4-5-20250929")
    assert not model_matches("claude-sonnet-5", "claude-opus-5")
    assert not model_matches("claude-sonnet-5", "")


def test_model_matches_alias_is_family_bound():
    assert model_matches("sonnet", "claude-sonnet-5")
    assert model_matches("haiku", "claude-haiku-4-5-20251001")
    assert not model_matches("sonnet", "claude-opus-5")
    assert not model_matches("fable", "claude-sonnet-5")


def test_normalize_model_strips_the_context_decoration():
    # The CLI announces long-context variants with a bracketed tag (observed
    # live: init and modelUsage say claude-opus-5[1m] while the executed
    # turns say claude-opus-5) — the same model, not a substitution.
    assert normalize_model("claude-opus-5[1m]") == "claude-opus-5"
    assert normalize_model("claude-opus-5") == "claude-opus-5"
    assert model_matches("claude-opus-5", "claude-opus-5[1m]")
    assert model_matches("opus", "claude-opus-5[1m]")
    assert not model_matches("claude-opus-5", "claude-opus-4-6[1m]")


# -- the identity marker (status.json channel) ----------------------------------


def test_identity_error_detail_is_anchored():
    assert identity_error_detail("identity: [substituted] a turn drifted") == (
        "[substituted] a turn drifted"
    )
    assert (
        identity_error_detail("harness failed: identity: [preflight-timeout] budget spent")
        == "[preflight-timeout] budget spent"
    )
    # Merely CONTAINING the marker is not an identity verdict.
    assert identity_error_detail("gate step echoed 'identity: [substituted]'") is None
    assert identity_error_detail("harness crashed: identity: nested") is None
    assert identity_error_detail("") is None


# -- the ConfigChange hook helper ---------------------------------------------


def _run_config_hook(tmp_path: Path, event: dict, baseline: dict | None = None) -> list[dict]:
    script = tmp_path / "configchange_hook.py"
    script.write_text(CONFIG_CHANGE_HOOK_SOURCE, encoding="utf-8")
    capture = tmp_path / "config-change.jsonl"
    argv = [sys.executable, str(script), str(capture)]
    if baseline is not None:
        baseline_path = tmp_path / "config-baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        argv.append(str(baseline_path))
    proc = subprocess.run(
        argv,
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    if not capture.exists():
        return []
    records = [json.loads(line) for line in capture.read_text().splitlines() if line.strip()]
    capture.unlink()
    return records


def test_config_hook_records_policy_and_user_tier_changes(tmp_path):
    # Managed/user/local settings inside a run container are image bytes —
    # any mid-session change there is identity-suspect, content unseen.
    for source in ("policy_settings", "user_settings", "local_settings", "future_source", ""):
        records = _run_config_hook(tmp_path, {"source": source, "file_path": "/etc/x.json"})
        assert len(records) == 1
        assert records[0]["source"] == source


def test_config_hook_ignores_skills_and_benign_project_settings(tmp_path):
    assert _run_config_hook(tmp_path, {"source": "skills", "file_path": "/w/.claude/skills"}) == []
    # Structurally benign (ADR-0045): a NESTED "model" key in an unrelated
    # object and a credential env entry are not identity-shaped — the filter
    # parses the document instead of pattern-matching its text, so a
    # legitimate checkout-settings edit never kills the Run.
    benign = tmp_path / "benign.json"
    benign.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash"]},
                "statusLine": {"model": "irrelevant"},
                "env": {"ANTHROPIC_API_KEY": "sk-redacted", "MY_FLAG": "1"},
            }
        )
    )
    records = _run_config_hook(tmp_path, {"source": "project_settings", "file_path": str(benign)})
    assert records == []


def test_config_hook_records_identity_keys_in_project_settings(tmp_path):
    hostile = tmp_path / "hostile.json"
    hostile.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x"}, "model": "opus"}))
    records = _run_config_hook(tmp_path, {"source": "project_settings", "file_path": str(hostile)})
    assert len(records) == 1
    assert "ANTHROPIC_BASE_URL" in records[0]["keys"] and "model" in records[0]["keys"]
    assert "http://x" not in json.dumps(records)  # values never recorded


def test_config_hook_subtracts_the_launch_baseline(tmp_path):
    """A checkout that legitimately SHIPS an identity key (inert — the
    managed tier outranks it, verified live) is not killed for a benign
    mid-session edit to the same file: only keys the change ADDED count."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "sonnet", "permissions": {"allow": ["Bash"]}}))
    baseline = {str(settings): ["model"]}
    # The same keys as launch: not a change, nothing recorded.
    assert (
        _run_config_hook(
            tmp_path, {"source": "project_settings", "file_path": str(settings)}, baseline
        )
        == []
    )
    # A NEW identity key appears mid-session: recorded, only the new key.
    settings.write_text(json.dumps({"model": "sonnet", "env": {"ANTHROPIC_BASE_URL": "http://x"}}))
    records = _run_config_hook(
        tmp_path, {"source": "project_settings", "file_path": str(settings)}, baseline
    )
    assert len(records) == 1
    assert records[0]["keys"] == ["ANTHROPIC_BASE_URL"]


def test_config_hook_records_unreadable_project_settings(tmp_path):
    records = _run_config_hook(
        tmp_path, {"source": "project_settings", "file_path": str(tmp_path / "gone.json")}
    )
    assert len(records) == 1  # unknowable is recorded


def test_config_hook_records_unparseable_project_settings(tmp_path):
    # A changed settings file that does not parse is unknowable policy —
    # recorded (and the Run dies), exactly like an unreadable one.
    garbled = tmp_path / "garbled.json"
    garbled.write_text('{"model": "opus"')  # truncated JSON
    records = _run_config_hook(tmp_path, {"source": "project_settings", "file_path": str(garbled)})
    assert len(records) == 1
    assert records[0]["keys"] == []  # nothing provable, no keys claimed


def test_config_hook_survives_garbage_stdin(tmp_path):
    script = tmp_path / "configchange_hook.py"
    script.write_text(CONFIG_CHANGE_HOOK_SOURCE, encoding="utf-8")
    capture = tmp_path / "config-change.jsonl"
    proc = subprocess.run(
        [sys.executable, str(script), str(capture)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Garbage input is an unknown source → recorded, fail loud.
    assert proc.returncode == 0, proc.stderr
    assert capture.exists()


# -- the Stop hook helper and the effort journal --------------------------------


def test_stop_hook_appends_one_redacted_record_per_firing(tmp_path):
    script = tmp_path / "stop_hook.py"
    script.write_text(STOP_HOOK_SOURCE, encoding="utf-8")
    capture = tmp_path / "stop.jsonl"
    payload = {
        "effort": {"level": "low", "source": "managed"},
        "session_id": "s-1",
        "transcript_path": "/home/u/.claude/t.jsonl",
    }
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, str(script), str(capture)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
    lines = capture.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line) == {"effort": "low"}  # value-redacted: level only

    # Garbage stdin still journals a record (no effort field) — the harness
    # records the missing observation as a gap.
    proc = subprocess.run(
        [sys.executable, str(script), str(capture)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(capture.read_text().splitlines()[-1]) == {}


def test_read_last_journal_effort_takes_the_latest_observation(tmp_path):
    journal = tmp_path / "stop.jsonl"
    assert read_last_journal_effort(journal) == ""  # absent file: a gap
    journal.write_text('{"effort": "low"}\n{"effort": "high"}\n')
    assert read_last_journal_effort(journal) == "high"
    journal.write_text('{"effort": "low"}\n{}\n')
    assert read_last_journal_effort(journal) == "low"  # latest OBSERVATION
    journal.write_text('{"effort": "low"}\n{"effort": "hi')  # mid-append tail
    assert read_last_journal_effort(journal) == "low"
    journal.write_text("junk\n")
    assert read_last_journal_effort(journal) == ""


# -- the fail-loud session monitor ----------------------------------------------


def _init_line(model: str) -> str:
    return json.dumps({"type": "system", "subtype": "init", "model": model})


def _turn_line(model: str, parent: str | None = None) -> str:
    event: dict = {"type": "assistant", "message": {"model": model, "content": []}}
    if parent is not None:
        event["parent_tool_use_id"] = parent
    return json.dumps(event)


def _hooks(tmp_path: Path) -> MonitorHooks:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return MonitorHooks(
        stop_capture=scratch / "stop.jsonl",
        config_capture=scratch / "config-change.jsonl",
        config_baseline=scratch / "config-baseline.json",
        config_hook_script=scratch / "configchange_hook.py",
        stop_hook_script=scratch / "stop_hook.py",
    )


def test_monitor_stays_quiet_on_a_clean_stream(tmp_path):
    monitor = ClaudeSessionMonitor(BakedIdentity("claude-sonnet-5"), _hooks(tmp_path))
    monitor.observe(_init_line("claude-sonnet-5"))
    monitor.observe(_turn_line("claude-sonnet-5"))
    assert monitor.violation() == ("", "")
    assert monitor.observed_model == "claude-sonnet-5"


def test_monitor_kills_on_an_off_identity_main_turn(tmp_path):
    monitor = ClaudeSessionMonitor(BakedIdentity("claude-sonnet-5"), _hooks(tmp_path))
    monitor.observe(_init_line("claude-sonnet-5"))
    monitor.observe(_turn_line("claude-opus-5"))
    reason, category = monitor.violation()
    assert "claude-opus-5" in reason and category == CATEGORY_SUBSTITUTED


def test_monitor_kills_on_an_off_identity_init(tmp_path):
    monitor = ClaudeSessionMonitor(BakedIdentity("claude-sonnet-5"), _hooks(tmp_path))
    monitor.observe(_init_line("claude-opus-5"))
    reason, category = monitor.violation()
    assert "initialized on" in reason and category == CATEGORY_SUBSTITUTED


def test_monitor_leaves_subagents_free(tmp_path):
    # Main-agent-only enforcement (ADR-0045): a subagent turn on another
    # model is a legitimate capability, never a violation.
    monitor = ClaudeSessionMonitor(BakedIdentity("claude-sonnet-5"), _hooks(tmp_path))
    monitor.observe(_init_line("claude-sonnet-5"))
    monitor.observe(_turn_line("claude-haiku-4-5", parent="toolu_01"))
    monitor.observe(_turn_line("claude-opus-5", parent="toolu_02"))
    monitor.observe(_turn_line("claude-sonnet-5"))
    assert monitor.violation() == ("", "")
    assert monitor.observed_model == "claude-sonnet-5"


def test_monitor_accepts_decorated_announcements(tmp_path):
    monitor = ClaudeSessionMonitor(BakedIdentity("claude-opus-5"), _hooks(tmp_path))
    monitor.observe(_init_line("claude-opus-5[1m]"))
    monitor.observe(_turn_line("claude-opus-5"))
    assert monitor.violation() == ("", "")


def test_monitor_alias_accepts_the_family(tmp_path):
    monitor = ClaudeSessionMonitor(BakedIdentity("sonnet"), _hooks(tmp_path))
    monitor.observe(_init_line("claude-sonnet-5"))
    monitor.observe(_turn_line("claude-sonnet-5"))
    assert monitor.violation() == ("", "")
    monitor.observe(_turn_line("claude-haiku-4-5"))
    reason, category = monitor.violation()
    assert reason and category == CATEGORY_SUBSTITUTED


def test_monitor_ignores_synthetic_turns_and_junk(tmp_path):
    monitor = ClaudeSessionMonitor(BakedIdentity("claude-sonnet-5"), _hooks(tmp_path))
    monitor.observe("not json at all")
    monitor.observe(json.dumps(["not", "an", "object"]))
    monitor.observe(_turn_line("<synthetic>"))
    assert monitor.violation() == ("", "")


def test_monitor_absence_is_a_gap_not_a_violation(tmp_path):
    # Best-effort doctrine: a stream with no signal at all detects nothing —
    # the harness records the gap in evidence; the monitor stays quiet.
    monitor = ClaudeSessionMonitor(BakedIdentity("claude-sonnet-5"), _hooks(tmp_path))
    assert monitor.violation() == ("", "")
    assert monitor.observed_model == ""


def test_monitor_kills_on_a_recorded_config_change(tmp_path):
    hooks = _hooks(tmp_path)
    monitor = ClaudeSessionMonitor(BakedIdentity("claude-sonnet-5"), hooks)
    monitor.observe(_init_line("claude-sonnet-5"))
    monitor.observe(_turn_line("claude-sonnet-5"))
    hooks.config_capture.write_text(
        json.dumps({"source": "policy_settings", "file_path": "/etc/x.json", "keys": []}) + "\n"
    )
    reason, category = monitor.violation()
    assert "policy_settings" in reason and category == CATEGORY_CONFIG_CHANGED


def test_monitor_config_kill_survives_a_malformed_record(tmp_path):
    # A capture line that does not parse is still a recorded change from an
    # unknown source — fail loud, never quietly ignore the channel.
    hooks = _hooks(tmp_path)
    monitor = ClaudeSessionMonitor(BakedIdentity("claude-sonnet-5"), hooks)
    hooks.config_capture.write_text("garbage\n")
    reason, category = monitor.violation()
    assert reason and category == CATEGORY_CONFIG_CHANGED
