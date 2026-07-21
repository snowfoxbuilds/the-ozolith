"""Run execution, driver-side: one stateless, disposable attempt at one issue.

Every Run: fresh token-free clone, fresh run container, fresh agent context.
Resume state is exactly what the Reviewer designated — the PR branch at the
resume commit (plus cherry-picked commits) and the revised plan in the
verdict comment — nothing else survives between Runs (ADR-0008).

The driver never executes repository code or model output (ADR-0013): the
agent session and every gate step run inside the ephemeral run container,
commissioned through the job directory; the driver performs the side effects
(commit, push, PR, labels) after sanitizing the container-touched checkout's
git metadata.

Run outcomes (ADR-0014, failure lane replaced by ADR-0016): a completed
session with commits ships the normal best-effort PR; a completed session
with zero commits but no-change reasoning in the decisions payload ships an
EMPTY PR (one driver-synthesized allow-empty commit) the Reviewer judges
like any other; everything else is a FAILED Run. The failed lane is local
retry (``execute_claim``): the driver keeps the claim and launches one full
second Run — new run_id, fresh clone and container, its own evidence
bundle — under a uniform budget across failure classes. A second
non-completion releases the claim and applies failed + needs_human with
both evidence links and each failure's class. Nothing is re-queued and no
state lives in comments.

Evidence is the sole durable audit trail (ADR-0016): every Run pushes its
bundle, and a job directory is deleted only after that push confirmed —
otherwise it is parked beside the jobs dir for the boot-time evidence
sweep. One enumerated exception (M5, ADR-0019): when parking AND its
collision-safe fallback both fail, the completed directory is removed
unpublished — accepted evidence loss, because a completed dir left in the
jobs dir reads as an in-flight Run to queue-behind.
"""

from __future__ import annotations

import itertools
import json
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from theozolith_worker import decisions, events, evidence, gitops, jobdir, verdict
from theozolith_worker.bootstrap.vocabulary import (
    FAILED,
    IN_PROGRESS,
    NEEDS_HUMAN,
    PLAN_READY,
    PR_READY,
    attempts_on,
)
from theozolith_worker.config import DriverConfig
from theozolith_worker.containers import (
    ContainerSpec,
    container_labels,
    run_container_name,
    run_session_name,
)
from theozolith_worker.gate.pipeline import Finding, GateResult, run_gate
from theozolith_worker.githubapi import Comment, GitHubClient, Issue
from theozolith_worker.sessions import SessionError, SessionFactory
from theozolith_worker.sweep import TOMBSTONE_PREFIX, park_job_dir, pending_dir

BRANCH_PREFIX = "ozolith/issue-"

# Paths in the checkout that are pipeline metadata, never repo content.
EXCLUDED_METADATA = (".theozolith/decisions.json", ".claude/settings.local.json")

# Runs per claim: the original plus exactly one local retry (ADR-0016).
CLAIM_RUN_BUDGET = 2
# Delivery attempts for the claimed event that activates a grant (ADR-0017).
ACTIVATION_ATTEMPTS = 3

# Distinguishes Runs started within the same second by one driver process.
_RUN_SEQUENCE = itertools.count(1)


def new_run_id(config: DriverConfig) -> str:
    """run-id = <utc-timestamp>-<worker-id>-<seq> (ADR-0014)."""
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{config.worker_id}-{next(_RUN_SEQUENCE)}"


def _log(message: str) -> None:
    print(message, flush=True)


def branch_for(issue_number: int) -> str:
    return f"{BRANCH_PREFIX}{issue_number}"


def issue_for_branch(head_ref: str) -> int | None:
    suffix = head_ref.removeprefix(BRANCH_PREFIX)
    return int(suffix) if head_ref != suffix and suffix.isdigit() else None


