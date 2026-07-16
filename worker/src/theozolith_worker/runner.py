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

Best-effort PR contract: every Run that reaches a checkout ends by pushing
whatever it has and opening/updating the one PR for the issue with a
Decisions Section, then applying pr_ready. The Worker never stops mid-Run to
ask; a Run that cannot produce a PR (nothing to push and no PR yet) leaves no
PR-side state and consumes no round budget.
"""

from __future__ import annotations

import itertools
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from theozolith_worker import decisions, evidence, gitops, jobdir, verdict
from theozolith_worker.bootstrap.vocabulary import PR_READY, attempts_on
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
decisions afterward.
- Record your run summary in `.theozolith/decisions.json` (create the file) \
with exactly this JSON shape:
  {{"decisions": [{{"what": "...", "why": "..."}}], "open_questions": [], \
"remaining_work": [], "dead_ends": []}}
  Decisions are judgment calls with rationale; open_questions are calls only \
a human can make; remaining_work is what a follow-up round still needs; \
dead_ends are approaches you tried and abandoned (so the next Run does not \
repeat them).
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
    phase: str = "claimed"  # claimed | checkout | agent | gate | pr-open | no-pr
    pr_number: int | None = None
    branch: str = ""
    head: str = ""
    container: str = ""
    agent_outcome: str = ""  # completed | timed out | session died | unknown
    gate_findings: int = 0
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


def execute_run(
    config: DriverConfig,
    client: GitHubClient,
    issue: Issue,
    session_factory: SessionFactory,
    *,
    run_id: str | None = None,
) -> RunReport:
    """One Run on one claimed issue. Assumes the claim already succeeded."""
    run_id = run_id or (
        f"{time.strftime('%Y%m%dT%H%M%S')}-{config.worker_id}-{next(_RUN_SEQUENCE)}"
    )
    branch = branch_for(issue.number)
    login = client.viewer_login()
    email = f"{login}@users.noreply.github.com"
    auth = gitops.auth_env(config.token)

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
        try:
            outcome = session.wait_for_agent()
            report.agent_outcome = outcome.describe()
            if not outcome.completed:
                report.notes.append(
                    f"agent session {outcome.describe()}; shipping best-effort state"
                )
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
        finally:
            session.finish()

        # The container touched the checkout: distrust its git metadata.
        gitops.sanitize_checkout(workdir, config.clone_url)
        _exclude_metadata(workdir)

        section = decisions.read_agent_decisions(workdir)
        if section is None:
            reason = (
                "agent exited without a valid .theozolith/decisions.json"
                if report.agent_outcome == "completed"
                else f"agent session {report.agent_outcome or 'failed'}"
            )
            section = decisions.fallback_section(reason)
        section.gate_findings = gate.findings

        committed = gitops.commit_all(
            workdir, f"Run {run_id} for #{issue.number} (round {report.round})", login, email
        )
        head = gitops.head_sha(workdir)
        report.head = head

        base_sha = gitops.git(["rev-parse", f"origin/{base_branch}"], workdir)
        if existing_pr is None and not committed and head == base_sha:
            # Nothing to push and no PR to update: no PR-side state, no round.
            report.phase = "no-pr"
            report.notes.append("run produced no changes and no PR exists; nothing shipped")
            return report

        gitops.push(workdir, branch, force=force, env=auth)

        title = f"#{issue.number}: {issue.title}"
        if existing_pr is None:
            pr = client.create_pr(
                head=branch,
                base=base_branch,
                title=title,
                body=decisions.upsert(f"Closes #{issue.number}.", section),
            )
        else:
            pr = existing_pr
            client.update_pr(pr.number, body=decisions.upsert(pr.body, section))
        report.pr_number = pr.number
        client.add_labels(pr.number, PR_READY)
        report.phase = "pr-open"

        _push_run_evidence(config, report, issue, job, gate, section, workdir, base_branch, auth)
        return report
    finally:
        shutil.rmtree(job, ignore_errors=True)


def _push_run_evidence(
    config: DriverConfig,
    report: RunReport,
    issue: Issue,
    job: Path,
    gate,
    section: decisions.DecisionsSection,
    workdir: Path,
    base_branch: str,
    auth: dict[str, str],
) -> None:
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
    except gitops.GitError as exc:
        # Evidence is traceability, not coordination: never fail the Run on it.
        report.notes.append(f"evidence push failed: {exc}")
