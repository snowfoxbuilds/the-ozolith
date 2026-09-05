"""`theozolith config ingest` (ADR-0048): harvest -> lint -> pin -> compile ->
commit -> reload. The acceptance criteria from #62: refuse a dirty pinned
tree, refuse live placeholder checksums, lint exactly what config load lints
BEFORE committing, stamp source provenance, and never leave a partial state."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from theozolith_control import configdist, configrepo, controltoml, product, repolock
from theozolith_control.ingest import PENDING_MARKER, IngestError, ingest, resolve_image_digest

DIGEST = "a" * 64
BASE = f"ghcr.io/snowfoxbuilds/theozolith-run-claude:1.2@sha256:{DIGEST}"
TAG_ONLY_BASE = "ghcr.io/snowfoxbuilds/theozolith-run-claude:1.2"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _commit_all(cwd: Path, message: str = "edit") -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


def write(root: Path, relpath: str, text: str) -> None:
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def source_repo(
    tmp_path: Path, *, git: bool = True, knowledge: bool = True, product_pin: bool = True
) -> Path:
    """A minimal valid Config Repo: one driver worker type, one stopped Stack,
    a knowledge root, and a [settings] surface."""
    src = tmp_path / "config-src"
    src.mkdir(exist_ok=True)
    knowledge_line = 'knowledge = "knowledge/dev"\n' if knowledge else ""
    write(
        src,
        "worker-types/claude-dev.toml",
        f'driver = "builtin:implementer"\nadapter = "claude"\n'
        f'model = "claude-sonnet-5"\nworkspace = "acme/sandbox"\n'
        f'base = "{BASE}"\n{knowledge_line}'
        '[secrets]\nGITHUB_TOKEN = "github-implementer"\n',
    )
    write(
        src,
        "stacks/implementer.toml",
        'worker_type = "claude-dev"\nnode = "box1"\nstate = "stopped"\n',
    )
    if knowledge:
        write(src, "knowledge/dev/AGENTS.md", "# team knowledge\n")
        write(src, "knowledge/dev/skills/hello/SKILL.md", "say hello\n")
    write(src, "control.toml", "[settings]\nheartbeat_seconds = 30\n")
    if product_pin:
        write(src, "product.toml", '[product]\nversion = "0.3.0"\n')
    if git:
        _git(src, "init", "-q")
        _commit_all(src, "config")
    return src


def pinned_dir(tmp_path: Path) -> Path:
    return tmp_path / "configs"


def test_ingest_compiles_pins_commits_and_the_result_loads(tmp_path):
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    report = ingest(str(src), pinned, log=lambda *_: None)

    assert report.changed
    assert report.source_commit == _git(src, "rev-parse", "HEAD")
    # Provenance is stamped in the commit message and in pins.toml.
    assert report.source_commit in _git(pinned, "log", "-1", "--format=%s")
    assert report.source_commit in (pinned / "pins.toml").read_text()
    # The knowledge tree is COMPILED output, once per tool (ADR-0052):
    # the claude view (AGENTS.md became CLAUDE.md) and the codex view
    # (AGENTS.md verbatim, skills shared) each under their tool subdir.
    assert (pinned / "knowledge/dev/claude/CLAUDE.md").is_file()
    assert (pinned / "knowledge/dev/claude/skills/hello/SKILL.md").is_file()
    assert not (pinned / "knowledge/dev/claude/AGENTS.md").exists()
    assert (pinned / "knowledge/dev/codex/AGENTS.md").is_file()
    assert (pinned / "knowledge/dev/codex/skills/hello/SKILL.md").is_file()
    assert report.knowledge_pins == {
        "dev/claude": configdist.knowledge_tree_hash(pinned, "dev/claude"),
        "dev/codex": configdist.knowledge_tree_hash(pinned, "dev/codex"),
    }
    # The pinned build loads under the real validator, pin joined per tool.
    config = configrepo.load_config(pinned)
    assert config.worker_types["claude-dev"].knowledge_pin == report.knowledge_pins["dev/claude"]
    # control.toml went through ingest: the settings surface arrived.
    assert controltoml.read_values(pinned)["heartbeat_seconds"] == 30.0
    # The working tree is clean — everything ingested is committed.
    assert _git(pinned, "status", "--porcelain") == ""


def test_second_ingest_is_a_no_op(tmp_path):
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    head = _git(pinned, "rev-parse", "HEAD")
    report = ingest(str(src), pinned, log=lambda *_: None)
    assert not report.changed
    assert _git(pinned, "rev-parse", "HEAD") == head


def test_dirty_pinned_build_is_refused(tmp_path):
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    (pinned / "hand-edit.toml").write_text("nope\n")
    with pytest.raises(IngestError, match="uncommitted changes"):
        ingest(str(src), pinned, log=lambda *_: None)


def test_dirty_git_source_is_refused(tmp_path):
    src = source_repo(tmp_path)
    write(src, "stacks/implementer.toml", 'worker_type = "claude-dev"\nnode = "box2"\n')
    with pytest.raises(IngestError, match="commit them first"):
        ingest(str(src), pinned_dir(tmp_path), log=lambda *_: None)


def test_lint_failure_commits_nothing(tmp_path):
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    head = _git(pinned, "rev-parse", "HEAD")
    write(
        src,
        "worker-types/broken.toml",
        'driver = "builtin:nope"\nbase = "x@sha256:' + "b" * 64 + '"\n',
    )
    _commit_all(src, "break it")
    with pytest.raises(IngestError, match="nothing committed"):
        ingest(str(src), pinned, log=lambda *_: None)
    assert _git(pinned, "rev-parse", "HEAD") == head
    assert _git(pinned, "status", "--porcelain") == ""
    assert not (pinned / "worker-types/broken.toml").exists()


def test_live_placeholder_checksum_is_refused_but_a_template_passes(tmp_path):
    """The fail-closed placeholder convention survives (ADR-0048): a stopped
    template may carry all-zero checksums (the init scaffold does), but a
    worker type a RUNNING Stack references may not."""
    src = source_repo(tmp_path, knowledge=False)
    write(
        src,
        "worker-types/claude-dev.toml",
        f'driver = "builtin:implementer"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{BASE}"\n'
        f'setup = ["TS_SHA256={"0" * 64} install-tailscale"]\n',
    )
    _commit_all(src, "placeholder")
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)  # stopped Stack: allowed

    write(src, "stacks/implementer.toml", 'worker_type = "claude-dev"\nnode = "box1"\n')
    _commit_all(src, "flip running")
    with pytest.raises(IngestError, match="placeholder"):
        ingest(str(src), pinned, log=lambda *_: None)


def test_tag_only_base_resolves_through_the_injected_resolver(tmp_path):
    src = source_repo(tmp_path, knowledge=False)
    write(
        src,
        "worker-types/claude-dev.toml",
        'driver = "builtin:implementer"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{TAG_ONLY_BASE}"\n',
    )
    _commit_all(src, "tag only")
    pinned = pinned_dir(tmp_path)
    seen: list[str] = []

    def resolver(ref: str) -> str:
        seen.append(ref)
        return f"sha256:{'c' * 64}"

    report = ingest(str(src), pinned, resolve_digest=resolver, log=lambda *_: None)
    assert seen == [TAG_ONLY_BASE]
    assert report.resolved_bases == {TAG_ONLY_BASE: f"sha256:{'c' * 64}"}
    wt = configrepo.load_config(pinned).worker_types["claude-dev"]
    assert wt.base == f"{TAG_ONLY_BASE}@sha256:{'c' * 64}"


def test_a_bad_resolver_answer_is_refused(tmp_path):
    src = source_repo(tmp_path, knowledge=False)
    write(
        src,
        "worker-types/claude-dev.toml",
        'driver = "builtin:implementer"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{TAG_ONLY_BASE}"\n',
    )
    _commit_all(src, "tag only")
    with pytest.raises(IngestError, match="expected 'sha256:"):
        ingest(
            str(src),
            pinned_dir(tmp_path),
            resolve_digest=lambda ref: "not-a-digest",
            log=lambda *_: None,
        )


def test_claude_pin_values_survive_the_per_tool_layout(tmp_path):
    """The migration's no-retag proof (ADR-0052): knowledge_tree_hash is
    computed with relpaths RELATIVE TO THE TREE ROOT, so the claude compile
    moving from knowledge/dev/ to knowledge/dev/claude/ keeps the pin value
    byte-identical — no claude worker type re-tags on the layout change."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    report = ingest(str(src), pinned, log=lambda *_: None)

    legacy = tmp_path / "legacy-shaped"
    shutil.copytree(pinned / "knowledge/dev/claude", legacy / "knowledge/dev")
    assert configdist.knowledge_tree_hash(legacy, "dev") == report.knowledge_pins["dev/claude"]