PROMPT_TEMPLATE = """\
You are a Worker in TheOzolith agentic coding pipeline, executing one \
stateless Run against the checked-out repository (your working directory). \
You run in an interactive session; a human operator may attach at any time \
and add instructions — treat those as authoritative input and record how \
they shaped your work.

## Issue #{number}: {title}

{body}

{round_context}## Rules

- Implement the issue's acceptance criteria directly in the working tree.
- Never stop to ask questions. When a judgment call comes up, decide, then \
record the decision and its rationale. A separate Reviewer adjudicates your \
decisions afterwards.
- Record your run summary in `.theozolith/decisions.json` (create the file) \
with exactly this JSON shape:
  {{"decisions": [{{"what": "...", "why": "..."}}], "open_questions": [], \
"remaining_work": [], "dead_ends": []}}
  Decisions are judgment calls with rationale; open_questions are calls only \
a human can make; remaining_work is what a follow-up round still needs; \
dead_ends are approaches you tried and abandoned (so the next Run does not \
repeat them).
- If you conclude that NO code change is needed, change nothing and record \
why in the decisions file — the pipeline ships an empty PR carrying your \
reasoning for review. A run with no changes and no recorded reasoning is \
treated as a failure.
- Do not run git commit/push and do not open PRs; the pipeline owns version \
control.
- Do not edit `.theozolith/gate.toml` or CI workflows unless the issue \
explicitly asks for it.
- When you are done, simply finish your reply; the pipeline detects \
completion and runs the quality gate.
"""

REVISED_PLAN_CONTEXT = """\
## Revised plan (round {round})

A Reviewer judged the previous round of this work; the branch you are on is \
its designated resume state. Execute this revised plan exactly:

{plan}

"""

DISCUSSION_CONTEXT = """\
## Review discussion since the last verdict

These comments (from the PR conversation) answer open questions or add \
decisions — honor them:

{comments}

"""


@dataclass
class RunReport:
    run_id: str
    issue: int
    round: int
    phase: str = "claimed"  # claimed | checkout | agent | gate | empty-pr | pr-open | failed
    pr_number: int | None = None
    branch: str = ""
    head: str = ""
    container: str = ""
    agent_outcome: str = ""  # completed | timed out | session died | harness-failed | unknown
    gate_findings: int = 0
    reason: str = ""  # failed Runs: what broke
    # ADR-0016 uniform budget classes: timeout | session-died | harness |
    # no-changes | infra ("" for completed Runs).
    failure_class: str = ""
    evidence_pushed: bool = False
    # True only on the compound failure: the push failed AND both parking
    # attempts failed, so the completed job dir was removed unpublished
    # (accepted evidence loss, M5 — never a false in-flight signal).
    evidence_discarded: bool = False
    notes: list[str] = field(default_factory=list)


class _RunFailed(Exception):
    """Internal control flow: this Run is a FAILED Run (ADR-0016)."""

    def __init__(self, reason: str, failure_class: str):
        super().__init__(reason)
        self.reason = reason
        self.failure_class = failure_class


@dataclass
class _RunContext:
    """What the failure path needs from however far the Run body got."""

    base_branch: str = ""
    gate: GateResult = field(default_factory=GateResult)
    section: decisions.DecisionsSection | None = None


def _resume_context(
    client: GitHubClient, pr_number: int
) -> tuple[verdict.Verdict | None, list[Comment]]:
    comments = client.list_comments(pr_number)
    found = verdict.latest_verdict(comments)
    if found is None:
        return None, comments
    latest, marker = found
    return latest, verdict.comments_after(comments, marker)


def _build_prompt(
    issue: Issue,
    round_number: int,
    revised: verdict.Verdict | None,
    discussion: list[Comment],
) -> str:
    round_context = ""
    if revised is not None and revised.verdict == verdict.REVISE and revised.revised_plan:
        round_context += REVISED_PLAN_CONTEXT.format(round=round_number, plan=revised.revised_plan)
    human_comments = [c for c in discussion if verdict.parse_comment(c.body) is None]
    if human_comments:
        rendered = "\n\n".join(f"@{c.author}:\n{c.body}" for c in human_comments)
        round_context += DISCUSSION_CONTEXT.format(comments=rendered)
    return PROMPT_TEMPLATE.format(
        number=issue.number,
        title=issue.title,
        body=issue.body or "(no body)",
        round_context=round_context,
    )


def _exclude_metadata(workdir: Path) -> None:
    """Keep pipeline metadata out of commits, surviving agent tampering."""
    exclude = workdir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("".join(f"{path}\n" for path in EXCLUDED_METADATA), encoding="utf-8")


def _read_output(job: Path, relpath: str) -> str:
    try:
        return (job / relpath).read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_issue_metadata(job: Path, issue: Issue, *, round_number: int) -> None:
    jobdir.atomic_write(
        job / "input" / "issue.json",
        json.dumps(
            {
                "number": issue.number,
                "title": issue.title,
                "body": issue.body,
                "labels": sorted(issue.labels),
                "round": round_number,
            },
            indent=2,
            sort_keys=True,
        ),
    )


