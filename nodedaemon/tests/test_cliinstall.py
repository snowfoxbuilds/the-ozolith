"""The fail-closed CLI Pin install (ADR-0055 point 4): ordered gates over
crafted in-memory tarballs and a fake fetch — platform selection before any
download, whole-tarball SRI before extraction, every member validated against
the tool's CLI ARCHIVE CONTRACT row before any is extracted (the closed
allowed-member set, executable tags agreeing in both directions, the layout
marker under its closed schema) by a BOUNDED parser whose limits are proven
to stop the read at the offending header, normalized modes, the product-rendered marker
and the daemon-created vendored link, atomic publication, and staging cleaned
on every failure with nothing partial ever visible at a non-dot path. Fixtures
replicate the real member listings of claude 2.1.260 and codex 0.153.3 (both
Linux architectures) with small synthetic bytes."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import os
import platform
import tarfile
from pathlib import Path

import pytest
from theozolith_nodedaemon import cliinstall
from theozolith_nodedaemon.cliinstall import (
    CLI_ARCHIVE_CONTRACT,
    CliArchiveInvalid,
    CliContractInvalid,
    CliDownloadFailed,
    CliDownloadTooLarge,
    CliInstallError,
    CliIntegrityMismatch,
    CliMember,
    CliPlatformUnsupported,
    CliPublishFailed,
    CliToolUnknown,
    ensure_cli_version,
    platform_tuple_key,
    supported_tuple_keys,
)

KEY = "linux-x64-glibc"
SIBLING = "linux-arm64-glibc"
PACKAGE = "@anthropic-ai/claude-code-linux-x64"
VERSION = "2.1.260"
BINARY = b"#!/bin/sh\necho claude\n"

CODEX_PACKAGE = "@openai/codex"
CODEX_VERSION = "0.153.3"
CODEX_BINARY = b"#!/bin/sh\necho codex\n"
X64 = "x86_64-unknown-linux-musl"
ARM64 = "aarch64-unknown-linux-musl"
CODEX_PREFIX = f"package/vendor/{X64}/"
CODEX_EXECUTABLES = (
    "bin/codex",
    "bin/codex-code-mode-host",
    "codex-path/rg",
    "codex-resources/bwrap",
    "codex-resources/zsh/bin/zsh",
)
CODEX_DIRECTORIES = ("bin", "codex-path", "codex-resources", "codex-resources/zsh")
CODEX_PUBLISHED = sorted(
    (
        *CODEX_EXECUTABLES,
        *CODEX_DIRECTORIES,
        "codex-resources/zsh/bin",
        "codex",
        "codex-package.json",
    )
)
DROP = object()  # a marker-field override meaning "omit the field"


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


def claude_members(*, name: str = PACKAGE, version: str = VERSION, marker_bytes=None):
    """The real four-member claude platform tarball. The vendor package.json
    carries keys beyond the two the contract binds (ignored, never
    published); README's archive mode is deliberately wrong (never trusted)."""
    if marker_bytes is None:
        marker_bytes = json.dumps({"name": name, "version": version, "os": ["linux"]}).encode()
    return [
        ("package/package.json", marker_bytes, 0o644),
        ("package/claude", BINARY, 0o755),
        ("package/LICENSE.md", b"license\n", 0o644),
        ("package/README.md", b"readme\n", 0o600),
    ]


def codex_marker(for_triple: str = X64, base_version: str = CODEX_VERSION, **overrides) -> dict:
    """The real codex-package.json: the seven bound fields, with ``DROP``
    removing one and any other override replacing or adding one."""
    document = {
        "layoutVersion": 1,
        "version": base_version,
        "target": for_triple,
        "variant": "codex",
        "entrypoint": "bin/codex",
        "resourcesDir": "codex-resources",
        "pathDir": "codex-path",
    }
    for field, value in overrides.items():
        if value is DROP:
            document.pop(field)
        else:
            document[field] = value
    return document


def codex_members(
    triple: str = X64,
    arch: str = "x64",
    *,
    version: str = CODEX_VERSION,
    marker: dict | None = None,
    marker_bytes: bytes | None = None,
    root_name: str = CODEX_PACKAGE,
    root_version: str | None = None,
):
    """The real eight-member codex platform tarball: two root members outside
    the vendored payload prefix, and under it the marker plus five
    executables (the listing verified identical at 0.150.0 and 0.153.3)."""
    prefix = f"package/vendor/{triple}/"
    if marker_bytes is None:
        marker_bytes = json.dumps(
            codex_marker(triple, version) if marker is None else marker
        ).encode()
    if root_version is None:
        root_version = f"{version}-linux-{arch}"
    root_document = {"name": root_name, "version": root_version, "bin": {"codex": "bin/codex.js"}}
    return [
        ("package/package.json", json.dumps(root_document).encode(), 0o644),
        ("package/README.md", b"codex readme\n", 0o644),
        (prefix + "codex-package.json", marker_bytes, 0o644),
        (prefix + "bin/codex", CODEX_BINARY, 0o755),
        (prefix + "bin/codex-code-mode-host", b"#!/bin/sh\necho host\n", 0o755),
        (prefix + "codex-path/rg", b"#!/bin/sh\necho rg\n", 0o755),
        (prefix + "codex-resources/bwrap", b"#!/bin/sh\necho bwrap\n", 0o755),
        (prefix + "codex-resources/zsh/bin/zsh", b"#!/bin/sh\necho zsh\n", 0o755),
    ]


