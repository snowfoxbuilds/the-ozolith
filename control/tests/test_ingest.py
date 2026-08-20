"""`theozolith config ingest` (ADR-0048): harvest -> lint -> pin -> compile ->
commit -> reload. The acceptance criteria from #62: refuse a dirty pinned
tree, refuse live placeholder checksums, lint exactly what config load lints
BEFORE committing, stamp source provenance, and never leave a partial state."""

from __future__ import annotations

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


def source_repo(tmp_path: Path, *, git: bool = True, knowledge: bool = True) -> Path:
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
    # The knowledge tree is COMPILED output (AGENTS.md became CLAUDE.md).
    assert (pinned / "knowledge/dev/CLAUDE.md").is_file()
    assert (pinned / "knowledge/dev/skills/hello/SKILL.md").is_file()
    assert not (pinned / "knowledge/dev/AGENTS.md").exists()
    assert report.knowledge_pins == {"dev": configdist.knowledge_tree_hash(pinned, "dev")}
    # The pinned build loads under the real validator, pin joined.
    config = configrepo.load_config(pinned)
    assert config.worker_types["claude-dev"].knowledge_pin == report.knowledge_pins["dev"]
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
    assert after.knowledge_pins["dev"] != before.knowledge_pins["dev"]
    assert set(after.retagged) == {"claude-dev"}
    assert configrepo.load_config(pinned).worker_types["claude-dev"].tag != before_tag
    assert configdist.dist_hash(pinned) != before_dist
    assert (pinned / "knowledge/dev/skills/hello/run.sh").stat().st_mode & 0o111


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
    with pytest.raises(IngestError, match="does not compile"):
        ingest(str(src), pinned_dir(tmp_path), log=lambda *_: None)


def test_source_symlink_is_refused(tmp_path):
    src = source_repo(tmp_path, git=False)
    (src / "worker-types" / "link.toml").symlink_to(src / "worker-types" / "claude-dev.toml")
    with pytest.raises(IngestError, match="symlink"):
        ingest(str(src), pinned_dir(tmp_path), log=lambda *_: None)


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
    assert node_configdist.knowledge_tree_hash(dest, "dev") == configdist.knowledge_tree_hash(
        pinned, "dev"
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
    assert (pinned / "knowledge/dev/skills/hello/run.sh").stat().st_mode & 0o111

    digest, path = configdist.build_artifact(pinned, tmp_path / "out", built_against="0.3.0")
    dest = tmp_path / "node-tree"
    dest.mkdir()
    node_configdist.extract_zip(path.read_bytes(), dest)
    assert (dest / "knowledge/dev/skills/hello/run.sh").stat().st_mode & 0o111
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
    assert "v2" in (pinned / "knowledge/dev/CLAUDE.md").read_text()


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
    assert "update knowledge/dev/CLAUDE.md" in report.changes
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
    assert "# team knowledge" in (pinned / "knowledge/dev/CLAUDE.md").read_text()


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
    assert "add knowledge/dev/CLAUDE.md" in report.changes
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
    assert "v2" not in (pinned / "knowledge/dev/CLAUDE.md").read_text()

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert report.changed and report.source_commit == edited
    assert edited in _git(pinned, "log", "-1", "--format=%s")
    assert edited in (pinned / "pins.toml").read_text()
    assert "v2" in (pinned / "knowledge/dev/CLAUDE.md").read_text()


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
    assert "v2" not in (pinned / "knowledge/dev/CLAUDE.md").read_text()

    report = ingest(str(src), pinned, log=lambda *_: None)
    assert any("recovered an interrupted ingest" in note for note in report.notes)
    # The commit already landed; recovery published it, so this run is a no-op.
    assert not report.changed
    assert not (pinned / ".git" / PENDING_MARKER).exists()
    assert _git(pinned, "status", "--porcelain") == ""
    assert edited in _git(pinned, "log", "-1", "--format=%s")
    assert edited in (pinned / "pins.toml").read_text()
    assert "v2" in (pinned / "knowledge/dev/CLAUDE.md").read_text()
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
    credential lacks pull scope (accept_credential=False)."""

    def __init__(self, *, private: bool = False, accept_credential: bool = True, digest=None):
        self.private = private
        self.accept_credential = accept_credential
        self.digest = digest or ("sha256:" + "c" * 64)
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
            return _FakeResp(body=b'{"token": "T"}')
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
