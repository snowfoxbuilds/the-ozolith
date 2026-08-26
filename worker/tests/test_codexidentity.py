"""The codex baked-identity machinery (ADR-0052, PROBE + STATIC doctrine).

Unit tests for codexidentity.py: the baked-identity reader against the
theozolith-owned config.toml, the per-session CODEX_HOME assembly, the
process-environment audit, the free static checks, the setup dry-run with a
scripted subprocess runner (whose probe writes a rollout journal — the
observation channel), and the benign per-Run observer. The rollout shapes
mirror records captured live from codex-cli 0.150.0."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from theozolith_worker.codexidentity import (
    BAKED_CONFIG_FILE,
    CodexSessionMonitor,
    assemble_codex_home,
    pair_error,
    read_baked_identity,
    read_rollout_turn_context,
    run_preflight,
    scan_process_environment,
    scan_stream_errors,
    static_identity_report,
)
from theozolith_worker.identity import (
    CATEGORY_CLI_TOO_OLD,
    CATEGORY_INCONSISTENT,
    CATEGORY_PAIR_INVALID,
    CATEGORY_POLICY_CONFLICT,
    CATEGORY_SUBSTITUTED,
    CATEGORY_TIMEOUT,
    CATEGORY_UNAVAILABLE,
    CATEGORY_UNVERIFIABLE,
    BakedIdentity,
    IdentityError,
    MonitorHooks,
)

MIN_CLI = (0, 150, 0)
AUTH = '{"tokens": {"access_token": "a", "refresh_token": "r"}}'


def _write(root: Path, relpath: str, text: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _bake(root: Path, model: str, effort: str = "") -> None:
    _write(root, "etc/theozolith/model", model + "\n")
    if effort:
        _write(root, "etc/theozolith/effort", effort + "\n")
    lines = [f'model = "{model}"']
    if effort:
        lines.append(f'model_reasoning_effort = "{effort}"')
    _write(root, BAKED_CONFIG_FILE, "\n".join(lines) + "\n")


def _rollout(home: Path, model: str, effort: str = "") -> Path:
    """A 0.150.0-shaped session rollout journal under ``home``."""
    payload = {"turn_id": "t1", "model": model}
    if effort:
        payload["effort"] = effort
    records = [
        {"type": "session_meta", "payload": {"model_provider": "openai"}},
        {"type": "turn_context", "payload": payload},
    ]
    return _write(
        home,
        "sessions/2026/08/26/rollout-2026-08-26T12-00-00-abc.jsonl",
        "".join(json.dumps(record) + "\n" for record in records),
    )


# -- the baked-identity reader -------------------------------------------------


def test_baked_identity_reads_the_declaration(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    assert read_baked_identity(tmp_path) == BakedIdentity(model="gpt-5.2-codex")


def test_no_declaration_is_none(tmp_path):
    assert read_baked_identity(tmp_path) is None


def test_empty_model_file_is_corrupt(tmp_path):
    _write(tmp_path, "etc/theozolith/model", "")
    with pytest.raises(IdentityError, match="empty"):
        read_baked_identity(tmp_path)


def test_effort_without_model_is_not_an_identity(tmp_path):
    _write(tmp_path, "etc/theozolith/effort", "high\n")
    with pytest.raises(IdentityError, match="not an identity"):
        read_baked_identity(tmp_path)


def test_declared_model_without_baked_config_selects_nothing(tmp_path):
    _write(tmp_path, "etc/theozolith/model", "gpt-5.2-codex\n")
    with pytest.raises(IdentityError, match=r"no.*selection configuration"):
        read_baked_identity(tmp_path)


def test_baked_config_mismatch_is_inconsistent(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    _write(tmp_path, BAKED_CONFIG_FILE, 'model = "o3"\n')
    with pytest.raises(IdentityError, match="does not carry the baked identity"):
        read_baked_identity(tmp_path)


def test_baked_config_without_declaration_is_refused(tmp_path):
    _write(tmp_path, BAKED_CONFIG_FILE, 'model = "o3"\n')
    with pytest.raises(IdentityError, match="declares no baked identity"):
        read_baked_identity(tmp_path)


def test_foreign_steering_keys_in_the_baked_config_are_refused(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    _write(tmp_path, BAKED_CONFIG_FILE, 'model = "gpt-5.2-codex"\nprofile = "fast"\n')
    with pytest.raises(IdentityError, match="steering keys"):
        read_baked_identity(tmp_path)


def test_malformed_baked_config_is_unknowable(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    _write(tmp_path, BAKED_CONFIG_FILE, "model = [broken\n")
    with pytest.raises(IdentityError, match="not valid TOML"):
        read_baked_identity(tmp_path)


# -- CODEX_HOME assembly -------------------------------------------------------


def test_assemble_codex_home_writes_config_and_auth(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    home = assemble_codex_home(tmp_path / "home", root=tmp_path, environ={"CODEX_AUTH_JSON": AUTH})
    assert (home / "config.toml").read_bytes() == (tmp_path / BAKED_CONFIG_FILE).read_bytes()
    auth = home / "auth.json"
    assert auth.read_text() == AUTH
    assert auth.stat().st_mode & 0o777 == 0o600
    assert home.stat().st_mode & 0o777 == 0o700


def test_assemble_codex_home_without_credential_is_actionable(tmp_path):
    with pytest.raises(IdentityError, match=r"CODEX_AUTH_JSON.*\[secrets\]"):
        assemble_codex_home(tmp_path / "home", root=tmp_path, environ={})
    assert not (tmp_path / "home").exists() or not list((tmp_path / "home").iterdir())


def test_assemble_codex_home_without_baked_config_still_authenticates(tmp_path):
    # A model-less worker type: no baked config, the CLI's default model.
    home = assemble_codex_home(tmp_path / "home", root=tmp_path, environ={"CODEX_AUTH_JSON": AUTH})
    assert not (home / "config.toml").exists()
    assert (home / "auth.json").is_file()


# -- the rollout journal -------------------------------------------------------


def test_rollout_turn_context_reads_model_and_effort(tmp_path):
    _rollout(tmp_path, "gpt-5.2-codex", "high")
    assert read_rollout_turn_context(tmp_path) == ("gpt-5.2-codex", "high")


def test_rollout_last_record_wins(tmp_path):
    path = _rollout(tmp_path, "gpt-5.2-codex")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "turn_context", "payload": {"model": "o3", "effort": "low"}}) + "\n"
        )
    assert read_rollout_turn_context(tmp_path) == ("o3", "low")


def test_rollout_absent_is_a_gap(tmp_path):
    assert read_rollout_turn_context(tmp_path) == ("", "")


def test_stream_errors_surface_the_first_fatal_event():
    text = (
        json.dumps({"type": "thread.started", "thread_id": "t"})
        + "\n"
        + json.dumps({"type": "error", "message": "401 Unauthorized"})
        + "\n"
    )
    assert scan_stream_errors(text) == "401 Unauthorized"
    assert scan_stream_errors("") == ""
    assert scan_stream_errors(json.dumps({"type": "turn.failed"}) + "\n") == "turn.failed"


# -- static checks -------------------------------------------------------------


def test_static_checks_pass_on_a_consistent_bake(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    report = static_identity_report(
        BakedIdentity("gpt-5.2-codex"), root=tmp_path, environ={"CODEX_AUTH_JSON": AUTH}
    )
    assert report.ok, report.detail


def test_static_checks_fail_on_a_drifted_config(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    _write(tmp_path, BAKED_CONFIG_FILE, 'model = "o3"\n')
    report = static_identity_report(BakedIdentity("gpt-5.2-codex"), root=tmp_path, environ={})
    assert not report.ok and report.category == CATEGORY_INCONSISTENT


def test_static_checks_fail_on_an_unproven_pair(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex", "high")
    report = static_identity_report(
        BakedIdentity("gpt-5.2-codex", "high"), root=tmp_path, environ={}
    )
    assert not report.ok and report.category == CATEGORY_PAIR_INVALID


def test_environment_audit_names_the_variable_never_the_value(tmp_path):
    conflicts = scan_process_environment(
        {"CODEX_HOME": "/evil", "OPENAI_BASE_URL": "https://mitm", "OPENAI_API_KEY": "sk-x"}
    )
    assert len(conflicts) == 3
    assert all("sk-x" not in line and "mitm" not in line for line in conflicts)
    # The credential itself is not a steering variable.
    assert scan_process_environment({"CODEX_AUTH_JSON": AUTH, "HOME": "/home/ozolith"}) == []


def test_static_checks_fail_on_a_steering_environment(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    report = static_identity_report(
        BakedIdentity("gpt-5.2-codex"), root=tmp_path, environ={"CODEX_HOME": "/evil"}
    )
    assert not report.ok and report.category == CATEGORY_POLICY_CONFLICT
    assert "CODEX_HOME" in report.detail


def test_pair_error_wording_names_the_spike():
    assert pair_error("gpt-5.2-codex", "") == ""
    assert "S7" in pair_error("gpt-5.2-codex", "high")


# -- the setup dry-run (scripted subprocess runner) ----------------------------


class ScriptedRunner:
    """Answers ``codex --version`` and the exec probe from a script.
    ``on_probe`` runs when the probe is invoked — tests use it to simulate
    the CLI writing the session rollout journal into the probe's home."""

    def __init__(self, version="codex-cli 0.150.0", probe_stdout="", probe_rc=0, on_probe=None):
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
            self.on_probe(kwargs)
        return subprocess.CompletedProcess(argv, self.probe_rc, stdout=self.probe_stdout, stderr="")

    @property
    def probe_calls(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls if "--version" not in call["argv"]]


