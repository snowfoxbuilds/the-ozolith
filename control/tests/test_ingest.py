"""`theozolith config ingest` (ADR-0048): harvest -> lint -> pin -> compile ->
commit -> reload. The acceptance criteria from #62: refuse a dirty pinned
tree, refuse live placeholder checksums, lint exactly what config load lints
BEFORE committing, stamp source provenance, and never leave a partial state."""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path

import pytest
from theozolith_control import configdist, configrepo, controltoml
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


def test_concurrent_ingest_is_refused_by_the_repository_lock(tmp_path):
    """The whole transaction holds an exclusive flock beside the pinned build:
    a second ingest is refused loudly while one runs, and proceeds normally
    once the lock is released — serialized, never interleaved."""
    src = source_repo(tmp_path)
    pinned = pinned_dir(tmp_path)
    ingest(str(src), pinned, log=lambda *_: None)
    lock_path = pinned.parent / f".{pinned.name}.ingest-lock"
    with open(lock_path, "w") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(IngestError, match="already running"):
            ingest(str(src), pinned, log=lambda *_: None)
        # The refused attempt changed nothing.
        assert _git(pinned, "status", "--porcelain") == ""
    report = ingest(str(src), pinned, log=lambda *_: None)
    assert not report.changed


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