def _has_reasoning(section: decisions.DecisionsSection | None) -> bool:
    """Did the agent record anything that can justify a no-change Run?"""
    return section is not None and bool(
        section.decisions or section.open_questions or section.remaining_work or section.dead_ends
    )


def _no_change_intro(section: decisions.DecisionsSection) -> str:
    lines = ["This Run concluded that **no code change is needed**. The Worker's reasoning:", ""]
    if section.decisions:
        lines += [f"- **{d.what}**" + (f" — {d.why}" if d.why else "") for d in section.decisions]
    else:
        lines += [f"- {item}" for item in section.open_questions + section.dead_ends]
    return "\n".join(lines)


def execute_run(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    session_factory: SessionFactory,
    *,
    run_id: str | None = None,
    log=_log,
    sink: events.EventSink | None = None,
) -> RunReport:
    """One Run on one claimed issue. Assumes the claim already exists on
    GitHub (written by the Control Node, ADR-0017). Never raises: every
    failure mode returns a failed report under the uniform budget."""
    run_id = run_id or new_run_id(config)
    sink = sink or events.make_sink(config, log)
    job = jobdir.create_job_dir(config.jobs_dir, run_id)
    # Issue metadata lands BEFORE any slow work (clone can take minutes):
    # a driver death from here on leaves a job dir the boot sweep can file
    # under the correct runs/issue-N path (ADR-0016). Rewritten with the
    # final round number once the checkout settles it.
    _write_issue_metadata(job, issue, round_number=1)
    report = RunReport(
        run_id=run_id,
        issue=issue.number,
        round=1,
        branch=branch_for(issue.number),
        container=run_container_name(run_id),
    )
    context = _RunContext()
    try:
        _run_to_pr(config, client, issue, session_factory, job, report, context, log, sink)
    except _RunFailed as failed:
        _fail_run(config, issue, report, job, context, failed, log, sink)
    except Exception as exc:  # pre/post-session driver-side breakage
        _fail_run(
            config,
            issue,
            report,
            job,
            context,
            _RunFailed(f"driver-side failure: {exc}", "infra"),
            log,
            sink,
        )
    finally:
        if report.evidence_pushed:
            shutil.rmtree(job, ignore_errors=True)
        else:
            # The bundle is the sole durable audit trail: never delete a job
            # dir whose push is unconfirmed — the evidence sweep retries it.
            # Parked in the -pending sibling immediately, so the Node
            # Daemon's queue-behind in-flight signal never reads a finished
            # Run's retained evidence as a live Run (ADR-0019).
            _park_for_sweep(config, job, report, log)
    return report


def _park_failure_record(log, event: str, report: RunReport, source: Path, destination: Path, exc):
    """One structured parking-failure record (M5): machine-greppable with
    everything an operator needs to find what was (or was not) saved."""
    log(
        "evidence parking failed: "
        + json.dumps(
            {
                "event": event,
                "run_id": report.run_id,
                "issue": report.issue,
                "source": str(source),
                "destination": str(destination),
                "error": str(exc),
            },
            sort_keys=True,
        )
    )


