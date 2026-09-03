"""The fail-closed CLI Pin install (ADR-0055 point 4): ordered gates over
crafted in-memory tarballs and a fake fetch — platform selection before any
download, whole-tarball SRI before extraction, allowlisted archive semantics
with every member validated before any is extracted, normalized modes, atomic
publication, and staging cleaned on every failure with nothing partial ever
visible at a non-dot path."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import platform
import tarfile
from pathlib import Path

import pytest
from theozolith_nodedaemon import cliinstall
from theozolith_nodedaemon.cliinstall import (
    CliArchiveInvalid,
    CliDownloadFailed,
    CliDownloadTooLarge,
    CliInstallError,
    CliIntegrityMismatch,
    CliPlatformUnsupported,
    CliPublishFailed,
    ensure_cli_version,
    platform_tuple_key,
    supported_tuple_keys,
)

KEY = "linux-x64-glibc"
SIBLING = "linux-arm64-glibc"
PACKAGE = "@anthropic-ai/claude-code-linux-x64"
VERSION = "2.1.257"
BINARY = b"#!/bin/sh\necho claude\n"


@pytest.fixture(autouse=True)
def pinned_host(monkeypatch):
    """Deterministic tuple detection for every test (the real-host detection
    is exercised explicitly in test_tuple_detection_on_this_host)."""
    monkeypatch.setattr(cliinstall, "platform_tuple_key", lambda: KEY)


@pytest.fixture(autouse=True)
def restrictive_umask():
    """EVERY test in this module runs under umask 077: published modes must
    be code-owned, never umask residue — the tree mounts read-only into deck
    containers running an arbitrary non-root UID (ADR-0055)."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def make_tarball(members) -> bytes:
    """members: (name, content, mode) for regular files (content=None makes a
    directory), or (name, linkname, mode, tartype) for special members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for member in members:
            name, content, mode = member[0], member[1], member[2]
            info = tarfile.TarInfo(name)
            info.mode = mode
            if len(member) > 3:
                info.type = member[3]
                if member[3] in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    info.linkname = content or "target"
                tar.addfile(info)
            elif content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def good_members():
    return [
        ("package/package.json", b'{"name": "x"}', 0o644),
        ("package/claude", BINARY, 0o755),
        ("package/README.md", b"readme\n", 0o600),
    ]


def sri(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def platform_map(data: bytes, *, key: str = KEY, integrity: str | None = None) -> dict:
    resolved = sri(data) if integrity is None else integrity
    return {key: {"package": PACKAGE, "integrity": resolved}}


def fetching(data: bytes, calls: list | None = None):
    def fetch(url: str):
        if calls is not None:
            calls.append(url)
        return io.BytesIO(data)

    return fetch


def non_dot(tool_root: Path) -> list[str]:
    if not tool_root.is_dir():
        return []
    return sorted(p.name for p in tool_root.iterdir() if not p.name.startswith("."))


def test_happy_path_publishes_atomically_with_normalized_modes(tmp_path):
    data = make_tarball(good_members())
    calls: list[str] = []
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data, calls)
    )
    assert published == tmp_path / "claude" / VERSION / "claude"
    assert published.read_bytes() == BINARY
    # The deck-facing contract: exactly the binary at the version root —
    # decoupled from npm packaging (no package.json, no README ride along).
    assert sorted(p.name for p in published.parent.iterdir()) == ["claude"]
    assert published.stat().st_mode & 0o777 == 0o755  # normalized, never archive metadata
    # The URL is npm's convention over pinned data.
    assert calls == [f"https://registry.npmjs.org/{PACKAGE}/-/claude-code-linux-x64-{VERSION}.tgz"]
    # Staging is gone; only the published version dir remains.
    assert non_dot(tmp_path / "claude") == [VERSION]
    assert not list((tmp_path / "claude").glob(".*"))


def test_modes_are_explicit_and_deck_readable_under_a_restrictive_umask(tmp_path):
    """Cross-UID mount contract: with umask 077 active (the autouse fixture),
    every directory on the launch path is world-traversable and the binary
    world-readable+executable, so a non-root deck UID can walk
    cli/<tool>/<version>/claude. (The test process shares the UID; mode bits
    ARE the cross-UID evidence.)"""
    data = make_tarball(good_members())
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data)
    )
    for directory in (tmp_path, tmp_path / "claude", published.parent):
        assert directory.stat().st_mode & 0o777 == 0o755, directory
    assert published.stat().st_mode & 0o777 == 0o755


def test_fast_path_repairs_restrictive_modes_without_a_download(tmp_path):
    """An install whose modes went restrictive (an older daemon amendment, a
    past umask) is REPAIRED on the already-installed fast path — chmod only,
    never a re-download."""
    data = make_tarball(good_members())
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data)
    )
    os.chmod(published, 0o700)
    for directory in (tmp_path, tmp_path / "claude", published.parent):
        os.chmod(directory, 0o700)

    def poisoned(url):
        raise AssertionError("mode repair must never re-download")

    again = ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=poisoned)
    assert again == published
    for directory in (tmp_path, tmp_path / "claude", published.parent):
        assert directory.stat().st_mode & 0o777 == 0o755, directory
    assert published.stat().st_mode & 0o777 == 0o755


def test_installed_version_returns_without_touching_the_network(tmp_path):
    data = make_tarball(good_members())
    ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))

    def poisoned(url):
        raise AssertionError("an installed version must never re-download")

    published = ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=poisoned)
    assert published.read_bytes() == BINARY


def test_absent_tuple_fails_before_any_download(tmp_path):
    def poisoned(url):
        raise AssertionError("fetch must not be called for an unsupported tuple")

    with pytest.raises(CliPlatformUnsupported, match=KEY):
        ensure_cli_version(
            tmp_path,
            "claude",
            VERSION,
            {SIBLING: {"package": PACKAGE, "integrity": "sha512-" + "A" * 96}},
            fetch=poisoned,
        )
    assert non_dot(tmp_path / "claude") == []


def test_each_platform_verifies_against_its_own_entry(tmp_path):
    """The node's tuple selects ITS map entry; a sibling entry's integrity
    never passes for bytes it does not describe."""
    served = make_tarball(good_members())
    other = make_tarball([("package/package.json", b"{}", 0o644), ("package/claude", b"B", 0o755)])
    platforms = {
        KEY: {"package": PACKAGE, "integrity": sri(other)},  # wrong bytes for our tuple
        SIBLING: {"package": PACKAGE, "integrity": sri(served)},  # right bytes, wrong tuple
    }
    with pytest.raises(CliIntegrityMismatch, match=KEY):
        ensure_cli_version(tmp_path, "claude", VERSION, platforms, fetch=fetching(served))
    assert non_dot(tmp_path / "claude") == []


def test_malformed_pinned_sri_is_an_integrity_failure(tmp_path):
    data = make_tarball(good_members())
    for bad in ("sha256-" + "A" * 44, "sha512-@@@", "sha512-short", ""):
        with pytest.raises(CliIntegrityMismatch):
            ensure_cli_version(
                tmp_path,
                "claude",
                VERSION,
                platform_map(data, integrity=bad),
                fetch=fetching(data),
            )
    assert non_dot(tmp_path / "claude") == []


def test_download_byte_cap_and_deadline_and_errors(tmp_path, monkeypatch):
    data = make_tarball(good_members())
    monkeypatch.setattr(cliinstall, "CLI_DOWNLOAD_MAX_BYTES", 8)
    with pytest.raises(CliDownloadTooLarge):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    monkeypatch.undo()
    monkeypatch.setattr(cliinstall, "platform_tuple_key", lambda: KEY)
    monkeypatch.setattr(cliinstall, "CLI_DOWNLOAD_TIMEOUT_SECONDS", -1)
    with pytest.raises(CliDownloadFailed, match="deadline"):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    monkeypatch.undo()
    monkeypatch.setattr(cliinstall, "platform_tuple_key", lambda: KEY)

    def broken(url):
        raise OSError("connection reset")

    with pytest.raises(CliDownloadFailed, match="download failed"):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=broken)
    assert non_dot(tmp_path / "claude") == []
    assert not list((tmp_path / "claude").glob(".*"))  # staging cleaned every time


ARCHIVE_CASES = [
    # (members, expected error needle)
    ([("package/package.json", b"{}", 0o644)], "no binary"),
    ([("other/claude", BINARY, 0o755), ("package/package.json", b"{}", 0o644)], "outside the"),
    ([("package/claude", BINARY, 0o755)], "package.json"),
    ([*good_members(), ("package/../evil", b"x", 0o644)], "path component"),
    ([*good_members(), ("/etc/passwd", b"x", 0o644)], "absolute"),
    ([*good_members(), ("package/link", "claude", 0o644, tarfile.SYMTYPE)], "symlink"),
    ([*good_members(), ("package/hard", "package/claude", 0o644, tarfile.LNKTYPE)], "hardlink"),
    ([*good_members(), ("package/dev", "", 0o644, tarfile.CHRTYPE)], "character device"),
    ([*good_members(), ("package/fifo", "", 0o644, tarfile.FIFOTYPE)], "FIFO"),
    ([*good_members(), ("package/README.md", b"again", 0o644)], "duplicate"),
    ([*good_members(), ("package/evil.sh", b"#!/bin/sh\n", 0o755)], "unexpected executable"),
    ([*good_members(), ("package/claude/nested", b"x", 0o644)], "conflicts with file"),
]


@pytest.mark.parametrize("members, needle", ARCHIVE_CASES)
def test_archive_validation_refuses_and_publishes_nothing(tmp_path, members, needle):
    data = make_tarball(members)
    with pytest.raises(CliArchiveInvalid, match=needle):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    assert non_dot(tmp_path / "claude") == []
    assert not list((tmp_path / "claude").glob(".*"))


def test_archive_caps_are_enforced(tmp_path, monkeypatch):
    data = make_tarball(good_members())
    monkeypatch.setattr(cliinstall, "CLI_ARCHIVE_MAX_ENTRIES", 2)
    with pytest.raises(CliArchiveInvalid, match="entries"):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    monkeypatch.undo()
    monkeypatch.setattr(cliinstall, "platform_tuple_key", lambda: KEY)
    monkeypatch.setattr(cliinstall, "CLI_ARCHIVE_MAX_EXPANDED_BYTES", 4)
    with pytest.raises(CliArchiveInvalid, match="expands past"):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    assert non_dot(tmp_path / "claude") == []


def test_garbage_bytes_with_matching_integrity_are_archive_invalid(tmp_path):
    """The SRI authenticates bytes only — it never substitutes for archive
    validation (a pin over garbage still refuses at the archive gate)."""
    data = b"this is not a gzip tarball"
    with pytest.raises(CliArchiveInvalid):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))


def test_interrupted_extraction_publishes_nothing_and_a_retry_succeeds(tmp_path, monkeypatch):
    data = make_tarball(good_members())
    real = cliinstall.shutil.copyfileobj

    def interrupted(*args, **kwargs):
        raise OSError(28, "injected ENOSPC")

    monkeypatch.setattr(cliinstall.shutil, "copyfileobj", interrupted)
    with pytest.raises(CliPublishFailed, match="mid-extract"):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    # NOTHING partial at any non-dot path, staging fully reclaimed.
    assert non_dot(tmp_path / "claude") == []
    assert not list((tmp_path / "claude").glob(".*"))
    monkeypatch.setattr(cliinstall.shutil, "copyfileobj", real)
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data)
    )
    assert published.read_bytes() == BINARY
    assert non_dot(tmp_path / "claude") == [VERSION]


def _fail_staging_mkdir(monkeypatch):
    real = Path.mkdir

    def failing(self, *args, **kwargs):
        if self.name.startswith(".staging-"):
            raise OSError(13, "injected EACCES")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing)


def _fail_integrity_read(monkeypatch):
    real = Path.open

    def failing(self, mode="r", *args, **kwargs):
        if self.name == "package.tgz" and "r" in mode:
            raise OSError(5, "injected EIO")
        return real(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing)


def _fail_publish_mkdir(monkeypatch):
    real = Path.mkdir

    def failing(self, *args, **kwargs):
        if self.name == "publish":
            raise OSError(13, "injected EACCES")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing)


def _fail_assembly_replace(monkeypatch):
    real = cliinstall.os.replace

    def failing(src, dst, *args, **kwargs):
        if str(src).endswith(f"extract/{cliinstall.CLI_BINARY_MEMBER}"):
            raise OSError(18, "injected EXDEV")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(cliinstall.os, "replace", failing)


def _fail_final_replace(monkeypatch):
    real = cliinstall.os.replace

    def failing(src, dst, *args, **kwargs):
        if Path(dst).name == VERSION:
            raise OSError(16, "injected EBUSY")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(cliinstall.os, "replace", failing)


FILESYSTEM_FAILURE_SITES = [
    # (injector, message needle) — one OSError per staging/publish-side site.
    (_fail_staging_mkdir, "staging setup"),
    (_fail_integrity_read, "integrity check"),
    (_fail_publish_mkdir, "publish-directory setup"),
    (_fail_assembly_replace, "binary assembly rename"),
    (_fail_final_replace, "publish rename"),
]


@pytest.mark.parametrize("inject, needle", FILESYSTEM_FAILURE_SITES)
def test_every_filesystem_failure_is_typed_cleaned_and_retryable(
    tmp_path, monkeypatch, inject, needle
):
    """The stable CliInstallError boundary: an OSError at ANY staging- or
    publish-side site surfaces as CliPublishFailed with the original cause
    chained, publishes nothing partial, reclaims staging, and a plain retry
    succeeds once the fault clears."""
    data = make_tarball(good_members())
    inject(monkeypatch)
    with pytest.raises(CliPublishFailed, match=needle) as excinfo:
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    assert isinstance(excinfo.value.__cause__, OSError)
    assert non_dot(tmp_path / "claude") == []
    assert not list((tmp_path / "claude").glob(".staging-*"))
    monkeypatch.undo()
    monkeypatch.setattr(cliinstall, "platform_tuple_key", lambda: KEY)
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data)
    )
    assert published.read_bytes() == BINARY
    assert non_dot(tmp_path / "claude") == [VERSION]


def test_broken_version_dir_is_retired_aside_and_reinstalled(tmp_path):
    data = make_tarball(good_members())
    broken = tmp_path / "claude" / VERSION
    broken.mkdir(parents=True)
    (broken / "debris").write_text("no binary here", encoding="utf-8")
    calls: list[str] = []
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data, calls)
    )
    assert calls  # the broken install triggered a real reinstall
    assert published.read_bytes() == BINARY
    assert not (published.parent / "debris").exists()


def test_symlinked_version_dir_is_never_served_and_is_reinstalled(tmp_path):
    """A version DIRECTORY that is a symlink is never accepted as the
    installed fast path, even when it currently resolves to a valid-looking
    executable: host-side verification would be vouching for a target that
    can live outside the mounted cli tree and resolve differently (or
    dangle) inside the deck container. The symlink is retired aside like any
    broken install and the version reinstalled as a REAL directory; the
    external target is never served and never touched."""
    data = make_tarball(good_members())
    external = tmp_path / "outside-the-tree"
    external.mkdir()
    decoy = external / "claude"
    decoy.write_bytes(b"#!/bin/sh\necho decoy\n")
    decoy.chmod(0o755)
    cli_root = tmp_path / "cli"
    tool_root = cli_root / "claude"
    tool_root.mkdir(parents=True)
    (tool_root / VERSION).symlink_to(external)
    calls: list[str] = []
    published = ensure_cli_version(
        cli_root, "claude", VERSION, platform_map(data), fetch=fetching(data, calls)
    )
    assert calls  # the symlinked dir was rejected: a real install ran
    assert not (tool_root / VERSION).is_symlink()  # published as a real directory
    assert published.read_bytes() == BINARY  # the pinned binary, never the external target
    assert non_dot(tool_root) == [VERSION]
    assert decoy.read_bytes() == b"#!/bin/sh\necho decoy\n"  # external target untouched


def test_symlinked_tool_root_is_refused_and_never_written_through(tmp_path):
    """The TOOL directory is a trust boundary: the fast path, mode
    normalization, the retire-aside rename, staging, and publication all
    resolve through it, so a symlinked tool root — which can point outside
    the mounted cli tree — is refused with the typed publish boundary before
    anything is read from, chmod'd through, or written under it. The repair
    lives in the daemon's converge pass (the link itself replaced with a
    real directory); after that the same call installs normally."""
    data = make_tarball(good_members())
    external = tmp_path / "outside-the-tree"
    (external / VERSION).mkdir(parents=True)
    decoy = external / VERSION / "claude"
    decoy.write_bytes(b"#!/bin/sh\necho decoy\n")
    decoy.chmod(0o700)  # the fast path through the link would "repair" this to 0o755
    snapshot = sorted(p.relative_to(external).as_posix() for p in external.rglob("*"))
    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    (cli_root / "claude").symlink_to(external)

    def poisoned(url):
        raise AssertionError("a symlinked tool root must fail before any download")

    with pytest.raises(CliPublishFailed, match="tool directory is a symlink"):
        ensure_cli_version(cli_root, "claude", VERSION, platform_map(data), fetch=poisoned)
    # Refused, never accepted: the decoy was not served as the fast path or
    # chmod'd, and nothing was staged, retired, or published at the target.
    assert (cli_root / "claude").is_symlink()  # the link is left for the daemon repair
    assert decoy.stat().st_mode & 0o777 == 0o700
    assert sorted(p.relative_to(external).as_posix() for p in external.rglob("*")) == snapshot

    # The daemon-layer repair (a real directory in the link's place) makes a
    # RETRY of the identical call install normally.
    (cli_root / "claude").unlink()
    published = ensure_cli_version(
        cli_root, "claude", VERSION, platform_map(data), fetch=fetching(data)
    )
    assert not (cli_root / "claude").is_symlink()
    assert published.read_bytes() == BINARY
    assert decoy.read_bytes() == b"#!/bin/sh\necho decoy\n"  # external target untouched


def test_error_messages_never_carry_urls_or_member_contents(tmp_path):
    data = make_tarball([*good_members(), ("package/evil.sh", b"SECRETBODY", 0o755)])
    with pytest.raises(CliArchiveInvalid) as excinfo:
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    assert "SECRETBODY" not in str(excinfo.value)
    assert "https://" not in str(excinfo.value)


def test_tuple_detection_on_this_host():
    """The real detection emits a key spelled from the supported set (the CI
    host is a supported linux tuple by construction)."""
    if platform.system() != "Linux":
        pytest.skip("tuple detection is linux-only by design")
    assert platform_tuple_key() in supported_tuple_keys()


def test_typed_errors_share_the_stable_base_class():
    for cls in (
        CliPlatformUnsupported,
        CliDownloadFailed,
        CliDownloadTooLarge,
        CliIntegrityMismatch,
        CliArchiveInvalid,
        CliPublishFailed,
    ):
        assert issubclass(cls, CliInstallError)
