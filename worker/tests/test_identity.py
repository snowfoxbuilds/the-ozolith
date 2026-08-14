"""The baked-identity enforcement machinery (ADR-0045, fail closed).

Unit tests for identity.py: the managed-tier conflict scan both gates run
(build and runtime), the freshness-key validation, the process-environment
audit, the pair-aware (model, effort) capability table, the baked-identity
reader, the subprocess preflight (canaries + neutral identity probe) with a
scripted runner, the ConfigChange hook helper, and the session guard's
gate/monitor state machine. Live proof that the real CLI behaves as these
tests assume is test_live_enforcement.py.
"""

from __future__ import annotations

import json
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
    CATEGORY_WIDENED,
    CONFIG_CHANGE_HOOK_SOURCE,
    GUARD_KILL,
    GUARD_RELEASE,
    GUARD_WAIT,
    PROBE_PROMPT,
    BakedIdentity,
    ClaudeSessionGuard,
    IdentityError,
    TaskGate,
    effort_capability,
    model_matches,
    pair_error,
    read_baked_identity,
    run_preflight,
    scan_managed_conflicts,
    scan_process_environment,
    sibling_for,
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
    """The exact artifact materialize() writes for (model, effort)."""
    _write(root, "etc/theozolith/model", model + "\n")
    settings: dict = {
        "model": model,
        "availableModels": [model],
        "enforceAvailableModels": True,
        "forceRemoteSettingsRefresh": True,
    }
    if effort:
        _write(root, "etc/theozolith/effort", effort + "\n")
        settings["effortLevel"] = effort
        settings["env"] = {"CLAUDE_CODE_EFFORT_LEVEL": effort}
    _write(root, MANAGED, settings)


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
    # Model/effort selectors AND provider/endpoint redirects: behind a
    # foreign base URL or provider switch, the stream's model names are
    # whatever the endpoint claims — unprovable, so flagged.
    _write(tmp_path, f"{DROPINS}/50-ops.json", {"env": {name: "anything", "UNRELATED": "ok"}})
    conflicts = scan_managed_conflicts(tmp_path)
    assert len(conflicts) == 1
    assert f"'{name}'" in conflicts[0] and "50-ops.json" in conflicts[0]
    assert "anything" not in conflicts[0]  # values never leak


def test_scan_covers_dropins_in_merge_order(tmp_path):
    # Claude Code merges the base file first, then *.json alphabetically —
    # arrays CONCATENATE across the merge, so a single-entry drop-in widens
    # the baked allowlist. Every fragment is scanned; conflicts come back in
    # merge order so the first-named file is the first the CLI would apply.
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


def test_scan_freshness_key_true_is_tolerated_everywhere(tmp_path):
    # An operator already forcing fresh policy agrees with the artifact —
    # at the build gate and in drop-ins alike.
    _write(tmp_path, MANAGED, {"forceRemoteSettingsRefresh": True})
    _write(tmp_path, f"{DROPINS}/10-fresh.json", {"forceRemoteSettingsRefresh": True})
    assert scan_managed_conflicts(tmp_path) == []


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_scan_freshness_key_non_true_is_a_conflict(tmp_path, value):
    # Type-validated: only the literal boolean true guarantees fresh policy;
    # false disables it and any other type is an unknowable configuration.
    _write(tmp_path, f"{DROPINS}/50-stale.json", {"forceRemoteSettingsRefresh": value})
    conflicts = scan_managed_conflicts(tmp_path)
    assert len(conflicts) == 1
    assert "'forceRemoteSettingsRefresh'" in conflicts[0] and "50-stale.json" in conflicts[0]
    expected = BakedIdentity(model="claude-sonnet-5")
    _bake(tmp_path, "claude-sonnet-5")
    conflicts = scan_managed_conflicts(tmp_path, expected)
    assert any("'forceRemoteSettingsRefresh'" in c for c in conflicts)


