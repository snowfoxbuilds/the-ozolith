"""Config-distribution packaging and the canonical hash (ADR-0042).

Covers the control side (theozolith_control.configdist) and pins it to the
node-side mirror (theozolith_nodedaemon.configdist) with a MANDATORY
cross-package contract test — the two implementations of the canonical hash
must agree byte-for-byte or convergence silently stalls.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from theozolith_control import configdist
from theozolith_nodedaemon import configdist as node_configdist


def _artifact_zip(members: dict[str, bytes], meta: dict | None, *, compression=zipfile.ZIP_STORED):
    """A config-distribution zip built from an explicit member map (plus the
    metadata member when ``meta`` is not None) — the escape hatch for the
    malformed shapes ``build_artifact`` would never itself produce."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        if meta is not None:
            archive.writestr(configdist.ARTIFACT_METADATA, json.dumps(meta).encode("utf-8"))
    return buf.getvalue()


def _corrupt_member_data(raw: bytes, name: str) -> bytes:
    """Corrupt the file-data region of ``name``, leaving the zip structurally
    openable but its member unreadable (bad CRC for STORED, a broken stream for
    DEFLATE). Flips every byte of the data region so a lenient decompressor
    cannot shrug it off."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        info = archive.getinfo(name)
    data_start = info.header_offset + 30 + len(name.encode()) + len(info.extra or b"")
    mutable = bytearray(raw)
    for offset in range(data_start, data_start + max(1, info.compress_size)):
        mutable[offset] ^= 0xFF
    return bytes(mutable)


def _write(root: Path, relpath: str, content: bytes | str) -> None:
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8")
    else:
        target.write_bytes(content)


def _populate(repo: Path) -> None:
    _write(repo, "drivers/custom/__init__.py", "print('hi')\n")
    _write(repo, "drivers/custom/impl.py", "def run():\n    return 1\n")
    _write(repo, "drivers/shared/util.py", "X = 2\n")


def test_missing_or_empty_drivers_hashes_to_empty(tmp_path):
    assert configdist.drivers_hash(tmp_path / "nope") == ""
    (tmp_path / "drivers").mkdir()
    assert configdist.drivers_hash(tmp_path) == ""  # dir present but no files
    # A tree of only excluded files is effectively empty.
    _write(tmp_path, "drivers/__pycache__/x.pyc", b"\x00")
    _write(tmp_path, "drivers/.hidden", "secret")
    assert configdist.drivers_hash(tmp_path) == ""


def test_manifest_is_content_only_and_order_independent(tmp_path):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _populate(repo_a)
    # Same content, files created in a different order, different mtimes.
    _write(repo_b, "drivers/shared/util.py", "X = 2\n")
    _write(repo_b, "drivers/custom/impl.py", "def run():\n    return 1\n")
    _write(repo_b, "drivers/custom/__init__.py", "print('hi')\n")
    for path in repo_b.rglob("*.py"):
        os.utime(path, (0, 0))  # zero the mtimes: the hash must not depend on them
    assert configdist.drivers_hash(repo_a) == configdist.drivers_hash(repo_b) != ""


def test_relpaths_include_the_drivers_prefix(tmp_path):
    _populate(tmp_path)
    entries = configdist.drivers_manifest(tmp_path)
    relpaths = [relpath for relpath, _ in entries]
    assert relpaths == sorted(relpaths)
    assert all(relpath.startswith("drivers/") for relpath in relpaths)
    assert "drivers/custom/impl.py" in relpaths


def test_exclusion_rules(tmp_path):
    _write(tmp_path, "drivers/mod/a.py", "a\n")
    _write(tmp_path, "drivers/mod/__pycache__/a.cpython-313.pyc", b"\x00")
    _write(tmp_path, "drivers/mod/b.pyc", b"\x00")
    _write(tmp_path, "drivers/.editor.swp", "junk")
    _write(tmp_path, "drivers/.hidden/keep.py", "nope\n")
    relpaths = [relpath for relpath, _ in configdist.drivers_manifest(tmp_path)]
    assert relpaths == ["drivers/mod/a.py"]


def test_symlink_is_a_packaging_error(tmp_path):
    _write(tmp_path, "drivers/real.py", "ok\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("escape", encoding="utf-8")
    os.symlink(outside, tmp_path / "drivers" / "link.py")
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_manifest(tmp_path)


def test_symlinked_directory_is_a_packaging_error(tmp_path):
    _write(tmp_path, "drivers/real.py", "ok\n")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "sneaky.py").write_text("x\n", encoding="utf-8")
    os.symlink(tmp_path / "elsewhere", tmp_path / "drivers" / "linkdir")
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_manifest(tmp_path)


def test_build_unpack_recompute_round_trip(tmp_path):
    _populate(tmp_path)
    expected = configdist.drivers_hash(tmp_path)
    out_dir = tmp_path / "artifacts"
    built, path = configdist.build_artifact(tmp_path, out_dir, built_against="1.2.3")
    assert built == expected
    assert path == out_dir / f"{expected}.zip"
    # Unpack and recompute the way a node verifies — never by archive bytes.
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    node_configdist.extract_zip(path.read_bytes(), unpacked)
    assert node_configdist.manifest_hash_of_tree(unpacked) == expected
    # The metadata member rides at the root and is NOT part of the manifest.
    with zipfile.ZipFile(path) as archive:
        assert configdist.ARTIFACT_METADATA in archive.namelist()
        import json

        meta = json.loads(archive.read(configdist.ARTIFACT_METADATA))
    assert meta["drivers_hash"] == expected
    assert meta["built_against"] == "1.2.3"
    assert meta["format"] == configdist.ARTIFACT_FORMAT


def test_build_with_no_drivers_yields_nothing(tmp_path):
    built, path = configdist.build_artifact(tmp_path, tmp_path / "out", built_against="1.0")
    assert built == "" and path is None


# -- the drivers root itself must fail closed (ADR-0042) ---------------------


def test_drivers_root_symlink_to_external_dir_is_rejected(tmp_path):
    """A ``drivers`` that is a symlink — even to a real directory — would
    package a whole tree from outside the repo. Refused at the root."""
    external = tmp_path / "external"
    external.mkdir()
    (external / "x.py").write_text("escape\n", encoding="utf-8")
    os.symlink(external, tmp_path / "drivers")
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_manifest(tmp_path)
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_hash(tmp_path)


def test_drivers_root_as_regular_file_is_rejected(tmp_path):
    """A ``drivers`` regular file must not read as the empty sentinel."""
    (tmp_path / "drivers").write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_hash(tmp_path)


def test_drivers_root_as_fifo_is_rejected(tmp_path):
    """A FIFO / socket / device at ``drivers`` is an irregular root — refused."""
    os.mkfifo(tmp_path / "drivers")
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_hash(tmp_path)


def test_missing_drivers_root_stays_the_empty_sentinel(tmp_path):
    """A genuinely absent ``drivers/`` is the no-distribution shape, not an
    error — the regression the root guard must not break."""
    assert configdist.drivers_hash(tmp_path) == ""
    assert configdist.drivers_manifest(tmp_path) == []


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0, reason="root bypasses file permissions"
)
def test_unreadable_drivers_file_is_config_dist_error(tmp_path):
    """An unreadable drivers/ file is normalized to ConfigDistError on BOTH
    sides — a packaging error control-side (so the config boundary can turn it
    into ConfigRepoError), a verification failure node-side (so the applied
    tree reads as non-converged and repairs)."""
    _write(tmp_path, "drivers/secret.py", "x = 1\n")
    target = tmp_path / "drivers" / "secret.py"
    os.chmod(target, 0)
    try:
        with pytest.raises(configdist.ConfigDistError):
            configdist.drivers_hash(tmp_path)
        with pytest.raises(node_configdist.ConfigDistError):
            node_configdist.manifest_hash_of_tree(tmp_path)
    finally:
        os.chmod(target, 0o644)


# -- artifact integrity: no poisoning past verify-by-recompute ---------------


def test_build_refuses_mutation_between_manifest_and_archive(tmp_path, monkeypatch):
    """A file that changes after the manifest is computed but before the archive
    is written must never publish an artifact whose bytes disagree with its
    hash. Simulated by a stale manifest: the byte snapshot detects the drift."""
    _populate(tmp_path)
    real = configdist.drivers_manifest(tmp_path)
    stale = [[relpath, "0" * 64] for relpath, _ in real]  # hashes no longer match disk
    monkeypatch.setattr(configdist, "drivers_manifest", lambda repo: stale)
    out = tmp_path / "out"
    with pytest.raises(configdist.ConfigDistError):
        configdist.build_artifact(tmp_path, out, built_against="1.0")
    assert not list(out.glob("*.zip"))  # nothing published, no torn tempfile
    assert not list(out.glob(".*"))


def test_verify_artifact_accepts_a_clean_build(tmp_path):
    _populate(tmp_path)
    expected = configdist.drivers_hash(tmp_path)
    _, path = configdist.build_artifact(tmp_path, tmp_path / "out", built_against="9.9")
    recomputed, meta = configdist.verify_artifact(path)
    assert recomputed == expected
    assert meta["drivers_hash"] == expected and meta["built_against"] == "9.9"


def test_verify_artifact_rejects_tampered_member(tmp_path):
    """A packaged file altered while the metadata still names the old hash is
    caught by unpack-and-recompute — never trusted by filename or bytes."""
    _populate(tmp_path)
    _, path = configdist.build_artifact(tmp_path, tmp_path / "out", built_against="1.0")
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tampered, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith("impl.py"):
                data = data + b"# injected\n"
            dst.writestr(info, data)
    with pytest.raises(configdist.ConfigDistError):
        configdist.verify_artifact(tampered)


def test_verify_artifact_rejects_bad_metadata_and_unsafe_members(tmp_path):
    _populate(tmp_path)
    entries = configdist.drivers_manifest(tmp_path)
    # A zip whose metadata format is wrong is rejected even if the tree is fine.
    import json

    bad_meta = tmp_path / "bad_meta.zip"
    with zipfile.ZipFile(bad_meta, "w") as z:
        for relpath, _ in entries:
            z.writestr(relpath, (tmp_path / relpath).read_bytes())
        z.writestr(
            configdist.ARTIFACT_METADATA,
            json.dumps({"format": 999, "drivers_hash": configdist.drivers_hash(tmp_path)}),
        )
    with pytest.raises(configdist.ConfigDistError):
        configdist.verify_artifact(bad_meta)
    # A traversal member is refused before any recompute.
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("drivers/ok.py", "ok\n")
        z.writestr("../escape.py", "pwned\n")
    with pytest.raises(configdist.ConfigDistError):
        configdist.verify_artifact(evil)


def test_control_and_node_safe_member_agree(tmp_path):
    """The control-side verifier and the node-side extractor apply the SAME
    member-safety rule (both must reject the same traversal names)."""
    for name in ("drivers/a.py", "../escape", "/abs", "C:\\win", "", "drivers/./x"):
        assert configdist.safe_member(name) == node_configdist.safe_member(name)


def test_prune_keeps_two(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    names = []
    for i in range(4):
        name = out / (f"{i:064d}"[:64] + ".zip")
        name.write_bytes(b"z")
        # Stagger mtimes so "most recent" is deterministic.
        os.utime(name, (100 + i, 100 + i))
        names.append(name)
    pruned = configdist.prune_config_artifacts(out, keep=2)
    survivors = sorted(p.name for p in out.iterdir())
    assert len(survivors) == 2
    assert names[2].name in survivors and names[3].name in survivors
    assert names[0].name in pruned and names[1].name in pruned


# -- the mandatory cross-package contract ------------------------------------


def test_cross_package_hash_agreement(tmp_path):
    """The control-side manifest hash and the node-side recompute-over-tree
    MUST agree on the identical fixture — the two stdlib implementations are
    pinned together here (nodedaemon cannot import theozolith_control)."""
    _populate(tmp_path)
    _write(tmp_path, "drivers/data/blob.bin", bytes(range(256)))
    _write(tmp_path, "drivers/nested/deep/leaf.py", "leaf = True\n")
    control_hash = configdist.drivers_hash(tmp_path)
    # manifest_hash_of_tree computes over <root>/drivers with relpaths relative
    # to <root> — identical to the control side pointed at the same repo.
    node_hash = node_configdist.manifest_hash_of_tree(tmp_path)
    assert control_hash == node_hash != ""


def test_node_rejects_zip_path_traversal(tmp_path):
    """A malicious member name is refused by explicit validation, never
    delegated to zipfile (ADR-0042)."""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("drivers/ok.py", "ok\n")
        archive.writestr("../escape.py", "pwned\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.extract_zip(evil.read_bytes(), dest)
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize(
    "name,ok",
    [
        ("drivers/a.py", True),
        ("drivers/sub/b.py", True),
        ("../escape", False),
        ("drivers/../../escape", False),
        ("/abs/path", False),
        ("C:\\win", False),
        ("", False),
        ("drivers/./x", False),
    ],
)
def test_safe_member(name, ok):
    assert node_configdist.safe_member(name) is ok


# -- the complete structural rule (ADR-0042) --------------------------------


def _clean_meta(tmp_path: Path) -> dict:
    return {
        "format": configdist.ARTIFACT_FORMAT,
        "drivers_hash": configdist.drivers_hash(tmp_path),
        "built_against": "1.0",
        "built_at": "1980-01-01T00:00:00+00:00",
    }


@pytest.mark.parametrize(
    "names,reason_fragment",
    [
        (["drivers/a.py", "config-dist.json", "extra.py"], "neither"),  # extra top-level file
        (["drivers/a.py", "drivers/b.pyc", "config-dist.json"], "ignored"),  # *.pyc
        (
            ["drivers/a.py", "drivers/__pycache__/a.pyc", "config-dist.json"],
            "ignored",
        ),  # __pycache__
        (["drivers/a.py", "drivers/.hidden.py", "config-dist.json"], "ignored"),  # dot member
        (["drivers/a.py", "config-dist.json", "config-dist.json"], "duplicate"),  # dup metadata
        (["drivers/a.py", "drivers/a.py", "config-dist.json"], "duplicate"),  # dup drivers member
        (["drivers/a.py"], "missing the"),  # no metadata member
        (["drivers/", "drivers/a.py", "config-dist.json"], "unsafe"),  # a directory entry
        (["a.py", "config-dist.json"], "neither"),  # bare top-level module
    ],
)
def test_artifact_structure_rule_rejections(names, reason_fragment):
    """Both sides' structural rule rejects the same shapes with the same reason,
    so a member the manifest would ignore can never ride an accepted archive."""
    control_reason = configdist.artifact_structure_error(names)
    node_reason = node_configdist.artifact_structure_error(names)
    assert control_reason == node_reason  # the two mirrors agree byte-for-byte
    assert control_reason is not None and reason_fragment in control_reason


def test_artifact_structure_rule_accepts_a_clean_shape():
    names = ["drivers/custom/impl.py", "drivers/shared/util.py", "config-dist.json"]
    assert configdist.artifact_structure_error(names) is None
    assert node_configdist.artifact_structure_error(names) is None


@pytest.mark.parametrize(
    "extra_name,extra_data",
    [
        ("extra.py", b"top-level module\n"),  # extra top-level file
        ("drivers/mod.pyc", b"\x00"),  # a *.pyc
        ("drivers/__pycache__/mod.cpython-313.pyc", b"\x00"),  # __pycache__
        ("drivers/.hidden.py", b"secret\n"),  # dot-prefixed member
    ],
)
def test_verify_and_extract_reject_ignored_members(tmp_path, extra_name, extra_data):
    """A verified/extracted artifact refuses any member the manifest ignores —
    control before publishing/serving, the node before extraction (ADR-0042)."""
    members = {"drivers/ok.py": b"ok = 1\n", extra_name: extra_data}
    raw = _artifact_zip(members, _clean_meta_for(members))
    control_zip = tmp_path / "art.zip"
    control_zip.write_bytes(raw)
    with pytest.raises(configdist.ConfigDistError):
        configdist.verify_artifact(control_zip)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.extract_zip(raw, dest)


def _clean_meta_for(members: dict[str, bytes]) -> dict:
    """Metadata whose drivers_hash matches ONLY the manifest-counted members —
    so a rejection is proven to come from the structural rule, not a hash
    mismatch (the ignored member never enters the manifest by construction)."""
    import hashlib

    entries = sorted(
        [name, hashlib.sha256(data).hexdigest()]
        for name, data in members.items()
        if name.startswith("drivers/")
        and not any(configdist.excluded_part(p) for p in name.split("/"))
    )
    digest = configdist.manifest_hash(entries)
    return {
        "format": configdist.ARTIFACT_FORMAT,
        "drivers_hash": digest,
        "built_against": "1.0",
        "built_at": "1980-01-01T00:00:00+00:00",
    }


def test_duplicate_config_dist_member_is_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("drivers/a.py", b"a = 1\n")
        archive.writestr(configdist.ARTIFACT_METADATA, json.dumps(_clean_meta(tmp_path)).encode())
        archive.writestr(configdist.ARTIFACT_METADATA, b"{}")  # a second one
    art = tmp_path / "dup.zip"
    art.write_bytes(buf.getvalue())
    with pytest.raises(configdist.ConfigDistError):
        configdist.verify_artifact(art)


def test_clean_control_built_artifact_verifies_on_both_sides(tmp_path):
    _populate(tmp_path)
    digest = configdist.drivers_hash(tmp_path)
    _, path = configdist.build_artifact(tmp_path, tmp_path / "out", built_against="1.0")
    # Control side: structural rule + recompute agree.
    recomputed, _ = configdist.verify_artifact(path)
    assert recomputed == digest
    # Node side: structural rule passes, extraction + recompute agree.
    dest = tmp_path / "dest"
    dest.mkdir()
    node_configdist.extract_zip(path.read_bytes(), dest)
    assert node_configdist.manifest_hash_of_tree(dest) == digest


# -- verification failures are ALL ConfigDistError (ADR-0042) ----------------


def test_verify_normalizes_bad_crc_member(tmp_path):
    """A zip that opens fine but whose stored member fails its CRC on read is a
    ConfigDistError, not a leaked BadZipFile."""
    members = {"drivers/a.py": b"payload-bytes-to-flip\n"}
    raw = _artifact_zip(members, _clean_meta_for(members), compression=zipfile.ZIP_STORED)
    corrupt = _corrupt_member_data(raw, "drivers/a.py")
    art = tmp_path / "badcrc.zip"
    art.write_bytes(corrupt)
    with pytest.raises(configdist.ConfigDistError):
        configdist.verify_artifact(art)


def test_verify_normalizes_malformed_compressed_member(tmp_path):
    """A member declared DEFLATE whose compressed bytes are garbage surfaces as
    ConfigDistError, not a leaked zlib.error/BadZipFile."""
    members = {"drivers/a.py": b"payload-bytes-to-flip-deflated\n" * 4}
    raw = _artifact_zip(members, _clean_meta_for(members), compression=zipfile.ZIP_DEFLATED)
    corrupt = _corrupt_member_data(raw, "drivers/a.py")
    art = tmp_path / "baddeflate.zip"
    art.write_bytes(corrupt)
    with pytest.raises(configdist.ConfigDistError):
        configdist.verify_artifact(art)


def test_node_extract_normalizes_bad_crc_member(tmp_path):
    members = {"drivers/a.py": b"payload-bytes-to-flip\n"}
    raw = _artifact_zip(members, _clean_meta_for(members), compression=zipfile.ZIP_STORED)
    corrupt = _corrupt_member_data(raw, "drivers/a.py")
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.extract_zip(corrupt, dest)


# -- fail closed on directory-enumeration errors (ADR-0042) ------------------


def test_root_enumeration_failure_fails_closed(tmp_path, monkeypatch):
    """An injected failure to LIST the drivers root raises ConfigDistError under
    refuse_irregular — never a silently truncated manifest. Injected, so the
    test holds under root (chmod would not)."""
    _write(tmp_path, "drivers/a.py", "a = 1\n")
    root = tmp_path / "drivers"
    real_scandir = os.scandir

    def boom(path):
        if Path(path) == root:
            raise OSError(13, "injected: cannot list drivers root")
        return real_scandir(path)

    monkeypatch.setattr(configdist.os, "scandir", boom)
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_hash(tmp_path)


def test_nested_enumeration_failure_fails_closed(tmp_path, monkeypatch):
    """An injected failure to list a nested included directory also fails closed
    — the subtree is never silently omitted."""
    _write(tmp_path, "drivers/top.py", "t = 1\n")
    _write(tmp_path, "drivers/sub/leaf.py", "leaf = 1\n")
    nested = tmp_path / "drivers" / "sub"
    real_scandir = os.scandir

    def boom(path):
        if Path(path) == nested:
            raise OSError(13, "injected: cannot list nested dir")
        return real_scandir(path)

    monkeypatch.setattr(configdist.os, "scandir", boom)
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_manifest(tmp_path)


def test_enumeration_failure_becomes_config_repo_error_at_load(tmp_path, monkeypatch):
    """The ConfigDistError from a fail-closed enumeration is normalized to
    ConfigRepoError at the config-loading boundary, and nothing is published."""
    from theozolith_control import configrepo

    _write(tmp_path, "drivers/a.py", "a = 1\n")
    root = tmp_path / "drivers"
    real_scandir = os.scandir

    def boom(path):
        if Path(path) == root:
            raise OSError(13, "injected: cannot list drivers root")
        return real_scandir(path)

    monkeypatch.setattr(configdist.os, "scandir", boom)
    with pytest.raises(configrepo.ConfigRepoError):
        configrepo.load_config(tmp_path)
    out = tmp_path / "out"
    with pytest.raises(configdist.ConfigDistError):
        configdist.build_artifact(tmp_path, out, built_against="1.0")
    assert not out.exists() or not list(out.glob("*.zip"))  # no partial artifact


def test_folder_mode_enumeration_stays_non_fail_closed(tmp_path, monkeypatch):
    """Folder-mode commit hashing (refuse_irregular=False) keeps its broader
    posture: an unreadable subtree is skipped, not fail-closed (ADR-0042)."""
    _write(tmp_path, "top.py", "t = 1\n")
    _write(tmp_path, "sub/leaf.py", "leaf = 1\n")
    nested = tmp_path / "sub"
    real_scandir = os.scandir

    def boom(path):
        if Path(path) == nested:
            raise OSError(13, "injected: cannot list nested dir")
        return real_scandir(path)

    monkeypatch.setattr(configdist.os, "scandir", boom)
    # No refuse_irregular: the unreadable subtree is skipped, top.py still found.
    found = configdist.regular_files(tmp_path)
    assert (tmp_path / "top.py") in found


# -- the applied tree fails closed on BOTH sides (ADR-0042 amendment) ---------


def test_cross_package_missing_root_is_the_empty_sentinel_on_both_sides(tmp_path):
    """Only a genuinely MISSING drivers root is the empty distribution; both
    implementations agree (the node's dist root holds config-dist.json but no
    drivers/ — same shape, same answer)."""
    (tmp_path / "config-dist.json").write_text("{}", encoding="utf-8")
    assert configdist.drivers_hash(tmp_path) == ""
    assert node_configdist.manifest_hash_of_tree(tmp_path) == ""


@pytest.mark.parametrize("plant", ["file_symlink", "dir_symlink", "fifo"])
def test_cross_package_fail_closed_on_irregular_descendants(tmp_path, plant):
    """Both implementations refuse the same irregular shapes below the drivers
    root — a symlinked file, a symlinked directory, a FIFO — rather than
    skipping them: an executable filesystem entry either participates in the
    hash or fails verification, never unhashed-but-present (ADR-0042)."""
    _write(tmp_path, "drivers/custom/impl.py", "x = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("evil = 1\n", encoding="utf-8")
    planted = tmp_path / "drivers" / "custom" / "planted"
    if plant == "file_symlink":
        planted.symlink_to(outside / "evil.py")
    elif plant == "dir_symlink":
        planted.symlink_to(outside)
    else:
        os.mkfifo(planted)
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_hash(tmp_path)
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.manifest_hash_of_tree(tmp_path)


@pytest.mark.parametrize("shape", ["symlink", "regular_file"])
def test_cross_package_fail_closed_on_irregular_drivers_root(tmp_path, shape):
    """A drivers root that is a symlink (even to a real directory of real
    files) or a regular file is refused by BOTH implementations — a pointer or
    directory name is never integrity evidence by itself (ADR-0042)."""
    root = tmp_path / "drivers"
    if shape == "symlink":
        external = tmp_path / "external"
        external.mkdir()
        (external / "impl.py").write_text("x = 1\n", encoding="utf-8")
        root.symlink_to(external)
    else:
        root.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_hash(tmp_path)
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.manifest_hash_of_tree(tmp_path)


def test_cross_package_fail_closed_on_enumeration_failure(tmp_path, monkeypatch):
    """The node's recompute fails closed on an unenumerable included subtree
    exactly like control's manifest — never a silently truncated hash. Injected
    via os.scandir, so the test holds under root."""
    _write(tmp_path, "drivers/top.py", "t = 1\n")
    _write(tmp_path, "drivers/sub/leaf.py", "leaf = 1\n")
    nested = tmp_path / "drivers" / "sub"
    real_scandir = os.scandir

    def boom(path):
        if Path(path) == nested:
            raise OSError(13, "injected: cannot list nested dir")
        return real_scandir(path)

    monkeypatch.setattr(configdist.os, "scandir", boom)
    with pytest.raises(configdist.ConfigDistError):
        configdist.drivers_manifest(tmp_path)
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.manifest_hash_of_tree(tmp_path)


def test_cross_package_agreement_survives_excluded_irregular_entries(tmp_path):
    """Excluded names (dot-prefixed) are tolerated whatever they are — a
    dot-prefixed symlink is skipped by BOTH sides before its shape is examined,
    so the two hashes still agree on the clean content."""
    _populate(tmp_path)
    clean_control = configdist.drivers_hash(tmp_path)
    clean_node = node_configdist.manifest_hash_of_tree(tmp_path)
    (tmp_path / "drivers" / ".editor-swap").symlink_to(tmp_path / "outside-nowhere")
    assert configdist.drivers_hash(tmp_path) == clean_control
    assert node_configdist.manifest_hash_of_tree(tmp_path) == clean_node