def sri(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def platform_map(
    data: bytes, *, key: str = KEY, package: str = PACKAGE, integrity: str | None = None
) -> dict:
    resolved = sri(data) if integrity is None else integrity
    return {key: {"package": package, "integrity": resolved}}


def fetching(data: bytes, calls: list | None = None):
    def fetch(url: str):
        if calls is not None:
            calls.append(url)
        return io.BytesIO(data)

    return fetch


def poisoned(url):
    raise AssertionError(f"fetch must not be called ({url})")


def non_dot(tool_root: Path) -> list[str]:
    if not tool_root.is_dir():
        return []
    return sorted(p.name for p in tool_root.iterdir() if not p.name.startswith("."))


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def snapshot(root: Path) -> list[tuple[str, int, bytes | str]]:
    """Every entry under root with its own mode and content (link target for a
    symlink) — the untouched-verified-versions evidence."""
    rows = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows.append((rel, 0, os.readlink(path)))
        elif path.is_dir():
            rows.append((rel, _mode(path), "dir"))
        else:
            rows.append((rel, _mode(path), path.read_bytes()))
    return rows


def install_codex(cli_root: Path, data: bytes, *, calls=None, version=CODEX_VERSION, key=KEY):
    return ensure_cli_version(
        cli_root,
        "codex",
        version,
        platform_map(data, key=key, package=CODEX_PACKAGE),
        fetch=fetching(data, calls),
    )


# -- success fixtures: both real archive shapes ---------------------------------------


def test_claude_happy_path_publishes_the_closed_set_with_a_rendered_marker(tmp_path):
    data = make_tarball(claude_members())
    calls: list[str] = []
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data, calls)
    )
    version_dir = tmp_path / "claude" / VERSION
    assert published == version_dir / "claude"
    assert published.read_bytes() == BINARY
    assert not published.is_symlink()
    # The deck-facing contract: exactly the row's four-member published set at
    # the version root, modes normalized (never archive metadata).
    assert listing(version_dir) == ["LICENSE.md", "README.md", "claude", "package.json"]
    assert _mode(published) == 0o755
    for name in ("LICENSE.md", "README.md", "package.json"):
        assert _mode(version_dir / name) == 0o644, name
    assert (version_dir / "README.md").read_bytes() == b"readme\n"  # the archive's bytes
    # The marker is PRODUCT-RENDERED: exactly the two bound values — the
    # vendor document's extra key never reaches the published layout.
    assert json.loads((version_dir / "package.json").read_text()) == {
        "name": PACKAGE,
        "version": VERSION,
    }
    # The URL is npm's convention over pinned data — unchanged from an older
    # Control's claude wire shape ({version, platforms: {tuple: {package,
    # integrity}}}), so a newer daemon converges it exactly as before.
    assert calls == [f"https://registry.npmjs.org/{PACKAGE}/-/claude-code-linux-x64-{VERSION}.tgz"]
    # Staging is gone; only the published version dir remains.
    assert non_dot(tmp_path / "claude") == [VERSION]
    assert not list((tmp_path / "claude").glob(".*"))


@pytest.mark.parametrize(
    "key, arch, triple",
    [
        ("linux-x64-glibc", "x64", X64),
        ("linux-x64-musl", "x64", X64),
        ("linux-arm64-glibc", "arm64", ARM64),
        ("linux-arm64-musl", "arm64", ARM64),
    ],
)
def test_codex_happy_path_publishes_the_vendored_set_for_each_tuple(
    tmp_path, monkeypatch, key, arch, triple
):
    """Both real codex tarballs (x64 and arm64; one static musl tarball
    serves both libc tuples of an architecture) install and publish exactly
    the six payload members at their prefix-relative paths, the
    product-rendered marker, and the daemon-created relative link — nothing
    from outside the payload prefix. The download URL carries the platform
    suffix an older daemon cannot derive."""
    monkeypatch.setattr(cliinstall, "platform_tuple_key", lambda: key)
    members = codex_members(triple, arch)
    data = make_tarball(members)
    calls: list[str] = []
    published = install_codex(tmp_path, data, calls=calls, key=key)
    version_dir = tmp_path / "codex" / CODEX_VERSION
    assert published == version_dir / "codex"
    assert published.is_symlink() and os.readlink(published) == "bin/codex"
    assert published.resolve() == (version_dir / "bin" / "codex").resolve()  # inside the dir
    assert published.read_bytes() == CODEX_BINARY
    assert listing(version_dir) == CODEX_PUBLISHED
    for rel in CODEX_EXECUTABLES:
        assert not (version_dir / rel).is_symlink() and _mode(version_dir / rel) == 0o755, rel
    for rel in (*CODEX_DIRECTORIES, "codex-resources/zsh/bin"):
        assert _mode(version_dir / rel) == 0o755, rel
    assert _mode(version_dir / "codex-package.json") == 0o644
    # Product-rendered: the seven bound values, never the archive's bytes.
    archived = next(content for name, content, _m in members if name.endswith("codex-package.json"))
    assert (version_dir / "codex-package.json").read_bytes() != archived
    assert json.loads((version_dir / "codex-package.json").read_text()) == codex_marker(triple)
    # The root members are validated and discarded — published nowhere.
    assert not (version_dir / "package.json").exists()
    assert not (version_dir / "README.md").exists()
    assert calls == [
        f"https://registry.npmjs.org/@openai/codex/-/codex-{CODEX_VERSION}-linux-{arch}.tgz"
    ]
    assert non_dot(tmp_path / "codex") == [CODEX_VERSION]
    assert not list((tmp_path / "codex").glob(".*"))


def test_download_coordinates_follow_each_row_tarball_version_rule(tmp_path):
    """Mixed-version wire behavior (ADR-0055 D4): the codex coordinate is the
    wrapper package at ``<version>-linux-<arch>`` — an older daemon derives
    the un-suffixed ``codex-<version>.tgz``, the wrapper's own tarball (a node
    launcher), whose bytes fail the pinned platform integrity; the claude
    coordinate is exactly what it always was."""
    served = {}

    def fetch(url):
        served.setdefault("urls", []).append(url)
        return io.BytesIO(served["data"])

    served["data"] = make_tarball(claude_members())
    ensure_cli_version(tmp_path, "claude", VERSION, platform_map(served["data"]), fetch=fetch)
    served["data"] = make_tarball(codex_members())
    ensure_cli_version(
        tmp_path,
        "codex",
        CODEX_VERSION,
        platform_map(served["data"], package=CODEX_PACKAGE),
        fetch=fetch,
    )
    assert served["urls"] == [
        f"https://registry.npmjs.org/{PACKAGE}/-/claude-code-linux-x64-{VERSION}.tgz",
        f"https://registry.npmjs.org/@openai/codex/-/codex-{CODEX_VERSION}-linux-x64.tgz",
    ]