def test_scan_with_expected_identity_tolerates_only_our_exact_pin(tmp_path):
    # The runtime re-check: the base file may carry exactly what materialize
    # wrote — anything else is still a conflict, and a drop-in never gets the
    # exemption even with matching values.
    expected = BakedIdentity(model="claude-sonnet-5", effort="low")
    _bake(tmp_path, "claude-sonnet-5", "low")
    assert scan_managed_conflicts(tmp_path, expected) == []
    # A drop-in repeating the same values is NOT ours: arrays concatenate on
    # merge, so even an identical availableModels entry doubles the list.
    _write(tmp_path, f"{DROPINS}/90-copy.json", {"availableModels": ["claude-sonnet-5"]})
    conflicts = scan_managed_conflicts(tmp_path, expected)
    assert len(conflicts) == 1 and "90-copy.json" in conflicts[0]


def test_scan_with_expected_identity_flags_a_different_pin(tmp_path):
    _bake(tmp_path, "claude-sonnet-5", "low")
    expected = BakedIdentity(model="claude-opus-5", effort="low")
    conflicts = scan_managed_conflicts(tmp_path, expected)
    assert any("'model'" in c for c in conflicts)
    assert any("'availableModels'" in c for c in conflicts)


def test_scan_with_expected_identity_flags_a_local_modeloverrides(tmp_path):
    # modelOverrides remaps what actually SERVES a model ID while the
    # allowlist still sees the Anthropic ID — never tolerable, base file or
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


# -- pair-aware capability (amendment C) --------------------------------------


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


def test_read_baked_identity_fails_on_enforcement_without_declaration(tmp_path):
    # Managed identity keys with no well-known model file: an enforcement
    # config nothing declares or verifies — never silently run under it.
    _write(tmp_path, MANAGED, {"availableModels": ["claude-opus-5"]})
    with pytest.raises(IdentityError, match="no baked identity"):
        read_baked_identity(tmp_path)


# -- the preflight (scripted subprocess runner) -------------------------------


def _stream_json(*events) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _session_ok(model: str) -> str:
    return _stream_json(
        {"type": "system", "subtype": "init", "model": model},
        {"type": "assistant", "message": {"model": model, "content": []}},
        {"type": "result", "subtype": "success", "is_error": False},
    )


class ScriptedRunner:
    """Answers ``claude --version``, the canary invocations (they carry
    ``--model``), and the neutral identity probe (it does not) from a
    script. ``on_probe`` runs when the probe is invoked — tests use it to
    simulate the Stop hook writing the effort capture."""

    def __init__(
        self,
        version="2.1.232 (Claude Code)",
        canary_stdout="",
        canary_rc=0,
        probe_stdout=None,
        probe_rc=0,
        on_probe=None,
    ):
        self.version = version
        self.canary_stdout = canary_stdout
        self.canary_rc = canary_rc
        self.probe_stdout = canary_stdout if probe_stdout is None else probe_stdout
        self.probe_rc = probe_rc
        self.on_probe = on_probe
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=self.version, stderr="")
        if "--model" in argv:
            return subprocess.CompletedProcess(
                argv, self.canary_rc, stdout=self.canary_stdout, stderr=""
            )
        if self.on_probe is not None:
            self.on_probe()
        return subprocess.CompletedProcess(argv, self.probe_rc, stdout=self.probe_stdout, stderr="")

    @property
    def canary_calls(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls if "--model" in call["argv"]]

    @property
    def probe_calls(self) -> list[list[str]]:
        return [
            call["argv"]
            for call in self.calls
            if "--model" not in call["argv"] and "--version" not in call["argv"]
        ]


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


def test_preflight_passes_on_a_bound_identity(tmp_path):
    _bake(tmp_path, "claude-sonnet-5", "low")
    capture = tmp_path / "scratch" / "preflight-effort.json"
    runner = ScriptedRunner(
        canary_stdout=_session_ok("claude-sonnet-5"),
        on_probe=lambda: _write(
            tmp_path, "scratch/preflight-effort.json", {"effort": {"level": "low"}}
        ),
    )
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "low"), runner)
    assert report.ok, report.detail
    assert report.cli_version.startswith("2.1.232")
    assert report.canary_model == "claude-sonnet-5"
    assert report.probe_model == "claude-sonnet-5"
    assert report.probe_effort == "low"
    assert capture.is_file()
    # A full-ID pin runs BOTH canaries: a different-family intruder and a
    # same-family sibling (family-granular enforcement would pass the first
    # and still run the wrong model) — then the no---model identity probe.
    assert len(runner.canary_calls) == 2
    first, second = runner.canary_calls
    assert first[first.index("--model") + 1] == "claude-haiku-4-5"
    assert second[second.index("--model") + 1] == "claude-sonnet-4-6"
    assert len(runner.probe_calls) == 1