def test_per_tool_compile_skips_empty_filesets(tmp_path):
    """A tree with content for only one tool records only that tool's pin
    and writes only that tool's directory (ADR-0052): workflows compile for
    claude alone; agents/codex compiles for codex alone."""
    src = source_repo(tmp_path)
    write(src, "knowledge/wf-only/workflows/pair.md", "review in pairs\n")
    write(src, "knowledge/cdx-only/agents/codex/triage.md", "triage first\n")
    _commit_all(src, "single-tool trees")
    pinned = pinned_dir(tmp_path)
    report = ingest(str(src), pinned, log=lambda *_: None)

    assert "wf-only/claude" in report.knowledge_pins
    assert "wf-only/codex" not in report.knowledge_pins
    assert not (pinned / "knowledge/wf-only/codex").exists()
    assert "cdx-only/codex" in report.knowledge_pins
    assert "cdx-only/claude" not in report.knowledge_pins
    assert not (pinned / "knowledge/cdx-only/claude").exists()
    assert (pinned / "knowledge/cdx-only/codex/prompts/triage.md").is_file()


def test_reingest_migrates_a_legacy_layout_pinned_build(tmp_path):
    """A pre-ADR-0052 pinned build (bare knowledge/<name>/ claude compile,
    bare pins keys) converts on the operator's next ingest of the UNCHANGED
    source — the recommended post-update step. Until then the legacy layout
    loads through the compat shims (exercised by the configrepo goldens)."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)

    # Reshape the pinned build into its pre-ADR-0052 form by hand: claude
    # compile bare under knowledge/dev/, bare pins key, committed.
    claude_tree = pinned / "knowledge/dev/claude"
    staged = pinned / "knowledge/.legacy-stage"
    claude_tree.rename(staged)
    shutil.rmtree(pinned / "knowledge/dev")
    staged.rename(pinned / "knowledge/dev")
    pin = configdist.knowledge_tree_hash(pinned, "dev")
    pins_text = (pinned / "pins.toml").read_text()
    pins_text = pins_text[: pins_text.index("[knowledge]")] + f'[knowledge]\n"dev" = "{pin}"\n'
    (pinned / "pins.toml").write_text(pins_text)
    _commit_all(pinned, "hand-shaped legacy layout")
    assert configrepo.load_config(pinned).worker_types["claude-dev"].knowledge_pin == pin

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert report.changed
    assert (pinned / "knowledge/dev/claude/CLAUDE.md").is_file()
    assert (pinned / "knowledge/dev/codex/AGENTS.md").is_file()
    assert not (pinned / "knowledge/dev/CLAUDE.md").exists()
    # Pin value unchanged for claude (no-retag), now under the per-tool key.
    assert report.knowledge_pins["dev/claude"] == pin


def test_knowledge_edit_retags_only_the_referencing_type(tmp_path):
    """Selective rebuild (ADR-0048): per-tree pins mean an edit to one
    knowledge tree moves exactly the worker types that reference it."""
    src = source_repo(tmp_path)
    write(
        src,
        "worker-types/other.toml",
        'driver = "builtin:reviewer"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{BASE}"\n',
    )
    _commit_all(src, "second type")
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)

    write(src, "knowledge/dev/skills/hello/SKILL.md", "say hello twice\n")
    _commit_all(src, "knowledge edit")
    report = ingest(str(src), pinned, log=lambda *_: None)
    assert set(report.retagged) == {"claude-dev"}
    old, new = report.retagged["claude-dev"]
    assert old and new and old != new


def test_chmod_only_knowledge_edit_repins_and_retags(tmp_path):
    """A chmod-only change (no byte edits) to a knowledge source script is a
    real content change (ADR-0048 amendment): the compiled tree's executable
    state flips, so ingest commits a new pinned build, records a new per-tree
    pin, re-tags the referencing worker type, and the distribution hash moves
    — a node rebakes the derived image from the re-distributed tree."""
    src = source_repo(tmp_path)
    script = src / "knowledge/dev/skills/hello/run.sh"
    script.write_text("#!/bin/sh\necho hello\n")
    _commit_all(src, "script, not yet executable")
    pinned = pinned_dir(tmp_path)
    before = ingest(str(src), pinned, log=lambda *_: None)
    before_dist = configdist.dist_hash(pinned)
    before_tag = configrepo.load_config(pinned).worker_types["claude-dev"].tag

    script.chmod(0o755)
    _commit_all(src, "chmod +x only")
    after = ingest(str(src), pinned, log=lambda *_: None)
    assert after.changed
    assert after.knowledge_pins["dev/claude"] != before.knowledge_pins["dev/claude"]
    assert set(after.retagged) == {"claude-dev"}
    assert configrepo.load_config(pinned).worker_types["claude-dev"].tag != before_tag
    assert configdist.dist_hash(pinned) != before_dist
    assert (pinned / "knowledge/dev/claude/skills/hello/run.sh").stat().st_mode & 0o111


def test_chmod_only_folder_source_edit_changes_the_provenance_stamp(tmp_path):
    """Folder-mode sources stamp a content hash as provenance; a chmod-only
    edit must move that stamp too (git sources get this from git tracking the
    exec bit) — the pinned build's source stamp stays truthful."""
    src = source_repo(tmp_path, git=False)
    script = src / "knowledge/dev/skills/hello/run.sh"
    script.write_text("#!/bin/sh\necho hello\n")
    pinned = pinned_dir(tmp_path)
    before = ingest(str(src), pinned, log=lambda *_: None)
    script.chmod(0o755)
    after = ingest(str(src), pinned, log=lambda *_: None)
    assert after.changed
    assert after.source_commit != before.source_commit


def test_knowledge_that_does_not_compile_is_refused_at_ingest(tmp_path):
    """Compile errors surface at ingest — never at image build or container
    start (ADR-0048). An empty knowledge root is the simplest reject."""
    src = source_repo(tmp_path, git=False)
    (src / "knowledge" / "empty").mkdir()
    with pytest.raises(IngestError, match="is not a knowledge root"):
        ingest(str(src), pinned_dir(tmp_path), log=lambda *_: None)


def test_source_symlink_is_refused(tmp_path):
    src = source_repo(tmp_path, git=False)
    (src / "worker-types" / "link.toml").symlink_to(src / "worker-types" / "claude-dev.toml")
    with pytest.raises(IngestError, match="symlink"):
        ingest(str(src), pinned_dir(tmp_path), log=lambda *_: None)


# -- Agent Policy trees (ADR-0055) -----------------------------------------------

POLICY_DOC = '{"attribution": {"sessionUrl": false}}\n'


def test_policy_trees_are_validated_pinned_and_round_trip(tmp_path):
    """A valid policy tree copies verbatim into the pinned build, its content
    pin lands under [policy] in pins.toml, and the pinned build loads with
    the pin joined onto the referencing worker type (ADR-0055)."""
    src = source_repo(tmp_path, git=False)
    write(src, "policy/claude-defaults/attribution.json", POLICY_DOC)
    write(
        src,
        "worker-types/claude-dev.toml",
        f'driver = "builtin:implementer"\nadapter = "claude"\n'
        f'model = "claude-sonnet-5"\nworkspace = "acme/sandbox"\n'
        f'base = "{BASE}"\npolicy = "policy/claude-defaults"\n',
    )
    pinned = pinned_dir(tmp_path)
    report = ingest(str(src), pinned, log=lambda *_: None)
    assert (pinned / "policy/claude-defaults/attribution.json").read_text() == POLICY_DOC
    expected = configdist.policy_tree_hash(pinned, "claude-defaults")
    assert report.policy_pins == {"claude-defaults": expected}
    assert f'"claude-defaults" = "{expected}"' in (pinned / "pins.toml").read_text()
    assert "policy/claude-defaults pinned" in report.summary()
    config = configrepo.load_config(pinned)
    assert config.worker_types["claude-dev"].policy_pin == expected