def test_modes_are_explicit_and_deck_readable_under_a_restrictive_umask(tmp_path):
    """Cross-UID mount contract: with umask 077 active (the autouse fixture),
    every directory on the launch path is world-traversable and every
    executable world-readable+executable, so a non-root deck UID can walk
    cli/<tool>/<version>/<name>. (The test process shares the UID; mode bits
    ARE the cross-UID evidence.)"""
    data = make_tarball(claude_members())
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data)
    )
    for directory in (tmp_path, tmp_path / "claude", published.parent):
        assert _mode(directory) == 0o755, directory
    assert _mode(published) == 0o755
    codex = install_codex(tmp_path, make_tarball(codex_members()))
    version_dir = codex.parent
    for rel in ("", *CODEX_DIRECTORIES, "codex-resources/zsh/bin"):
        assert _mode(version_dir / rel) == 0o755, rel
    for rel in CODEX_EXECUTABLES:
        assert _mode(version_dir / rel) == 0o755, rel


# -- the fast path: whole-published-set health, mode repair, no download -----------


def test_fast_path_repairs_restrictive_modes_without_a_download(tmp_path):
    """An install whose modes went restrictive (a past umask) is REPAIRED on
    the already-installed fast path — chmod only, never a re-download — for
    both layouts."""
    data = make_tarball(claude_members())
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data)
    )
    for path in (tmp_path, tmp_path / "claude", published.parent, published):
        os.chmod(path, 0o700)
    again = ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=poisoned)
    assert again == published
    for directory in (tmp_path, tmp_path / "claude", published.parent):
        assert _mode(directory) == 0o755, directory
    assert _mode(published) == 0o755
    assert _mode(published.parent / "README.md") == 0o644

    codex = install_codex(tmp_path, make_tarball(codex_members()))
    version_dir = codex.parent
    for rel in ("", *CODEX_DIRECTORIES, *CODEX_EXECUTABLES, "codex-package.json"):
        os.chmod(version_dir / rel, 0o700)
    again = ensure_cli_version(
        tmp_path,
        "codex",
        CODEX_VERSION,
        platform_map(b"", package=CODEX_PACKAGE, integrity="sha512-" + "A" * 96),
        fetch=poisoned,
    )
    assert again == codex and os.readlink(codex) == "bin/codex"
    for rel in ("", *CODEX_DIRECTORIES, *CODEX_EXECUTABLES):
        assert _mode(version_dir / rel) == 0o755, rel
    assert _mode(version_dir / "codex-package.json") == 0o644


def test_installed_version_returns_without_touching_the_network(tmp_path):
    data = make_tarball(claude_members())
    ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    published = ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=poisoned)
    assert published.read_bytes() == BINARY


def _absolute_link(version_dir: Path) -> None:
    (version_dir / "codex").unlink()
    (version_dir / "codex").symlink_to(version_dir / "bin" / "codex")


def _other_target_link(version_dir: Path) -> None:
    # An archive-supplied link would point wherever the archive chose.
    (version_dir / "codex").unlink()
    (version_dir / "codex").symlink_to("bin/codex-code-mode-host")


def _traversal_link(version_dir: Path) -> None:
    (version_dir / "codex").unlink()
    (version_dir / "codex").symlink_to("../" + CODEX_VERSION + "/bin/codex")


def _regular_file_in_place_of_the_link(version_dir: Path) -> None:
    (version_dir / "codex").unlink()
    (version_dir / "codex").write_bytes(CODEX_BINARY)
    (version_dir / "codex").chmod(0o755)


def _missing_member(version_dir: Path) -> None:
    (version_dir / "codex-path" / "rg").unlink()


def _symlinked_member(version_dir: Path) -> None:
    (version_dir / "codex-resources" / "bwrap").unlink()
    (version_dir / "codex-resources" / "bwrap").symlink_to("../bin/codex")


def _symlinked_directory(version_dir: Path) -> None:
    elsewhere = version_dir.parent.parent / "elsewhere"
    os.replace(version_dir / "bin", elsewhere)
    (version_dir / "bin").symlink_to(elsewhere)


@pytest.mark.parametrize(
    "damage",
    [
        _absolute_link,
        _other_target_link,
        _traversal_link,
        _regular_file_in_place_of_the_link,
        _missing_member,
        _symlinked_member,
        _symlinked_directory,
    ],
)
def test_unhealthy_codex_version_dir_is_retired_and_reinstalled(tmp_path, damage):
    """The fast path accepts only the COMPLETE published set with the expected
    types: a ``codex`` that is absolute, archive-supplied to another target,
    ``..``-bearing, or a regular file, a missing or symlinked member, or a
    symlinked interior directory retires the version dir aside and reinstalls
    it (one download) as the contract's layout."""
    data = make_tarball(codex_members())
    published = install_codex(tmp_path, data)
    version_dir = published.parent
    damage(version_dir)
    calls: list[str] = []
    again = install_codex(tmp_path, data, calls=calls)
    assert len(calls) == 1  # the broken install triggered a real reinstall
    assert again == published and os.readlink(again) == "bin/codex"
    assert listing(version_dir) == CODEX_PUBLISHED
    assert non_dot(tmp_path / "codex") == [CODEX_VERSION]
    # Retired aside dot-prefixed (invisible to the export contract; the
    # daemon's leftover sweep reclaims it), staging fully reclaimed.
    assert [p.name[:8] for p in (tmp_path / "codex").glob(".*")] == [".broken-"]


def test_pre_contract_claude_version_dir_is_retired_and_reinstalled_once(tmp_path):
    """A claude version dir published by a pre-contract daemon holds only the
    binary — unhealthy under the closed published set — so the daemon
    upgrade re-downloads it ONCE and publishes the four members (ADR-0055
    D4; while that runs the pin is non-converged and launches refuse)."""
    data = make_tarball(claude_members())
    version_dir = tmp_path / "claude" / VERSION
    version_dir.mkdir(parents=True)
    (version_dir / "claude").write_bytes(BINARY)
    (version_dir / "claude").chmod(0o755)
    calls: list[str] = []
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data, calls)
    )
    assert len(calls) == 1
    assert listing(version_dir) == ["LICENSE.md", "README.md", "claude", "package.json"]
    ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=poisoned)
    assert published.read_bytes() == BINARY


# -- gate (a): tuple selection and the unknown-tool / malformed-row classes --------


