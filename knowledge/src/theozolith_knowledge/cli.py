"""Command-line interface: theozolith-knowledge {validate,sync,bake,clone-init}."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from theozolith_knowledge.bake import bake
from theozolith_knowledge.cloneinit import clone_init
from theozolith_knowledge.model import KnowledgeError, load_knowledge_root
from theozolith_knowledge.sync import SyncReport, sync


def _print_report(report: SyncReport) -> None:
    for path in report.created:
        print(f"created   {path}")
    for path in report.updated:
        print(f"updated   {path}")
    for path in report.deleted:
        print(f"deleted   {path}")
    for path in sorted(report.hand_edited):
        print(
            f"warning: {path} had diverged from the last sync; the source won "
            "(sync targets are never hand-edited, ADR-0001)",
            file=sys.stderr,
        )
    print(f"sync: {report.summary()}")


def _cmd_validate(args: argparse.Namespace) -> int:
    root = load_knowledge_root(args.source)
    print(
        f"ok: {root.path} — "
        f"AGENTS.md: {'yes' if root.agents_md else 'no'}, "
        f"skills: {len(root.skills)}, "
        f"claude agents: {len(root.claude_agents)}, "
        f"workflows: {len(root.workflows)}"
    )
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    if args.scope == "project" and args.target is None:
        print("error: --scope project requires --target <project-root>", file=sys.stderr)
        return 2
    target = Path(args.target).expanduser() if args.target else Path.home() / ".claude"
    report = sync(
        args.source,
        args.scope,
        target,
        tool=args.tool,
        check=args.check,
        strict=args.strict,
    )
    _print_report(report)
    if args.check and report.changed:
        return 1
    return 0


def _cmd_bake(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser() if args.target else Path.home() / ".claude"
    result = bake(args.source, args.pin, target, scope=args.scope, subdir=args.subdir)
    print(f"baked {result.source} @ {result.pin} ({result.commit}) into {target}")
    _print_report(result.report)
    return 0


def _cmd_clone_init(args: argparse.Namespace) -> int:
    result = clone_init(args.source, args.target, branch=args.branch)
    if result == "cloned":
        print(f"clone-init: cloned {args.source} into {args.target}")
    elif result == "recovered":
        print(
            f"clone-init: recovered an interrupted initialization of {args.target} —"
            f" it now tracks {args.source}"
        )
    else:
        print(f"clone-init: {args.target} already tracks {args.source} — unchanged")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="theozolith-knowledge",
        description="TheOzolith agent-knowledge machinery: validate, sync, and bake "
        "knowledge repos (skills, subagents, workflows) into tool config dirs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="Validate a knowledge root.")
    p.add_argument("--source", required=True, help="Knowledge root directory.")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser(
        "sync",
        help="Compile a knowledge root and sync it one-way into a tool config dir.",
    )
    p.add_argument("--source", required=True, help="Knowledge root directory.")
    p.add_argument(
        "--scope",
        required=True,
        choices=("global", "project"),
        help="global: target is the Claude config dir (default ~/.claude). "
        "project: target is the project repo root (CLAUDE.md at the root, "
        "assets under .claude/).",
    )
    p.add_argument("--target", help="Target root (see --scope).")
    p.add_argument("--tool", default="claude", choices=("claude",), help="Target tool.")
    p.add_argument(
        "--check",
        action="store_true",
        help="Report what would change without writing; exit 1 if out of date.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Fail instead of overwriting a target that diverged from the last sync.",
    )
    p.set_defaults(func=_cmd_sync)

    p = sub.add_parser(
        "bake",
        help="Install a pinned Knowledge Source (git URL + pin) into an image "
        "during docker build. Nothing executes at container start.",
    )
    p.add_argument("--source", required=True, help="Git URL (or local path) of the knowledge repo.")
    p.add_argument(
        "--pin",
        required=True,
        help="Commit SHA, tag, or branch. Required: Knowledge Sources are always pinned.",
    )
    p.add_argument("--target", help="Claude config dir to bake into (default ~/.claude).")
    p.add_argument("--scope", default="global", choices=("global", "project"))
    p.add_argument("--subdir", help="Knowledge root within the repo, if not the repo root.")
    p.set_defaults(func=_cmd_bake)

    p = sub.add_parser(
        "clone-init",
        help="Initialize the Flight Deck's shared knowledge clone at container "
        "start (ADR-0043): clone once into an empty target, verify origin on a "
        "match, and never fetch or pull. flock-guarded against concurrent "
        "sibling starts. NEVER run 'sync' against a Flight Deck ~/.claude — its "
        "knowledge dirs are symlinks into this clone.",
    )
    p.add_argument("--source", required=True, help="Git URL (or local path) of the knowledge repo.")
    p.add_argument("--target", required=True, help="Directory the shared clone lives in.")
    p.add_argument("--branch", help="Branch to clone (default: the remote's default branch).")
    p.set_defaults(func=_cmd_clone_init)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KnowledgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
