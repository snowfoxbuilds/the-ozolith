"""Fixture wiring only — the rig lives in daemonrig.py.

Test modules import helpers from `daemonrig` by that unique name: a module
literally named `conftest` is order-dependent across a multi-directory
pytest run (sys.modules holds exactly one "conftest"), and worker/tests
already claims that name.
"""

from daemonrig import rig  # noqa: F401  (fixture re-export)
