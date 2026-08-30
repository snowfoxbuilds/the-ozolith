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
from theozolith_worker.basedon import BasedOn, upsert_zone
from theozolith_worker.bootstrap import vocabulary
from theozolith_worker.config import ConfigError, DriverConfig, load_config
from theozolith_worker.containers import ContainerSpec, DockerEngine
from theozolith_worker.contexttree import ContextSnapshot, PrCommit, git_pr_commits, write_tree
from theozolith_worker.decisions import Decision, DecisionsSection
from theozolith_worker.dispatch import DispatchClient, WorkDispatch
from theozolith_worker.drivercli import run_driver
from theozolith_worker.events import EventSink, emit_error, run_event
from theozolith_worker.gate import (
    STEP_ORDER,
    Finding,
    GateConfigError,
    GateResult,
    StepSpec,
    load_steps,
    run_gate,
)
from theozolith_worker.githubapi import (
    Comment,
    GitHubClient,
    Issue,
    PullRequest,
    RepoMergeSettings,
)
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
    read_manifest,
    write_manifest,
)
from theozolith_worker.proposal import (
    PROPOSAL_FILE,
    SCHEMA_VERSION,
    RunProposal,
    required_fields,
    validate_review,
    validate_run,
)
from theozolith_worker.reviewer import Reviewer, render_base_md, render_review_prompt
from theozolith_worker.runner import (
    RunReport,
    commit_message_with_trailer,
    compose_pr_body,
    execute_claim,
    render_run_prompt,
)
from theozolith_worker.sessions import (
    ContainerSession,
    SessionError,
    SessionFactory,
    container_session_factory,
)
from theozolith_worker.signals import DiffSignals, signals_from_git
from theozolith_worker.verdict import APPROVE, ESCALATE, REVISE, Verdict

# The full stable surface, one flat sorted list (categories: worker types —
# Worker/Implementer/Reviewer/run_driver; config; job directory; claim
# protocol / events; sessions / runs; GitHub; the Run Contract — prompt
# renderers, PR-body composition, gate sequence, proposal validation —
# exposed for bench replay under ``schema_version`` (ADR-0054)).
# Additions/removals are release-note events (ADR-0042); the api-surface
# test pins this literal.
__all__ = [
    "APPROVE",
    "CONTAINER_JOB_PATH",
    "ESCALATE",
    "MANIFEST_FILE",
    "MODE_REVIEW",
    "MODE_RUN",
    "PROMPT_FILE",
    "PROPOSAL_FILE",
    "REVISE",
    "SCHEMA_VERSION",
    "STATUS_FILE",
    "STEP_ORDER",
    "TRANSCRIPT_FILE",
    "WORK_DIR",
    "AgentOutcome",
    "BasedOn",
    "Comment",
    "ConfigError",
    "ContainerSession",
    "ContainerSpec",
    "ContextSnapshot",
    "Decision",
    "DecisionsSection",
    "DiffSignals",
    "DispatchClient",
    "DockerEngine",
    "DriverConfig",
    "EventSink",
    "Finding",
    "GateConfigError",
    "GateResult",
    "GitHubClient",
    "Implementer",
    "Issue",
    "JobDirError",
    "JobRequest",
    "JobResult",
    "Manifest",
    "PrCommit",
    "PullRequest",
    "RepoMergeSettings",
    "Reviewer",
    "RunProposal",
    "RunReport",
    "SessionError",
    "SessionFactory",
    "Status",
    "StepSpec",
    "Verdict",
    "WorkDispatch",
    "Worker",
    "atomic_write",
    "commit_message_with_trailer",
    "compose_pr_body",
    "container_session_factory",
    "create_job_dir",
    "emit_error",
    "execute_claim",
    "git_pr_commits",
    "load_config",
    "load_steps",
    "read_manifest",
    "render_base_md",
    "render_review_prompt",
    "render_run_prompt",
    "required_fields",
    "run_driver",
    "run_event",
    "run_gate",
    "signals_from_git",
    "upsert_zone",
    "validate_review",
    "validate_run",
    "vocabulary",
    "write_manifest",
    "write_tree",
]