def test_invalid_policy_tree_fails_ingest_with_nothing_committed(tmp_path):
    """An unadmitted key refuses at ingest (the allowlist's first site), and
    the refusal commits nothing; a dry run reports the same refusal."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    head = _git(pinned, "rev-parse", "HEAD")
    write(src, "policy/rogue/hooks.json", '{"hooks": {"Stop": []}}\n')
    _commit_all(src, "rogue policy")
    with pytest.raises(IngestError, match=r"policy/rogue/hooks\.json.*'hooks'"):
        ingest(str(src), pinned, log=lambda *_: None)
    assert _git(pinned, "rev-parse", "HEAD") == head
    assert not (pinned / "policy").exists()
    with pytest.raises(IngestError, match=r"policy/rogue/hooks\.json.*'hooks'"):
        ingest(str(src), pinned, dry_run=True, log=lambda *_: None)


def test_dot_prefixed_policy_drop_in_is_refused_not_silently_dropped(tmp_path):
    """Validation runs over the SOURCE tree: a dot-prefixed drop-in would be
    silently excluded by the staging copy filter, so it refuses instead —
    a file the operator wrote must never become dead policy (ADR-0055)."""
    src = source_repo(tmp_path, git=False)
    write(src, "policy/t/attribution.json", POLICY_DOC)
    write(src, "policy/t/.draft.json", POLICY_DOC)
    with pytest.raises(IngestError, match=r"policy/t/\.draft\.json"):
        ingest(str(src), pinned_dir(tmp_path), log=lambda *_: None)


def test_policy_root_entries_must_be_directories_with_plain_names(tmp_path):
    src = source_repo(tmp_path, git=False)
    write(src, "policy/stray.json", POLICY_DOC)
    with pytest.raises(IngestError, match="must be a directory"):
        ingest(str(src), pinned_dir(tmp_path), log=lambda *_: None)


def test_empty_policy_tree_records_no_pin(tmp_path):
    src = source_repo(tmp_path, git=False)
    (src / "policy" / "empty").mkdir(parents=True)
    report = ingest(str(src), pinned_dir(tmp_path), log=lambda *_: None)
    assert report.policy_pins == {}
    assert "[policy]" not in (pinned_dir(tmp_path) / "pins.toml").read_text()


def test_ingest_and_config_load_provably_share_the_policy_validator(tmp_path, monkeypatch):
    """ADR-0055 §2: one validator at both sites. A single monkeypatch of
    theozolith_worker.policy.validate_policy_tree is observed from ingest
    (--dry-run included) AND from configrepo.load_config — both invoke the
    module attribute, so they cannot drift apart."""
    from theozolith_worker import policy as worker_policy

    src = source_repo(tmp_path, git=False)
    write(src, "policy/claude-defaults/attribution.json", POLICY_DOC)
    pinned = pinned_dir(tmp_path)

    calls: list[tuple[str, str]] = []
    real = worker_policy.validate_policy_tree

    def spy(root, *, label):
        calls.append((str(root), label))
        return real(root, label=label)

    monkeypatch.setattr(worker_policy, "validate_policy_tree", spy)

    ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert any(label == "policy/claude-defaults" for _, label in calls)

    calls.clear()
    ingest(str(src), pinned, log=lambda *_: None)
    # A real ingest hits the validator from _pin_policy AND from the lint's
    # load_config over staging; both observations come through the one spy.
    assert len(calls) >= 2

    calls.clear()
    configrepo.load_config(pinned)
    assert any(label == "policy/claude-defaults" for _, label in calls)


def test_source_control_table_is_refused_and_address_is_preserved(tmp_path):
    src = source_repo(tmp_path, knowledge=False)
    pinned = pinned_dir(tmp_path)
    # Machine state in the pinned build (what init wrote).
    pinned.mkdir(parents=True)
    _git(pinned, "init", "-q")
    controltoml.write_control_address(pinned, "192.0.2.10", port=9443)
    ingest(str(src), pinned, log=lambda *_: None)
    assert controltoml.read_control_ip(pinned) == "192.0.2.10"
    assert controltoml.read_control_port(pinned) == 9443
    assert controltoml.read_values(pinned)["heartbeat_seconds"] == 30.0

    write(src, "control.toml", '[control]\ncontrol_ip = "10.0.0.1"\n')
    _commit_all(src, "smuggle address")
    with pytest.raises(IngestError, match=r"\[control\] table is machine state"):
        ingest(str(src), pinned, log=lambda *_: None)


def test_folder_source_stamps_a_content_hash(tmp_path):
    src = source_repo(tmp_path, git=False)
    report = ingest(str(src), pinned_dir(tmp_path), log=lambda *_: None)
    assert report.source_commit.startswith("folder-")


def test_git_url_source_is_cloned_and_stamped(tmp_path):
    src = source_repo(tmp_path)
    url = f"file://{src}"
    pinned = pinned_dir(tmp_path)
    report = ingest(url, pinned, log=lambda *_: None)
    assert report.changed
    assert report.source_commit == _git(src, "rev-parse", "HEAD")
    assert (pinned / "worker-types/claude-dev.toml").is_file()


def test_ingested_distribution_round_trips_to_the_node_side(tmp_path):
    """The pinned build's knowledge trees ride the config distribution and
    verify node-side — the per-tree pin agrees across packages."""
    from theozolith_nodedaemon import configdist as node_configdist

    src = source_repo(tmp_path)
    write(src, "drivers/hello.py", "class Driver:  # pragma: no cover\n    pass\n")
    _commit_all(src, "driver too")
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)

    digest, path = configdist.build_artifact(pinned, tmp_path / "out", built_against="0.3.0")
    assert digest and path is not None
    dest = tmp_path / "node-tree"
    dest.mkdir()
    node_configdist.extract_zip(path.read_bytes(), dest)
    assert node_configdist.manifest_hash_of_tree(dest) == digest
    for tree in ("dev/claude", "dev/codex"):
        assert node_configdist.knowledge_tree_hash(dest, tree) == configdist.knowledge_tree_hash(
            pinned, tree
        )


def test_executable_skill_scripts_survive_the_round_trip(tmp_path):
    """The exec bit must survive compile -> pinned build -> artifact ->
    node extraction (ADR-0048): a skill script bakes runnable."""
    from theozolith_nodedaemon import configdist as node_configdist

    src = source_repo(tmp_path, git=False)
    script = src / "knowledge/dev/skills/hello/run.sh"
    script.write_text("#!/bin/sh\necho hello\n")
    script.chmod(0o755)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    assert (pinned / "knowledge/dev/claude/skills/hello/run.sh").stat().st_mode & 0o111

    digest, path = configdist.build_artifact(pinned, tmp_path / "out", built_against="0.3.0")
    dest = tmp_path / "node-tree"
    dest.mkdir()
    node_configdist.extract_zip(path.read_bytes(), dest)
    assert (dest / "knowledge/dev/claude/skills/hello/run.sh").stat().st_mode & 0o111
    assert node_configdist.manifest_hash_of_tree(dest) == digest


# -- the ingest transaction: lock, commit-first, crash recovery -------------------


def _failing_git(*fail_tokens: str):
    """A runner that delegates to the real subprocess.run but fails any git
    invocation whose argv carries one of the tokens — the injected-failure
    harness for the transaction tests."""

    def run(argv, **kwargs):
        if argv[0] == "git" and any(token in argv for token in fail_tokens):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="injected failure")
        return subprocess.run(argv, **kwargs)

    return run


def test_concurrent_ingest_is_refused_by_the_shared_write_lock(tmp_path):
    """The whole transaction holds the shared pinned-build write lock: a
    second writer is refused loudly while one runs, and proceeds normally
    once the lock is released — serialized, never interleaved."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    with repolock.pinned_write_lock(pinned, writer="the test, standing in for a writer"):
        with pytest.raises(IngestError, match="already running"):
            ingest(str(src), pinned, log=lambda *_: None)
        # The refused attempt changed nothing.
        assert _git(pinned, "status", "--porcelain") == ""
    report = ingest(str(src), pinned, log=lambda *_: None)
    assert not report.changed


def test_short_writers_racing_an_ingest_are_refused_cleanly(tmp_path):
    """Interleaving one: the product-pin and control-address writers attempt
    while an ingest transaction holds the write lock (the server's
    POST /api/v1/product/update racing a CLI ingest). Each is refused with
    nothing written and nothing committed — the ingest transaction can never
    be interleaved with, overwritten, or orphaned."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    head = _git(pinned, "rev-parse", "HEAD")
    with repolock.pinned_write_lock(pinned, writer="ingest (held by the test)"):
        with pytest.raises(product.ProductError, match="serialized"):
            product.write_pin(pinned, "9.9.9", log=lambda *_: None)
        with pytest.raises(controltoml.ControlTomlError, match="serialized"):
            controltoml.write_control_address(pinned, "192.0.2.99", log=lambda *_: None)
        with pytest.raises(controltoml.ControlTomlError, match="serialized"):
            controltoml.write_browser_origin(pinned, "https://ozolith.example", log=lambda *_: None)
    assert _git(pinned, "rev-parse", "HEAD") == head
    assert _git(pinned, "status", "--porcelain") == ""
    assert "9.9.9" not in (pinned / "product.toml").read_text()


def test_ingest_racing_a_short_writer_is_refused_cleanly(tmp_path):
    """Interleaving two: an ingest attempts while a short writer (a product
    pin bump mid-commit) holds the write lock — refused with the pinned
    build untouched, and normal once the writer finishes."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)

    committed: list[str] = []

    def pin_and_race(argv, **kwargs):
        if argv[0] == "git" and "commit" in argv and not committed:
            committed.append("racing")
            with pytest.raises(IngestError, match="already running"):
                ingest(str(src), pinned, log=lambda *_: None)
        return subprocess.run(argv, **kwargs)

    product.write_pin(pinned, "9.9.9", runner=pin_and_race, log=lambda *_: None)
    assert committed == ["racing"]  # the race actually ran, mid-transaction
    assert "9.9.9" in (pinned / "product.toml").read_text()
    assert _git(pinned, "status", "--porcelain") == ""


