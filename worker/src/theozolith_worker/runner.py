"""Run execution: one stateless, disposable attempt at one claimed issue.

Every Run: fresh clone, fresh agent context. Resume state is exactly what the
Reviewer designated — the PR branch at the resume commit (plus cherry-picked
commits) and the revised plan in the verdict comment — nothing else survives
between Runs (ADR-0008).

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

from theozolith_worker import decisions, evidence, gitops, verdict
from theozolith_worker.adapters import Adapter
from theozolith_worker.bootstrap.vocabulary import PR_READY, attempts_on
from theozolith_worker.config import ActorConfig
from theozolith_worker.gate.pipeline import run_gate
from theozolith_worker.githubapi import Comment, GitHubClient, Issue

BRANCH_PREFIX = "ozolith/issue-"

# Distinguishes Runs started within the same second by one Worker process.
_RUN_SEQUENCE = itertools.count(1)


def branch_for(issue_number: int) -> str:
    return f"{BRANCH_PREFIX}{issue_number}"


def issue_for_branch(head_ref: str) -> int | None:
    suffix = head_ref.removeprefix(BRANCH_PREFIX)
    return int(suffix) if head_ref != suffix and suffix.isdigit() else None


PROMPT_TEMPLATE = """\
You are a Worker in TheOzolith agentic coding pipeline, executing one \
stateless Run against the checked-out repository (your working directory).

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
    adapter_ok: bool = False
    gate_findings: int = 0
    notes: list[str] = field(default_factory=list)


def _resume_context(
    client: GitHubClient, pr_number: int, round_number: int
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


def execute_run(
    config: ActorConfig,
    client: GitHubClient,
    adapter: Adapter,
    issue: Issue,
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

    workdir = config.workdir / f"run-{run_id}"
    workdir.parent.mkdir(parents=True, exist_ok=True)
    report = RunReport(run_id=run_id, issue=issue.number, round=1, branch=branch)
    try:
        gitops.clone(config.clone_url, workdir)
        report.phase = "checkout"
        # The decisions file is pipeline metadata, not repo content.
        exclude = workdir / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{decisions.AGENT_FILE.as_posix()}\n")

        base_branch = gitops.git(["rev-parse", "--abbrev-ref", "HEAD"], workdir)
        existing_pr = client.find_open_pr_by_head(branch)
        revised: verdict.Verdict | None = None
        discussion: list[Comment] = []
        force = False
        if existing_pr is not None:
            report.round = attempts_on(existing_pr.labels) + 1
            report.pr_number = existing_pr.number
            gitops.git(["fetch", "--quiet", "origin", branch], workdir)
            gitops.checkout_branch(workdir, branch, create=True)
            gitops.reset_hard(workdir, "FETCH_HEAD")
            revised, discussion = _resume_context(client, existing_pr.number, report.round)
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
        agent = adapter.execute(prompt, workdir)
        report.phase = "agent"
        report.adapter_ok = agent.ok
        if not agent.ok:
            report.notes.append("agent process exited non-zero; shipping best-effort state")

        gate = run_gate(workdir)
        report.phase = "gate"
        report.gate_findings = len(gate.findings)

        section = decisions.read_agent_decisions(workdir)
        if section is None:
            reason = (
                "agent exited without a valid .theozolith/decisions.json"
                if agent.ok
                else "agent process failed"
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

        gitops.push(workdir, branch, force=force)

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

        _push_run_evidence(
            config,
            report,
            issue,
            agent.transcript,
            gate,
            section,
            workdir,
            base_branch,
            login,
            email,
        )
        return report
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _push_run_evidence(
    config: ActorConfig,
    report: RunReport,
    issue: Issue,
    transcript: str,
    gate,
    section: decisions.DecisionsSection,
    workdir: Path,
    base_branch: str,
    author: str,
    email: str,
) -> None:
    prefix = evidence.run_dir(issue.number, report.run_id)
    files = {
        f"{prefix}/run.json": json.dumps(
            {
                "run_id": report.run_id,
                "worker_id": config.worker_id,
                "adapter": config.adapter,
                "model": config.model,
                "issue": issue.number,
                "round": report.round,
                "pr": report.pr_number,
                "branch": report.branch,
                "head": report.head,
                "phase": report.phase,
                "adapter_ok": report.adapter_ok,
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
        f"{prefix}/transcript.txt": transcript or "(empty)\n",
        f"{prefix}/diffstat.txt": gitops.diff_stat(workdir, f"origin/{base_branch}") + "\n",
    }
    try:
        evidence.push_bundle(
            config.clone_url,
            files,
            message=f"Evidence: run {report.run_id} (issue #{issue.number})",
            author_name=author,
            author_email=email,
        )
    except gitops.GitError as exc:
        # Evidence is traceability, not coordination: never fail the Run on it.
        report.notes.append(f"evidence push failed: {exc}")
