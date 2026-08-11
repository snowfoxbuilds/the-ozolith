# A minimal custom driver (ADR-0042). It is delivered to nodes as part of the
# Config Repo's `drivers/` tree — a hash-pinned, daemon-verified config
# distribution — and run by `theozolith-driver drivers/hello_logger`, the same
# generic launcher the built-in workers use. This one claims no work; it just
# logs a heartbeat every idle pass, so you can watch a custom worker type come
# up end to end without wiring real dispatch behavior first.
#
# THE EXPORT CONTRACT. A driver module MUST export a top-level class named
# `Driver` that subclasses `theozolith_worker.api.Worker`. The launcher looks
# up exactly that name and checks the subclass relationship (`issubclass`); a
# missing or wrong `Driver` is a start-time crash. There is no `main()`
# convention — the class is the whole extension surface, and the product owns
# the run loop.
#
# THE IMPORT RULE. `theozolith_worker.api` is the ONLY stable import a custom
# driver may depend on. Everything outside it is internal and carries no
# stability promise; api additions and removals are release-note events. The
# `built_against` stamp the config distribution carries is compared to the
# running worker version and any skew is logged as one advisory line — never a
# failure — so a driver written against an older api keeps running until it
# actually touches something that moved.
#
# NEVER put secret VALUES in `drivers/` (or anywhere in the Config Repo). This
# file travels in git and is code-reviewed; secrets live in the encrypted store
# and arrive as `<ENV>_FILE` at run time (see worker-types/hello-logger.toml).

from __future__ import annotations

from typing import ClassVar

from theozolith_worker import api

# FORK PROTOCOL (ADR-0042, v0). If this driver were forked from a built-in (or
# another custom driver), you would record the ancestry in a single leading
# comment line, which the launcher logs when the driver starts:
#
#     # forked-from: builtin:implementer @ 0.3.0
#
# It is documentation only — no enforcement, no tooling. A fresh-authored
# driver like this one carries no header.


class Driver(api.Worker):
    """A do-nothing custom worker type: it logs a heartbeat each idle pass and
    never claims work. Replace the two seams below with real behavior — see the
    built-in Implementer/Reviewer for how a worker fetches and executes work."""

    # `role` is this type's env prefix and dispatch role; `default_model` is the
    # model it runs when neither <ROLE>_MODEL nor THEOZOLITH_MODEL is set. Both
    # are required class attributes on every Worker.
    role: ClassVar[str] = "hello_logger"
    default_model: ClassVar[str] = "claude-sonnet-5"

    def fetch_work(self) -> list | None:
        """This pass's work-list. `[]` means "reachable, nothing to do" so the
        loop idles; `None` would signal the Control Node was unreachable (the
        backoff path). A real driver queries dispatch or GitHub here."""
        return []

    def execute(self, item) -> int:
        """Apply this type's transition set to one work item, returning the
        number of Runs/verdicts produced. Never called while fetch_work stays
        empty; a real driver does its work here."""
        return 0

    def on_idle(self) -> None:
        """Runs once per idle pass (nothing fetched). The heartbeat that proves
        the custom driver is alive."""
        self.log(f"hello from the {self.role} custom driver ({self.config.worker_id})")