def _park_for_sweep(config: DriverConfig, job: Path, report: RunReport, log) -> None:
    """Move a retained job dir into the sweep's parking sibling. Failure
    ladder (M5): the plain atomic rename; then a structured failure record
    plus a collision-safe unique destination; then a second structured
    record and REMOVAL of the completed directory from the active jobs dir
    — evidence loss in that compound case is explicitly accepted, because
    a completed dir left in place reads as an in-flight Run to the Node
    Daemon's queue-behind signal. Never raises: escalation must proceed."""
    try:
        kept = park_job_dir(config, job)
        log(f"run {job.name}: job directory kept for the evidence sweep ({kept})")
        return
    except OSError as exc:
        _park_failure_record(
            log, "theozolith.evidence-park-failed", report, job, pending_dir(config) / job.name, exc
        )
    # The unique suffix keeps -<worker-id>- in the name, so the sweep still
    # recognizes the parked dir as this driver's to push.
    fallback = f"{job.name}-parked-{secrets.token_hex(4)}"
    try:
        kept = park_job_dir(config, job, target_name=fallback)
        log(f"run {job.name}: job directory kept for the evidence sweep ({kept})")
        return
    except OSError as exc:
        _park_failure_record(
            log, "theozolith.evidence-lost", report, job, pending_dir(config) / fallback, exc
        )
    shutil.rmtree(job, ignore_errors=True)
    if not job.exists():
        report.evidence_discarded = True
        log(
            f"run {job.name}: parking failed twice — the completed job directory is removed"
            " unpublished (accepted evidence loss) so queue-behind never mistakes it for an"
            " active Run"
        )
        return
    # Removal left remnants (e.g. container-owned files the driver cannot
    # unlink). A tombstone rename WITHIN the jobs dir needs only write
    # permission on the jobs dir itself, never on the remnants — and the
    # dot-prefixed form is skipped by both queue-behind's in-flight signal
    # and the sweep, so it can no longer read as a live Run.
    tombstone = job.with_name(f"{TOMBSTONE_PREFIX}{job.name}-{secrets.token_hex(4)}")
    try:
        job.rename(tombstone)
    except OSError as exc:
        # Unwritable jobs dir: the one state with nothing left to try. The
        # dir stays under its Run name — the sweep may still publish it, so
        # the comment's retained branch stays truthful — but queue-behind
        # will defer until an operator clears it. Record exactly that.
        _park_failure_record(log, "theozolith.evidence-remnants", report, job, tombstone, exc)
        log(
            f"run {job.name}: parking failed twice AND removal left remnants at {job} —"
            " queue-behind may read it as in-flight until an operator clears it"
        )
        return
    report.evidence_discarded = True
    shutil.rmtree(tombstone, ignore_errors=True)  # best-effort; the name is inert either way
    log(
        f"run {job.name}: parking failed twice and removal left remnants — tombstoned as"
        f" {tombstone.name} (accepted evidence loss; ignored by queue-behind and the sweep;"
        " clear it manually)"
    )


