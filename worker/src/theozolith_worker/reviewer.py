"""The Reviewer driver: a separate node-resident process owning all post-PR state.

Own GitHub identity, stronger model than the Implementer adapters — no
self-grading by construction (ADR-0008). Discovers reviewable pr_ready PRs
through the Control Node's dispatch endpoint (ADR-0017, discovery-only) and
runs each review round as an
ephemeral container (ADR-0013): the driver materializes the review inputs as
files (issue intent, diff, Decisions Section, mechanical signals), the
judging agent runs headless (ADR-0019) and writes its verdict as a file,
and the driver validates the file, renders the evidence-citing comment, and
applies exactly one verdict:

- approve: needs_human (keeping pr_ready) + deviation:* + risk:* + an
  evidence-citing comment; the human stamps and merges. Approve means no
  revisions are needed at all.
- revise: verdict comment (revised plan + resume commit) first, then
  attempt-N, then pr_ready comes off, then the issue claim is stripped and
  the issue re-queued to plan_ready — explicitly delegated human authority.
- escalate: blocked + needs_human with the evidence bundle link; also forced
  deterministically when the round budget is exhausted.

ANY invalid verdict — missing file, unparseable JSON, schema failure, or a
revise on the final budgeted round — escalates immediately (ADR-0014): the
driver applies blocked + needs_human with a comment carrying the raw
validation error, the bundle link, and the evidence path of the offending
verdict file. One strike; there is no driver-side retry — the judging
agent's second chances live inside its own session via the validate-verdict
harness job.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import ClassVar

from theozolith_worker import adapters, evidence, gitops, jobdir, runner, verdict
from theozolith_worker.base import Worker
from theozolith_worker.bootstrap.vocabulary import (
    BLOCKED,
    DEVIATION_PREFIX,
    IN_PROGRESS,
    NEEDS_HUMAN,
    PLAN_READY,
    PR_READY,
    RISK_PREFIX,
    ROUND_BUDGET,
    attempt_label,
    attempts_on,
    reviewable,
)
from theozolith_worker.config import DriverConfig
from theozolith_worker.containers import (
    ContainerSpec,
    container_labels,
    review_container_name,
)
from theozolith_worker.decisions import section_text
from theozolith_worker.events import EventSink, emit_error, make_sink, review_event
from theozolith_worker.githubapi import GitHubClient, PullRequest
from theozolith_worker.sessions import SessionFactory
from theozolith_worker.signals import compute_signals

DIFF_LIMIT = 200_000

NO_PATCH_NOTE = "(no patch available — binary or very large file; see signals.md)"

REVIEW_PROMPT = """\
You are the Reviewer in TheOzolith agentic coding pipeline. An Implementer \
shipped a best-effort PR; you own the verdict. You never implement — you judge \
the diff against the issue's stated intent and acceptance criteria, and you \
judge the decisions the Implementer recorded, not just the code.

Your working directory contains the review inputs as files:

- `issue.md` — the issue: stated intent and acceptance criteria
- `diff.patch` — the diff of the PR as shipped, built from the PR-files \
API. Binary and very large files carry no patch (marked "{no_patch_note}" \
and listed in `signals.md`): the diff is partial for those entries — say so \
in your evidence rather than guessing at their contents.
- `decisions.md` — the PR's Decisions Section (recorded by the Implementer)
- `signals.md` — mechanical diff signals (computed evidence — weigh it, it \
is not a grader)

## Review round

This verdict closes round {round} of {budget}. {round_rule}

## Verdict semantics

- **approve** means NO revisions are needed at all. Approve-with-nits is \
forbidden: anything worth fixing is a revise; anything not worth fixing is \
dropped, not mentioned as a condition of approval.
- An empty PR (a single no-change commit) whose body and Decisions Section \
carry justified no-change reasoning earns approve — the human closes the \
issue.
- **revise** re-queues the issue: give a numbered, concrete revised plan and \
the resume commit the next Run starts from.
- **escalate** hands the PR to a human (blocked + needs_human) when a \
decision only a human may make is blocking (contradictory acceptance \
criteria, an open question the Worker flagged that you cannot settle).

