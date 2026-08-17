"""``format-output`` / ``view-output``: the in-session Output Proposal CLI.

The agent's half of the ADR-0046 channel: every field the pipeline may apply
post-exit is written through ``format-output``, which parses, applies the
write-time rules (unknown fields refused, enums enforced, the final-round
revise refused), persists atomically to ``output/proposal.json``, and echoes
fill state. ``format-output status`` prints the full fill-state table and
runs the driver's own validation; the prompt requires it before exit.
``view-output <field>`` reads pending state.

Names deliberately do not imply live GitHub writes: nothing here mutates
GitHub — the proposal is pending state the driver validates and applies
after the session exits (the CLI is convenience, never the trust boundary).
Ships in the product distribution and is baked into run images. The CLI
asserts the manifest's stamped schema version at every invocation; a
mismatched pairing is refused with the same message the harness fails
pre-work with.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from theozolith_worker import jobdir, proposal

JOB_ENV = "THEOZOLITH_JOB"

USAGE_OK = 0
INVALID = 1
USAGE_ERROR = 2


def _job_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--job",
        default=os.environ.get(JOB_ENV, jobdir.CONTAINER_JOB_PATH),
        help=f"job directory (default: ${JOB_ENV} or {jobdir.CONTAINER_JOB_PATH})",
    )


def _load_manifest(job: Path) -> jobdir.Manifest:
    """The manifest, with the schema stamp asserted (ADR-0046): a run image
    whose distribution speaks a different proposal schema than the driver
    stamped must refuse to write anything."""
    manifest = jobdir.read_manifest(job)
    mismatch = proposal.schema_mismatch(manifest.schema_version)
    if mismatch is not None:
        raise jobdir.JobDirError(mismatch)
    return manifest


def _read_value(args: argparse.Namespace) -> str:
    if args.file is not None:
        return Path(args.file).read_text(encoding="utf-8")
    if args.value is None or args.value == "-":
        return sys.stdin.read()
    return args.value


def _fill_table(manifest: jobdir.Manifest, fields: dict) -> list[str]:
    specs = proposal.fields_for(manifest.mode)
    required = proposal.required_fields(manifest.mode, manifest.round or 1)
    width = max(len(name) for name in specs)
    lines = []
    for name, spec in specs.items():
        state = proposal.describe_value(spec, fields.get(name))
        mark = "  (required)" if name in required and name not in fields else ""
        lines.append(f"  {name.ljust(width)}  {state}{mark}")
    return lines


def _validation_errors(manifest: jobdir.Manifest, job: Path) -> list[str]:
    """Exactly the driver's post-exit validation, against the pending file."""
    round_number = manifest.round or 1
    if manifest.mode == jobdir.MODE_REVIEW:
        verdict, reason = proposal.validate_review_job(
            job,
            round_number=round_number,
            final_round=manifest.final_round,
            default_resume="HEAD",
            bundle_url="",
        )
        return [reason] if verdict is None else []
    _, errors = proposal.validate_run_job(job, round_number=round_number)
    return errors


def _status(manifest: jobdir.Manifest, job: Path) -> int:
    fields = proposal.pending_fields(proposal.read_raw(job))
    print(
        f"Output Proposal fill state ({manifest.mode} mode, round {manifest.round or 1},"
        f" schema v{proposal.SCHEMA_VERSION}):"
    )
    for line in _fill_table(manifest, fields):
        print(line)
    errors = _validation_errors(manifest, job)
    if errors:
        print("INVALID — the pipeline would refuse this proposal:")
        for error in errors:
            print(f"  - {error}")
        return INVALID
    print("valid: the proposal is complete and would be accepted")
    return USAGE_OK


def format_output_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="format-output",
        description=(
            "Write one field of this Run's Output Proposal — the pending state the "
            "pipeline validates and applies after the session exits. Nothing here "
            "touches GitHub. Run `format-output status` before finishing."
        ),
    )
    _job_argument(parser)
    parser.add_argument("field", help="field name, or `status` for the full fill-state table")
    parser.add_argument(
        "value",
        nargs="?",
        default=None,
        help="field value; omit or use `-` to read stdin (multi-line fields)",
    )
    parser.add_argument("--file", default=None, help="read the value from a file")
    args = parser.parse_args(argv)
    job = Path(args.job)
    try:
        manifest = _load_manifest(job)
        proposal.fields_for(manifest.mode)  # a schema-less mode is unusable
        if args.field == "status":
            return _status(manifest, job)
        value_text = _read_value(args)
    except (jobdir.JobDirError, proposal.ProposalError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return USAGE_ERROR
    try:
        value = proposal.write_field(job, manifest, args.field, value_text)
    except proposal.ProposalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return INVALID
    spec = proposal.fields_for(manifest.mode)[args.field]
    print(f"{args.field}: {proposal.describe_value(spec, value)}")
    fields = proposal.pending_fields(proposal.read_raw(job))
    missing = [
        name
        for name in proposal.required_fields(manifest.mode, manifest.round or 1)
        if name not in fields
    ]
    if missing:
        print(f"still missing required: {', '.join(missing)}")
    return USAGE_OK


def view_output_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="view-output",
        description="Read one pending field of this Run's Output Proposal.",
    )
    _job_argument(parser)
    parser.add_argument("field", help="field name")
    args = parser.parse_args(argv)
    job = Path(args.job)
    try:
        manifest = _load_manifest(job)
        specs = proposal.fields_for(manifest.mode)
    except (jobdir.JobDirError, proposal.ProposalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return USAGE_ERROR
    if args.field not in specs:
        print(
            f"error: unknown field {args.field!r} for {manifest.mode} mode"
            f" (the schema allows: {', '.join(specs)})",
            file=sys.stderr,
        )
        return USAGE_ERROR
    fields = proposal.pending_fields(proposal.read_raw(job))
    if args.field not in fields:
        print("(empty)", file=sys.stderr)
        return INVALID
    value = fields[args.field]
    print(value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True))
    return USAGE_OK


def main(argv: list[str] | None = None) -> int:  # format-output console script
    return format_output_main(argv)


def view_main(argv: list[str] | None = None) -> int:  # view-output console script
    return view_output_main(argv)


if __name__ == "__main__":
    sys.exit(main())