def test_preflight_sessions_run_inside_the_boundary(tmp_path):
    """Every verification session carries the isolation argv — no tools, no
    permission prompts, no user/project/local settings, no non-managed MCP —
    and runs cwd-neutral in the scratch, where no task input exists."""
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(canary_stdout=_session_ok("claude-sonnet-5"))
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert report.ok, report.detail
    sessions = [call for call in runner.calls if "--version" not in call["argv"]]
    assert sessions
    for call in sessions:
        argv = call["argv"]
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert argv[argv.index("--setting-sources") + 1] == ""
        assert "--strict-mcp-config" in argv
        assert "--dangerously-skip-permissions" not in argv
        assert call["cwd"] == str(tmp_path / "scratch")


def test_preflight_alias_pin_skips_the_same_family_canary(tmp_path):
    # An alias pin legitimately accepts its whole family — a same-family
    # canary would fail it for working as designed.
    _bake(tmp_path, "sonnet")
    runner = ScriptedRunner(canary_stdout=_session_ok("claude-sonnet-5"))
    report = _preflight(tmp_path, BakedIdentity("sonnet"), runner)
    assert report.ok, report.detail
    assert len(runner.canary_calls) == 1


def test_preflight_fails_on_policy_conflict_before_spending_tokens(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    _write(tmp_path, f"{DROPINS}/50-widen.json", {"availableModels": ["claude-opus-5"]})
    runner = ScriptedRunner()
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_POLICY_CONFLICT
    assert "50-widen.json" in report.detail
    assert runner.calls == []  # static failure: no subprocess ever ran


def test_preflight_fails_on_a_process_environment_conflict(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner()
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    report = run_preflight(
        BakedIdentity("claude-sonnet-5"),
        binary="claude",
        root=tmp_path,
        scratch=scratch,
        min_cli=MIN_CLI,
        run=runner,
        environ={"ANTHROPIC_BASE_URL": "https://proxy.example"},
    )
    assert not report.ok and report.category == CATEGORY_POLICY_CONFLICT
    assert "'ANTHROPIC_BASE_URL'" in report.detail
    assert "proxy.example" not in report.detail  # value redacted
    assert runner.calls == []


def test_preflight_fails_on_a_missing_or_mismatched_managed_pin(tmp_path):
    _write(tmp_path, "etc/theozolith/model", "claude-sonnet-5\n")  # no managed file
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), ScriptedRunner())
    assert not report.ok and report.category == CATEGORY_INCONSISTENT
    assert "no enforcement configuration" in report.detail

    _bake(tmp_path, "claude-sonnet-5", "low")
    (tmp_path / MANAGED).write_text(
        json.dumps(
            {
                "model": "claude-sonnet-5",
                "availableModels": ["claude-sonnet-5"],
                "enforceAvailableModels": True,
                "forceRemoteSettingsRefresh": True,
                "effortLevel": "low",
                "env": {"CLAUDE_CODE_EFFORT_LEVEL": "high"},  # drifted pin
            }
        )
    )
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "low"), ScriptedRunner())
    assert not report.ok
    # The drifted env pin is both a conflict (env identity key with a foreign
    # value) and an inconsistency — either category names the real problem.
    assert report.category in (CATEGORY_POLICY_CONFLICT, CATEGORY_INCONSISTENT)


def test_preflight_fails_on_an_image_without_the_freshness_key(tmp_path):
    # An image built by the previous toolchain: correct pin, no
    # forceRemoteSettingsRefresh — its sessions may start on stale server
    # policy, so nothing it proves counts. The named key says "rebuild".
    _bake(tmp_path, "claude-sonnet-5")
    document = json.loads((tmp_path / MANAGED).read_text())
    del document["forceRemoteSettingsRefresh"]
    _write(tmp_path, MANAGED, document)
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), ScriptedRunner())
    assert not report.ok and report.category == CATEGORY_INCONSISTENT
    assert "'forceRemoteSettingsRefresh'" in report.detail