def test_absent_tuple_fails_before_any_download(tmp_path):
    with pytest.raises(CliPlatformUnsupported, match=KEY):
        ensure_cli_version(
            tmp_path,
            "claude",
            VERSION,
            {SIBLING: {"package": PACKAGE, "integrity": "sha512-" + "A" * 96}},
            fetch=poisoned,
        )
    assert non_dot(tmp_path / "claude") == []


def test_unknown_tool_fails_typed_before_any_download_with_the_store_untouched(tmp_path):
    """A tool absent from the CLI archive contract fails closed with its own
    class before the network or the store is touched — an unknown wire tool
    is a per-pin issue, never a fleet-wide product bug."""
    data = make_tarball(claude_members())
    with pytest.raises(CliToolUnknown, match="pi") as excinfo:
        ensure_cli_version(tmp_path, "pi", "1.0.0", platform_map(data), fetch=poisoned)
    assert isinstance(excinfo.value, CliInstallError)
    assert not (tmp_path / "pi").exists()
    assert list(tmp_path.iterdir()) == []


def _row(tool: str):
    return CLI_ARCHIVE_CONTRACT[tool]


MALFORMED_ROWS = [
    # (tool, replacement fields, message needle) — each a product bug.
    ("claude", {"executable_member": "package.json"}, "executable member"),
    ("claude", {"tuples": {KEY: _row("claude").tuples[KEY]}}, "tuple table"),
    ("claude", {"marker": dataclasses.replace(_row("claude").marker, member="nope")}, "marker"),
    ("claude", {"vendored": True}, "vendored flag"),
    ("claude", {"published_name": "bin/claude"}, "published name"),
    ("claude", {"payload_members": (CliMember("../claude", True),)}, "clean relative path"),
    ("codex", {"payload_prefix_template": "vendor/{unknown}/"}, "template"),
    (
        "codex",
        {"root_members": (CliMember("vendor", False), CliMember("package.json", False))},
        "directory",
    ),
    (
        "codex",
        {"payload_members": (*_row("codex").payload_members, CliMember("codex/x", True))},
        "link name collides",
    ),
    (
        "codex",
        {"root_marker": dataclasses.replace(_row("codex").root_marker, member="LICENSE")},
        "root marker",
    ),
]


@pytest.mark.parametrize("tool, fields, needle", MALFORMED_ROWS)
def test_malformed_contract_row_fails_typed_before_any_download(
    tmp_path, monkeypatch, tool, fields, needle
):
    broken = dataclasses.replace(_row(tool), **fields)
    monkeypatch.setattr(cliinstall, "CLI_ARCHIVE_CONTRACT", {**CLI_ARCHIVE_CONTRACT, tool: broken})
    version = VERSION if tool == "claude" else CODEX_VERSION
    package = PACKAGE if tool == "claude" else CODEX_PACKAGE
    with pytest.raises(CliContractInvalid, match=needle) as excinfo:
        ensure_cli_version(
            tmp_path, tool, version, platform_map(b"", package=package), fetch=poisoned
        )
    assert isinstance(excinfo.value, CliInstallError)
    assert list(tmp_path.iterdir()) == []


def test_shipped_rows_are_well_formed_and_cover_every_supported_tuple():
    for tool, row in CLI_ARCHIVE_CONTRACT.items():
        cliinstall._check_row(tool, row)
        assert set(row.tuples) == set(supported_tuple_keys())
    assert set(CLI_ARCHIVE_CONTRACT) == {"claude", "codex"}
    assert not hasattr(cliinstall, "CLI_BINARY_NAME") and not hasattr(
        cliinstall, "CLI_BINARY_MEMBER"
    )


# -- gates (b)/(c): download and integrity ------------------------------------------------


def test_each_platform_verifies_against_its_own_entry(tmp_path):
    """The node's tuple selects ITS map entry; a sibling entry's integrity
    never passes for bytes it does not describe."""
    served = make_tarball(claude_members())
    other = make_tarball(claude_members(version="2.1.259"))
    platforms = {
        KEY: {"package": PACKAGE, "integrity": sri(other)},  # wrong bytes for our tuple
        SIBLING: {"package": PACKAGE, "integrity": sri(served)},  # right bytes, wrong tuple
    }
    with pytest.raises(CliIntegrityMismatch, match=KEY):
        ensure_cli_version(tmp_path, "claude", VERSION, platforms, fetch=fetching(served))
    assert non_dot(tmp_path / "claude") == []


def test_malformed_pinned_sri_is_an_integrity_failure(tmp_path):
    data = make_tarball(claude_members())
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
    data = make_tarball(claude_members())
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


# -- gate (d): the closed allowed-member set ---------------------------------------------


def _without(members, name: str):
    return [member for member in members if member[0] != name]


def _renamed(members, old: str, new: str):
    return [(new, *member[1:]) if member[0] == old else member for member in members]


def _remoded(members, name: str, mode: int):
    return [
        (member[0], member[1], mode, *member[3:]) if member[0] == name else member
        for member in members
    ]