def test_unsupported_concurrent_commit_fails_the_publish_and_is_preserved(tmp_path):
    """A hand-run git commit racing the transaction (supported writers share
    the lock and cannot get here): the compare-and-swap ref move fails
    cleanly, the interloper's commit stays HEAD — never overwritten or
    orphaned — no marker survives, and the next ingest extends the
    interloper's history."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    write(src, "knowledge/dev/AGENTS.md", "# team knowledge v2\n")
    _commit_all(src, "edit")

    def interloping(argv, **kwargs):
        if argv[0] == "git" and "update-ref" in argv:
            (pinned / "interloper.toml").write_text("# a hand edit, hand-committed\n")
            _commit_all(pinned, "interloper")
        return subprocess.run(argv, **kwargs)

    with pytest.raises(IngestError, match="HEAD moved during the ingest"):
        ingest(str(src), pinned, runner=interloping, log=lambda *_: None)
    assert _git(pinned, "log", "-1", "--format=%s") == "interloper"
    assert (pinned / "interloper.toml").is_file()
    assert _git(pinned, "status", "--porcelain") == ""
    assert not (pinned / ".git" / PENDING_MARKER).exists()

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert report.changed
    assert "interloper" in _git(pinned, "log", "--format=%s")
    assert "v2" in (pinned / "knowledge/dev/claude/CLAUDE.md").read_text()


def test_ignored_leftovers_are_purged_and_the_worktree_is_exactly_head(tmp_path):
    """A `.git/info/exclude` rule hides files from the clean check, but
    nothing outside committed HEAD may remain loadable or distributable: the
    next ingest — even a no-op one — removes ignored files and nested
    repositories from the machine-owned worktree and reports it."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    head = _git(pinned, "rev-parse", "HEAD")
    (pinned / ".git" / "info").mkdir(exist_ok=True)
    (pinned / ".git" / "info" / "exclude").write_text("stacks/leftover.toml\njunk/\n")
    # A stray Stack file config load WOULD read, and a nested repository.
    write(
        pinned,
        "stacks/leftover.toml",
        'worker_type = "claude-dev"\nnode = "box9"\nstate = "stopped"\n',
    )
    nested = pinned / "junk"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "file.txt").write_text("x\n")

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert not report.changed  # the no-op path purges too
    assert not (pinned / "stacks" / "leftover.toml").exists()
    assert not nested.exists()
    assert any("ignored leftovers" in note for note in report.notes)
    assert _git(pinned, "rev-parse", "HEAD") == head
    assert _git(pinned, "status", "--porcelain", "--ignored") == ""
    assert [s.name for s in configrepo.load_config(pinned).stacks] == ["implementer"]


