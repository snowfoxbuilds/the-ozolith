Status: ACCEPTED

Date: 2026-07-28

Provenance: delegated decision from the M7 brief; implements ADR-0023's bootstrap-from-source contract.

# ADR-0030: build.py / `theozolith build` shared-implementation boundary

## Context

A fresh checkout bootstraps with `python3 build.py` — a thin shim over the same build implementation `theozolith build` wraps (one implementation, two entry paths — they cannot drift), finishing by installing the `theozolith`/`theozolith-control` entry points. The module boundary was delegated.

## Decision

- **The shared implementation is `theozolith_control.product`** — specifically `build_distribution()` (which calls `source_version()`: clean-tree check, `<base>+g<sha12>` pin, per-component wheel builds). It already exists, is the exact code `theozolith build` runs, and is deliberately import-clean: stdlib-only at module import time (the existing component-separability test pins this), so a bare interpreter can import it straight from the checkout.
- **`build.py` (repo root) contains no build logic**: it inserts `control/src` on `sys.path`, imports `build_distribution`, builds every component wheel into `dist/`, and pip-installs the built wheels (not the source trees — what this box runs is byte-identical to what nodes will pull). It is the sole sanctioned exception to "never a script run out of the repo directory" and exists to end that state.
- **The identity is test-asserted**: a control test verifies `build.py` resolves to the same function object as the module's, so a copy-paste fork fails CI, not a deployment.

## Alternatives rejected

- **A dedicated `buildimpl` module extracted from product.py**: a module boundary invented purely for the shim; product.py's import-cleanliness already provides the property the split would buy.
- **build.py shelling out to `pip install ./control && theozolith build`**: installs source trees unversioned first, and needs a running Control Node for the upload half — the bootstrap box may be the Control Node being born.
- **Duplicated build steps in build.py**: exactly the drift ADR-0023 forbids.