OUTSIDE = "outside the allowed member set"
MISSING = "missing allowed member"
ARCHIVE_CASES = [
    # (tool, members, expected error needle) — each refuses at the archive gate.
    # claude: the shared structural refusals, re-run under the row.
    ("claude", [("package/package.json", b"{}", 0o644)], MISSING),
    (
        "claude",
        [("other/claude", BINARY, 0o755), ("package/package.json", b"{}", 0o644)],
        "outside the package/ layout",
    ),
    ("claude", _without(claude_members(), "package/package.json"), MISSING),
    ("claude", [*claude_members(), ("package/../evil", b"x", 0o644)], "path component"),
    ("claude", [*claude_members(), ("/etc/passwd", b"x", 0o644)], "absolute"),
    ("claude", [*claude_members(), ("package/link", "claude", 0o644, tarfile.SYMTYPE)], "symlink"),
    (
        "claude",
        [*claude_members(), ("package/hard", "package/claude", 0o644, tarfile.LNKTYPE)],
        "hardlink",
    ),
    (
        "claude",
        [*claude_members(), ("package/dev", "", 0o644, tarfile.CHRTYPE)],
        "character device",
    ),
    ("claude", [*claude_members(), ("package/blk", "", 0o644, tarfile.BLKTYPE)], "block device"),
    ("claude", [*claude_members(), ("package/fifo", "", 0o644, tarfile.FIFOTYPE)], "FIFO"),
    ("claude", [*claude_members(), ("package/README.md", b"again", 0o644)], "duplicate"),
    # A member beneath a file: unreachable past the closed set (refused as
    # outside it first); the ancestor-conflict guard stays as defense in depth.
    ("claude", [*claude_members(), ("package/claude/nested", b"x", 0o644)], f"{OUTSIDE}|conflicts"),
    # claude: the closed-set refusals.
    (
        "claude",
        _renamed(claude_members(), "package/claude", "package/claud"),
        OUTSIDE,
    ),  # wrong name
    (
        "claude",
        _renamed(claude_members(), "package/claude", "package/bin/claude"),
        OUTSIDE,
    ),  # wrong path
    ("claude", [*claude_members(), ("package/notes.txt", b"x", 0o644)], OUTSIDE),  # non-exec extra
    (
        "claude",
        [*claude_members(), ("package/evil.sh", b"#!/bin/sh\n", 0o755)],
        "unexpected executable",
    ),
    ("claude", [*claude_members(), ("package/extra/", None, 0o755)], OUTSIDE),  # a stray directory
    ("claude", [*claude_members(), ("vendor/", None, 0o755)], "outside the package/ layout"),
    ("claude", _without(claude_members(), "package/LICENSE.md"), MISSING),
    ("claude", _remoded(claude_members(), "package/README.md", 0o755), "unexpected executable"),
    ("claude", _remoded(claude_members(), "package/claude", 0o644), "not executable"),
    # codex: the closed-set refusals under the vendored prefix and at the root.
    ("codex", [*codex_members(), (CODEX_PREFIX + "notes.txt", b"x", 0o644)], OUTSIDE),
    (
        "codex",
        [*codex_members(), (CODEX_PREFIX + "bin/evil", b"#!/bin/sh\n", 0o755)],
        "unexpected executable",
    ),
    ("codex", [*codex_members(), ("package/LICENSE", b"x", 0o644)], OUTSIDE),  # a root extra
    (
        "codex",
        [*codex_members(), ("package/bin/codex.js", b"#!/usr/bin/env node\n", 0o755)],
        "unexpected executable",
    ),
    ("codex", _without(codex_members(), CODEX_PREFIX + "codex-path/rg"), MISSING),
    ("codex", _without(codex_members(), "package/README.md"), MISSING),
    (
        "codex",
        _remoded(codex_members(), CODEX_PREFIX + "codex-package.json", 0o755),
        "unexpected executable",
    ),
    ("codex", _remoded(codex_members(), "package/README.md", 0o755), "unexpected executable"),
    ("codex", _remoded(codex_members(), CODEX_PREFIX + "bin/codex", 0o644), "not executable"),
    (
        "codex",
        _renamed(codex_members(), CODEX_PREFIX + "bin/codex", CODEX_PREFIX + "codex"),
        OUTSIDE,
    ),
    # A permitted executable name planted under the OTHER triple's prefix.
    (
        "codex",
        [*codex_members(), (f"package/vendor/{ARM64}/bin/codex", CODEX_BINARY, 0o755)],
        OUTSIDE,
    ),
    # The other architecture's whole tarball offered to this node's tuple.
    ("codex", codex_members(ARM64, "arm64"), f"{OUTSIDE}|{MISSING}"),
    (
        "codex",
        [*codex_members(), (CODEX_PREFIX + "bin/codex/x", b"x", 0o644)],
        f"{OUTSIDE}|conflicts",
    ),
]


@pytest.mark.parametrize("tool, members, needle", ARCHIVE_CASES)
def test_archive_validation_refuses_and_publishes_nothing(tmp_path, tool, members, needle):
    data = make_tarball(members)
    version = VERSION if tool == "claude" else CODEX_VERSION
    package = PACKAGE if tool == "claude" else CODEX_PACKAGE
    with pytest.raises(CliArchiveInvalid, match=needle):
        ensure_cli_version(
            tmp_path, tool, version, platform_map(data, package=package), fetch=fetching(data)
        )
    assert non_dot(tmp_path / tool) == []
    assert not list((tmp_path / tool).glob(".*"))


def test_cross_tool_substitution_refuses_both_directions_keeping_verified_versions(tmp_path):
    """A claude tarball offered to the codex contract has no payload-prefix
    members and no layout marker; a codex tarball offered to the claude
    contract carries members outside the four-member set. Both refuse without
    publishing, and the previously verified version of each tool — the
    recovery state — is byte-for-byte untouched."""
    claude_data = make_tarball(claude_members())
    codex_data = make_tarball(codex_members())
    ensure_cli_version(
        tmp_path,
        "claude",
        "2.1.259",
        platform_map(make_tarball(claude_members(version="2.1.259"))),
        fetch=fetching(make_tarball(claude_members(version="2.1.259"))),
    )
    install_codex(tmp_path, make_tarball(codex_members(version="0.150.0")), version="0.150.0")
    before = snapshot(tmp_path)

    with pytest.raises(CliArchiveInvalid):
        ensure_cli_version(
            tmp_path,
            "codex",
            CODEX_VERSION,
            platform_map(claude_data, package=CODEX_PACKAGE),
            fetch=fetching(claude_data),
        )
    with pytest.raises(CliArchiveInvalid):
        ensure_cli_version(
            tmp_path, "claude", VERSION, platform_map(codex_data), fetch=fetching(codex_data)
        )
    assert non_dot(tmp_path / "claude") == ["2.1.259"]
    assert non_dot(tmp_path / "codex") == ["0.150.0"]
    assert snapshot(tmp_path) == before


def test_archive_caps_are_enforced(tmp_path, monkeypatch):
    data = make_tarball(claude_members())
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


# -- gate (d): bounded parsing — the caps constrain the PARSER, not a post-parse check


RECORD = tarfile.RECORDSIZE  # the streaming parser's read unit: one record per read
BIG = b"a" * (1024 * 1024)


class _Metered:
    """The decompressed stream the parser reads through, counting every byte
    handed out and tripping past a ceiling — the evidence that a refusal
    came from a header, never from having walked the body."""

    def __init__(self, source, ceiling: int | None):
        self.source = source
        self.ceiling = ceiling
        self.consumed = 0

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.consumed += len(data)
        if self.ceiling is not None and self.consumed > self.ceiling:
            raise AssertionError(
                f"parser read {self.consumed} decompressed bytes past the {self.ceiling} ceiling"
            )
        return data

    def close(self) -> None:
        self.source.close()