def _run_to_pr(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    session_factory: SessionFactory,
    job: Path,
    report: RunReport,
    context: _RunContext,
    log,
    sink: events.EventSink,
) -> None:
    """The Run body: checkout, agent session, gate, ship. Raises _RunFailed
    for every non-shipping outcome (ADR-0016 classification)."""
    workdir = job / jobdir.CHECKOUT_DIR
    branch = report.branch
    login = client.viewer_login()
    email = f"{login}@users.noreply.github.com"
    auth = gitops.auth_env(config.token)

    gitops.clone(config.clone_url, workdir, env=auth)
    gitops.sanitize_checkout(workdir, config.clone_url)
    _exclude_metadata(workdir)
    report.phase = "checkout"

    context.base_branch = gitops.git(["rev-parse", "--abbrev-ref", "HEAD"], workdir)
    existing_pr = client.find_open_pr_by_head(branch)
    revised: verdict.Verdict | None = None
    discussion: list[Comment] = []
    force = False
    if existing_pr is not None:
        report.round = attempts_on(existing_pr.labels) + 1
        report.pr_number = existing_pr.number
        gitops.fetch(workdir, branch, env=auth)
        gitops.checkout_branch(workdir, branch, create=True)
        gitops.reset_hard(workdir, "FETCH_HEAD")
        revised, discussion = _resume_context(client, existing_pr.number)
        if revised is not None and revised.resume_commit:
            if gitops.commit_exists(workdir, revised.resume_commit):
                if revised.resume_commit != gitops.head_sha(workdir):
                    gitops.reset_hard(workdir, revised.resume_commit)
                    force = True
                if revised.cherry_pick:
                    gitops.cherry_pick(workdir, login, email, *revised.cherry_pick)
            else:
                report.notes.append(
                    f"designated resume commit {revised.resume_commit} not found; "
                    "resuming from branch head"
                )
    else:
        gitops.checkout_branch(workdir, branch, create=True)
        if gitops.ref_exists(workdir, f"origin/{branch}"):
            # A previous Run pushed but crashed before opening the PR.
            # That state was never Reviewer-designated: overwrite it.
            force = True
            report.notes.append("stale branch from a crashed Run overwritten (no PR referenced it)")

    prompt = _build_prompt(issue, report.round, revised, discussion)
    jobdir.atomic_write(job / jobdir.PROMPT_FILE, prompt)
    _write_issue_metadata(job, issue, round_number=report.round)
    manifest = jobdir.Manifest(
        run_id=report.run_id,
        mode=jobdir.MODE_RUN,
        session=run_session_name(report.run_id),
        adapter=config.adapter,
        model=config.model,
        workdir=jobdir.CHECKOUT_DIR,
        agent_timeout_seconds=config.agent_timeout_seconds,
        settle_seconds=config.settle_seconds,
    )
    jobdir.write_manifest(job, manifest)
    spec = ContainerSpec(
        name=run_container_name(report.run_id),
        image=config.run_image,
        labels=container_labels(report.run_id, config.stack),
        mounts=((str(job), jobdir.CONTAINER_JOB_PATH),),
        volumes=config.cache_volumes,
        env=dict(config.agent_env),  # never the GitHub PAT (ADR-0013)
        user=config.container_user,
    )

    session = session_factory(spec, job, manifest)
    session.launch()
    report.phase = "agent"
    outcome: jobdir.AgentOutcome | None = None
    harness_error = ""
    gate = GateResult()
    reporter = events.ProgressReporter(
        config, sink, job, issue=issue.number, run_id=report.run_id, attempt=report.round
    )
    try:
        with reporter:
            try:
                outcome = session.wait_for_agent()
                report.agent_outcome = outcome.describe()
            except SessionError as exc:
                harness_error = str(exc)
                report.agent_outcome = "harness-failed"
            if outcome is not None and outcome.completed:
                try:
                    gate = run_gate(
                        workdir,
                        runner=lambda command, timeout: session.run_job("gate", command, timeout),
                    )
                except SessionError as exc:
                    # The agent's work is already in the checkout: ship it
                    # best-effort with the gate failure recorded as a finding.
                    gate = GateResult(
                        findings=[
                            Finding(
                                step="gate",
                                severity="error",
                                summary=f"gate infrastructure failed: {exc}",
                            )
                        ]
                    )
                report.phase = "gate"
                report.gate_findings = len(gate.findings)
                context.gate = gate
                sink.emit(
                    events.run_event(
                        config,
                        issue=issue.number,
                        run_id=report.run_id,
                        phase=events.PHASE_GATE,
                        attempt=report.round,
                    )
                )
    finally:
        session.finish()

    # The container touched the checkout: distrust its git metadata.
    gitops.sanitize_checkout(workdir, config.clone_url)
    _exclude_metadata(workdir)

    agent_section = decisions.read_agent_decisions(workdir)
    context.section = agent_section

    # -- Run-outcome classification (ADR-0014/0016) ---------------------------

    if outcome is None or not outcome.completed:
        if harness_error:
            raise _RunFailed(harness_error, "harness")
        if outcome is not None and outcome.timed_out:
            raise _RunFailed("agent session timed out", "timeout")
        if outcome is not None and outcome.session_died:
            raise _RunFailed("agent session died", "session-died")
        raise _RunFailed(
            f"agent session {(outcome or jobdir.AgentOutcome()).describe()}", "harness"
        )

    section = agent_section or decisions.fallback_section(
        "agent exited without a valid .theozolith/decisions.json"
    )
    section.gate_findings = gate.findings
    context.section = section

    committed = gitops.commit_all(
        workdir, f"Run {report.run_id} for #{issue.number} (round {report.round})", login, email
    )
    empty = not committed
    if empty and not _has_reasoning(agent_section):
        raise _RunFailed("run completed with no commits and no no-change reasoning", "no-changes")
    if empty:
        # A concluded no-change Run still ships: one allow-empty commit,
        # reasoning in the PR body, judged like any PR (ADR-0014).
        gitops.commit_empty(
            workdir,
            f"Run {report.run_id} for #{issue.number} (round {report.round}): no changes required",
            login,
            email,
        )
    report.head = gitops.head_sha(workdir)

    gitops.push(workdir, branch, force=force, env=auth)

    title = f"#{issue.number}: {issue.title}"
    if existing_pr is None:
        body = f"Closes #{issue.number}."
        if empty:
            body += "\n\n" + _no_change_intro(section)
        pr = client.create_pr(
            head=branch, base=context.base_branch, title=title, body=decisions.upsert(body, section)
        )
    else:
        pr = existing_pr
        client.update_pr(pr.number, body=decisions.upsert(pr.body, section))
    report.pr_number = pr.number
    client.add_labels(pr.number, PR_READY)
    report.phase = "empty-pr" if empty else "pr-open"
    sink.emit(
        events.run_event(
            config,
            issue=issue.number,
            run_id=report.run_id,
            phase=events.PHASE_PR_OPEN,
            attempt=report.round,
            pr=pr.number,
        )
    )

    report.evidence_pushed = _push_run_evidence(config, report, issue, job, context, log)