def test_exclude_rules_cannot_drop_staged_content_from_the_commit(tmp_path):
    """The staged add is FORCED: a `.git/info/exclude` pattern (or a
    user-global excludes file) matching a real config file must not silently
    drop it from the pinned commit — the published worktree still carries it,
    committed."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    (pinned / ".git" / "info").mkdir(exist_ok=True)
    (pinned / ".git" / "info" / "exclude").write_text("product.toml\n")
    write(src, "product.toml", '[product]\nversion = "0.4.0"\n')
    _commit_all(src, "bump")

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert report.changed
    assert "product.toml" in _git(pinned, "ls-tree", "-r", "--name-only", "HEAD")
    assert (pinned / "product.toml").read_text() == '[product]\nversion = "0.4.0"\n'
    assert _git(pinned, "status", "--porcelain", "--ignored") == ""


# -- the product pin: a pin-less source preserves, a declared one wins ---------
# (ADR-0051 amending ADR-0048)


def test_a_pinless_source_preserves_the_update_flows_pin(tmp_path):
    """A Config Repo without product.toml means "the update flow owns the
    pin": the pin `theozolith build`/`update` wrote survives the next ingest
    verbatim. (It used to be DELETED — the whole-tree commit dropped it —
    silently retargeting the fleet to the latest release at the next serve
    start's ensure_pin.)"""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    # The real update-flow writer: same shared lock, same commit machinery.
    product.write_pin(pinned, "0.5.0+gdeadbeef1234", log=lambda *_: None)
    (src / "product.toml").unlink()
    _commit_all(src, "stop declaring the pin")

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert report.changed  # the pins.toml source stamp moved
    assert 'version = "0.5.0+gdeadbeef1234"' in (pinned / "product.toml").read_text()
    assert configrepo.load_config(pinned).product_version == "0.5.0+gdeadbeef1234"
    assert any(
        "preserved the pinned build's product pin 0.5.0+gdeadbeef1234" in note
        for note in report.notes
    )
    # Preservation is not movement: the divergence note must not fire.
    assert not any(note.startswith("product version:") for note in report.notes)
    assert _git(pinned, "status", "--porcelain") == ""


def test_a_pin_absent_everywhere_stays_absent(tmp_path):
    """No declared pin and no update-flow pin: nothing to preserve, nothing
    invented — a fresh deployment's first ingest ships pin-less and the
    serve-start ensure_pin resolves one, exactly as before."""
    src = source_repo(tmp_path, product_pin=False)
    pinned = pinned_dir(tmp_path)
    report = ingest(str(src), pinned, log=lambda *_: None)
    assert report.changed
    assert not (pinned / "product.toml").exists()
    assert configrepo.load_config(pinned).product_version == ""
    assert not any("product.toml" in note for note in report.notes)


def test_a_declared_product_toml_still_wins_over_the_update_flow_pin(tmp_path):
    """ADR-0048 unchanged for the declared case: a source that carries
    product.toml overwrites whatever the update flow wrote, with the
    divergence surfaced — and the preserve note must not fire."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    product.write_pin(pinned, "9.9.9", log=lambda *_: None)
    write(src, "product.toml", '[product]\nversion = "0.4.0"\n')
    _commit_all(src, "bump the declared pin")

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert (pinned / "product.toml").read_text() == '[product]\nversion = "0.4.0"\n'
    assert any("wins over any pin the update flow wrote" in note for note in report.notes)
    assert not any("preserved the pinned build's" in note for note in report.notes)


def test_a_non_regular_product_toml_is_refused(tmp_path):
    """product.toml has exactly two valid states — absent, or a regular
    file. A committed DIRECTORY at that path is refused loudly with nothing
    committed: silently preserving the update-flow pin INTO it would ship a
    `product.toml/product.toml` no loader reads while the report claims the
    pin survived. HEAD, the worktree, and the deployed pin stay untouched."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    product.write_pin(pinned, "0.5.0+gdeadbeef1234", log=lambda *_: None)
    head = _git(pinned, "rev-parse", "HEAD")
    (src / "product.toml").unlink()
    write(src, "product.toml/product.toml", '[product]\nversion = "6.6.6"\n')
    _commit_all(src, "a directory where the pin file goes")

    with pytest.raises(IngestError, match=r"product\.toml is a directory"):
        ingest(str(src), pinned, log=lambda *_: None)
    assert _git(pinned, "rev-parse", "HEAD") == head
    assert _git(pinned, "status", "--porcelain") == ""
    assert 'version = "0.5.0+gdeadbeef1234"' in (pinned / "product.toml").read_text()
    # The dry run — the config linter — refuses with the same message.
    with pytest.raises(IngestError, match=r"product\.toml is a directory"):
        ingest(str(src), pinned, dry_run=True, log=lambda *_: None)


# -- dry run: the config linter (lint + preview, nothing committed) ------------


def test_dry_run_previews_changes_without_committing(tmp_path):
    """`config ingest --dry-run`: the full pipeline through lint, then a
    per-file preview of what the commit would change — with NOTHING written
    into the pinned build, not even loose objects."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    head = _git(pinned, "rev-parse", "HEAD")
    objects_before = _git(pinned, "count-objects", "-v")
    write(src, "knowledge/dev/AGENTS.md", "# updated knowledge\n")
    write(src, "product.toml", '[product]\nversion = "0.4.0"\n')
    _commit_all(src, "update")

    report = ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert report.dry_run and report.changed and not report.pinned_commit
    assert "update knowledge/dev/claude/CLAUDE.md" in report.changes
    assert "update product.toml" in report.changes
    assert "update pins.toml" in report.changes  # the source stamp moved
    # The knowledge pin moved, so the referencing type WOULD re-tag.
    assert "claude-dev" in report.retagged
    assert any("product version would move" in note for note in report.notes)
    assert "would re-tag" in report.summary()
    # Nothing moved: HEAD, worktree, no marker — and no objects were written.
    assert _git(pinned, "rev-parse", "HEAD") == head
    assert _git(pinned, "status", "--porcelain", "--ignored") == ""
    assert not (pinned / ".git" / PENDING_MARKER).exists()
    assert _git(pinned, "count-objects", "-v") == objects_before
    assert "# team knowledge" in (pinned / "knowledge/dev/claude/CLAUDE.md").read_text()


def test_dry_run_previews_the_preserved_pin_not_a_delete(tmp_path):
    """The preview must tell the truth about preservation (ADR-0051): a
    pin-less source shows NO `delete product.toml`, carries the dry-run
    preserve note, and fires no would-move note — the preserved pin is not
    movement."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    product.write_pin(pinned, "0.5.0+gfeedface1234", log=lambda *_: None)
    (src / "product.toml").unlink()
    write(src, "knowledge/dev/AGENTS.md", "# updated knowledge\n")
    _commit_all(src, "drop the pin declaration, touch knowledge")
    head = _git(pinned, "rev-parse", "HEAD")

    report = ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert report.dry_run and report.changed
    assert not any("product.toml" in change for change in report.changes)
    assert any(
        "a real ingest preserves the pinned build's product pin 0.5.0+gfeedface1234" in note
        for note in report.notes
    )
    assert not any("product version would move" in note for note in report.notes)
    assert _git(pinned, "rev-parse", "HEAD") == head
    assert _git(pinned, "status", "--porcelain", "--ignored") == ""


def test_dry_run_of_an_up_to_date_build_reports_a_no_op(tmp_path):
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    report = ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert report.dry_run and not report.changed
    assert report.changes == []
    assert "no-op" in report.summary()


def test_dry_run_previews_a_dirty_source_working_tree(tmp_path):
    """The linter's home case: preview uncommitted Config Repo edits. The
    preview reflects the working tree under a folder content stamp and says a
    real ingest would refuse — and the real ingest still does."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    write(src, "control.toml", "[settings]\nheartbeat_seconds = 60\n")  # uncommitted

    report = ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert report.changed
    assert "update control.toml" in report.changes
    assert report.source_commit.startswith("folder-")
    assert any("WORKING TREE" in note for note in report.notes)
    assert any("control.toml would change" in note for note in report.notes)
    with pytest.raises(IngestError, match="commit them first"):
        ingest(str(src), pinned, log=lambda *_: None)


def test_dry_run_lints_with_the_real_refusals(tmp_path):
    """A broken Config Repo fails the dry run with the exact ingest refusal —
    that is the linter contract — and the pinned build stays untouched."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    head = _git(pinned, "rev-parse", "HEAD")
    write(
        src,
        "worker-types/broken.toml",
        'driver = "builtin:nope"\nbase = "x@sha256:' + "b" * 64 + '"\n',
    )
    _commit_all(src, "break it")
    with pytest.raises(IngestError, match="nothing committed"):
        ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert _git(pinned, "rev-parse", "HEAD") == head
    assert _git(pinned, "status", "--porcelain") == ""


def test_dry_run_against_a_missing_pinned_build_creates_nothing(tmp_path):
    """Previewing before the first real ingest: everything would be added,
    and the preview does not even create the pinned-build directory."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    report = ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert report.changed
    assert not pinned.exists()
    assert "add worker-types/claude-dev.toml" in report.changes
    assert "add knowledge/dev/claude/CLAUDE.md" in report.changes
    assert report.retagged["claude-dev"][0] == ""  # every type is (new)


def test_dry_run_reports_what_a_real_ingest_would_purge(tmp_path):
    """Ignored leftovers are REPORTED by the dry run, removed only by a real
    ingest."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    (pinned / ".git" / "info").mkdir(exist_ok=True)
    (pinned / ".git" / "info" / "exclude").write_text("leftover.toml\n")
    (pinned / "leftover.toml").write_text("stray\n")

    report = ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert any(
        "a real ingest will remove" in note and "leftover.toml" in note for note in report.notes
    )
    assert (pinned / "leftover.toml").exists()


def test_dry_run_notes_an_interrupted_ingest_and_previews_against_head(tmp_path):
    """A pending marker means the worktree may lag HEAD; the dry run neither
    repairs nor refuses — it notes the state and previews against HEAD."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    (pinned / ".git" / PENDING_MARKER).write_text("x\n")
    (pinned / "half-published.toml").write_text("ours\n")

    report = ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert any("interrupted ingest" in note for note in report.notes)
    assert not report.changed  # same source vs HEAD: a no-op
    assert (pinned / ".git" / PENDING_MARKER).exists()  # not repaired
    assert (pinned / "half-published.toml").exists()  # not touched


def _tree_fingerprint(root: Path) -> list[tuple[str, int, bytes]]:
    """Every path under ``root`` — the ``.git`` internals included — with
    mode and content: the byte-for-byte "the dry run touched nothing"
    witness."""
    entries = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink():
            entries.append((rel, path.stat().st_mode, path.read_bytes()))
        else:
            entries.append((rel, path.lstat().st_mode, b""))
    return entries


def test_pending_marker_dry_run_preserves_the_committed_pin_not_the_lagging_worktree(tmp_path):
    """The full interrupted lifecycle (ADR-0051): (1) an ingest of a DECLARED
    pin bump dies between update-ref and the worktree publish — HEAD carries
    the new pin, the worktree still the old one, the pending marker stays;
    (2) the source then stops declaring product.toml; (3) the dry run must
    read the COMMITTED pin (what the real ingest's repair-then-preserve
    lands), report no product.toml change or movement, and leave the pinned
    repo byte-for-byte untouched; (4) the real ingest repairs and converges
    on exactly that pin."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)  # the source declares 0.3.0
    write(src, "product.toml", '[product]\nversion = "0.6.0"\n')
    _commit_all(src, "declared pin bump")
    with pytest.raises(IngestError, match="git reset failed"):
        ingest(str(src), pinned, runner=_failing_git("reset"), log=lambda *_: None)
    # The split state the marker brackets: HEAD 0.6.0, worktree still 0.3.0.
    assert (pinned / ".git" / PENDING_MARKER).exists()
    assert 'version = "0.6.0"' in _git(pinned, "show", "HEAD:product.toml")
    assert 'version = "0.3.0"' in (pinned / "product.toml").read_text()

    (src / "product.toml").unlink()
    _commit_all(src, "stop declaring the pin")
    before = _tree_fingerprint(pinned)

    report = ingest(str(src), pinned, dry_run=True, log=lambda *_: None)
    assert report.dry_run
    assert any("interrupted ingest" in note for note in report.notes)
    # The preserved pin is HEAD's 0.6.0 — never the lagging worktree's 0.3.0
    # — so the preview shows NO product.toml change and no movement.
    assert any(
        "a real ingest preserves the pinned build's product pin 0.6.0" in note
        for note in report.notes
    )
    assert not any("product.toml" in change for change in report.changes)
    assert not any("product version would move" in note for note in report.notes)
    assert _tree_fingerprint(pinned) == before  # byte-for-byte untouched

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert any("recovered an interrupted ingest" in note for note in report.notes)
    assert any("preserved the pinned build's product pin 0.6.0" in note for note in report.notes)
    assert 'version = "0.6.0"' in (pinned / "product.toml").read_text()
    assert configrepo.load_config(pinned).product_version == "0.6.0"
    assert not (pinned / ".git" / PENDING_MARKER).exists()
    assert _git(pinned, "status", "--porcelain") == ""


def test_dry_run_holds_the_shared_writer_lock(tmp_path):
    """The preview reads one consistent build — and a refusal on contention
    is itself an honest preview (a real ingest would be refused too)."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    with (
        repolock.pinned_write_lock(pinned, writer="test holder"),
        pytest.raises(IngestError, match="already running"),
    ):
        ingest(str(src), pinned, dry_run=True, log=lambda *_: None)


@pytest.mark.parametrize("fail_on", ["commit-tree", "update-ref"])
def test_git_failure_before_the_publish_leaves_the_pinned_build_untouched(tmp_path, fail_on):
    """A git failure before the working-tree publish (creating the commit
    object, or moving the ref) aborts with the pinned build byte-for-byte at
    its previous state: old HEAD, clean tree, old content, no pending marker
    — and the next ingest succeeds with correct provenance."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    first = ingest(str(src), pinned, log=lambda *_: None)
    head_before = _git(pinned, "rev-parse", "HEAD")
    write(src, "knowledge/dev/AGENTS.md", "# team knowledge v2\n")
    edited = _commit_all(src, "edit")

    with pytest.raises(IngestError, match=f"git {fail_on} failed"):
        ingest(str(src), pinned, runner=_failing_git(fail_on), log=lambda *_: None)
    assert _git(pinned, "rev-parse", "HEAD") == head_before
    assert _git(pinned, "status", "--porcelain") == ""
    assert not (pinned / ".git" / PENDING_MARKER).exists()
    assert first.source_commit in (pinned / "pins.toml").read_text()
    assert "v2" not in (pinned / "knowledge/dev/claude/CLAUDE.md").read_text()

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert report.changed and report.source_commit == edited
    assert edited in _git(pinned, "log", "-1", "--format=%s")
    assert edited in (pinned / "pins.toml").read_text()
    assert "v2" in (pinned / "knowledge/dev/claude/CLAUDE.md").read_text()


def test_interrupted_publish_is_recovered_by_the_next_ingest(tmp_path):
    """An interruption AFTER the ref moves but before the working tree syncs
    (the only window a crash can leave a stale tree) leaves the pending
    marker; the next ingest repairs the tree to HEAD instead of refusing it
    as a hand edit, and provenance — commit message, pins.toml, content —
    matches the committed transaction exactly."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    head_before = _git(pinned, "rev-parse", "HEAD")
    write(src, "knowledge/dev/AGENTS.md", "# team knowledge v2\n")
    edited = _commit_all(src, "edit")

    with pytest.raises(IngestError, match="git reset failed"):
        ingest(str(src), pinned, runner=_failing_git("reset"), log=lambda *_: None)
    # Commit-first: the ref moved, the publish did not — the marker brackets it.
    assert (pinned / ".git" / PENDING_MARKER).exists()
    assert _git(pinned, "rev-parse", "HEAD") != head_before
    assert "v2" not in (pinned / "knowledge/dev/claude/CLAUDE.md").read_text()

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert any("recovered an interrupted ingest" in note for note in report.notes)
    # The commit already landed; recovery published it, so this run is a no-op.
    assert not report.changed
    assert not (pinned / ".git" / PENDING_MARKER).exists()
    assert _git(pinned, "status", "--porcelain") == ""
    assert edited in _git(pinned, "log", "-1", "--format=%s")
    assert edited in (pinned / "pins.toml").read_text()
    assert "v2" in (pinned / "knowledge/dev/claude/CLAUDE.md").read_text()
    assert configrepo.load_config(pinned).worker_types["claude-dev"].knowledge_pin


def test_resolve_image_digest_ref_parsing():
    """The ref grammar behind live resolution (the network path itself is
    exercised only against an injected resolver)."""
    from theozolith_control.ingest import _split_image_ref

    assert _split_image_ref("ghcr.io/acme/run:1.2") == ("ghcr.io", "acme/run", "1.2")
    assert _split_image_ref("localhost:5000/run:1") == ("localhost:5000", "run", "1")
    assert _split_image_ref("ubuntu:24.04") == ("registry-1.docker.io", "library/ubuntu", "24.04")
    assert _split_image_ref("acme/run:1") == ("registry-1.docker.io", "acme/run", "1")
    with pytest.raises(IngestError, match="already carries a digest"):
        _split_image_ref(f"ghcr.io/acme/run@sha256:{'a' * 64}")
    assert callable(resolve_image_digest)


# -- authenticated base resolution (ADR-0049) -----------------------------------

import base64  # noqa: E402
import email.message  # noqa: E402
import urllib.error  # noqa: E402

from theozolith_control import ingest as ingest_mod  # noqa: E402

_TOKEN_REALM = "https://auth.example/token"


def _challenge() -> email.message.Message:
    hdrs = email.message.Message()
    hdrs["WWW-Authenticate"] = (
        f'Bearer realm="{_TOKEN_REALM}",service="registry.example",scope="repository:acme/run:pull"'
    )
    return hdrs


class _FakeResp:
    """A context-manager response over ingest._urlopen: dict headers for the
    manifest HEAD, a JSON body for the token realm."""

    def __init__(self, *, headers=None, body: bytes = b""):
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class FakeRegistry:
    """A scripted registry over ingest._urlopen. The manifest HEAD 401s with a
    bearer challenge until a token is presented; the realm mints a token
    (recording whether HTTP Basic was sent). A PRIVATE base 403s the authorized
    HEAD when the token was minted anonymously (GHCR's failure mode) or when the
    credential lacks pull scope (accept_credential=False). A realm that REJECTS
    the request outright (a registry account rejecting Basic at token-mint
    time) is scripted with realm_status; a broken realm with realm_body."""

    def __init__(
        self,
        *,
        private: bool = False,
        accept_credential: bool = True,
        digest=None,
        realm_status: int = 0,
        realm_body: bytes = b'{"token": "T"}',
    ):
        self.private = private
        self.accept_credential = accept_credential
        self.digest = digest or ("sha256:" + "c" * 64)
        self.realm_status = realm_status
        self.realm_body = realm_body
        self.calls: list[tuple[str, str]] = []
        self.token_auth = None
        self.minted_authenticated = False

    def __call__(self, request, timeout=None):
        url = request.full_url
        method = request.get_method()
        self.calls.append((method, url))
        if url.startswith(_TOKEN_REALM):
            self.token_auth = request.get_header("Authorization")
            self.minted_authenticated = self.token_auth is not None
            if self.realm_status:
                raise urllib.error.HTTPError(
                    url, self.realm_status, "Denied", email.message.Message(), None
                )
            return _FakeResp(body=self.realm_body)
        # manifest HEAD
        if request.get_header("Authorization") is None:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", _challenge(), None)
        if self.private and (not self.minted_authenticated or not self.accept_credential):
            raise urllib.error.HTTPError(url, 403, "Forbidden", email.message.Message(), None)
        return _FakeResp(headers={"Docker-Content-Digest": self.digest})


def test_public_base_resolves_anonymously_over_the_url_seam(monkeypatch):
    """Regression: the anonymous 401 -> token -> digest fast path public bases
    keep, exercised through the _urlopen seam."""
    reg = FakeRegistry(private=False)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    assert ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2") == reg.digest
    assert reg.token_auth is None  # anonymous token, no Basic header
    assert [m for m, _ in reg.calls] == ["HEAD", "GET", "HEAD"]


def test_private_base_resolves_with_a_registry_credential(monkeypatch):
    """The realm request carries the stored credential as HTTP Basic, so GHCR
    mints a token that can pull the private manifest."""
    reg = FakeRegistry(private=True)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    digest = ingest_mod.resolve_image_digest(
        "ghcr.io/acme/run:1.2", {"ghcr.io": "octocat:ghp_token"}
    )
    assert digest == reg.digest
    assert reg.token_auth == "Basic " + base64.b64encode(b"octocat:ghp_token").decode()


def test_a_200_on_the_first_head_skips_the_token_round_trip(monkeypatch):
    """A registry that serves the manifest with no challenge never hits the
    token realm — attempt 1 is authoritative."""

    class OpenRegistry:
        def __init__(self):
            self.calls: list[str] = []

        def __call__(self, request, timeout=None):
            self.calls.append(request.get_method())
            return _FakeResp(headers={"Docker-Content-Digest": "sha256:" + "d" * 64})

    reg = OpenRegistry()
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    assert ingest_mod.resolve_image_digest("registry.example/acme/run:1") == "sha256:" + "d" * 64
    assert reg.calls == ["HEAD"]


def test_private_base_without_a_credential_names_the_secret_set_command(monkeypatch):
    """The actionable message: the exact host, the exact `secret set` command,
    read:packages, and the digest-pin escape hatch."""
    reg = FakeRegistry(private=True)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    with pytest.raises(IngestError) as exc:
        ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2")
    msg = str(exc.value)
    assert "theozolith secret set registry:ghcr.io" in msg
    assert "read:packages" in msg
    assert "pin the base by digest" in msg
    # No third manifest attempt after the 403.
    assert [m for m, _ in reg.calls] == ["HEAD", "GET", "HEAD"]


def test_a_refused_credential_says_the_credential_was_refused(monkeypatch):
    """A credential that authenticates but lacks pull scope yields a distinct
    message that points at the stored value, not `secret set`."""
    reg = FakeRegistry(private=True, accept_credential=False)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    with pytest.raises(IngestError, match="credential was refused"):
        ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2", {"ghcr.io": "octocat:bad"})
    assert reg.minted_authenticated  # the credential WAS sent to the realm


def test_a_realm_that_rejects_basic_auth_is_an_actionable_ingest_error(monkeypatch):
    """The token realm itself 401s the Basic credential (rejection at
    token-mint time, before any second manifest attempt): an IngestError
    naming the host and the refused credential — never a raw HTTPError, and
    never the Authorization header or credential material."""
    reg = FakeRegistry(private=True, realm_status=401)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    with pytest.raises(IngestError) as exc:
        ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2", {"ghcr.io": "octocat:bad"})
    msg = str(exc.value)
    assert "registry:ghcr.io" in msg
    assert "credential was refused" in msg
    assert "Basic " not in msg
    assert base64.b64encode(b"octocat:bad").decode() not in msg
    # The realm rejection is terminal — no second manifest attempt.
    assert [m for m, _ in reg.calls] == ["HEAD", "GET"]


def test_an_anonymous_realm_rejection_names_the_secret_set_command(monkeypatch):
    """A realm that refuses even the anonymous token request (some registries
    401 the mint itself for private repos) points at `secret set` and the
    digest-pin escape, same as the manifest-403 path."""
    reg = FakeRegistry(private=True, realm_status=401)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    with pytest.raises(IngestError) as exc:
        ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2")
    msg = str(exc.value)
    assert "theozolith secret set registry:ghcr.io" in msg
    assert "pin the base by digest" in msg
    assert reg.token_auth is None  # the request really was anonymous


def test_a_malformed_realm_response_is_an_actionable_ingest_error(monkeypatch):
    """A realm answering 200 with a non-JSON body (an HTML error page) stays
    inside the IngestError contract instead of leaking a JSONDecodeError."""
    reg = FakeRegistry(realm_body=b"<html>oops</html>")
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    with pytest.raises(IngestError, match="malformed response"):
        ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2")


@pytest.mark.parametrize("body", [b"[]", b"null", b'"tok"', b"7"])
def test_realm_json_that_is_not_an_object_is_an_actionable_ingest_error(monkeypatch, body):
    """Valid JSON that is not an object (an array, null, or a scalar) is the
    same malformed-realm story — never an AttributeError escaping the
    IngestError contract."""
    reg = FakeRegistry(realm_body=body)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    with pytest.raises(IngestError, match="malformed response"):
        ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2")


@pytest.mark.parametrize(
    "body", [b'{"token": 123}', b'{"token": {"v": "x"}}', b'{"token": ""}', b"{}"]
)
def test_a_realm_token_that_is_not_a_nonempty_string_is_rejected(monkeypatch, body):
    """The minted token must be a non-empty string before it rides a Bearer
    header: numbers, nested objects, empty strings, and absent keys all get
    the actionable no-token message, never a TypeError downstream."""
    reg = FakeRegistry(realm_body=body)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    with pytest.raises(IngestError, match="returned no token"):
        ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2")


def test_the_access_token_alias_remains_accepted(monkeypatch):
    """Registries that answer with `access_token` instead of `token` (the
    OAuth2 spelling) keep working."""
    reg = FakeRegistry(realm_body=b'{"access_token": "T"}')
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    assert ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2") == reg.digest


def test_a_malformed_credential_is_rejected_loudly(monkeypatch):
    """A stored credential missing the `<user>:<token>` colon fails loud at the
    token step, naming the contract."""
    reg = FakeRegistry(private=True)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    with pytest.raises(IngestError, match="<user>:<token>"):
        ingest_mod.resolve_image_digest("ghcr.io/acme/run:1.2", {"ghcr.io": "no-colon"})


def test_ingest_resolves_a_private_tag_only_base_with_a_credential(tmp_path, monkeypatch):
    """End to end: `ingest(registry_credentials=...)` drives the default
    resolver to pin a private tag-only base."""
    src = source_repo(tmp_path, knowledge=False)
    write(
        src,
        "worker-types/claude-dev.toml",
        'driver = "builtin:implementer"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{TAG_ONLY_BASE}"\n',
    )
    _commit_all(src, "tag only")
    reg = FakeRegistry(private=True)
    monkeypatch.setattr(ingest_mod, "_urlopen", reg)
    pinned = pinned_dir(tmp_path)
    report = ingest(
        str(src),
        pinned,
        registry_credentials={"ghcr.io": "octocat:tok"},
        log=lambda *_: None,
    )
    assert report.resolved_bases == {TAG_ONLY_BASE: reg.digest}
    assert reg.minted_authenticated
    wt = configrepo.load_config(pinned).worker_types["claude-dev"]
    assert wt.base == f"{TAG_ONLY_BASE}@{reg.digest}"


def test_an_injected_resolver_ignores_registry_credentials(tmp_path, monkeypatch):
    """The injected resolver seam wins — registry_credentials never reaches the
    network path, so every existing fake keeps working."""
    src = source_repo(tmp_path, knowledge=False)
    write(
        src,
        "worker-types/claude-dev.toml",
        'driver = "builtin:implementer"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{TAG_ONLY_BASE}"\n',
    )
    _commit_all(src, "tag only")

    def boom(*a, **k):
        raise AssertionError("_urlopen must not be called when a resolver is injected")

    monkeypatch.setattr(ingest_mod, "_urlopen", boom)
    seen: list[str] = []

    def resolver(ref: str) -> str:
        seen.append(ref)
        return "sha256:" + "e" * 64

    report = ingest(
        str(src),
        pinned_dir(tmp_path),
        resolve_digest=resolver,
        registry_credentials={"ghcr.io": "octocat:tok"},
        log=lambda *_: None,
    )
    assert seen == [TAG_ONLY_BASE]
    assert report.resolved_bases == {TAG_ONLY_BASE: "sha256:" + "e" * 64}


# -- CLI Pin resolution (ADR-0055) ----------------------------------------------

import json  # noqa: E402
import urllib.parse  # noqa: E402

from theozolith_control.ingest import resolve_cli_pin  # noqa: E402
from theozolith_worker.adapters import (  # noqa: E402
    ClaudeAdapter,
    CodexAdapter,
    make_agent_adapter,
)

_SRI = "sha512-" + base64.b64encode(b"x" * 64).decode()
CODEX_BASE = f"ghcr.io/snowfoxbuilds/theozolith-run-codex:main@sha256:{DIGEST}"


class FakeNpm:
    """A scripted npm registry over the shared ingest._urlopen seam: the
    wrapper package's version document (exact versions echo, dist-tags map
    through ``tags``) and one document per platform coordinate. ``missing``
    coordinates 404; ``bad_integrity`` overrides per coordinate; ``raw``
    replaces every body verbatim (the malformed-response shapes). A
    coordinate is a platform PACKAGE (claude publishes distinct platform
    packages) or a suffixed platform SELECTOR ``<version>-linux-<arch>``
    (codex publishes one static tarball per architecture as a version of the
    wrapper itself) — both are accepted so codex and claude tests key the
    same way."""

    def __init__(
        self,
        *,
        wrapper=ClaudeAdapter.CLI_WRAPPER_PACKAGE,
        tags=None,
        missing=(),
        bad_integrity=None,
        raw=None,
    ):
        self.wrapper = wrapper
        self.tags = dict(tags or {})
        self.missing = set(missing)
        self.bad_integrity = dict(bad_integrity or {})
        self.raw = raw
        self.calls: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        path = url.removeprefix("https://registry.npmjs.org/")
        package_enc, _, selector = path.rpartition("/")
        package = urllib.parse.unquote(package_enc)
        if self.raw is not None:
            return _FakeResp(body=self.raw)
        # The wrapper VERSION document is the wrapper package at a bare
        # version/dist-tag; a codex platform document is the SAME package at
        # "<version>-linux-<arch>", told apart by the platform suffix.
        if package == self.wrapper and "-linux-" not in selector:
            declared = urllib.parse.unquote(selector)
            version = self.tags.get(declared, declared)
            return _FakeResp(body=json.dumps({"version": version}).encode())
        if package in self.missing or selector in self.missing:
            raise urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None)
        coord = selector if selector in self.bad_integrity else package
        integrity = self.bad_integrity.get(coord, _SRI)
        dist = {} if integrity is None else {"integrity": integrity}
        return _FakeResp(body=json.dumps({"version": selector, "dist": dist}).encode())


def test_cli_pin_exact_version_resolves_every_supported_tuple(monkeypatch):
    npm = FakeNpm()
    monkeypatch.setattr(ingest_mod, "_urlopen", npm)
    pin = resolve_cli_pin("2.1.260")
    assert pin["version"] == "2.1.260"
    assert pin["platforms"] == {
        key: {"package": package, "integrity": _SRI}
        for key, package in ClaudeAdapter.CLI_PLATFORM_PACKAGES.items()
    }
    # The wrapper document plus one document per supported tuple, scoped
    # names URL-encoded whole (quote with safe='').
    assert npm.calls[0].startswith("https://registry.npmjs.org/%40anthropic-ai%2Fclaude-code/")
    assert len(npm.calls) == 1 + len(ClaudeAdapter.CLI_PLATFORM_PACKAGES)


def test_cli_pin_dist_tag_resolves_and_reresolves(monkeypatch):
    """A dist-tag re-resolves on every resolution, like a moving base tag."""
    npm = FakeNpm(tags={"latest": "2.1.260"})
    monkeypatch.setattr(ingest_mod, "_urlopen", npm)
    assert resolve_cli_pin("latest")["version"] == "2.1.260"
    npm.tags["latest"] = "2.1.261"
    assert resolve_cli_pin("latest")["version"] == "2.1.261"


def test_cli_pin_unsuppliable_tuple_fails_naming_it(monkeypatch):
    """A supported tuple the registry cannot supply fails the whole ingest
    (ADR-0055) — a 404, a missing integrity, and a non-sha512 algorithm all
    name the tuple key and package."""
    package = ClaudeAdapter.CLI_PLATFORM_PACKAGES["linux-arm64-musl"]
    for npm in (
        FakeNpm(missing={package}),
        FakeNpm(bad_integrity={package: None}),
        FakeNpm(bad_integrity={package: "sha256-" + "a" * 32}),
    ):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ingest_mod, "_urlopen", npm)
            with pytest.raises(IngestError, match=r"linux-arm64-musl"):
                resolve_cli_pin("2.1.260")


def test_cli_pin_below_the_floor_fails(monkeypatch):
    monkeypatch.setattr(ingest_mod, "_urlopen", FakeNpm(tags={"old": "2.0.0"}))
    with pytest.raises(IngestError, match="below the claude adapter's enforcement floor"):
        resolve_cli_pin("old")


def test_cli_pin_malformed_registry_answers_fail_actionably(monkeypatch):
    monkeypatch.setattr(ingest_mod, "_urlopen", FakeNpm(raw=b"not json"))
    with pytest.raises(IngestError, match="malformed response"):
        resolve_cli_pin("2.1.260")
    monkeypatch.setattr(ingest_mod, "_urlopen", FakeNpm(tags={"latest": "not-semver"}))
    with pytest.raises(IngestError, match="not an exact"):
        resolve_cli_pin("latest")


def _codex_npm(**kwargs) -> FakeNpm:
    return FakeNpm(wrapper=CodexAdapter.CLI_WRAPPER_PACKAGE, **kwargs)


def test_codex_cli_pin_exact_version_resolves_every_supported_tuple(monkeypatch):
    """codex resolves the wrapper version once, then one suffixed platform
    document per tuple (<version>-linux-<arch>); every tuple's package is the
    wrapper itself (ADR-0055 D3)."""
    npm = _codex_npm()
    monkeypatch.setattr(ingest_mod, "_urlopen", npm)
    pin = resolve_cli_pin("0.153.3", tool="codex")
    assert pin["version"] == "0.153.3"
    assert pin["platforms"] == {
        key: {"package": "@openai/codex", "integrity": _SRI}
        for key in CodexAdapter.CLI_PLATFORM_PACKAGES
    }
    assert npm.calls[0] == "https://registry.npmjs.org/%40openai%2Fcodex/0.153.3"
    # x64 tuples resolve at -linux-x64, arm64 tuples at -linux-arm64.
    assert any(c.endswith("/0.153.3-linux-x64") for c in npm.calls)
    assert any(c.endswith("/0.153.3-linux-arm64") for c in npm.calls)
    assert len(npm.calls) == 1 + len(CodexAdapter.CLI_PLATFORM_PACKAGES)


def test_codex_cli_pin_dist_tag_resolves_and_reresolves(monkeypatch):
    npm = _codex_npm(tags={"latest": "0.153.3"})
    monkeypatch.setattr(ingest_mod, "_urlopen", npm)
    assert resolve_cli_pin("latest", tool="codex")["version"] == "0.153.3"
    npm.tags["latest"] = "0.153.4"
    assert resolve_cli_pin("latest", tool="codex")["version"] == "0.153.4"


def test_codex_cli_pin_unsuppliable_tuple_fails_naming_it(monkeypatch):
    """A suffixed platform document the registry cannot supply fails the
    ingest naming the first tuple that addresses it (ADR-0055)."""
    for npm in (
        _codex_npm(missing={"0.153.3-linux-arm64"}),
        _codex_npm(bad_integrity={"0.153.3-linux-arm64": None}),
        _codex_npm(bad_integrity={"0.153.3-linux-arm64": "sha256-" + "a" * 32}),
    ):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ingest_mod, "_urlopen", npm)
            with pytest.raises(IngestError, match=r"linux-arm64-glibc"):
                resolve_cli_pin("0.153.3", tool="codex")


def test_codex_cli_pin_below_the_floor_fails(monkeypatch):
    monkeypatch.setattr(ingest_mod, "_urlopen", _codex_npm(tags={"old": "0.149.0"}))
    with pytest.raises(IngestError, match="below the codex adapter's enforcement floor"):
        resolve_cli_pin("old", tool="codex")


def test_codex_cli_pin_malformed_registry_answers_fail_actionably(monkeypatch):
    monkeypatch.setattr(ingest_mod, "_urlopen", _codex_npm(raw=b"not json"))
    with pytest.raises(IngestError, match="malformed response"):
        resolve_cli_pin("0.153.3", tool="codex")
    monkeypatch.setattr(ingest_mod, "_urlopen", _codex_npm(tags={"latest": "not-semver"}))
    with pytest.raises(IngestError, match="not an exact"):
        resolve_cli_pin("latest", tool="codex")


def _deck_source(tmp_path, declared: str = "latest") -> Path:
    """source_repo plus a driverless claude deck declaring a CLI Pin."""
    src = source_repo(tmp_path)
    write(
        src,
        "worker-types/deck.toml",
        f'base = "{BASE}"\ncommand = "flightdeck-start"\ncli = "{declared}"\n',
    )
    _commit_all(src, "deck with cli pin")
    return src


def _fake_cli_resolver(version_cell: list[str]):
    def resolve_cli(declared: str, *, tool: str = "claude") -> dict:
        adapter = make_agent_adapter(tool)
        return {
            "version": version_cell[0],
            "platforms": {
                key: {"package": package, "integrity": _SRI}
                for key, package in adapter.CLI_PLATFORM_PACKAGES.items()
            },
        }

    return resolve_cli


def test_ingest_writes_the_cli_pins_and_the_result_loads(tmp_path):
    """The injected resolve_cli seam round-trips: pins.toml carries the
    documented [cli] shape byte-for-byte, the report and summary name the
    pin, and the loaded config joins version + platform map onto the type."""
    src = _deck_source(tmp_path)
    pinned = pinned_dir(tmp_path)
    report = ingest(
        str(src), pinned, resolve_cli=_fake_cli_resolver(["2.1.260"]), log=lambda *_: None
    )
    assert report.cli_pins["claude/latest"]["version"] == "2.1.260"
    assert "cli claude/latest resolved 2.1.260 (4 platform(s))" in report.summary()
    pins_text = (pinned / "pins.toml").read_text()
    entries = "".join(
        f'"{key}" = {{ package = "{ClaudeAdapter.CLI_PLATFORM_PACKAGES[key]}",'
        f' integrity = "{_SRI}" }}\n'
        for key in sorted(ClaudeAdapter.CLI_PLATFORM_PACKAGES)
    )
    golden = (
        '[cli."claude/latest"]\nversion = "2.1.260"\n\n[cli."claude/latest".platforms]\n' + entries
    )
    assert golden in pins_text
    wt = configrepo.load_config(pinned).worker_types["deck"]
    assert wt.cli == "latest" and wt.cli_version == "2.1.260"
    assert set(wt.cli_platforms) == set(ClaudeAdapter.CLI_PLATFORM_PACKAGES)
    recipe = wt.recipe_wire()
    assert recipe["cli_tool"] == "claude" and recipe["cli_version"] == "2.1.260"


def test_reingest_with_a_moved_dist_tag_reresolves(tmp_path):
    src = _deck_source(tmp_path)
    pinned = pinned_dir(tmp_path)
    cell = ["2.1.260"]
    ingest(str(src), pinned, resolve_cli=_fake_cli_resolver(cell), log=lambda *_: None)
    cell[0] = "2.1.261"
    report = ingest(str(src), pinned, resolve_cli=_fake_cli_resolver(cell), log=lambda *_: None)
    assert report.changed  # the moved pin alone recommits the pinned build
    assert 'version = "2.1.261"' in (pinned / "pins.toml").read_text()
    assert configrepo.load_config(pinned).worker_types["deck"].cli_version == "2.1.261"


def test_ingest_writes_a_codex_cli_pin_end_to_end(tmp_path):
    """A driverless codex deck declaring cli resolves against the codex table
    (keyed codex/<declared>): pins.toml carries [cli."codex/0.153.3"] with the
    four-tuple platform map (every package the wrapper), and the pinned build
    loads with the pin joined onto the codex type carrying cli_tool == 'codex'."""
    src = source_repo(tmp_path)
    write(
        src,
        "worker-types/codexdeck.toml",
        f'base = "{CODEX_BASE}"\ncommand = "flightdeck-start"\n'
        'adapter = "codex"\ncli = "0.153.3"\n',
    )
    _commit_all(src, "codex deck with cli pin")
    pinned = pinned_dir(tmp_path)
    report = ingest(
        str(src), pinned, resolve_cli=_fake_cli_resolver(["0.153.3"]), log=lambda *_: None
    )
    assert report.cli_pins["codex/0.153.3"]["version"] == "0.153.3"
    pins_text = (pinned / "pins.toml").read_text()
    assert '[cli."codex/0.153.3"]' in pins_text
    for key in CodexAdapter.CLI_PLATFORM_PACKAGES:
        assert f'"{key}" = {{ package = "@openai/codex", integrity = "{_SRI}" }}' in pins_text
    wt = configrepo.load_config(pinned).worker_types["codexdeck"]
    assert wt.cli == "0.153.3" and wt.cli_version == "0.153.3"
    assert set(wt.cli_platforms) == set(CodexAdapter.CLI_PLATFORM_PACKAGES)
    assert wt.recipe_wire()["cli_tool"] == "codex"


def test_cli_on_a_driver_type_is_skipped_at_resolution_and_refused_by_the_lint(tmp_path):
    """Ingest deliberately resolves nothing for a driver type's cli — the
    staged config load refuses it with the precise driverless-only message,
    and nothing commits."""
    src = source_repo(tmp_path)
    write(
        src,
        "worker-types/claude-dev.toml",
        f'driver = "builtin:implementer"\nadapter = "claude"\nmodel = "claude-sonnet-5"\n'
        f'workspace = "acme/sandbox"\nbase = "{BASE}"\ncli = "2.1.260"\n'
        'knowledge = "knowledge/dev"\n',
    )
    _commit_all(src, "driver with cli")
    calls: list[str] = []

    def resolve_cli(declared: str) -> dict:
        calls.append(declared)
        return {"version": declared, "platforms": {}}

    with pytest.raises(IngestError, match="driverless-only in v1"):
        ingest(str(src), pinned_dir(tmp_path), resolve_cli=resolve_cli, log=lambda *_: None)
    assert calls == []  # skipped at resolution: the lint owns the refusal