def metered(monkeypatch, ceiling: int | None = None) -> list[_Metered]:
    """Meter every parse the installer opens (one per pass) at the
    decompression seam; returns the streams in opening order."""
    real = cliinstall._gunzip
    streams: list[_Metered] = []

    def wrapped(raw):
        stream = _Metered(real(raw), ceiling)
        streams.append(stream)
        return stream

    monkeypatch.setattr(cliinstall, "_gunzip", wrapped)
    return streams


def entry(
    name: str,
    body: bytes = b"",
    *,
    tartype: bytes = tarfile.REGTYPE,
    mode: int = 0o644,
    pax: dict | None = None,
) -> tuple[tarfile.TarInfo, bytes]:
    """A raw archive entry: any type may declare (and carry) a body."""
    info = tarfile.TarInfo(name)
    info.type = tartype
    info.mode = mode
    info.size = len(body)
    if pax:
        info.pax_headers = pax
    return info, body


def raw_tarball(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, body in entries:
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _regular(members):
    return [entry(name, body, mode=mode) for name, body, mode in members]


def _big_first():
    """The 1 MiB binary as the FIRST entry, the rest of the real set after."""
    return [
        entry("package/claude", BIG, mode=0o755),
        *_regular(_without(claude_members(), "package/claude")),
    ]


def _big_third():
    """Two small entries, then the 1 MiB binary as the THIRD."""
    small = _without(claude_members(), "package/claude")
    return [*_regular(small[:2]), entry("package/claude", BIG, mode=0o755), *_regular(small[2:])]


def _fronted(*bombs):
    """Special entries ahead of an otherwise valid claude archive."""
    return [*bombs, *_regular(claude_members())]


KIB = 1024
BOUNDED_PARSE_CASES = [
    # (label, caps, entries, needle, consumed ceiling) — each refuses from a
    # header, typed, having read at most the ceiling of decompressed bytes.
    (
        "oversized first member",
        {"CLI_ARCHIVE_MAX_EXPANDED_BYTES": 64 * KIB},
        _big_first,
        "expands past",
        RECORD,
    ),
    (
        "entry past the count cap",
        {"CLI_ARCHIVE_MAX_ENTRIES": 2},
        _big_third,
        "more than 2 entries",
        RECORD,
    ),
    (
        "PAX extended record declaring 4 MiB",
        {"CLI_ARCHIVE_MAX_OVERHEAD_BYTES": 64 * KIB},
        lambda: _fronted(entry("package/pad", bytes(4 * 1024 * KIB), tartype=tarfile.XHDTYPE)),
        "parse budget",
        64 * KIB + 1,
    ),
    (
        "GNU long-name record declaring 4 MiB",
        {"CLI_ARCHIVE_MAX_OVERHEAD_BYTES": 64 * KIB},
        lambda: _fronted(
            entry("././@LongLink", bytes(4 * 1024 * KIB), tartype=tarfile.GNUTYPE_LONGNAME)
        ),
        "parse budget",
        64 * KIB + 1,
    ),
    (
        "directory entry declaring a body",
        {},
        lambda: _fronted(entry("package", BIG, tartype=tarfile.DIRTYPE, mode=0o755)),
        "directory entry declares a body",
        RECORD,
    ),
    (
        "FIFO declaring a body",
        {},
        lambda: _fronted(entry("package/fifo", BIG, tartype=tarfile.FIFOTYPE)),
        "FIFO refused",
        RECORD,
    ),
    (
        "PAX sparse map on the binary",
        {},
        lambda: [
            entry("package/claude", BINARY, mode=0o755, pax={"GNU.sparse.map": "0,1048576"}),
            *_regular(_without(claude_members(), "package/claude")),
        ],
        "sparse member refused",
        RECORD,
    ),
]


@pytest.mark.parametrize(
    "label, caps, build, needle, ceiling",
    BOUNDED_PARSE_CASES,
    ids=[c[0] for c in BOUNDED_PARSE_CASES],
)
def test_parse_limits_refuse_from_the_header_and_stop_reading(
    tmp_path, monkeypatch, label, caps, build, needle, ceiling
):
    """Every limit constrains the PARSER: the refusal is raised from the
    offending header (or the first byte past the budget) with the metered
    stream proving the body — or the remainder — was never read, the
    failure is a typed CliInstallError, nothing is published at a non-dot
    path, staging is reclaimed, and the previously verified version is
    byte-for-byte untouched."""
    prior = make_tarball(claude_members(version="2.1.259"))
    ensure_cli_version(tmp_path, "claude", "2.1.259", platform_map(prior), fetch=fetching(prior))
    before = snapshot(tmp_path)
    for name, value in caps.items():
        monkeypatch.setattr(cliinstall, name, value)
    data = raw_tarball(build())
    assert len(data) < 64 * KIB  # the bombs are small on the wire
    streams = metered(monkeypatch, ceiling=ceiling)
    with pytest.raises(CliArchiveInvalid, match=needle) as excinfo:
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    assert isinstance(excinfo.value, CliInstallError)
    assert len(streams) == 1, "the extraction pass never opened"
    assert streams[0].consumed <= ceiling
    assert non_dot(tmp_path / "claude") == ["2.1.259"]
    assert not list((tmp_path / "claude").glob(".*"))
    assert snapshot(tmp_path) == before


def test_member_bodies_are_granted_beyond_the_overhead_allowance(tmp_path, monkeypatch):
    """The overhead allowance covers headers, records and padding only: a
    validated body is granted explicitly, so an archive whose binary dwarfs
    the allowance installs — through both bounded passes — while an
    unvalidated byte never could."""
    monkeypatch.setattr(cliinstall, "CLI_ARCHIVE_MAX_OVERHEAD_BYTES", 32 * KIB)
    data = raw_tarball(_big_first())
    streams = metered(monkeypatch)
    published = ensure_cli_version(
        tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data)
    )
    assert published.read_bytes() == BIG
    assert len(streams) == 2  # the validation pass, then the extraction pass
    assert all(stream.consumed > len(BIG) for stream in streams)
    assert non_dot(tmp_path / "claude") == [VERSION]


