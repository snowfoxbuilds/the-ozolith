Status: ACCEPTED

Date: 2026-08-05

Provenance: interactive ruling 2026-08-05 — the venv-first bootstrap was operator-managed friction on PEP 668 distros. Amends ADR-0023's bootstrap-from-source bullet and ADR-0030's shim description (the entry contract `python3 build.py` is unchanged); consumes ADR-0034 (world-reachable system-path exec policy) and ADR-0037's refuse-with-remediation posture.

# ADR-0041: build.py owns the bootstrap environment

## Context

Modern Debian/Ubuntu interpreters are externally managed (PEP 668): `pip install` into the distro Python refuses outright. The documented bootstrap therefore grew three manual environment steps — `sudo python3 -m venv /opt/theozolith`, run build.py with that venv's interpreter, symlink the CLI into `/usr/local/bin` — leaking environment management into the operator surface. The node-shaped install never had this problem: `install-nodedaemon.sh` builds the `/opt/theozolith` venv itself. The control-shaped bootstrap should own its environment the same way.

## Decision

- **The entry contract is unchanged**: a fresh checkout bootstraps with `python3 build.py` (ADR-0023/0030) — same file, same command, still the sole sanctioned exception to "never a script run out of the repo directory". What changes is that the command is now complete: `sudo python3 build.py` is the whole bootstrap.
- **The shim owns the managed environment.** Run outside the target venv it pre-flights (root for the managed default; venv capability), creates-or-reuses `/opt/theozolith` — the same layout `install-nodedaemon.sh` builds on nodes, a world-reachable system path per ADR-0034's exec policy — and **re-executes itself with the venv's interpreter**. The build and install then run inside the venv unchanged, and the shim finishes by linking `theozolith`, `theozolith-control`, and `theozolith-nodedaemon` into `/usr/local/bin` (the nodedaemon CLI because `theozolith init --with-local-node` resolves it from PATH, ADR-0037). A marker environment variable turns a venv whose interpreter fails to identify as the venv into a hard error instead of an exec loop.
- **Re-exec, not system pip.** Before the re-exec the shim needs only the stdlib `venv` module; every pip invocation happens inside the venv, so PEP 668 is never encountered and the only OS prerequisite beyond `python3 >= 3.11` is the distro's `python3-venv` package. A venv-incapable interpreter is refused with that exact remediation — the shim never package-manages on its own (the ADR-0037 posture, extended: a root bootstrap path never runs apt either).
- **`--venv PATH` is the unmanaged escape hatch** (dev checkouts, tests): same build and install into the named venv, no root requirement, no `/usr/local/bin` links.
- **The ADR-0030 boundary holds.** Environment management is install-side bootstrap logic and lives in the shim; build logic remains `product.build_distribution` alone, and the identity test is untouched. An existing non-venv directory at the target is refused, never overwritten.

## Consequences

- **Positive**: `git clone` → `sudo python3 build.py` is the complete first install — the operator never creates, activates, or names a venv. ADR-0037's pre-flight claim ("the bare-metal build installs all four distributions into one venv") is now made true by the tool rather than by operator diligence, and control-shaped and node-shaped installs share one `/opt/theozolith` layout.
- **Negative**: the shim gains environment logic (pinned by the shim test: root refusal, non-venv refusal, ensurepip remediation, create-and-re-exec, reuse, loop guard, link completeness); the linked-entry-point set and the re-exec marker are two more small contracts.
- **Neutral**: the previously documented `sudo /opt/theozolith/bin/python build.py` still works — running inside the target venv short-circuits straight to build-and-install. `PIP_BREAK_SYSTEM_PACKAGES` remains available user-side but is no longer relevant to any documented flow.

## Alternatives rejected

- **`pip install --break-system-packages` in the shim**: installs control's dependency closure (FastAPI, Textual, cryptography, …) into apt-managed site-packages, where upgrades can shadow distro-owned packages — the flag's name is literal — and forces the override on every operator instead of the one box that might want it.
- **A `build.sh` wrapper owning the venv**: `python3 build.py` is settled across ADR-0023/0030, the NODE-SUBSTRATE grilling, and the remediation strings init prints; env-setup in bash would leave the tested surface and add a third layer to "one implementation, two entry paths" — and buys nothing, since the pre-exec phase needs only the stdlib `venv` module.
- **Requiring system pip and building wheels outside the venv**: adds `python3-pip` as a second OS prerequisite and splits the flow across two interpreters; the re-exec needs one prerequisite and one interpreter.
- **Auto-installing `python3-venv` when missing**: a root path never package-manages on its own; refuse with the exact remediation instead.
