"""``theozolith_worker.api`` — the stable surface for custom worker types (ADR-0042).

This module is the ONLY import contract a custom driver may depend on: it
exports the base ``Worker``, the built-in type classes, the loop launcher, and
the job-directory / claim-protocol / session interfaces a transition set needs.
Everything not re-exported here is internal and carries no stability promise;
additions to and removals from this surface are release-note events (ADR-0042).

The module is re-exports only — no logic lives here. ``__all__`` is the
contract; the api-surface test pins it to a literal snapshot.
"""

from __future__ import annotations

from theozolith_worker.base import Worker
from theozolith_worker.bootstrap import vocabulary
from theozolith_worker.config import ConfigError, DriverConfig, load_config
from theozolith_worker.containers import ContainerSpec, DockerEngine
from theozolith_worker.dispatch import DispatchClient, WorkDispatch
from theozolith_worker.drivercli import run_driver
from theozolith_worker.events import EventSink, emit_error, run_event
from theozolith_worker.githubapi import Comment, GitHubClient, Issue, PullRequest
from theozolith_worker.implementer import Implementer
from theozolith_worker.jobdir import (
    CONTAINER_JOB_PATH,
    MANIFEST_FILE,
    MODE_REVIEW,
    MODE_RUN,
    PROMPT_FILE,
    STATUS_FILE,
    TRANSCRIPT_FILE,
    WORK_DIR,
    AgentOutcome,
    JobDirError,
    JobRequest,
    JobResult,
    Manifest,
    Status,
    atomic_write,
    create_job_dir,
)
from theozolith_worker.proposal import PROPOSAL_FILE, SCHEMA_VERSION
from theozolith_worker.reviewer import Reviewer
from theozolith_worker.runner import RunReport, execute_claim
from theozolith_worker.sessions import (
    ContainerSession,
    SessionError,
    SessionFactory,
    container_session_factory,
)

# The full stable surface, one flat sorted list (categories: worker types —
# Worker/Implementer/Reviewer/run_driver; config; job directory; claim
# protocol / events; sessions / runs; GitHub). Additions/removals are
# release-note events (ADR-0042); the api-surface test pins this literal.
__all__ = [
    "CONTAINER_JOB_PATH",
    "MANIFEST_FILE",
    "MODE_REVIEW",
    "MODE_RUN",
    "PROMPT_FILE",
    "PROPOSAL_FILE",
    "SCHEMA_VERSION",
    "STATUS_FILE",
    "TRANSCRIPT_FILE",
    "WORK_DIR",
    "AgentOutcome",
    "Comment",
    "ConfigError",
    "ContainerSession",
    "ContainerSpec",
    "DispatchClient",
    "DockerEngine",
    "DriverConfig",
    "EventSink",
    "GitHubClient",
    "Implementer",
    "Issue",
    "JobDirError",
    "JobRequest",
    "JobResult",
    "Manifest",
    "PullRequest",
    "Reviewer",
    "RunReport",
    "SessionError",
    "SessionFactory",
    "Status",
    "WorkDispatch",
    "Worker",
    "atomic_write",
    "container_session_factory",
    "create_job_dir",
    "emit_error",
    "execute_claim",
    "load_config",
    "run_driver",
    "run_event",
    "vocabulary",
]