def test_the_extraction_pass_replays_exactly_the_validated_sequence(tmp_path, monkeypatch):
    """The second pass extracts nothing the first did not validate: a header
    that deviates from the validated entry at its position refuses, typed,
    with nothing published."""
    real = cliinstall._validate_archive

    def skewed(*args, **kwargs):
        plan = real(*args, **kwargs)
        first = dataclasses.replace(plan.entries[0], size=plan.entries[0].size + 1)
        return dataclasses.replace(plan, entries=(first, *plan.entries[1:]))

    monkeypatch.setattr(cliinstall, "_validate_archive", skewed)
    data = make_tarball(claude_members())
    with pytest.raises(CliArchiveInvalid, match="changed between validation and extraction"):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    assert non_dot(tmp_path / "claude") == []
    assert not list((tmp_path / "claude").glob(".*"))


def test_a_truncated_compressed_stream_is_archive_invalid_not_a_publish_failure(tmp_path):
    """A gzip stream cut mid-body is archive content gone wrong, classed
    with the archive gate (the staged file itself was readable)."""
    data = raw_tarball(_big_first())
    cut = data[: len(data) // 2]
    with pytest.raises(CliArchiveInvalid, match="compressed stream invalid"):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(cut), fetch=fetching(cut))
    assert non_dot(tmp_path / "claude") == []
    assert not list((tmp_path / "claude").glob(".*"))


# -- gate (d): the layout markers under their closed schemas ------------------------


MISMATCH = "does not match the contract"
NOT_PLAIN = "not a plain relative value"
CODEX_MARKER_CASES = [
    # (marker overrides, needle) — each refuses BEFORE any extraction.
    *[({field: DROP}, f"field {field!r} missing") for field in codex_marker()],
    ({"extra": "x"}, "outside the closed schema"),
    ({"layoutVersion": "1"}, "wrong type"),
    ({"layoutVersion": True}, "wrong type"),
    ({"layoutVersion": 1.0}, "wrong type"),
    ({"version": 1}, "wrong type"),
    ({"target": ["x"]}, "wrong type"),
    ({"variant": None}, "wrong type"),
    ({"entrypoint": 1}, "wrong type"),
    ({"resourcesDir": {}}, "wrong type"),
    ({"pathDir": 1}, "wrong type"),
    ({"layoutVersion": 2}, MISMATCH),
    ({"layoutVersion": 0}, MISMATCH),
    ({"version": CODEX_VERSION + "-linux-x64"}, MISMATCH),  # the platform suffix
    ({"version": "0.153.4"}, MISMATCH),  # another base version
    ({"target": ARM64}, MISMATCH),  # the other triple
    ({"target": "x86_64-unknown-linux-gnu"}, MISMATCH),
    ({"variant": "claude"}, MISMATCH),
    ({"entrypoint": "/bin/codex"}, NOT_PLAIN),
    ({"entrypoint": "../codex"}, NOT_PLAIN),
    ({"entrypoint": "bin/../bin/codex"}, NOT_PLAIN),
    ({"entrypoint": "bin\\codex"}, NOT_PLAIN),
    ({"entrypoint": "bin/codex\0"}, NOT_PLAIN),
    ({"entrypoint": ""}, NOT_PLAIN),
    ({"entrypoint": "codex-package.json"}, MISMATCH),  # outside the executable-tagged set
    ({"entrypoint": "bin/codex-code-mode-host"}, MISMATCH),  # executable, but not THE member
    ({"resourcesDir": "/codex-resources"}, NOT_PLAIN),
    ({"resourcesDir": "../codex-resources"}, NOT_PLAIN),
    ({"resourcesDir": "resources"}, MISMATCH),
    ({"pathDir": "/codex-path"}, NOT_PLAIN),
    ({"pathDir": "codex-path/.."}, NOT_PLAIN),
    ({"pathDir": "path"}, MISMATCH),
    ({"pathDir": ""}, NOT_PLAIN),
]


@pytest.mark.parametrize("overrides, needle", CODEX_MARKER_CASES)
def test_codex_layout_marker_mutations_refuse_before_extraction(tmp_path, overrides, needle):
    members = codex_members(marker=codex_marker(**overrides))
    data = make_tarball(members)
    with pytest.raises(CliArchiveInvalid, match=needle) as excinfo:
        install_codex(tmp_path, data)
    assert "codex-package.json" in str(excinfo.value)
    assert non_dot(tmp_path / "codex") == []
    assert not list((tmp_path / "codex").glob(".*"))


@pytest.mark.parametrize(
    "marker_bytes, needle",
    [
        (b"not json", "not a JSON document"),
        (b"\xff\xfe", "not a JSON document"),
        (b"[1, 2]", "not a JSON object"),
        (
            json.dumps({**codex_marker(), "pad": "x" * (cliinstall.CLI_MARKER_MAX_BYTES)}).encode(),
            "exceeds",
        ),
    ],
)
def test_codex_layout_marker_shape_refusals(tmp_path, marker_bytes, needle):
    """A marker that is not a JSON object, or one past CLI_MARKER_MAX_BYTES
    (refused on its declared size, never read whole), fails closed."""
    data = make_tarball(codex_members(marker_bytes=marker_bytes))
    with pytest.raises(CliArchiveInvalid, match=needle):
        install_codex(tmp_path, data)
    assert non_dot(tmp_path / "codex") == []


@pytest.mark.parametrize(
    "root_name, root_version, needle",
    [
        ("@openai/codex-cli", None, MISMATCH),  # a name other than the wrapper
        (CODEX_PACKAGE, CODEX_VERSION, MISMATCH),  # no platform suffix
        (CODEX_PACKAGE, CODEX_VERSION + "-linux-arm64", MISMATCH),  # the other architecture
        (CODEX_PACKAGE, "0.153.4-linux-x64", MISMATCH),  # another base version
    ],
)
def test_codex_root_package_json_is_bound_to_the_wrapper_and_platform_version(
    tmp_path, root_name, root_version, needle
):
    data = make_tarball(codex_members(root_name=root_name, root_version=root_version))
    with pytest.raises(CliArchiveInvalid, match=needle) as excinfo:
        install_codex(tmp_path, data)
    assert "'package/package.json'" in str(excinfo.value)
    assert non_dot(tmp_path / "codex") == []


