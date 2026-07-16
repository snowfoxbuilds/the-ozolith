"""The validate-verdict harness job: in-session verdict checking.

Invalid verdicts escalate to a human immediately — the driver never retries
(ADR-0014). The judging agent's second chances therefore live inside its own
session: this command runs the SAME validation the driver will apply (one
shared implementation, ``verdict.validate_verdict_file`` — never forked)
against the in-progress ``verdict.json``, with the round context from the
manifest so the final-round rule matches exactly. The review prompt tells the
agent to run it before finishing.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from theozolith_worker.jobdir import (
    CONTAINER_JOB_PATH,
    JobDirError,
    Manifest,
    read_manifest,
)
from theozolith_worker.verdict import validate_verdict_file

JOB_ENV = "THEOZOLITH_JOB"


def validate_session_verdict(job: Path, manifest: Manifest | None = None) -> tuple[bool, str]:
    """Validate the session's verdict.json exactly as the driver will.

    Returns (valid, message). The resume default and bundle URL are
    driver-side rendering inputs that cannot affect validity, so placeholders
    are used here.
    """
    manifest = manifest or read_manifest(job)
    result, reason = validate_verdict_file(
        job / manifest.workdir / "verdict.json",
        round_number=manifest.round or 1,
        final_round=manifest.final_round,
        default_resume="HEAD",
        bundle_url="",
    )
    if result is None:
        return False, reason
    return True, f"verdict.json is valid ({result.verdict}, round {manifest.round or 1})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-validate-verdict",
        description=(
            "Check this review session's verdict.json against the schema and round "
            "rules — the exact validation the driver applies. Run it before finishing; "
            "an invalid verdict escalates straight to a human."
        ),
    )
    parser.add_argument(
        "--job",
        default=os.environ.get(JOB_ENV, CONTAINER_JOB_PATH),
        help=f"job directory (default: ${JOB_ENV} or {CONTAINER_JOB_PATH})",
    )
    args = parser.parse_args(argv)
    try:
        valid, message = validate_session_verdict(Path(args.job))
    except JobDirError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not valid:
        print(f"verdict.json is INVALID: {message}")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
