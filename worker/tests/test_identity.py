"""The baked-identity enforcement machinery (ADR-0045, fail closed).

Unit tests for identity.py: the managed-tier conflict scan both gates run
(build and runtime), the pair-aware (model, effort) capability table, the
baked-identity reader, the subprocess preflight with a scripted runner, and
the session guard's gate/monitor state machine. Live proof that the real CLI
behaves as these tests assume is test_live_enforcement.py.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from theozolith_worker import identity
from theozolith_worker.identity import (
    CATEGORY_CLI_TOO_OLD,
    CATEGORY_INCONSISTENT,
    CATEGORY_PAIR_INVALID,
    CATEGORY_POLICY_CONFLICT,
    CATEGORY_SUBSTITUTED,
    CATEGORY_TIMEOUT,
    CATEGORY_UNAVAILABLE,
    CATEGORY_UNVERIFIABLE,
    CATEGORY_WIDENED,
    GUARD_KILL,
    GUARD_RELEASE,
    GUARD_WAIT,
    BakedIdentity,
    ClaudeSessionGuard,
    IdentityError,
    effort_capability,
    model_matches,
    pair_error,
    read_baked_identity,
    run_preflight,
    scan_managed_conflicts,
)

MANAGED = "etc/claude-code/managed-settings.json"
DROPINS = "etc/claude-code/managed-settings.d"


def _write(root: Path, relpath: str, data) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def _bake(root: Path, model: str, effort: str = "") -> None:
    """The exact artifact materialize() writes for (model, effort)."""
    _write(root, "etc/theozolith/model", model + "\n")
    settings: dict = {"model": model, "availableModels": [model], "enforceAvailableModels": True}
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
    ],
)
def test_scan_flags_identity_env_keys(tmp_path, name):
    _write(tmp_path, f"{DROPINS}/50-ops.json", {"env": {name: "anything", "UNRELATED": "ok"}})
    conflicts = scan_managed_conflicts(tmp_path)
    assert len(conflicts) == 1
    assert f"'{name}'" in conflicts[0] and "50-ops.json" in conflicts[0]


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


def _canary_ok(model: str) -> str:
    return _stream_json(
        {"type": "system", "subtype": "init", "model": model},
        {"type": "assistant", "message": {"model": model, "content": []}},
        {"type": "result", "subtype": "success", "is_error": False},
    )


class ScriptedRunner:
    """Answers `claude --version` and the canary invocation from a script."""

    def __init__(self, version="2.1.231 (Claude Code)", canary_stdout="", canary_rc=0):
        self.version = version
        self.canary_stdout = canary_stdout
        self.canary_rc = canary_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=self.version, stderr="")
        return subprocess.CompletedProcess(
            argv, self.canary_rc, stdout=self.canary_stdout, stderr=""
        )


def _preflight(tmp_path, identity_obj, runner, **kwargs):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return run_preflight(
        identity_obj,
        binary="claude",
        root=tmp_path,
        scratch=scratch,
        min_cli=(2, 1, 223),
        run=runner,
        **kwargs,
    )


def test_preflight_passes_on_a_bound_identity(tmp_path):
    _bake(tmp_path, "claude-sonnet-5", "low")
    runner = ScriptedRunner(canary_stdout=_canary_ok("claude-sonnet-5"))
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5", "low"), runner)
    assert report.ok, report.detail
    assert report.cli_version.startswith("2.1.231")
    assert report.canary_model == "claude-sonnet-5"
    # The canary asked for an intruder of a DIFFERENT family with the Run's
    # own credential; enforcement coerced it back to the pin.
    canary = runner.calls[-1]
    assert canary[canary.index("--model") + 1] == "claude-haiku-4-5"


def test_preflight_fails_on_policy_conflict_before_spending_tokens(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    _write(tmp_path, f"{DROPINS}/50-widen.json", {"availableModels": ["claude-opus-5"]})
    runner = ScriptedRunner()
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_POLICY_CONFLICT
    assert "50-widen.json" in report.detail
    assert runner.calls == []  # static failure: no subprocess ever ran


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


def test_preflight_fails_on_an_invalid_pair(tmp_path):
    # An image baked by an older toolchain can carry a pair the new table
    # rejects: the runtime gate re-validates rather than trusting the build.
    _bake(tmp_path, "claude-opus-4-6", "xhigh")
    report = _preflight(tmp_path, BakedIdentity("claude-opus-4-6", "xhigh"), ScriptedRunner())
    assert not report.ok and report.category == CATEGORY_PAIR_INVALID


def test_preflight_fails_on_a_pre_enforcement_cli(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(version="2.1.222 (Claude Code)")
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_CLI_TOO_OLD
    assert "2.1.223" in report.detail
    assert len(runner.calls) == 1  # version probe only; no canary tokens


def test_preflight_widened_policy_is_detected_by_the_canary(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(canary_stdout=_canary_ok("claude-haiku-4-5"))
    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), runner)
    assert not report.ok and report.category == CATEGORY_WIDENED
    assert "claude-haiku-4-5" in report.detail


def test_preflight_substitution_is_detected_by_the_canary(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    runner = ScriptedRunner(canary_stdout=_canary_ok("claude-opus-5"))
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
            return subprocess.CompletedProcess(argv, 0, stdout="2.1.231", stderr="")
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    report = _preflight(tmp_path, BakedIdentity("claude-sonnet-5"), hanging)
    assert not report.ok and report.category == CATEGORY_TIMEOUT


def test_preflight_alias_pin_accepts_family_and_rejects_foreigners(tmp_path):
    _bake(tmp_path, "sonnet")
    runner = ScriptedRunner(canary_stdout=_canary_ok("claude-sonnet-5"))
    report = _preflight(tmp_path, BakedIdentity("sonnet"), runner)
    assert report.ok, report.detail
    # The intruder for a sonnet-family pin is haiku; a haiku execution is a
    # widened policy, not a family member.
    runner = ScriptedRunner(canary_stdout=_canary_ok("claude-haiku-4-5"))
    report = _preflight(tmp_path, BakedIdentity("sonnet"), runner)
    assert not report.ok and report.category == CATEGORY_WIDENED


def test_preflight_report_describe_confirms_the_prompt_was_withheld(tmp_path):
    _bake(tmp_path, "claude-sonnet-5")
    report = _preflight(
        tmp_path,
        BakedIdentity("claude-sonnet-5"),
        ScriptedRunner(canary_stdout=_canary_ok("claude-opus-5")),
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


# -- the session guard --------------------------------------------------------


def _init_line(model: str) -> str:
    return json.dumps({"type": "system", "subtype": "init", "model": model})


def _turn_line(model: str) -> str:
    return json.dumps({"type": "assistant", "message": {"model": model, "content": []}})


def test_guard_releases_only_after_init_and_an_executed_turn(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), None)
    assert guard.decision().action == GUARD_WAIT
    guard.observe(_init_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_WAIT  # announced is not executed
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_RELEASE
    assert guard.observed_model == "claude-sonnet-5"


def test_guard_kills_on_a_substituted_init(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), None)
    guard.observe(_init_line("claude-opus-5"))
    decision = guard.decision()
    assert decision.action == GUARD_KILL
    assert "claude-opus-5" in decision.reason and "substituted" in decision.reason


def test_guard_kills_on_model_drift_after_release(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), None)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_RELEASE
    guard.observe(_turn_line("claude-opus-5"))  # mid-run policy change
    decision = guard.decision()
    assert decision.action == GUARD_KILL and "claude-opus-5" in decision.reason


def test_guard_ignores_synthetic_turns_and_junk_lines(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), None)
    guard.observe("not json at all")
    guard.observe(json.dumps(["not", "an", "object"]))
    guard.observe(_turn_line("<synthetic>"))
    assert guard.decision().action == GUARD_WAIT


def test_guard_probe_error_before_a_turn_is_unavailable(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), None)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(json.dumps({"type": "result", "subtype": "error", "is_error": True}))
    decision = guard.decision()
    assert decision.action == GUARD_KILL and "available" in decision.reason


def test_guard_waits_for_the_effort_capture_then_releases(tmp_path):
    capture = tmp_path / "effort.json"
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5", "low"), capture)
    assert guard.probe_prompt != identity.PROBE_PROMPT  # tool variant: hook must fire
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_WAIT  # effort not yet observed
    capture.write_text(json.dumps({"effort": {"level": "low"}}))
    assert guard.decision().action == GUARD_RELEASE
    assert guard.observed_effort == "low"


def test_guard_kills_on_a_clamped_effort(tmp_path):
    # An organization effort cap clamps silently in stream-json output — the
    # hook capture is the observation, and a clamp is a preflight failure,
    # never an accepted downgrade.
    capture = tmp_path / "effort.json"
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5", "xhigh"), capture)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    capture.write_text(json.dumps({"effort": {"level": "high"}}))
    decision = guard.decision()
    assert decision.action == GUARD_KILL
    assert "'high'" in decision.reason and "'xhigh'" in decision.reason


def test_guard_kills_on_effort_drift_after_release(tmp_path):
    capture = tmp_path / "effort.json"
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5", "low"), capture)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    capture.write_text(json.dumps({"effort": {"level": "low"}}))
    assert guard.decision().action == GUARD_RELEASE
    capture.write_text(json.dumps({"effort": {"level": "high"}}))  # mid-run change
    assert guard.decision().action == GUARD_KILL


def test_guard_partial_capture_file_is_wait_not_kill(tmp_path):
    capture = tmp_path / "effort.json"
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5", "low"), capture)
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    capture.write_text('{"effort": {"le')  # hook mid-write
    assert guard.decision().action == GUARD_WAIT


def test_guard_no_effort_baked_ignores_the_capture_channel(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), tmp_path / "effort.json")
    assert guard.probe_prompt == identity.PROBE_PROMPT  # no tool needed
    guard.observe(_init_line("claude-sonnet-5"))
    guard.observe(_turn_line("claude-sonnet-5"))
    assert guard.decision().action == GUARD_RELEASE


def test_guard_input_lines_are_the_stream_json_user_shape(tmp_path):
    guard = ClaudeSessionGuard(BakedIdentity("claude-sonnet-5"), None)
    line = guard.render_input("do the task")
    event = json.loads(line)
    assert event["type"] == "user"
    assert event["message"]["role"] == "user"
    assert event["message"]["content"] == [{"type": "text", "text": "do the task"}]
    assert "\n" not in line
    probe = json.loads(guard.probe_input())
    assert "Preflight check" in probe["message"]["content"][0]["text"]
