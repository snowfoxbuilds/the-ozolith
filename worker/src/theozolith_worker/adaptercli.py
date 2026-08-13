"""The in-image materialization entry point: ``theozolith-adapter`` (ADR-0045).

The Control Node synthesizes one setup instruction into the wire recipe of any
worker type that sets ``model`` or ``effort`` (see
``adapters.materialize_instruction``); the node's ``docker build`` runs it here,
inside the image being built. This script re-validates the values against the
adapter registry shipped *in this same image* — version-matched to the agent
CLI beside it, the backstop behind control's config-load validation — proves
the agent CLI beside it is new enough to ENFORCE the config (managed scope
only), then writes the adapter's native configuration. An unmappable value or
a pre-enforcement CLI exits non-zero, failing the build loudly through the
daemon's existing error-event path.

Scopes: ``managed`` (driver run images — config lands where a workspace
checkout cannot override it) and ``interactive`` (driverless Flight Deck
images — only the well-known ``/etc/theozolith/*`` files, never anything under
``/home/ozolith/.claude``, which the claude-state volume shadows, ADR-0043).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from theozolith_worker.adapters import (
    MATERIALIZE_SCOPES,
    MODEL_UNMAPPABLE,
    SCOPE_INTERACTIVE,
    SCOPE_MANAGED,
    AgentAdapterError,
    make_agent_adapter,
)


def _materialize(args) -> int:
    if not args.model and not args.effort:
        print("error: nothing to materialize — pass --model and/or --effort", file=sys.stderr)
        return 2
    try:
        adapter = make_agent_adapter(args.adapter)
    except AgentAdapterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.model and adapter.classify_model(args.model) == MODEL_UNMAPPABLE:
        print(
            f"error: adapter {args.adapter!r} cannot map model {args.model!r} —"
            " the base image may predate this model; bump the worker type's base"
            " (ADR-0045)",
            file=sys.stderr,
        )
        return 2
    if args.effort and args.effort not in adapter.mappable_efforts():
        known = ", ".join(sorted(adapter.mappable_efforts())) or "none"
        print(
            f"error: adapter {args.adapter!r} cannot map effort {args.effort!r}"
            f" (mappable: {known}) (ADR-0045)",
            file=sys.stderr,
        )
        return 2
    if args.effort and args.scope == SCOPE_INTERACTIVE:
        print(
            "error: effort has no interactive-scope materialization — driverless"
            " (Flight Deck) worker types reject 'effort' until a runtime consumer"
            " exists (ADR-0045)",
            file=sys.stderr,
        )
        return 2
    if args.scope == SCOPE_MANAGED:
        # The written config only binds if the agent CLI in THIS image
        # enforces it; an allowlist a pre-enforcement CLI would silently
        # ignore is a fake identity, so the build fails instead.
        try:
            print(f"agent CLI: {adapter.verify_enforceable()}", flush=True)
        except AgentAdapterError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        written = adapter.materialize(
            args.model, args.effort, root=Path(args.root), scope=args.scope
        )
    except (AgentAdapterError, OSError, ValueError) as exc:
        print(f"error: materialize failed: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"materialized {path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-adapter",
        description=(
            "Bake a worker type's model/effort into the Agent adapter's native"
            " configuration at derived-image build time (ADR-0045). Invoked by"
            " the setup instruction the Control Node synthesizes; never at"
            " container start."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    mat = sub.add_parser("materialize", help="write the native model/effort config")
    mat.add_argument("--adapter", required=True, help="Agent adapter name (e.g. claude)")
    mat.add_argument("--model", default="", help="model ID to bake")
    mat.add_argument("--effort", default="", help="reasoning-effort value to bake")
    mat.add_argument(
        "--scope",
        required=True,
        choices=MATERIALIZE_SCOPES,
        help="managed = driver run image; interactive = driverless Flight Deck image",
    )
    mat.add_argument(
        "--root",
        default="/",
        help="filesystem root to write under (the image build uses /; tests use a tmp dir)",
    )
    args = parser.parse_args(argv)
    return _materialize(args)


if __name__ == "__main__":
    sys.exit(main())