def test_preflight_fails_on_an_invalid_pair(tmp_path):
    # An image baked by an older toolchain can carry a pair the new table
    # rejects: the runtime gate re-validates rather than trusting the build.
    _bake(tmp_path, "claude-opus-4-6", "xhigh")
    report = _preflight(tmp_path, BakedIdentity("claude-opus-4-6", "xhigh"), ScriptedRunner())
    assert not report.ok and report.category == CATEGORY_PAIR_INVALID


def test_preflight_fails_on_a_pre_enforcement_cli(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(version="2.1.231 (Claude Code)")
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_CLI_TOO_OLD
    assert "2.1.232" in report.detail
    assert len(runner.calls) == 1  # version probe only; no canary tokens


def test_preflight_widened_policy_is_detected_by_the_canary(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(canary_stdout=_session_ok("claude-haiku-4-5"))
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_WIDENED
    assert "claude-haiku-4-5" in report.detail


def test_preflight_same_family_widening_fails_a_full_id_pin(tmp_path):
    """Enforcement that quietly matched at FAMILY granularity: the
    different-family canary coerces correctly, but the same-family sibling
    executes — a full-ID pin must reject its own family too."""
    _bake(tmp_path, "claude-sonnet-5")

    def runner(argv, **kwargs):
        runner.calls.append({"argv": list(argv), **kwargs})
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="2.1.232", stderr="")
        if "--model" in argv:
            asked = argv[argv.index("--model") + 1]
            executed = "claude-sonnet-5" if asked == "claude-haiku-4-5" else asked
            return subprocess.CompletedProcess(argv, 0, stdout=_session_ok(executed), stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout=_session_ok("claude-sonnet-5"), stderr=""
        )

    runner.calls = []
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_WIDENED
    assert "same-family" in report.detail and "claude-sonnet-4-6" in report.detail


def test_preflight_substitution_is_detected_by_the_canary(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(canary_stdout=_session_ok("claude-opus-5"))
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_SUBSTITUTED


def test_preflight_canary_error_means_unavailable(tmp_path):
    _bake(tmp_path, "claude-nonexistent-9")
    stdout = _stream_json(
        {"type": "system", "subtype": "init", "model": "claude-nonexistent-9"},
        {"type": "result", "subtype": "error_during_execution", "is_error": True},
    )
    runner = ScriptedRunner(canary_stdout=stdout, canary_rc=1)
    report = _preflight(tmp_path, BakedIdentity("claude-nonexistent-9"), runner)
    assert not report.ok and report.category == CATEGORY_UNAVAILABLE


def test_preflight_no_signal_is_unverifiable(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(canary_stdout="")
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_UNVERIFIABLE


def test_preflight_timeout_fails_closed(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")

    def hanging(argv, **kwargs):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="2.1.232", stderr="")
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), hanging)
    assert not report.ok and report.category == CATEGORY_TIMEOUT


def test_preflight_probe_substitution_fails(tmp_path):
    # Canaries coerce (enforcement binds) but the probe's OWN session — the
    # one the effective policy picks with no --model at all — runs elsewhere.
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(
        canary_stdout=_session_ok("claude-sonnet-5"),
        probe_stdout=_session_ok("claude-opus-5"),
    )
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_SUBSTITUTED
    assert "identity probe" in report.detail and "claude-opus-5" in report.detail


def test_preflight_probe_error_means_unavailable(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(
        canary_stdout=_session_ok("claude-sonnet-5"), probe_stdout="", probe_rc=1
    )
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_UNAVAILABLE
    assert "identity probe" in report.detail


def test_preflight_effort_clamp_is_detected_by_the_probe(tmp_path):
    # An organization effort cap clamps silently in the stream — the Stop
    # hook capture is the observation, and a clamp is a preflight failure,
    # never an accepted downgrade.
    _bake(tmp_path, "claude-sonnet-5", "xhigh")
    runner = ScriptedRunner(
        canary_stdout=_session_ok("claude-sonnet-5"),
        on_probe=lambda: _write(
            tmp_path, "scratch/preflight-effort.json", {"effort": {"level": "high"}}
        ),
    )
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "xhigh"), runner)
    assert not report.ok and report.category == CATEGORY_EFFORT_CLAMPED
    assert "'high'" in report.detail and "'xhigh'" in report.detail
    assert report.probe_effort == "high"


