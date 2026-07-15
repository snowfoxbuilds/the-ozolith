"""Command-line interface: theozolith-bootstrap."""

from __future__ import annotations

import argparse
import os
import sys

from theozolith_worker.bootstrap.core import bootstrap_repo
from theozolith_worker.bootstrap.github import BootstrapError, GitHubRepoClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-bootstrap",
        description="Apply the TheOzolith pipeline substrate (label vocabulary + issue "
        "forms) to a target GitHub repo, idempotently.",
    )
    parser.add_argument("--repo", required=True, help="Target repo as owner/name.")
    parser.add_argument("--api-url", default="https://api.github.com", help="GitHub API base URL.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report what would change without writing; exit 1 if anything would.",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("error: set GITHUB_TOKEN (or GH_TOKEN) with repo access", file=sys.stderr)
        return 2

    try:
        client = GitHubRepoClient(args.repo, token, api_url=args.api_url)
        report = bootstrap_repo(client, check=args.check)
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for name in report.labels_created:
        print(f"label created   {name}")
    for name in report.labels_updated:
        print(f"label updated   {name}")
    for path in report.files_created:
        print(f"form created    {path}")
    for path in report.files_updated:
        print(f"form updated    {path}")
    print(f"bootstrap: {report.summary()}")
    if args.check and report.changed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