@pytest.mark.parametrize(
    "marker_bytes, needle",
    [
        (json.dumps({"name": "@anthropic-ai/claude-code", "version": VERSION}).encode(), MISMATCH),
        (json.dumps({"name": PACKAGE, "version": "2.1.259"}).encode(), MISMATCH),
        (json.dumps({"version": VERSION}).encode(), "field 'name' missing"),
        (json.dumps({"name": PACKAGE}).encode(), "field 'version' missing"),
        (json.dumps({"name": 1, "version": VERSION}).encode(), "wrong type"),
        (json.dumps({"name": PACKAGE, "version": "/" + VERSION}).encode(), NOT_PLAIN),
        (b"[]", "not a JSON object"),
    ],
)
def test_claude_package_json_binds_name_and_version(tmp_path, marker_bytes, needle):
    data = make_tarball(claude_members(marker_bytes=marker_bytes))
    with pytest.raises(CliArchiveInvalid, match=needle):
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    assert non_dot(tmp_path / "claude") == []


# -- gates (e)-(g): extraction, assembly, publication -----------------------------------


def test_interrupted_extraction_publishes_nothing_and_a_retry_succeeds(tmp_path, monkeypatch):
    data = make_tarball(claude_members())
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


def _fail_marker_render(monkeypatch):
    real = Path.write_text

    def failing(self, *args, **kwargs):
        if self.name == "package.json":
            raise OSError(28, "injected ENOSPC")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing)


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
    (_fail_marker_render, "marker rendering"),
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
    data = make_tarball(claude_members())
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


def test_vendored_link_creation_failure_is_typed_and_publishes_nothing(tmp_path, monkeypatch):
    data = make_tarball(codex_members())

    def failing(target, link, *args, **kwargs):
        raise OSError(28, "injected ENOSPC")

    monkeypatch.setattr(cliinstall.os, "symlink", failing)
    with pytest.raises(CliPublishFailed, match="link creation") as excinfo:
        install_codex(tmp_path, data)
    assert isinstance(excinfo.value.__cause__, OSError)
    assert non_dot(tmp_path / "codex") == []
    assert not list((tmp_path / "codex").glob(".*"))


def test_broken_version_dir_is_retired_aside_and_reinstalled(tmp_path):
    data = make_tarball(claude_members())
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
    layout: host-side verification would be vouching for a target that can
    live outside the mounted cli tree and resolve differently (or dangle)
    inside the deck container. The symlink is retired aside like any broken
    install and the version reinstalled as a REAL directory; the external
    target is never served and never touched."""
    data = make_tarball(claude_members())
    external = tmp_path / "outside-the-tree"
    external.mkdir()
    for name in ("claude", "package.json", "LICENSE.md", "README.md"):
        (external / name).write_bytes(b"#!/bin/sh\necho decoy\n")
    (external / "claude").chmod(0o755)
    before = snapshot(external)
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
    assert snapshot(external) == before  # external target untouched


def test_symlinked_tool_root_is_refused_and_never_written_through(tmp_path):
    """The TOOL directory is a trust boundary: the fast path, mode
    normalization, the retire-aside rename, staging, and publication all
    resolve through it, so a symlinked tool root — which can point outside
    the mounted cli tree — is refused with the typed publish boundary before
    anything is read from, chmod'd through, or written under it. The repair
    lives in the daemon's converge pass (the link itself replaced with a
    real directory); after that the same call installs normally."""
    data = make_tarball(claude_members())
    external = tmp_path / "outside-the-tree"
    (external / VERSION).mkdir(parents=True)
    for name in ("claude", "package.json", "LICENSE.md", "README.md"):
        (external / VERSION / name).write_bytes(b"#!/bin/sh\necho decoy\n")
        (external / VERSION / name).chmod(0o700)  # the fast path would "repair" these
    before = snapshot(external)
    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    (cli_root / "claude").symlink_to(external)

    with pytest.raises(CliPublishFailed, match="tool directory is a symlink"):
        ensure_cli_version(cli_root, "claude", VERSION, platform_map(data), fetch=poisoned)
    # Refused, never accepted: the decoy was not served as the fast path or
    # chmod'd, and nothing was staged, retired, or published at the target.
    assert (cli_root / "claude").is_symlink()  # the link is left for the daemon repair
    assert snapshot(external) == before

    # The daemon-layer repair (a real directory in the link's place) makes a
    # RETRY of the identical call install normally.
    (cli_root / "claude").unlink()
    published = ensure_cli_version(
        cli_root, "claude", VERSION, platform_map(data), fetch=fetching(data)
    )
    assert not (cli_root / "claude").is_symlink()
    assert published.read_bytes() == BINARY
    assert snapshot(external) == before


def test_error_messages_never_carry_urls_or_member_contents(tmp_path):
    data = make_tarball([*claude_members(), ("package/evil.sh", b"SECRETBODY", 0o755)])
    with pytest.raises(CliArchiveInvalid) as excinfo:
        ensure_cli_version(tmp_path, "claude", VERSION, platform_map(data), fetch=fetching(data))
    assert "SECRETBODY" not in str(excinfo.value)
    assert "https://" not in str(excinfo.value)
    # A marker's own keys and values are archive content too: a refusal names
    # the SCHEMA field, never what the archive carried.
    marker = codex_marker(entrypoint="SECRETVALUE", SECRETKEY="x")
    data = make_tarball(codex_members(marker=marker))
    with pytest.raises(CliArchiveInvalid) as excinfo:
        install_codex(tmp_path, data)
    assert "SECRET" not in str(excinfo.value)
    assert "https://" not in str(excinfo.value)


def test_tuple_detection_on_this_host():
    """The real detection emits a key spelled from the supported set (the CI
    host is a supported linux tuple by construction)."""
    if platform.system() != "Linux":
        pytest.skip("tuple detection is linux-only by design")
    assert platform_tuple_key() in supported_tuple_keys()


def test_typed_errors_share_the_stable_base_class():
    for cls in (
        CliToolUnknown,
        CliContractInvalid,
        CliPlatformUnsupported,
        CliDownloadFailed,
        CliDownloadTooLarge,
        CliIntegrityMismatch,
        CliArchiveInvalid,
        CliPublishFailed,
    ):
        assert issubclass(cls, CliInstallError)
