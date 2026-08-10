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


def test_prune_keep_zero_removes_everything(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for i in range(3):
        target = out / (f"{i:064d}"[:64] + ".zip")
        target.write_bytes(b"z")
        os.utime(target, (100 + i, 100 + i))
    pruned = configdist.prune_config_artifacts(out, keep=0)
    assert len(pruned) == 3
    assert not [p for p in out.iterdir() if p.suffix == ".zip"]


# -- race-safe keep-two pruning (ADR-0042 amendment) --------------------------


def test_prune_tolerates_entry_vanishing_before_mtime_lookup(tmp_path, monkeypatch):
    """DETERMINISTIC concurrent-churn injection: a collected entry is unlinked
    between enumeration and its stat — exactly what a racing pruner or a
    replacement does. Pruning must complete without raising, skip the vanished
    entry (it is not reported as pruned: this pruner never removed it), and
    still prune the genuine stale survivor deterministically."""
    out = tmp_path / "out"
    out.mkdir()
    names = []
    for i in range(4):
        target = out / (f"{i:064d}"[:64] + ".zip")
        target.write_bytes(b"z")
        os.utime(target, (100 + i, 100 + i))
        names.append(target)
    victim = names[0]
    state = {"vanished": False}
    real_stat = Path.stat

    def racing_stat(self, **kwargs):
        if not state["vanished"] and self.name == victim.name and self.parent == out:
            state["vanished"] = True
            os.unlink(victim)  # a concurrent pruner takes it first
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", racing_stat)
    pruned = configdist.prune_config_artifacts(out, keep=2)
    monkeypatch.undo()
    assert state["vanished"]  # the race really fired inside the collection loop
    assert pruned == [names[1].name]  # the vanished entry is never claimed
    survivors = sorted(p.name for p in out.iterdir())
    assert survivors == sorted([names[2].name, names[3].name])


def test_prune_tolerates_entry_vanishing_before_unlink(tmp_path, monkeypatch):
    """The second race window: a selected stale entry vanishes between the
    sort and this pruner's unlink (two prune operations racing). The deletion
    failure is ordinary churn — suppressed, not raised — and the entry is not
    reported as pruned by the loser."""
    out = tmp_path / "out"
    out.mkdir()
    names = []
    for i in range(4):
        target = out / (f"{i:064d}"[:64] + ".zip")
        target.write_bytes(b"z")
        os.utime(target, (100 + i, 100 + i))
        names.append(target)
    victim = names[0]
    state = {"vanished": False}
    real_unlink = Path.unlink

    def racing_unlink(self, missing_ok=False):
        if not state["vanished"] and self.name == victim.name:
            state["vanished"] = True
            os.unlink(victim)  # the other pruner wins the race
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", racing_unlink)
    pruned = configdist.prune_config_artifacts(out, keep=2)
    monkeypatch.undo()
    assert state["vanished"]
    assert pruned == [names[1].name]  # only what THIS pruner actually removed
    survivors = sorted(p.name for p in out.iterdir())
    assert survivors == sorted([names[2].name, names[3].name])


def test_racing_prunes_settle_to_keep_two(tmp_path):
    """Two pruners over the same churning cache complete without raising and
    the survivors settle to the keep-two set."""
    import threading

    out = tmp_path / "out"
    out.mkdir()
    for i in range(6):
        target = out / (f"{i:064d}"[:64] + ".zip")
        target.write_bytes(b"z")
        os.utime(target, (100 + i, 100 + i))
    errors: list[BaseException] = []

    def prune() -> None:
        try:
            configdist.prune_config_artifacts(out, keep=2)
        except BaseException as exc:  # pragma: no cover - the assertion target
            errors.append(exc)

    threads = [threading.Thread(target=prune) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    survivors = sorted(p.name for p in out.iterdir())
    assert survivors == sorted(f"{i:064d}"[:64] + ".zip" for i in (4, 5))


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


# -- the complete metadata envelope (ADR-0042 amendment) ----------------------


def _artifact_zip_raw_meta(members: dict[str, bytes], raw_meta: bytes) -> bytes:
    """A structurally valid artifact whose ``config-dist.json`` member carries
    ``raw_meta`` verbatim — the escape hatch for envelope bytes ``json.dumps``
    could never produce (invalid UTF-8, malformed JSON)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        archive.writestr(configdist.ARTIFACT_METADATA, raw_meta)
    return buf.getvalue()


def test_verify_normalizes_invalid_utf8_metadata():
    """``config-dist.json`` holding invalid UTF-8 inside a structurally valid
    zip is a ConfigDistError — never a leaked UnicodeDecodeError (the raw
    data-format exception must not escape the config-distribution boundary)."""
    members = {"drivers/a.py": b"ok = 1\n"}
    raw = _artifact_zip_raw_meta(members, b"\xff\xfe not utf-8 {")
    with pytest.raises(configdist.ConfigDistError):
        configdist.verify_artifact_bytes(raw)


_ENVELOPE_HASH = "ab" * 32


@pytest.mark.parametrize(
    "raw_meta",
    [
        b"\xff\xfe not utf-8 {",  # invalid UTF-8
        b"{ not json",  # valid UTF-8, malformed JSON
        b"42",  # a JSON scalar, not an object
        b"[]",  # a JSON array, not an object
        json.dumps({"format": 999, "drivers_hash": _ENVELOPE_HASH}).encode(),  # wrong format
        json.dumps({"format": True, "drivers_hash": _ENVELOPE_HASH}).encode(),  # bool is not 1
        json.dumps({"drivers_hash": _ENVELOPE_HASH}).encode(),  # format absent
        json.dumps({"format": 1}).encode(),  # drivers_hash absent
        json.dumps({"format": 1, "drivers_hash": 7}).encode(),  # non-string hash
        json.dumps({"format": 1, "drivers_hash": "0" * 64}).encode(),  # mismatching hash
        b"[" * 4000,  # pathologically nested JSON (RecursionError normalized)
    ],
)
def test_cross_package_metadata_envelope_rejections(raw_meta):
    """Both sides' envelope rule rejects the same malformed shapes with
    ConfigDistError and only ConfigDistError — control before publishing or
    serving, the node before stopping a driver or exchanging a tree."""
    with pytest.raises(configdist.ConfigDistError):
        configdist.validate_metadata_bytes(raw_meta, _ENVELOPE_HASH)
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.validate_metadata_bytes(raw_meta, _ENVELOPE_HASH)


def test_cross_package_metadata_envelope_acceptance_and_advisory_stamp():
    """A valid envelope is accepted identically on both sides; ``built_against``
    stays advisory — present-and-string is returned, absent or non-string reads
    as the empty stamp, never a rejection (ADR-0042)."""
    stamped = json.dumps(
        {"format": 1, "drivers_hash": _ENVELOPE_HASH, "built_against": "0.3.0"}
    ).encode()
    assert configdist.validate_metadata_bytes(stamped, _ENVELOPE_HASH)["built_against"] == "0.3.0"
    meta = node_configdist.validate_metadata_bytes(stamped, _ENVELOPE_HASH)
    assert node_configdist.advisory_built_against(meta) == "0.3.0"
    for advisory_empty in (
        json.dumps({"format": 1, "drivers_hash": _ENVELOPE_HASH}).encode(),
        json.dumps({"format": 1, "drivers_hash": _ENVELOPE_HASH, "built_against": 7}).encode(),
    ):
        assert configdist.validate_metadata_bytes(advisory_empty, _ENVELOPE_HASH)
        meta = node_configdist.validate_metadata_bytes(advisory_empty, _ENVELOPE_HASH)
        assert node_configdist.advisory_built_against(meta) == ""


def test_node_validate_metadata_tree_normalizes_all_failures(tmp_path):
    """The node's tree-level envelope validation: a missing metadata file, raw
    invalid-UTF-8 bytes on disk, and a foreign envelope all normalize to
    ConfigDistError — the callers (staging verify, applied-tree re-verify)
    treat every one as non-converged, never as a crash."""
    recomputed = node_configdist.manifest_hash_of_tree(tmp_path)  # empty tree: ""
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.validate_metadata_tree(tmp_path, recomputed)  # missing file
    meta_path = tmp_path / node_configdist.ARTIFACT_METADATA
    meta_path.write_bytes(b"\xff\xfe not utf-8 {")
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.validate_metadata_tree(tmp_path, recomputed)
    meta_path.write_text(json.dumps({"format": 1, "drivers_hash": "0" * 64}), encoding="utf-8")
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.validate_metadata_tree(tmp_path, recomputed)


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


# -- per-entry classifier failures normalize like enumeration (ADR-0042) ------
#
# DirEntry.is_symlink/is_dir/is_file can each raise OSError AFTER a successful
# scandir (the metadata stat is lazy). The posture split mirrors enumeration:
# the drivers manifest fails closed with ConfigDistError, folder-mode commit
# hashing skips the entry, and the node's recompute rejects like the manifest.


class _UnstatableEntry:
    """A DirEntry stand-in whose metadata classifiers raise OSError — the
    stat-after-scandir failure mode. ``name``/``path`` still read fine; only
    classification fails."""

    def __init__(self, entry):
        self.name = entry.name
        self.path = entry.path

    def is_symlink(self):
        raise OSError(5, "injected: cannot stat entry")

    def is_dir(self, follow_symlinks=True):
        raise OSError(5, "injected: cannot stat entry")

    def is_file(self, follow_symlinks=True):
        raise OSError(5, "injected: cannot stat entry")


def _boom_entry_classifiers(monkeypatch, target: Path) -> None:
    """Patch ``os.scandir`` so the single entry at ``target`` fails
    classification while enumeration itself still succeeds; every other
    directory (and every fd-based scandir) passes through untouched."""
    import contextlib

    real_scandir = os.scandir

    @contextlib.contextmanager
    def fake(path="."):
        with real_scandir(path) as it:
            entries = list(it)
        if isinstance(path, (str, os.PathLike)):
            entries = [_UnstatableEntry(e) if Path(e.path) == target else e for e in entries]
        yield iter(entries)

    monkeypatch.setattr(os, "scandir", fake)


def test_root_entry_classifier_failure_fails_closed(tmp_path, monkeypatch):
    """An entry directly under the drivers root whose metadata cannot be read
    raises ConfigDistError under refuse_irregular — an unclassifiable entry
    must never silently drop from the manifest."""
    _write(tmp_path, "drivers/a.py", "a = 1\n")
    _boom_entry_classifiers(monkeypatch, tmp_path / "drivers" / "a.py")
    with pytest.raises(configdist.ConfigDistError, match="cannot classify"):
        configdist.drivers_hash(tmp_path)


@pytest.mark.parametrize("target_rel", ["sub", "sub/leaf.py"])
def test_nested_entry_classifier_failure_fails_closed(tmp_path, monkeypatch, target_rel):
    """A classifier failure on a nested directory entry or a nested file also
    fails closed — neither the subtree nor the file is silently omitted."""
    _write(tmp_path, "drivers/top.py", "t = 1\n")
    _write(tmp_path, "drivers/sub/leaf.py", "leaf = 1\n")
    _boom_entry_classifiers(monkeypatch, tmp_path / "drivers" / target_rel)
    with pytest.raises(configdist.ConfigDistError, match="cannot classify"):
        configdist.drivers_manifest(tmp_path)


def test_entry_classifier_failure_becomes_config_repo_error_at_load(tmp_path, monkeypatch):
    """The ConfigDistError from a per-entry classifier failure is normalized to
    ConfigRepoError at the config-loading boundary — control config loading
    sees the documented error type, never a raw OSError."""
    from theozolith_control import configrepo

    _write(tmp_path, "drivers/a.py", "a = 1\n")
    _boom_entry_classifiers(monkeypatch, tmp_path / "drivers" / "a.py")
    with pytest.raises(configrepo.ConfigRepoError, match="cannot classify"):
        configrepo.load_config(tmp_path)


def test_folder_mode_entry_classifier_failure_is_skipped(tmp_path, monkeypatch):
    """Folder-mode commit hashing keeps its non-fail-closed contract: an entry
    whose metadata cannot be classified is skipped (never descended), not an
    abort of the commit calculation."""
    _write(tmp_path, "top.py", "t = 1\n")
    _write(tmp_path, "sub/leaf.py", "leaf = 1\n")
    _boom_entry_classifiers(monkeypatch, tmp_path / "sub")
    found = configdist.regular_files(tmp_path)
    assert (tmp_path / "top.py") in found
    assert not any(p.name == "leaf.py" for p in found)  # skipped, never descended


def test_cross_package_entry_classifier_failure_fails_closed(tmp_path, monkeypatch):
    """The node's recompute normalizes a per-entry classifier OSError into its
    ConfigDistError exactly like control's manifest — the manifest/staging
    boundary rejects, never leaks a raw OSError."""
    _write(tmp_path, "drivers/custom/impl.py", "x = 1\n")
    _boom_entry_classifiers(monkeypatch, tmp_path / "drivers" / "custom" / "impl.py")
    with pytest.raises(configdist.ConfigDistError, match="cannot classify"):
        configdist.drivers_hash(tmp_path)
    with pytest.raises(node_configdist.ConfigDistError, match="cannot classify"):
        node_configdist.manifest_hash_of_tree(tmp_path)


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


def test_source_exclusion_and_applied_tree_validation_are_different_layers(tmp_path):
    """Source exclusion and applied-tree validation are DIFFERENT LAYERS
    (ADR-0042 amendment). Control-side SOURCE selection skips excluded names
    whatever their shape — a working repo legitimately holds editor droppings,
    so a dot-prefixed symlink never changes the packaged hash. The node
    verifies the PRODUCT OF AN EXTRACTION, and an accepted artifact can never
    contain an excluded member — so the very same entry FAILS node-side
    verification instead of riding unhashed beside a converged report."""
    _populate(tmp_path)
    clean_control = configdist.drivers_hash(tmp_path)
    assert node_configdist.manifest_hash_of_tree(tmp_path) == clean_control  # clean agreement
    (tmp_path / "drivers" / ".editor-swap").symlink_to(tmp_path / "outside-nowhere")
    assert configdist.drivers_hash(tmp_path) == clean_control  # source layer: skipped
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.manifest_hash_of_tree(tmp_path)  # applied layer: rejected


@pytest.mark.parametrize(
    "plant",
    ["evil.pyc", "__pycache__/evil.pyc", ".hidden.py", ".hidden-dir/x.py"],
)
def test_node_applied_tree_rejects_excluded_names_the_source_manifest_skips(tmp_path, plant):
    """Every excluded-name shape — an importable ``*.pyc``, a ``__pycache__``d
    ``*.pyc``, a dot-prefixed file, a file under a dot-prefixed directory —
    stays invisible to the control-side SOURCE manifest (hash unchanged) yet
    fails the node's post-extraction validation: no member the artifact rule
    rejects can be planted into an applied tree while preserving a converged
    report."""
    _populate(tmp_path)
    clean_control = configdist.drivers_hash(tmp_path)
    _write(tmp_path, f"drivers/custom/{plant}", b"\x00evil")
    assert configdist.drivers_hash(tmp_path) == clean_control  # source layer: skipped
    with pytest.raises(node_configdist.ConfigDistError):
        node_configdist.manifest_hash_of_tree(tmp_path)  # applied layer: rejected