def test_preflight_missing_effort_signal_is_unverifiable(tmp_path):
    # A CLI whose Stop payload carries no effort (or a hook that never ran):
    # the baked effort cannot be proven, so the gate fails closed instead of
    # assuming the pin held.
    _bake(tmp_path, "claude-sonnet-5", "low")
    runner = ScriptedRunner(canary_stdout=_session_ok("claude-sonnet-5"))
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "low"), runner)
    assert not report.ok and report.category == CATEGORY_UNVERIFIABLE
    assert "applied effort" in report.detail


def test_preflight_effort_probe_carries_the_stop_hook(tmp_path):
    _bake(tmp_path, "claude-sonnet-5", "low")
    runner = ScriptedRunner(
        canary_stdout=_session_ok("claude-sonnet-5"),
        on_probe=lambda: _write(
            tmp_path, "scratch/preflight-effort.json", {"effort": {"level": "low"}}
        ),
    )
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "low"), runner)
    assert report.ok, report.detail
    (probe,) = runner.probe_calls
    settings_json = probe[probe.index("--settings") + 1]
    assert "Stop" in settings_json and "preflight-effort.json" in settings_json
    # No tool-carrying probe anywhere: the Stop hook fires after a plain
    # no-tool turn (verified live), so the boundary stays intact.
    assert "PostToolUse" not in settings_json
    assert PROBE_PROMPT in probe
    assert "tool" in PROBE_PROMPT  # the prompt itself forbids tool use


def test_preflight_alias_pin_accepts_family_and_rejects_foreigners(tmp_path):
    _bake(tmp_path, "sonnet")
    runner = ScriptedRunner(canary_stdout=_session_ok("claude-sonnet-5"))
    report = _preflight(tmp_path, BakedIdentity("sonnet"), runner)
    assert report.ok, report.detail
    # The intruder for a sonnet-family pin is haiku; a haiku execution is a
    # widened policy, not a family member.
    runner = ScriptedRunner(canary_stdout=_session_ok("claude-haiku-4-5"))
    report = _preflight(tmp_path, BakedIdentity("sonnet"), runner)
    assert not report.ok and report.category == CATEGORY_WIDENED


