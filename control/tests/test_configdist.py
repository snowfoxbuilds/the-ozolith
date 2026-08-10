"""Config-distribution packaging and the canonical hash (ADR-0042).

Covers the control side (theozolith_control.configdist) and pins it to the
node-side mirror (theozolith_nodedaemon.configdist) with a MANDATORY
cross-package contract test — the two implementations of the canonical hash
must agree byte-for-byte or convergence silently stalls.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from theozolith_control import configdist
from theozolith_nodedaemon import configdist as node_configdist


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
