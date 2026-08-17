"""Evidence bundles: per-Run traceability committed to a dedicated git ref.

Bundles live on an orphan branch ``theozolith/evidence`` in the target repo,
one directory per Run under ``runs/issue-<N>/<run-id>/`` (layout settled by
the M2 brief; format is the delegated decision recorded in the M2 ADR).
Workers push a bundle after every Run that reached a checkout; the Reviewer
adds a review record next to it. Concurrent pushes retry on a fresh clone.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

from theozolith_worker import gitops
from theozolith_worker.gitops import GitError

EVIDENCE_BRANCH = "theozolith/evidence"
PUSH_ATTEMPTS = 3

# The Run's exact input, preserved in every bundle (#52): the rendered
# prompt, the machine-readable issue metadata, and the Context Tree. These
# enumerated paths are the ONLY things collected recursively — never the
# checkout, credentials, or unrelated job data.
INPUT_ARTIFACT_FILES = ("input/issue.json", "input/prompt.md")
INPUT_ARTIFACT_TREES = ("input/issue", "input/pr")

# The trusted input snapshots live in a dot-prefixed directory INSIDE the
# jobs dir: same filesystem (atomic renames), but outside every job dir —
# the /job bind mount never contains it, so nothing that runs in a
# container can reach it. Dot-prefixed names are ignored by the Node
# Daemon's queue-behind in-flight signal and by the evidence sweep's
# orphan candidate scan alike.
SNAPSHOT_PARENT = ".trusted-input"
SNAPSHOT_STAGING_PREFIX = ".staging-"


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _collect_input(job: Path) -> dict[str, bytes]:
    """Byte-exact reads (never text mode, which folds CRLF) restricted to
    the enumerated input paths; dot-prefixed entries (atomic-write
    temporaries) and non-regular files are skipped, and whatever is missing
    (a Run that died before the tree was written) is simply absent."""
    files: dict[str, bytes] = {}
    for relpath in INPUT_ARTIFACT_FILES:
        content = _read_bytes(job / relpath)
        if content is not None:
            files[relpath] = content
    for tree in INPUT_ARTIFACT_TREES:
        root = job / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            content = _read_bytes(path)
            if content is not None:
                files[str(Path(tree) / relative)] = content
    return files


def input_artifacts(job: Path) -> dict[str, bytes]:
    """The exact input the Run saw, keyed by job-dir-relative path (#52).

    Trust boundary: valid ONLY while the run container has never launched —
    the job dir is bind-mounted at /job, so after launch its input/ is
    agent-writable and no longer evidence. Post-launch collection must come
    from the pre-launch trusted snapshot (:func:`capture_input_snapshot` /
    :func:`load_input_snapshot`) instead."""
    return _collect_input(job)


def snapshot_dir(jobs_dir: Path, run_id: str) -> Path:
    return Path(jobs_dir) / SNAPSHOT_PARENT / run_id


def capture_input_snapshot(jobs_dir: Path, job: Path, run_id: str) -> dict[str, bytes]:
    """Freeze the job dir's input for evidence, immediately before the
    container launches (#52 amendment): every enumerated input file is
    copied byte-exact to ``<jobs-dir>/.trusted-input/<run-id>/``, staged and
    atomically renamed so a crash mid-copy never leaves a half snapshot
    posing as a complete one. Returns the captured bytes so the live push
    path never re-reads anything the agent could have touched; the on-disk
    copy exists for the boot sweep, which recovers Runs this process did
    not survive."""
    files = _collect_input(job)
    parent = Path(jobs_dir) / SNAPSHOT_PARENT
    staging = parent / f"{SNAPSHOT_STAGING_PREFIX}{run_id}"
    final = parent / run_id
    shutil.rmtree(staging, ignore_errors=True)
    for relpath, content in files.items():
        target = staging / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    staging.mkdir(parents=True, exist_ok=True)  # a Run with no input still snapshots
    shutil.rmtree(final, ignore_errors=True)
    staging.rename(final)
    return files


def load_input_snapshot(jobs_dir: Path, run_id: str) -> dict[str, bytes] | None:
    """The persisted pre-launch snapshot, or None when no container was ever
    launched for ``run_id`` (capture happens strictly before launch, so a
    missing snapshot proves the job dir's input is still driver-authored)."""
    root = snapshot_dir(jobs_dir, run_id)
    if not root.is_dir():
        return None
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        content = _read_bytes(path)
        if content is not None:
            files[str(relative)] = content
    return files


def discard_input_snapshot(jobs_dir: Path, run_id: str) -> None:
    """Best-effort removal once the Run's evidence is durable (or declared
    lost); a leaked snapshot is garbage-collected by the boot sweep. The
    parent goes too when this was the last snapshot — no filesystem state
    survives a fully settled Run."""
    root = snapshot_dir(jobs_dir, run_id)
    shutil.rmtree(root, ignore_errors=True)
    with contextlib.suppress(OSError):  # other Runs' snapshots remain
        root.parent.rmdir()


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
    files: dict[str, str | bytes],
    *,
    message: str,
    author_name: str,
    author_email: str,
    env: dict[str, str] | None = None,
) -> None:
    """Commit ``files`` (relative path -> content) to the evidence branch.
    Bytes values land verbatim — input artifacts are byte-exact end to end
    (#52); text values are written as UTF-8."""
    last_error: GitError | None = None
    for _ in range(PUSH_ATTEMPTS):
        workdir = Path(tempfile.mkdtemp(prefix="theozolith-evidence-"))
        try:
            _prepare_checkout(clone_url, workdir, env)
            for relpath, content in files.items():
                target = workdir / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                data = content if isinstance(content, bytes) else content.encode("utf-8")
                target.write_bytes(data)
            gitops.commit_all(workdir, message, author_name, author_email)
            gitops.push(workdir, EVIDENCE_BRANCH, env=env)
            return
        except GitError as exc:
            last_error = exc  # non-fast-forward or transient: retry fresh
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    raise GitError(f"evidence push failed after {PUSH_ATTEMPTS} attempts: {last_error}")
