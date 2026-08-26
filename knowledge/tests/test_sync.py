from __future__ import annotations

import hashlib
import json
import shutil

import pytest
from theozolith_knowledge.model import KnowledgeError
from theozolith_knowledge.sync import MANIFEST_NAME, sync
from treeutil import GOLDEN, stat_snapshot, tree_snapshot

IGNORE_MANIFEST = frozenset({MANIFEST_NAME})


def test_golden_global_sync(tmp_path, sample_knowledge):
    """Acceptance 1: the fixture knowledge repo syncs to the expected
    ~/.claude tree, including CLAUDE.md derived from AGENTS.md."""
    target = tmp_path / "claude"
    report = sync(sample_knowledge, "global", target)

    assert len(report.created) == 8
    assert not report.hand_edited
    assert tree_snapshot(target, IGNORE_MANIFEST) == tree_snapshot(GOLDEN / "claude_global")

    manifest = json.loads((target / MANIFEST_NAME).read_text())
    assert set(manifest["files"]) == set(tree_snapshot(GOLDEN / "claude_global"))
    for path, sha in manifest["files"].items():
        assert sha == hashlib.sha256((target / path).read_bytes()).hexdigest()


def test_rerun_on_unchanged_source_is_noop(tmp_path, sample_knowledge):
    """Acceptance 2: re-running sync on an unchanged source produces zero
    changes — no writes at all, manifest included."""
    target = tmp_path / "claude"
    sync(sample_knowledge, "global", target)
    before = stat_snapshot(target)

    report = sync(sample_knowledge, "global", target)

    assert not report.changed
    assert report.unchanged == 8
    assert stat_snapshot(target) == before


def test_project_scope_layout(tmp_path, sample_knowledge):
    project = tmp_path / "project"
    report = sync(sample_knowledge, "project", project)
    assert report.changed
    assert (project / "CLAUDE.md").is_file()
    assert (project / ".claude" / "skills" / "greet" / "SKILL.md").is_file()
    assert (project / ".claude" / MANIFEST_NAME).is_file()


def test_golden_codex_global_sync(tmp_path, sample_knowledge):
    """The same fixture root compiled for codex: verbatim AGENTS.md, skills
    verbatim, agents/codex as prompts, workflows dropped."""
    target = tmp_path / "codex"
    report = sync(sample_knowledge, "global", target, tool="codex")

    assert not report.hand_edited
    assert tree_snapshot(target, IGNORE_MANIFEST) == tree_snapshot(GOLDEN / "codex_global")


def test_codex_project_scope_is_rejected(tmp_path, sample_knowledge):
    with pytest.raises(KnowledgeError, match="global-scope only"):
        sync(sample_knowledge, "project", tmp_path / "project", tool="codex")


def test_unknown_tool_is_rejected(tmp_path, sample_knowledge):
    with pytest.raises(KnowledgeError, match="no compiler for tool 'pi'"):
        sync(sample_knowledge, "global", tmp_path / "pi", tool="pi")


def test_hand_edit_is_warned_and_overwritten(tmp_path, sample_knowledge):
    target = tmp_path / "claude"
    sync(sample_knowledge, "global", target)
    edited = target / "agents" / "planner.md"
    edited.write_text("local tweak\n")

    report = sync(sample_knowledge, "global", target)

    assert report.hand_edited == ["agents/planner.md"]
    assert report.updated == ["agents/planner.md"]
    assert edited.read_text() != "local tweak\n"


def test_strict_fails_without_writing(tmp_path, sample_knowledge):
    target = tmp_path / "claude"
    sync(sample_knowledge, "global", target)
    edited = target / "agents" / "planner.md"
    edited.write_text("local tweak\n")

    with pytest.raises(KnowledgeError, match="diverged"):
        sync(sample_knowledge, "global", target, strict=True)
    assert edited.read_text() == "local tweak\n"


def test_check_mode_writes_nothing(tmp_path, sample_knowledge):
    target = tmp_path / "claude"
    report = sync(sample_knowledge, "global", target, check=True)
    assert len(report.created) == 8
    assert not target.exists()


def test_removed_source_files_are_deleted_and_dirs_pruned(tmp_path, sample_knowledge):
    source = tmp_path / "source"
    shutil.copytree(sample_knowledge, source)
    target = tmp_path / "claude"
    sync(source, "global", target)

    shutil.rmtree(source / "skills" / "greet")
    report = sync(source, "global", target)

    assert sorted(report.deleted) == [
        "skills/greet/SKILL.md",
        "skills/greet/scripts/hello.sh",
    ]
    assert not (target / "skills" / "greet").exists()
    assert (target / "skills" / "code-review").is_dir()


def test_foreign_files_are_never_touched(tmp_path, sample_knowledge):
    target = tmp_path / "claude"
    target.mkdir()
    settings = target / "settings.json"
    settings.write_text('{"mine": true}\n')

    sync(sample_knowledge, "global", target)
    report = sync(sample_knowledge, "global", target)

    assert settings.read_text() == '{"mine": true}\n'
    assert not report.changed


def test_symlink_target_is_replaced_not_written_through(tmp_path, sample_knowledge):
    """A symlink where a managed file belongs (e.g. CLAUDE.md -> AGENTS.md)
    must be replaced; writing through it would clobber the link target."""
    target = tmp_path / "claude"
    target.mkdir()
    linked_to = tmp_path / "AGENTS.md"
    linked_to.write_text("the real AGENTS.md\n")
    (target / "CLAUDE.md").symlink_to(linked_to)

    report = sync(sample_knowledge, "global", target)

    assert "CLAUDE.md" in report.hand_edited
    assert not (target / "CLAUDE.md").is_symlink()
    assert linked_to.read_text() == "the real AGENTS.md\n"


def test_identical_preexisting_file_is_adopted_silently(tmp_path, sample_knowledge):
    target = tmp_path / "claude"
    sync(sample_knowledge, "global", target)
    (target / MANIFEST_NAME).unlink()

    report = sync(sample_knowledge, "global", target)

    assert not report.changed
    assert not report.hand_edited
    assert (target / MANIFEST_NAME).is_file()