def _fail_run(
    config: DriverConfig,
    issue: Issue,
    report: RunReport,
    job: Path,
    context: _RunContext,
    failed: _RunFailed,
    log,
    sink: events.EventSink,
) -> None:
    """The FAILED-Run path (ADR-0016): evidence and the failed event, nothing
    else. The claim stays with the driver — retry or escalation is
    ``execute_claim``'s call; no state is stamped into comments."""
    report.phase = "failed"
    report.reason = failed.reason
    report.failure_class = failed.failure_class
    if context.section is None:
        context.section = decisions.fallback_section(failed.reason)
    report.evidence_pushed = _push_run_evidence(config, report, issue, job, context, log)
    sink.emit(
        events.run_event(
            config,
            issue=issue.number,
            run_id=report.run_id,
            phase=events.PHASE_FAILED,
            attempt=report.round,
        )
    )
    log(f"run {report.run_id} failed [{report.failure_class}]: {report.reason}")


def _push_run_evidence(
    config: DriverConfig,
    report: RunReport,
    issue: Issue,
    job: Path,
    context: _RunContext,
    log,
) -> bool:
    """Every Run pushes its bundle (ADR-0014) — normal, empty-PR, and failed
    Runs alike. True only when the push confirmed (ADR-0016: the job dir
    outlives an unconfirmed push)."""
    workdir = job / jobdir.CHECKOUT_DIR
    diffstat = ""
    if context.base_branch and workdir.is_dir():
        try:
            diffstat = gitops.diff_stat(workdir, f"origin/{context.base_branch}")
        except Exception as exc:
            diffstat = f"(diffstat unavailable: {exc})"
    section = context.section or decisions.fallback_section(
        report.reason or "no decisions recorded"
    )
    prefix = evidence.run_dir(issue.number, report.run_id)
    transcript = _read_output(job, jobdir.TRANSCRIPT_FILE)
    files = {
        f"{prefix}/run.json": json.dumps(
            {
                "run_id": report.run_id,
                "worker_id": config.worker_id,
                "stack": config.stack,
                "adapter": config.adapter,
                "model": config.model,
                "container": report.container,
                "issue": issue.number,
                "round": report.round,
                "pr": report.pr_number,
                "branch": report.branch,
                "head": report.head,
                "phase": report.phase,
                "agent_outcome": report.agent_outcome,
                "reason": report.reason,
                "failure_class": report.failure_class,
                "notes": report.notes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        f"{prefix}/findings.json": json.dumps(
            [f.__dict__ for f in context.gate.findings], indent=2, sort_keys=True
        )
        + "\n",
        f"{prefix}/decisions.json": section.to_json() + "\n",
        # The full tmux session transcript: the audit trail for any human
        # interaction mid-Run (M2 brief; captured via pipe-pane).
        f"{prefix}/transcript.txt": transcript or "(empty)\n",
        f"{prefix}/diffstat.txt": diffstat + "\n",
    }
    try:
        evidence.push_bundle(
            config.clone_url,
            files,
            message=f"Evidence: run {report.run_id} (issue #{issue.number})",
            author_name=config.worker_id,
            author_email=f"{config.worker_id}@theozolith.invalid",
            env=gitops.auth_env(config.token),
        )
    except Exception as exc:
        # Never fail the Run on evidence, but never swallow it silently: a
        # structured record (ADR-0019) so operators and log scrapers see
        # exactly which bundle is missing and why.
        report.notes.append(f"evidence push failed: {exc}")
        log(
            "evidence push failed: "
            + json.dumps(
                {
                    "event": "theozolith.evidence-push-failed",
                    "run_id": report.run_id,
                    "issue": issue.number,
                    "bundle": prefix,
                    "attempts": evidence.PUSH_ATTEMPTS,
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return False
    return True


# -- the claim lane: local retry, then escalation (ADR-0016) -------------------


def render_claim_escalation(repo: str, issue_number: int, reports: list[RunReport]) -> str:
    lines = ["Both Runs for this claim failed; the local-retry budget is spent (ADR-0016).", ""]
    for report in reports:
        line = f"- Run `{report.run_id}` — **{report.failure_class}**: {report.reason}"
        if report.evidence_pushed:
            url = evidence.run_evidence_url(repo, issue_number, report.run_id)
            line += f" ([evidence]({url}))"
        elif report.evidence_discarded:
            # Never a dead link and never a false promise (ADR-0019/M5):
            # the compound parking failure discarded this bundle for good.
            line += (
                f" — evidence bundle `{evidence.run_dir(issue_number, report.run_id)}` was"
                " lost (push failed after bounded retries and the job directory could not"
                " be parked for boot-sweep recovery; loss accepted over a false in-flight"
                " signal)"
            )
        else:
            # Never a dead link (ADR-0019): name the bundle path the boot
            # sweep will publish to, best-effort, and say why it is absent.
            line += (
                f" — evidence bundle `{evidence.run_dir(issue_number, report.run_id)}` is not"
                " yet published (push failed after bounded retries; the job directory is"
                " retained for boot-sweep recovery)"
            )
        lines.append(line)
    lines += [
        "",
        f"The claim is released and escalated: `{FAILED}` + `{NEEDS_HUMAN}`."
        f" A human re-queues by removing `{FAILED}` and restoring `{PLAN_READY}`.",
    ]
    return "\n".join(lines)


def execute_claim(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    session_factory: SessionFactory,
    *,
    log=_log,
    sink: events.EventSink | None = None,
) -> list[RunReport]:
    """Everything one granted claim owes: up to CLAIM_RUN_BUDGET Runs (the
    original and one local retry), then release + failed + needs_human with
    complete forensics. Returns the reports, one per Run executed."""
    sink = sink or events.make_sink(config, log)

    def _emit_claimed(run_id: str) -> bool:
        claimed = events.run_event(
            config, issue=issue.number, run_id=run_id, phase=events.PHASE_CLAIMED
        )
        return sink.emit(claimed)

    reports: list[RunReport] = []
    for run_number in range(1, CLAIM_RUN_BUDGET + 1):
        run_id = new_run_id(config)
        if run_number == 1:
            # The activation handshake (ADR-0017): without a landed claimed
            # event the Control Node releases this grant after ~60s; running
            # anyway would fork ownership. Walk away and let it.
            if not any(_emit_claimed(run_id) for _ in range(ACTIVATION_ATTEMPTS)):
                log(
                    f"issue #{issue.number}: claimed event never reached the Control Node;"
                    " abandoning the grant (it will be released, ADR-0017)"
                )
                return reports
        else:
            # The claim is already activated: the retry's claimed event is
            # best-effort visibility, never a gate.
            _emit_claimed(run_id)
        report = execute_run(
            config, client, issue, session_factory, run_id=run_id, log=log, sink=sink
        )
        reports.append(report)
        if report.phase != "failed":
            return reports
        if run_number < CLAIM_RUN_BUDGET:
            log(
                f"run {report.run_id} failed [{report.failure_class}]; keeping the claim and"
                " launching the one local retry (ADR-0016)"
            )
    _escalate_claim(config, client, issue, reports, log, sink)
    return reports


def _escalate_claim(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    reports: list[RunReport],
    log,
    sink: events.EventSink,
) -> None:
    """The local-retry budget is spent: release the claim and hand the issue
    to a human with both failures' forensics (ADR-0016). failed means
    execution broke — autopsy the evidence; only a human removes it.

    Write order matters: the escalation labels and forensics land BEFORE the
    claim is stripped, so a GitHub failure mid-sequence can never leave the
    issue label-less and invisible — the worst partial outcome is an
    escalated issue still wearing its claim, which a human sees either way.
    """
    client.add_labels(issue.number, FAILED, NEEDS_HUMAN)
    client.add_comment(issue.number, render_claim_escalation(config.repo, issue.number, reports))
    me = client.viewer_login()
    fresh = client.get_issue(issue.number)
    if me in fresh.assignees:
        client.remove_assignee(issue.number, me)
    client.remove_label(issue.number, IN_PROGRESS)
    last = reports[-1]
    sink.emit(
        events.run_event(
            config,
            issue=issue.number,
            run_id=last.run_id,
            phase=events.PHASE_ESCALATED,
            attempt=last.round,
        )
    )
    log(
        f"issue #{issue.number}: local-retry budget spent; claim released and escalated"
        f" ({FAILED} + {NEEDS_HUMAN})"
    )
