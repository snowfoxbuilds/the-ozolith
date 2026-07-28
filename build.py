#!/usr/bin/env python3
"""Bootstrap build for a bare checkout (ADR-0023).

``theozolith build`` cannot be the first command — it presupposes an
installed CLI. This shim is the one sanctioned exception to "never a script
run out of the repo directory": it exists to end that state. It wraps the
SAME build implementation ``theozolith build`` wraps
(``theozolith_control.product.build_distribution`` — one implementation,
two entry paths; they cannot drift), builds every component wheel from the
CLEAN checkout into ``dist/``, and finishes by installing them, which
yields the ``theozolith`` and ``theozolith-control`` entry points. From
then on, source-based updates are ``theozolith build``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# The shared implementation, imported straight from the checkout: the
# product module is deliberately stdlib-only at import time (component
# separability), so a bare interpreter can run the build.
sys.path.insert(0, str(REPO_ROOT / "control" / "src"))

from theozolith_control.product import ProductError, build_distribution  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    out_dir = REPO_ROOT / "dist"
    try:
        version, wheels = build_distribution(REPO_ROOT, out_dir)
    except ProductError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Installing the built wheels (not the source trees) keeps the two entry
    # paths byte-identical: what this box runs is what nodes will pull.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            *(str(out_dir / name) for name in wheels),
        ],
        check=False,
    )
    if proc.returncode != 0:
        print("error: pip install of the built wheels failed", file=sys.stderr)
        return proc.returncode
    print(f"built and installed {len(wheels)} wheel(s) at version {version}")
    print("next: 'theozolith-control init' on the Control Node box, then 'theozolith build'")
    print("for future source updates (this shim was only the bootstrap).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
