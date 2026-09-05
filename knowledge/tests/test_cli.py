from __future__ import annotations

from theozolith_knowledge.cli import main


def test_validate_ok(sample_knowledge, capsys):
    assert main(["validate", "--source", str(sample_knowledge)]) == 0
    out = capsys.readouterr().out
    assert "skills: 2" in out
    # Per-tool section counts are the visibility for content a compiler
    # drops (codex has no workflows target, ADR-0052).
    assert "claude agents: 2" in out
    assert "codex prompts: 1" in out
    assert "codex agent roles: 1" in out
    assert "hooks: 2" in out


def test_sync_tool_codex(tmp_path, sample_knowledge):
    target = tmp_path / "codex"
    args = [
        "sync",
        "--source",
        str(sample_knowledge),
        "--scope",
        "global",
        "--target",
        str(target),
        "--tool",
        "codex",
    ]
    assert main(args) == 0
    assert (target / "AGENTS.md").is_file()
    assert (target / "prompts" / "triage.md").is_file()
    assert (target / "agents" / "grunt.toml").is_file()
    assert (target / "hooks" / "hooks.json").is_file()
    assert not (target / "CLAUDE.md").exists()


def test_validate_rejects_non_root(tmp_path, capsys):
    assert main(["validate", "--source", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err


def test_sync_project_scope_requires_target(sample_knowledge, capsys):
    assert main(["sync", "--source", str(sample_knowledge), "--scope", "project"]) == 2


def test_sync_check_exit_codes(tmp_path, sample_knowledge):
    target = tmp_path / "claude"
    args = ["sync", "--source", str(sample_knowledge), "--scope", "global", "--target", str(target)]
    assert main([*args, "--check"]) == 1  # out of date
    assert main(args) == 0  # apply
    assert main([*args, "--check"]) == 0  # clean


def test_sync_warns_on_hand_edit(tmp_path, sample_knowledge, capsys):
    target = tmp_path / "claude"
    args = ["sync", "--source", str(sample_knowledge), "--scope", "global", "--target", str(target)]
    assert main(args) == 0
    (target / "CLAUDE.md").write_text("tweak\n")
    assert main(args) == 0
    assert "never hand-edited" in capsys.readouterr().err
