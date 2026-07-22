"""The two update paths' machinery (ADR-0015 amendment 2026-07-22): release
resolution, the committed pin, the source build's SHA-stamped version, and
the wheel build sandbox."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from theozolith_control import product


def _git(args: list[str], cwd: Path) -> str:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    proc = subprocess.run(
        ["git", *identity, *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


# -- release resolution (user path) ----------------------------------------------


def test_resolve_latest_release_reads_the_index():
    def fake_get(url: str) -> bytes:
        assert url == product.RELEASE_INDEX_URL
        return json.dumps({"info": {"version": "0.4.2"}}).encode()

    assert product.resolve_latest_release(fake_get) == "0.4.2"


def test_resolve_latest_release_honors_the_index_override():
    seen = []

    def fake_get(url: str) -> bytes:
        seen.append(url)
        return json.dumps({"info": {"version": "9.9.9"}}).encode()

    version = product.resolve_latest_release(
        fake_get, environ={"THEOZOLITH_RELEASE_INDEX_URL": "https://mirror.internal/x/json"}
    )
    assert version == "9.9.9"
    assert seen == ["https://mirror.internal/x/json"]


def test_resolve_latest_release_failures_are_clean_errors():
    def broken(url: str) -> bytes:
        raise OSError("index unreachable")

    with pytest.raises(product.ProductError, match="cannot resolve"):
        product.resolve_latest_release(broken)
    with pytest.raises(product.ProductError, match="no version"):
        product.resolve_latest_release(lambda url: b"{}")


# -- the pin (product.toml, committed) --------------------------------------------


def test_write_pin_commits_to_a_git_backed_config_repo(tmp_path):
    repo = tmp_path / "configs"
    repo.mkdir()
    _git(["init", "--quiet"], repo)
    (repo / "README").write_text("x")
    _git(["add", "-A"], repo)
    _git(["commit", "--quiet", "-m", "seed"], repo)

    product.write_pin(repo, "0.4.0", log=lambda *_: None)

    assert product.read_pin(repo) == "0.4.0"
    subject = _git(["log", "-1", "--format=%s"], repo)
    assert "pin product version 0.4.0" in subject
    assert _git(["status", "--porcelain"], repo) == ""  # committed, not loose

    # Re-pinning the same version is a no-op commit-wise (idempotent).
    head = _git(["rev-parse", "HEAD"], repo)
    product.write_pin(repo, "0.4.0", log=lambda *_: None)
    assert _git(["rev-parse", "HEAD"], repo) == head

    # Rollback = re-pinning a previous version with the same machinery.
    product.write_pin(repo, "0.3.0", log=lambda *_: None)
    assert product.read_pin(repo) == "0.3.0"


def test_write_pin_works_in_folder_mode_and_refuses_empty(tmp_path):
    repo = tmp_path / "configs"  # no .git: the file itself is the record
    product.write_pin(repo, "0.4.0", log=lambda *_: None)
    assert product.read_pin(repo) == "0.4.0"
    with pytest.raises(product.ProductError, match="unrecorded"):
        product.write_pin(repo, "")


def test_ensure_pin_resolves_and_writes_only_when_absent(tmp_path):
    repo = tmp_path / "configs"
    calls = []

    def fake_get(url: str) -> bytes:
        calls.append(url)
        return json.dumps({"info": {"version": "0.5.0"}}).encode()

    # Fresh install, no pin: resolve the latest release and write it.
    assert product.ensure_pin(repo, http_get=fake_get, log=lambda *_: None) == "0.5.0"
    assert product.read_pin(repo) == "0.5.0"
    # An existing pin is authoritative: no resolution happens at all.
    assert product.ensure_pin(repo, http_get=fake_get, log=lambda *_: None) == "0.5.0"
    assert len(calls) == 1


# -- the source build (developer path) ---------------------------------------------


def _checkout(tmp_path: Path) -> Path:
    source = tmp_path / "src"
    for component in product.COMPONENTS:
        pkg = source / component / "src" / f"theozolith_{component}"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('__version__ = "0.3.0"\n')
        (source / component / "pyproject.toml").write_text(
            f'[project]\nname = "theozolith-{component}"\nversion = "0.3.0"\n'
        )
    _git(["init", "--quiet"], source)
    _git(["add", "-A"], source)
    _git(["commit", "--quiet", "-m", "seed"], source)
    return source


def test_source_version_stamps_the_sha_and_dirty_suffix(tmp_path):
    source = _checkout(tmp_path)
    sha = _git(["rev-parse", "--short=12", "HEAD"], source)

    assert product.source_version(source) == f"0.3.0+g{sha}"

    (source / "worker" / "uncommitted.txt").write_text("wip")
    assert product.source_version(source) == f"0.3.0+g{sha}.dirty"

    with pytest.raises(product.ProductError, match="not a git checkout"):
        product.source_version(tmp_path / "not-a-repo-at-all")


def test_build_distribution_stamps_versions_and_collects_wheels(tmp_path):
    source = _checkout(tmp_path)
    sha = _git(["rev-parse", "--short=12", "HEAD"], source)
    expected = f"0.3.0+g{sha}"
    out = tmp_path / "wheels"
    stamped: dict[str, str] = {}

    def fake_runner(args, **kwargs):
        if args[:3] == [__import__("sys").executable, "-m", "pip"]:
            component_dir = Path(args[-1])
            wheel_dir = Path(args[args.index("--wheel-dir") + 1])
            text = (component_dir / "pyproject.toml").read_text()
            name = component_dir.name
            stamped[name] = text
            (wheel_dir / f"theozolith_{name}-{expected}-py3-none-any.whl").write_bytes(b"whl")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, **kwargs)  # the git subcommands are real

    version, wheels = product.build_distribution(
        source, out, runner=fake_runner, log=lambda *_: None
    )

    assert version == expected
    assert len(wheels) == len(product.COMPONENTS)
    # Every component's sandbox pyproject carried the stamped version, so
    # the installed distributions report it back in heartbeats.
    for component in product.COMPONENTS:
        assert f'version = "{expected}"' in stamped[component]
    # The source checkout itself was never touched.
    assert 'version = "0.3.0"' in (source / "worker" / "pyproject.toml").read_text()


def test_safe_segment_rejects_traversal():
    assert product.safe_segment("0.3.0+gabc.dirty")
    assert product.safe_segment("theozolith_worker-0.3.0-py3-none-any.whl")
    for hostile in ("", "..", "a/../b", "a/b", ".hidden", "-flag", "a\x00b"):
        assert not product.safe_segment(hostile), hostile