def test_preflight_report_describe_confirms_the_prompt_was_withheld(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    report = _preflight(
        tmp_path,
        BakedIdentity("claude-sonnet-5"),
        ScriptedRunner(canary_stdout=_session_ok("claude-opus-5")),
    )
    text = report.describe()
    assert "claude-sonnet-5" in text and report.category in text
    assert "the real task prompt was not sent" in text


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


def test_sibling_for_full_id_pins():
    # Never a candidate the provider would merely resolve TO the pin.
    assert sibling_for("claude-sonnet-5") == "claude-sonnet-4-6"
    assert sibling_for("claude-sonnet-4-6") == "claude-sonnet-5"
    assert sibling_for("claude-opus-5") == "claude-opus-4-6"
    assert sibling_for("claude-haiku-4-5-20251001") == "claude-3-5-haiku-20241022"
    # Single-member families have no sibling: the same-family canary is
    # skipped, the different-family canary and the in-session guard remain.
    assert sibling_for("claude-fable-5") == ""
    assert sibling_for("claude-mythos-5") == ""
    # Aliases legitimately accept their family — no sibling canary.
    assert sibling_for("sonnet") == ""
    assert sibling_for("haiku") == ""


# -- the ConfigChange hook helper ---------------------------------------------


def _run_config_hook(tmp_path: Path, event: dict) -> list[dict]:
    script = tmp_path / "configchange_hook.py"
    script.write_text(CONFIG_CHANGE_HOOK_SOURCE, encoding="utf-8")
    capture = tmp_path / "config-change.jsonl"
    proc = subprocess.run(
        [sys.executable, str(script), str(capture)],
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
        assert records[0]["source"] == source and records[0]["file_path"] == "/etc/x.json"


def test_config_hook_ignores_skills_and_benign_project_settings(tmp_path):
    # The task legitimately edits the checkout: a skills change or a
    # project-settings change with no identity-shaped key is task work, and
    # skill model frontmatter is bound by the enforced allowlist anyway.
    assert _run_config_hook(tmp_path, {"source": "skills", "file_path": "/w/.claude/skills"}) == []
    benign = tmp_path / "settings.json"
    benign.write_text(json.dumps({"permissions": {"allow": ["Bash(npm test:*)"]}}))
    assert (
        _run_config_hook(tmp_path, {"source": "project_settings", "file_path": str(benign)}) == []
    )


def test_config_hook_records_identity_keys_in_project_settings(tmp_path):
    changed = tmp_path / "settings.json"
    changed.write_text(json.dumps({"model": "claude-opus-5", "env": {"ANTHROPIC_MODEL": "x"}}))
    records = _run_config_hook(tmp_path, {"source": "project_settings", "file_path": str(changed)})
    assert len(records) == 1
    assert records[0]["keys"] == ["ANTHROPIC_MODEL", "model"]
    # Key names only — never the values.
    assert "claude-opus-5" not in json.dumps(records)


def test_config_hook_records_unreadable_project_settings(tmp_path):
    # A changed file the helper cannot read back is unknowable → recorded.
    records = _run_config_hook(
        tmp_path, {"source": "project_settings", "file_path": str(tmp_path / "gone.json")}
    )
    assert len(records) == 1 and records[0]["keys"] == []


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
    # Garbage input is an unknown source → recorded, fail closed.
    assert proc.returncode == 0, proc.stderr
    assert capture.exists()


# -- the session guard --------------------------------------------------------


def _init_line(model: str) -> str:
    return json.dumps({"type": "system", "subtype": "init", "model": model})


def _turn_line(model: str) -> str:
    return json.dumps({"type": "assistant", "message": {"model": model, "content": []}})


def _gate(tmp_path: Path, effort: bool = False) -> TaskGate:
    gate_dir = tmp_path / "gate"
    gate_dir.mkdir(exist_ok=True)
    return TaskGate(
        release_marker=gate_dir / "released",
        effort_capture=(gate_dir / "effort.json") if effort else None,
        config_capture=gate_dir / "config-change.jsonl",
        config_hook_script=gate_dir / "configchange_hook.py",
    )


def test_guard_releases_only_after_init_and_an_executed_turn(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), _gate(tmp_path))
    assert guard.decision().action == GUARD_WAIT
    guard.observe(_init_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_WAIT  # announced is not executed
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_RELEASE
    assert guard.observed_model == "claude-sonnet-5"
    assert guard.probe_turns == 1 and guard.task_turns == 0


def test_guard_kills_on_a_substituted_init(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), _gate(tmp_path))
    guard.observe(_init_line("claude-opus-5"))
    decision = guard.decision()
    assert decision.action == GUARD_KILL
    assert decision.category == CATEGORY_SUBSTITUTED
    assert "claude-opus-5" in decision.reason and "substituted" in decision.reason


def test_guard_counts_probe_and_task_turns_separately(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), _gate(tmp_path))
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_RELEASE
    guard.mark_released()
    assert guard.probe_turns == 1 and guard.task_turns == 0
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.probe_turns == 1 and guard.task_turns == 1
    assert guard.decision().action == GUARD_WAIT  # released: monitor only


def test_guard_kills_on_model_drift_after_release(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), _gate(tmp_path))
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_RELEASE
    guard.mark_released()
    guard.observe(_turn_line("claude-opus-5"))  # mid-run policy change
    decision = guard.decision()
    assert decision.action == GUARD_KILL and "claude-opus-5" in decision.reason
    assert decision.category == CATEGORY_SUBSTITUTED


