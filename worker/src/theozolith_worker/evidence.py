"""Evidence bundles: per-Run traceability committed to a dedicated git ref.

Bundles live on an orphan branch ``theozolith/evidence`` in the target repo,
one directory per Run under ``runs/issue-<N>/<run-id>/`` (layout settled by
the M2 brief; format is the delegated decision recorded in the M2 ADR).
Workers push a bundle after every Run that reached a checkout; the Reviewer
adds a review record next to it. Concurrent pushes retry on a fresh clone.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from theozolith_worker import gitops
from theozolith_worker.gitops import GitError

EVIDENCE_BRANCH = "theozolith/evidence"
PUSH_ATTEMPTS = 3


def run_dir(issue_number: int, run_id: str) -> str:
    return f"runs/issue-{issue_number}/{run_id}"


def issue_evidence_url(repo: str, issue_number: int) -> str:
    """Web URL of the issue's evidence directory (resolves to the git ref)."""
    return f"https://github.com/{repo}/tree/{EVIDENCE_BRANCH}/runs/issue-{issue_number}"


def run_evidence_url(repo: str, issue_number: int, run_id: str) -> str:
    """Web URL of one Run's evidence bundle."""
    return f"https://github.com/{repo}/tree/{EVIDENCE_BRANCH}/{run_dir(issue_number, run_id)}"


def _prepare_checkout(clone_url: str, workdir: Path, env: dict[str, str] | None) -> None:
    try:
        gitops.clone(clone_url, workdir, branch=EVIDENCE_BRANCH, env=env)
    except GitError:
        # First bundle ever: start the orphan branch.
        workdir.mkdir(parents=True, exist_ok=True)
        gitops.git(["init", "--quiet", "--initial-branch", EVIDENCE_BRANCH], workdir)
        gitops.git(["remote", "add", "origin", clone_url], workdir)


def push_bundle(
    clone_url: str,
    files: dict[str, str],
    *,
    message: str,
    author_name: str,
    author_email: str,
    env: dict[str, str] | None = None,
) -> None:
    """Commit ``files`` (relative path -> text) to the evidence branch."""
    last_error: GitError | None = None
    for _ in range(PUSH_ATTEMPTS):
        workdir = Path(tempfile.mkdtemp(prefix="theozolith-evidence-"))
        try:
            _prepare_checkout(clone_url, workdir, env)
            for relpath, content in files.items():
                target = workdir / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            gitops.commit_all(workdir, message, author_name, author_email)
            gitops.push(workdir, EVIDENCE_BRANCH, env=env)
            return
        except GitError as exc:
            last_error = exc  # non-fast-forward or transient: retry fresh
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    raise GitError(f"evidence push failed after {PUSH_ATTEMPTS} attempts: {last_error}")
