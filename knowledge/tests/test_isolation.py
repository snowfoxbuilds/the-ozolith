"""Acceptance 5: knowledge/ installs and runs with zero dependencies on
cluster components (the laptop-only user path, ADR-0007).

CI additionally installs knowledge/ alone in a fresh venv and runs this whole
suite against it (the `knowledge-isolated` job).
"""

from __future__ import annotations

import subprocess
import sys
import tomllib

from treeutil import PACKAGE_ROOT


def test_no_runtime_dependencies_at_all():
    pyproject = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == []


def test_no_console_script_reaches_cluster_components():
    scripts = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert all(target.startswith("theozolith_knowledge.") for target in scripts.values())


def test_import_pulls_in_no_other_theozolith_package():
    code = (
        "import sys\n"
        "import theozolith_knowledge.cli, theozolith_knowledge.bake\n"
        "bad = [m for m in sys.modules\n"
        "       if m.startswith('theozolith') and not m.startswith('theozolith_knowledge')]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
