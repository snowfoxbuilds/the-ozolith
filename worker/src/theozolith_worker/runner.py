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

Run outcomes (ADR-0014): a completed session with commits ships the normal
best-effort PR; a completed session with zero commits but no-change
reasoning in the decisions payload ships an EMPTY PR (one driver-synthesized
allow-empty commit) the Reviewer judges like any other; everything else — no
commits and no reasoning, a timed-out or dead session, a container/harness
crash — is a FAILED Run: evidence is pushed, the claim is stripped, the
issue returns to plan_ready, and a machine-readable run-failed marker is
stamped on the issue. The failed-Run budget is one retry: a failure with a
marker already present escalates blocked + needs_human instead. Every Run
that reaches a checkout pushes its evidence bundle before job-dir cleanup.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from theozolith_worker import decisions, events, evidence, gitops, jobdir, verdict
from theozolith_worker.bootstrap.vocabulary import (
    BLOCKED,
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

BRANCH_PREFIX = "ozolith/issue-"

# Paths in the checkout that are pipeline metadata, never repo content.
EXCLUDED_METADATA = (".theozolith/decisions.json", ".claude/settings.local.json")

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


# -- the run-failed marker (issue-comment bookkeeping, ADR-0014) --------------

RUN_FAILED_RE = re.compile(r'<!-- theozolith:run-failed run_id=(\S+) reason="([^"]*)" -->')


def _sanitize_reason(reason: str) -> str:
    return re.sub(r"\s+", " ", reason).replace('"', "'").strip()[:300]


def render_run_failed(run_id: str, reason: str, *, escalated: bool) -> str:
    outcome = (
        "Retry budget exhausted — escalating to a human (`blocked` + `needs_human`)."
        if escalated
        else "Re-queued for one retry (the failed-Run budget, ADR-0014)."
    )
    return (
        f"Run `{run_id}` failed: {reason}. {outcome}\n\n"
        f'<!-- theozolith:run-failed run_id={run_id} reason="{_sanitize_reason(reason)}" -->'
    )


def parse_run_failed(body: str) -> tuple[str, str] | None:
    """(run_id, reason) from a run-failed marker comment, or None."""
    match = RUN_FAILED_RE.search(body)
    return (match.group(1), match.group(2)) if match else None


def _issue_has_failure_marker(client: GitHubClient, issue_number: int) -> bool:
    return any(parse_run_failed(comment.body) for comment in client.list_comments(issue_number))


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
decisions afterward.
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
    reason: str = ""  # failed Runs: the reason stamped in the marker
    notes: list[str] = field(default_factory=list)


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
    """One Run on one claimed issue. Assumes the claim already succeeded."""
    run_id = run_id or new_run_id(config)
    sink = sink or events.make_sink(config, log)
    branch = branch_for(issue.number)
    login = client.viewer_login()
    email = f"{login}@users.noreply.github.com"
    auth = gitops.auth_env(config.token)

    # The failed-Run budget check happens at claim time: a marker already on
    # the issue means this Run is the one retry (ADR-0014).
    already_failed = _issue_has_failure_marker(client, issue.number)

    job = jobdir.create_job_dir(config.jobs_dir, run_id)
    workdir = job / jobdir.CHECKOUT_DIR
    report = RunReport(
        run_id=run_id,
        issue=issue.number,
        round=1,
        branch=branch,
        container=run_container_name(run_id),
    )
    try:
        gitops.clone(config.clone_url, workdir, env=auth)
        gitops.sanitize_checkout(workdir, config.clone_url)
        _exclude_metadata(workdir)
        report.phase = "checkout"

        base_branch = gitops.git(["rev-parse", "--abbrev-ref", "HEAD"], workdir)
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
                report.notes.append(
                    "stale branch from a crashed Run overwritten (no PR referenced it)"
                )

        prompt = _build_prompt(issue, report.round, revised, discussion)
        jobdir.atomic_write(job / jobdir.PROMPT_FILE, prompt)
        jobdir.atomic_write(
            job / "input" / "issue.json",
            json.dumps(
                {
                    "number": issue.number,
                    "title": issue.title,
                    "body": issue.body,
                    "labels": sorted(issue.labels),
                    "round": report.round,
                },
                indent=2,
                sort_keys=True,
            ),
        )
        manifest = jobdir.Manifest(
            run_id=run_id,
            mode=jobdir.MODE_RUN,
            session=run_session_name(run_id),
            adapter=config.adapter,
            model=config.model,
            workdir=jobdir.CHECKOUT_DIR,
            agent_timeout_seconds=config.agent_timeout_seconds,
            settle_seconds=config.settle_seconds,
        )
        jobdir.write_manifest(job, manifest)
        spec = ContainerSpec(
            name=run_container_name(run_id),
            image=config.run_image,
            labels=container_labels(run_id, config.stack),
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
        try:
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
                sink.emit(
                    events.run_event(
                        config,
                        issue=issue.number,
                        run_id=run_id,
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

        # -- Run-outcome classification (ADR-0014) ---------------------------

        if outcome is None or not outcome.completed:
            reason = harness_error or f"agent session {outcome.describe()}"
            return _fail_run(
                config,
                client,
                issue,
                report,
                job,
                workdir,
                base_branch,
                agent_section,
                gate,
                reason,
                already_failed,
                auth,
                log,
                sink,
            )

        section = agent_section or decisions.fallback_section(
            "agent exited without a valid .theozolith/decisions.json"
        )
        section.gate_findings = gate.findings

        committed = gitops.commit_all(
            workdir, f"Run {run_id} for #{issue.number} (round {report.round})", login, email
        )
        empty = not committed
        if empty and not _has_reasoning(agent_section):
            return _fail_run(
                config,
                client,
                issue,
                report,
                job,
                workdir,
                base_branch,
                agent_section,
                gate,
                "run completed with no commits and no no-change reasoning",
                already_failed,
                auth,
                log,
                sink,
            )
        if empty:
            # A concluded no-change Run still ships: one allow-empty commit,
            # reasoning in the PR body, judged like any PR (ADR-0014).
            gitops.commit_empty(
                workdir,
                f"Run {run_id} for #{issue.number} (round {report.round}): no changes required",
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
                head=branch, base=base_branch, title=title, body=decisions.upsert(body, section)
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
                run_id=run_id,
                phase=events.PHASE_PR_OPEN,
                attempt=report.round,
                pr=pr.number,
            )
        )

        _push_run_evidence(
            config, report, issue, job, gate, section, workdir, base_branch, auth, log
        )
        return report
    finally:
        shutil.rmtree(job, ignore_errors=True)


def _fail_run(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    report: RunReport,
    job: Path,
    workdir: Path,
    base_branch: str,
    agent_section: decisions.DecisionsSection | None,
    gate: GateResult,
    reason: str,
    already_failed: bool,
    auth: dict[str, str],
    log,
    sink: events.EventSink,
) -> RunReport:
    """The failed-Run path: evidence, claim strip, re-queue or escalate,
    run-failed marker. Failed Runs never touch attempt-N and open no PR."""
    report.phase = "failed"
    report.reason = reason
    section = agent_section or decisions.fallback_section(reason)
    _push_run_evidence(config, report, issue, job, gate, section, workdir, base_branch, auth, log)

    me = client.viewer_login()
    fresh = client.get_issue(issue.number)
    if me in fresh.assignees:
        client.remove_assignee(issue.number, me)
    client.remove_label(issue.number, IN_PROGRESS)
    if already_failed:
        # The one-retry budget is spent: a human takes over.
        client.add_labels(issue.number, BLOCKED, NEEDS_HUMAN)
        report.notes.append("failed-Run budget exhausted; escalated to a human")
    else:
        client.add_labels(issue.number, PLAN_READY)
        report.notes.append("re-queued for one retry")
    client.add_comment(
        issue.number, render_run_failed(report.run_id, reason, escalated=already_failed)
    )
    sink.emit(
        events.run_event(
            config,
            issue=issue.number,
            run_id=report.run_id,
            phase=events.PHASE_ESCALATED if already_failed else events.PHASE_FAILED,
            attempt=report.round,
        )
    )
    log(f"run {report.run_id} failed ({reason}); " + report.notes[-1])
    return report


def _push_run_evidence(
    config: DriverConfig,
    report: RunReport,
    issue: Issue,
    job: Path,
    gate: GateResult,
    section: decisions.DecisionsSection,
    workdir: Path,
    base_branch: str,
    auth: dict[str, str],
    log,
) -> None:
    """Every Run that reached a checkout pushes its bundle (ADR-0014) —
    normal, empty-PR, and failed Runs alike, before job-dir cleanup."""
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
                "notes": report.notes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        f"{prefix}/findings.json": json.dumps(
            [f.__dict__ for f in gate.findings], indent=2, sort_keys=True
        )
        + "\n",
        f"{prefix}/decisions.json": section.to_json() + "\n",
        # The full tmux session transcript: the audit trail for any human
        # interaction mid-Run (M2 brief; captured via pipe-pane).
        f"{prefix}/transcript.txt": transcript or "(empty)\n",
        f"{prefix}/diffstat.txt": gitops.diff_stat(workdir, f"origin/{base_branch}") + "\n",
    }
    try:
        evidence.push_bundle(
            config.clone_url,
            files,
            message=f"Evidence: run {report.run_id} (issue #{issue.number})",
            author_name=config.worker_id,
            author_email=f"{config.worker_id}@theozolith.invalid",
            env=auth,
        )
    except Exception as exc:
        # Never fail the Run on evidence, but never swallow it silently.
        report.notes.append(f"evidence push failed: {exc}")
        log(f"evidence push failed for run {report.run_id}: {exc}")