def test_guard_ignores_synthetic_turns_and_junk_lines(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), _gate(tmp_path))
    guard.observe("not json at all")
    guard.observe(json.dumps(["not", "an", "object"]))
    guard.observe(_turn_line("<synthetic>"))
    assert guard.decision().action == GUARD_WAIT
    assert guard.probe_turns == 0


def test_guard_probe_error_before_a_turn_is_unavailable(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), _gate(tmp_path))
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(json.dumps({"type": "result", "subtype": "error", "is_error": True}))
    decision = guard.decision()
    assert decision.action == GUARD_KILL and "available" in decision.reason
    assert decision.category == CATEGORY_UNAVAILABLE


def test_guard_waits_for_the_effort_capture_then_releases(tmp_path):
    gate = _gate(tmp_path, effort=True)
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5", "low"), gate)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_WAIT  # effort not yet observed
    gate.effort_capture.write_text(json.dumps({"effort": {"level": "low"}}))
    assert guard.decision().action == GUARD_RELEASE
    assert guard.observed_effort == "low"


def test_guard_kills_on_a_clamped_effort_with_the_exact_category(tmp_path):
    # An organization effort cap clamps silently in stream-json output — the
    # Stop-hook capture is the observation, and a clamp is GUARD_KILL with
    # the effort-clamped category, never merely "not release".
    gate = _gate(tmp_path, effort=True)
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5", "xhigh"), gate)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    gate.effort_capture.write_text(json.dumps({"effort": {"level": "high"}}))
    decision = guard.decision()
    assert decision.action == GUARD_KILL
    assert decision.category == CATEGORY_EFFORT_CLAMPED
    assert "'high'" in decision.reason and "'xhigh'" in decision.reason


def test_guard_kills_on_effort_drift_after_release(tmp_path):
    gate = _gate(tmp_path, effort=True)
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5", "low"), gate)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    gate.effort_capture.write_text(json.dumps({"effort": {"level": "low"}}))
    assert guard.decision().action == GUARD_RELEASE
    guard.mark_released()
    gate.effort_capture.write_text(json.dumps({"effort": {"level": "high"}}))  # mid-run change
    decision = guard.decision()
    assert decision.action == GUARD_KILL
    assert decision.category == CATEGORY_EFFORT_CLAMPED


def test_guard_partial_capture_file_is_wait_not_kill(tmp_path):
    gate = _gate(tmp_path, effort=True)
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5", "low"), gate)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    gate.effort_capture.write_text('{"effort": {"le')  # hook mid-write
    assert guard.decision().action == GUARD_WAIT


def test_guard_no_effort_baked_ignores_the_capture_channel(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), _gate(tmp_path))
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_RELEASE


def test_guard_kills_on_a_recorded_config_change(tmp_path):
    # The ConfigChange helper recorded an identity-relevant mid-session
    # settings change: the session no longer runs under the proven
    # configuration — kill before further task work, pre- or post-release.
    gate = _gate(tmp_path)
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), gate)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    guard.mark_released()
    gate.config_capture.write_text(
        json.dumps({"source": "policy_settings", "file_path": "/etc/x.json", "keys": []}) + "\n"
    )
    decision = guard.decision()
    assert decision.action == GUARD_KILL
    assert decision.category == CATEGORY_CONFIG_CHANGED
    assert "policy_settings" in decision.reason


def test_guard_config_change_blocks_release_too(tmp_path):
    gate = _gate(tmp_path)
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), gate)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    gate.config_capture.write_text(
        json.dumps({"source": "user_settings", "file_path": "/h/.claude/settings.json"}) + "\n"
    )
    assert guard.decision().action == GUARD_KILL


def test_guard_input_lines_are_the_stream_json_user_shape(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), _gate(tmp_path))
    line = guard.render_input("do the task")
    event = json.loads(line)
    assert event["type"] == "user"
    assert event["message"]["role"] == "user"
    assert event["message"]["content"] == [{"type": "text", "text": "do the task"}]
    assert "\n" not in line
    probe = json.loads(guard.probe_input())
    assert "Preflight check" in probe["message"]["content"][0]["text"]
