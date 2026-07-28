"""build.py, the bootstrap shim (ADR-0023/0030, acceptance 1): no build
logic of its own — the same implementation `theozolith build` wraps."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from theozolith_control import product

REPO_ROOT = Path(__file__).parents[2]


def _load_shim():
    spec = importlib.util.spec_from_file_location("build_shim", REPO_ROOT / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_py_wraps_the_exact_same_implementation():
    """One implementation, two entry paths — identity-asserted so a
    copy-paste fork fails here, not in a deployment."""
    shim = _load_shim()
    assert shim.build_distribution is product.build_distribution
    # The shim finishes by installing the built wheels (the entry points),
    # and adds no second build pipeline.
    source = (REPO_ROOT / "build.py").read_text()
    assert "pip" in source and "install" in source
    assert "pip wheel" not in source  # building is product.py's job alone


def test_the_shared_module_is_importable_without_dependencies():
    """The bare-checkout property: theozolith_control.product imports
    stdlib-only at module import time (also pinned by the separability test
    in test_product.py) — a bare interpreter can run the bootstrap build."""
    source = (REPO_ROOT / "control" / "src" / "theozolith_control" / "product.py").read_text()
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            module = line.split()[1]
            assert not module.startswith(("fastapi", "uvicorn", "cryptography", "jinja2")), line