## Your verdict

Write exactly one file named `verdict.json` in your working directory (no \
other output counts) with this JSON shape:

{{"verdict": "approve" | "revise" | "escalate",
  "deviation": "low" | "medium" | "high",
  "risk": "low" | "medium" | "high",
  "evidence": "2-6 sentences citing specific files, criteria, and recorded decisions",
  "revised_plan": "numbered, concrete steps for the next Run (revise only, else empty)",
  "resume_commit": "commit SHA the next Run resets the branch to (revise \
only; empty means current head)",
  "cherry_pick": [],
  "process_issues": []}}

process_issues is optional and advisory: observations about the PIPELINE \
itself (friction you hit reviewing — missing inputs, confusing evidence), \
each as {{"friction": "...", "suggested_fix": "..."}}. Never findings about \
the change under review; it influences no verdict, label, or gate outcome.

deviation grades divergence from the plan (files outside the plan's \
footprint, unrequested behavior, new dependencies, size far beyond \
expectation). risk is your own overall read of the change as implemented, \
independent of the issue's baseline label. approve only when the acceptance \
criteria are met by the diff as shipped.

Any INVALID verdict — missing file, unparseable JSON, schema violation, or \
a revise on the final budgeted round — escalates this PR straight to a \
human; there is NO retry. Before finishing, run `theozolith-validate-verdict` \
in your working directory: it applies the exact validation the driver will \
(schema and round rules) and reports errors while you can still fix them.
"""

MIDDLE_ROUND_RULE = (
    "A revise verdict re-queues the issue for another Run that a later round reviews."
)
FINAL_ROUND_RULE = (
    "This is the LAST budgeted round: revise is unavailable — approve or "
    "escalate only. A revise would be an invalid verdict and escalate this "
    "PR to a human."
)


def _log(message: str) -> None:
    print(message, flush=True)


def _strip_claim_and_requeue(client: GitHubClient, issue_number: int) -> None:
    """Delegated authority: return the issue to the claimable pool."""
    issue = client.get_issue(issue_number)
    for login in issue.assignees:
        client.remove_assignee(issue_number, login)
    client.remove_label(issue_number, IN_PROGRESS)
    client.add_labels(issue_number, PLAN_READY)


def _review_inputs(client: GitHubClient, pr: PullRequest, issue_number: int) -> dict[str, str]:
    issue = client.get_issue(issue_number)
    files = client.pr_files(pr.number)
    signals = compute_signals(files)
    diff = "\n".join(
        f"--- {f.path} ({f.status})\n{f.patch if f.patch else NO_PATCH_NOTE}" for f in files
    )
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n[diff truncated]"
    return {
        "issue.md": f"# Issue #{issue.number}: {issue.title}\n\n{issue.body or '(no body)'}\n",
        "diff.patch": (diff or "(empty diff)") + "\n",
        "decisions.md": (section_text(pr.body) or "(missing — judge accordingly)") + "\n",
        "signals.md": signals.render() + "\n",
    }


def _read_transcript(job: Path) -> str:
    try:
        return (job / jobdir.TRANSCRIPT_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""


def _emit_review(
    sink: EventSink,
    config: DriverConfig,
    pr: PullRequest,
    issue_number: int,
    result: verdict.Verdict,
) -> None:
    sink.emit(
        review_event(
            config,
            pr=pr.number,
            issue=issue_number,
            round_number=result.round,
            verdict=result.verdict,
        )
    )


def review_pr(
    config: DriverConfig,
    client: GitHubClient,
    pr: PullRequest,
    session_factory: SessionFactory,
    *,
    log=_log,
    sink: EventSink | None = None,
) -> verdict.Verdict | None:
    """Run one review round and apply the verdict. None = PR skipped."""
    sink = sink or make_sink(config, log)
    issue_number = runner.issue_for_branch(pr.head_ref)
    if issue_number is None:
        log(f"PR #{pr.number} head {pr.head_ref!r} is not a pipeline branch; skipping")
        return None
    rounds_spent = attempts_on(pr.labels)
    round_number = rounds_spent + 1
    bundle_url = evidence.issue_evidence_url(config.repo, issue_number)

    if rounds_spent >= ROUND_BUDGET:
        # The budget check cannot be argued with: no model call. The verdict
        # closes no new round, so it is stamped with the last budgeted one.
        result = verdict.Verdict(
            verdict=verdict.ESCALATE,
            round=ROUND_BUDGET,
            evidence=(
                f"Round budget exhausted: {ROUND_BUDGET} review rounds spent on this "
                "issue. A human decision is required to continue."
            ),
            bundle_url=bundle_url,
        )
        _apply(config, client, pr, issue_number, result, log, container="", sink=sink)
        _emit_review(sink, config, pr, issue_number, result)
        return result

    review_id = f"review-{pr.number}-round-{round_number}"
    container = review_container_name(pr.number, round_number)
    job = jobdir.create_job_dir(config.jobs_dir, review_id)
    try:
        work = job / jobdir.WORK_DIR
        work.mkdir(parents=True, exist_ok=True)
        for name, content in _review_inputs(client, pr, issue_number).items():
            (work / name).write_text(content, encoding="utf-8")

        final_round = round_number >= ROUND_BUDGET
        prompt = REVIEW_PROMPT.format(
            round=round_number,
            budget=ROUND_BUDGET,
            round_rule=FINAL_ROUND_RULE if final_round else MIDDLE_ROUND_RULE,
            no_patch_note=NO_PATCH_NOTE,
        )
        jobdir.atomic_write(job / jobdir.PROMPT_FILE, prompt)
        manifest = jobdir.Manifest(
            run_id=review_id,
            mode=jobdir.MODE_REVIEW,
            adapter=config.adapter,
            workdir=jobdir.WORK_DIR,
            agent_timeout_seconds=config.agent_timeout_seconds,
            round=round_number,
            round_budget=ROUND_BUDGET,
        )
        jobdir.write_manifest(job, manifest)
        spec = ContainerSpec(
            name=container,
            image=config.run_image,
            labels=container_labels(review_id, config.stack),
            mounts=((str(job), jobdir.CONTAINER_JOB_PATH),),
            volumes=config.cache_volumes,
            env=dict(config.agent_env),  # never the GitHub PAT (ADR-0013)
            user=config.container_user,
        )

        session = session_factory(spec, job, manifest)
        session.launch()
        try:
            session.wait_for_agent()
        finally:
            session.finish()

        result, reason = verdict.validate_verdict_file(
            job / jobdir.VERDICT_FILE,
            round_number=round_number,
            final_round=final_round,
            default_resume=pr.head_sha,
            bundle_url=bundle_url,
        )
        transcript = _read_transcript(job)
        if result is None:
            # One strike: an invalid verdict escalates immediately — no
            # second model call, no re-poll of this PR (ADR-0014).
            escalated = _escalate_invalid_verdict(
                config,
                client,
                pr,
                issue_number,
                round_number,
                reason,
                job,
                bundle_url,
                transcript,
                container,
                log,
                sink=sink,
            )
            _emit_review(sink, config, pr, issue_number, escalated)
            return escalated

        _apply(
            config,
            client,
            pr,
            issue_number,
            result,
            log,
            transcript=transcript,
            container=container,
            observed=adapters.stream_stats(config.adapter, job / jobdir.TRANSCRIPT_FILE),
            sink=sink,
        )
        _emit_review(sink, config, pr, issue_number, result)
        return result
    finally:
        shutil.rmtree(job, ignore_errors=True)


def _escalate_invalid_verdict(
    config: DriverConfig,
    client: GitHubClient,
    pr: PullRequest,
    issue_number: int,
    round_number: int,
    reason: str,
    job: Path,
    bundle_url: str,
    transcript: str,
    container: str,
    log,
    sink: EventSink | None = None,
) -> verdict.Verdict:
    """Apply the one-strike rule: evidence first (so the cited path
    resolves), then blocked + needs_human with the raw validation error."""
    prefix = f"runs/issue-{issue_number}/reviews/round-{round_number}-{pr.head_sha[:12]}-invalid"
    stats = adapters.stream_stats(config.adapter, job / jobdir.TRANSCRIPT_FILE)
    record = {
        "pr": pr.number,
        "issue": issue_number,
        "round": round_number,
        "verdict": None,
        "error": reason,
        "head": pr.head_sha,
        # ADR-0045: image identity (its tag covers the baked model) plus the
        # reconciled stream-observed model; model_note carries any
        # disagreement between the stream's model signals.
        "run_image": config.run_image,
        "model": stats.model,
        "model_note": stats.model_note,
        "container": container,
    }
    files = {f"{prefix}.json": json.dumps(record, indent=2, sort_keys=True) + "\n"}
    verdict_path: str | None = None
    try:
        raw = (job / jobdir.VERDICT_FILE).read_text(encoding="utf-8")
    except OSError:
        raw = None
    if raw is not None:
        verdict_path = f"{prefix}-verdict.json"
        files[verdict_path] = raw if raw.endswith("\n") else raw + "\n"
    if transcript:
        files[f"{prefix}-transcript.txt"] = transcript
    _push_evidence_files(
        config,
        files,
        message=f"Evidence: invalid verdict, review round {round_number} (issue #{issue_number})",
        log=log,
        context=f"PR #{pr.number}",
        sink=sink,
    )

    location = (
        f"The offending verdict file is preserved in the evidence bundle at `{verdict_path}`."
        if verdict_path
        else "The session emitted no verdict file."
    )
    result = verdict.Verdict(
        verdict=verdict.ESCALATE,
        round=round_number,
        evidence=(
            "The review session produced an invalid verdict; escalating to a human "
            f"(one strike, no retry — ADR-0014). Validation error: {reason}. {location}"
        ),
        bundle_url=bundle_url,
    )
    _publish(client, pr, issue_number, result, log)
    return result


def _apply(
    config: DriverConfig,
    client: GitHubClient,
    pr: PullRequest,
    issue_number: int,
    result: verdict.Verdict,
    log,
    *,
    transcript: str = "",
    container: str = "",
    observed: adapters.StreamStats | None = None,
    sink: EventSink | None = None,
) -> None:
    _publish(client, pr, issue_number, result, log)
    _push_review_evidence(
        config,
        pr,
        issue_number,
        result,
        transcript,
        container,
        log,
        observed=observed,
        sink=sink,
    )


def _publish(
    client: GitHubClient,
    pr: PullRequest,
    issue_number: int,
    result: verdict.Verdict,
    log,
) -> None:
    # The verdict comment lands first: no observable verdict state without
    # the plan and evidence that explain it.
    client.add_comment(pr.number, verdict.render_comment(result))
    if result.verdict == verdict.APPROVE:
        client.add_labels(
            pr.number,
            NEEDS_HUMAN,  # keeping pr_ready: awaiting human stamp
            f"{DEVIATION_PREFIX}{result.deviation}",
            f"{RISK_PREFIX}{result.risk}",
        )
    elif result.verdict == verdict.REVISE:
        client.add_labels(pr.number, attempt_label(result.round))
        client.remove_label(pr.number, PR_READY)
        _strip_claim_and_requeue(client, issue_number)
    else:  # escalate
        client.remove_label(pr.number, PR_READY)
        client.add_labels(pr.number, BLOCKED, NEEDS_HUMAN)
    log(f"PR #{pr.number}: {result.verdict} (round {result.round})")


def _push_evidence_files(
    config: DriverConfig,
    files: dict[str, str],
    *,
    message: str,
    log,
    context: str,
    sink: EventSink | None = None,
) -> None:
    """Push evidence, logging failures — never silently, never fatally."""
    try:
        evidence.push_bundle(
            config.clone_url,
            files,
            message=message,
            author_name=config.worker_id,
            author_email=f"{config.worker_id}@theozolith.invalid",
            env=gitops.auth_env(config.token),
        )
    except Exception as exc:
        log(f"evidence push failed for {context}: {exc}")
        if sink is not None:
            emit_error(
                sink,
                config,
                error_class=type(exc).__name__,
                message=f"evidence push failed for {context}: {exc}",
            )


def _push_review_evidence(
    config: DriverConfig,
    pr: PullRequest,
    issue_number: int,
    result: verdict.Verdict,
    transcript: str,
    container: str,
    log,
    observed: adapters.StreamStats | None = None,
    sink: EventSink | None = None,
) -> None:
    record = {
        "pr": pr.number,
        "issue": issue_number,
        "round": result.round,
        "verdict": result.verdict,
        "deviation": result.deviation,
        "risk": result.risk,
        "resume_commit": result.resume_commit,
        "evidence": result.evidence,
        "head": pr.head_sha,
        # ADR-0045: image identity plus the reconciled stream-observed model;
        # both empty of a config-time model on purpose — the driver no longer
        # has one. model_note carries any disagreement between the stream's
        # model signals instead of flattening it away.
        "run_image": config.run_image,
        "model": observed.model if observed else "",
        "model_note": observed.model_note if observed else "",
        # Empty when no review container ran (deterministic escalation).
        "container": container,
    }
    prefix = f"runs/issue-{issue_number}/reviews/round-{result.round}-{pr.head_sha[:12]}"
    files = {f"{prefix}.json": json.dumps(record, indent=2, sort_keys=True) + "\n"}
    if transcript:
        files[f"{prefix}-transcript.txt"] = transcript
    _push_evidence_files(
        config,
        files,
        message=f"Evidence: review round {result.round} (issue #{issue_number})",
        log=log,
        context=f"PR #{pr.number}",
        sink=sink,
    )


class Reviewer(Worker):
    """Discovers reviewable pr_ready PRs and applies one verdict per round.

    Discovery goes through the Control Node's dispatch endpoint (ADR-0017,
    discovery-only — no claim label exists on PRs); the verdict itself is
    still applied with the Reviewer's own PAT. Control Node down = new
    review rounds pause. The whole reviewable set is fetched per pass, so the
    loop sleeps out the poll interval after each (base default)."""

    role: ClassVar[str] = "reviewer"
    # The ADR-0008 "stronger model for review" rule is now a deploy-time
    # convention expressed in the worker-type definition's model field
    # (ADR-0045); the driver ships no default.
    pass_label: ClassVar[str] = "review"

    def _startup_log(self) -> None:
        self.log(
            f"reviewer driver ({self.me}) requesting {PR_READY} PRs"
            f" for {self.config.repo} via dispatch"
        )

    def fetch_work(self) -> list | None:
        targets = self.dispatch.review_targets(
            self.config.worker_id, self.config.node_name, self.me
        )
        if targets is None:
            self.log("control node unreachable; review rounds paused (ADR-0017)")
            return None
        return targets

    def execute(self, item: int) -> int:
        pr = self.client.get_pull(item)
        if not reviewable(pr.labels):
            return 0  # discovery is advisory: GitHub decides
        result = review_pr(
            self.config, self.client, pr, self.session_factory, log=self.log, sink=self.sink
        )
        return 1 if result is not None else 0