def _preflight(tmp_path, identity_obj, runner, **kwargs):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return run_preflight(
        identity_obj,
        binary="codex",
        root=tmp_path,
        scratch=scratch,
        min_cli=MIN_CLI,
        run=runner,
        environ={"CODEX_AUTH_JSON": AUTH},
        **kwargs,
    )


def _writes_rollout(tmp_path, model, effort=""):
    def on_probe(kwargs):
        home = Path(kwargs["env"]["CODEX_HOME"])
        _rollout(home, model, effort)

    return on_probe


def test_preflight_passes_via_the_rollout_journal(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    runner = ScriptedRunner(on_probe=_writes_rollout(tmp_path, "gpt-5.2-codex"))
    report = _preflight(tmp_path, BakedIdentity("gpt-5.2-codex"), runner)
    assert report.ok, report.detail
    assert report.cli_version == "codex-cli 0.150.0"
    assert report.probe_model == "gpt-5.2-codex"
    (probe,) = runner.probe_calls
    # One probe exec in the throwaway home: no model selection on the argv,
    # read-only sandbox, no git-repo requirement, cwd in the scratch.
    assert probe[:2] == ["codex", "exec"]
    for flag in ("-m", "--model", "-c", "--profile"):
        assert flag not in probe
    assert probe[probe.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in probe
    (call,) = [c for c in runner.calls if "--version" not in c["argv"]]
    assert call["cwd"] == str(tmp_path / "scratch")
    # The probe's CODEX_HOME lives under the scratch and got the baked
    # config plus the 0600 credential.
    home = Path(call["env"]["CODEX_HOME"])
    assert home == tmp_path / "scratch" / "codex-home"
    assert (home / "config.toml").is_file() and (home / "auth.json").is_file()
    assert len(runner.calls) == 2  # version + probe, nothing else


def test_preflight_static_failure_spends_no_subprocess(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    _write(tmp_path, BAKED_CONFIG_FILE, 'model = "o3"\n')
    runner = ScriptedRunner()
    report = _preflight(tmp_path, BakedIdentity("gpt-5.2-codex"), runner)
    assert not report.ok and report.category == CATEGORY_INCONSISTENT
    assert runner.calls == []


def test_preflight_fails_on_a_pre_floor_cli(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    runner = ScriptedRunner(version="codex-cli 0.149.9")
    report = _preflight(tmp_path, BakedIdentity("gpt-5.2-codex"), runner)
    assert not report.ok and report.category == CATEGORY_CLI_TOO_OLD


def test_preflight_unparseable_version_is_unverifiable(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    runner = ScriptedRunner(version="codex 150")
    report = _preflight(tmp_path, BakedIdentity("gpt-5.2-codex"), runner)
    assert not report.ok and report.category == CATEGORY_UNVERIFIABLE


def test_preflight_missing_credential_is_unverifiable(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    report = run_preflight(
        BakedIdentity("gpt-5.2-codex"),
        binary="codex",
        root=tmp_path,
        scratch=scratch,
        min_cli=MIN_CLI,
        run=ScriptedRunner(),
        environ={},
    )
    assert not report.ok and report.category == CATEGORY_UNVERIFIABLE
    assert "CODEX_AUTH_JSON" in report.detail


def test_preflight_probe_error_means_unavailable(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    stream = json.dumps({"type": "error", "message": "401 Unauthorized"}) + "\n"
    runner = ScriptedRunner(probe_stdout=stream, probe_rc=1)
    report = _preflight(tmp_path, BakedIdentity("gpt-5.2-codex"), runner)
    assert not report.ok and report.category == CATEGORY_UNAVAILABLE
    assert "401 Unauthorized" in report.detail


def test_preflight_no_rollout_signal_is_unverifiable(tmp_path):
    """Strict where the per-Run observer is lenient: a probe that leaves no
    turn_context record means the observation channel itself is broken."""
    _bake(tmp_path, "gpt-5.2-codex")
    runner = ScriptedRunner()  # exits 0, writes no rollout
    report = _preflight(tmp_path, BakedIdentity("gpt-5.2-codex"), runner)
    assert not report.ok and report.category == CATEGORY_UNVERIFIABLE
    assert "turn_context" in report.detail


def test_preflight_substitution_fails(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")
    runner = ScriptedRunner(on_probe=_writes_rollout(tmp_path, "o3"))
    report = _preflight(tmp_path, BakedIdentity("gpt-5.2-codex"), runner)
    assert not report.ok and report.category == CATEGORY_SUBSTITUTED
    assert report.probe_model == "o3"


def test_preflight_timeout_fails_closed(tmp_path):
    _bake(tmp_path, "gpt-5.2-codex")

    class TimeoutRunner(ScriptedRunner):
        def __call__(self, argv, **kwargs):
            if "--version" in argv:
                return super().__call__(argv, **kwargs)
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    report = _preflight(tmp_path, BakedIdentity("gpt-5.2-codex"), TimeoutRunner())
    assert not report.ok and report.category == CATEGORY_TIMEOUT


# -- the benign per-Run observer -----------------------------------------------


def _hooks(scratch: Path) -> MonitorHooks:
    return MonitorHooks(
        stop_capture=scratch / "stop.jsonl",
        config_capture=scratch / "config-change.jsonl",
        config_baseline=scratch / "config-baseline.json",
        config_hook_script=scratch / "configchange_hook.py",
        stop_hook_script=scratch / "stop_hook.py",
    )


def test_monitor_never_violates_even_on_an_off_model_rollout(tmp_path):
    """The doctrine test (ADR-0052): the observer records what ran and NEVER
    kills — a detected mismatch surfaces through evidence, not a mid-run
    shutdown."""
    home = tmp_path / "home"
    _rollout(home, "o3", "low")
    monitor = CodexSessionMonitor(BakedIdentity("gpt-5.2-codex"), _hooks(tmp_path), home)
    monitor.observe(json.dumps({"type": "thread.started", "thread_id": "t"}))
    assert monitor.violation() == ("", "")
    assert monitor.observed_model == "o3"
    assert monitor.observed_effort == "low"


def test_monitor_without_a_home_records_nothing(tmp_path):
    monitor = CodexSessionMonitor(BakedIdentity("gpt-5.2-codex"), _hooks(tmp_path), None)
    assert monitor.violation() == ("", "")
    assert monitor.observed_model == ""
