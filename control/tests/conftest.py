"""Fixture wiring only — the rig lives in controlrig.py (see its docstring
for why it is not named conftest)."""

from controlrig import control, github  # noqa: F401  (fixture re-exports)
