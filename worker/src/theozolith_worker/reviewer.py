"""The Reviewer driver: a separate node-resident process owning all post-PR state.

Own GitHub identity, stronger model than the Worker adapters — no
self-grading by construction (ADR-0008). Polls one label on one object type
(pr_ready PRs without needs_human) and runs each review round as an
ephemeral container (ADR-0013): the driver materializes the review inputs as
files (issue intent, diff, Decisions Section, mechanical signals), the
judging agent runs in an interactive tmux session and writes its verdict as
a file, and the driver validates the file, renders the evidence-citing
comment, and applies exactly one verdict:

- approve: needs_human (keeping pr_ready) + deviation:* + risk:* + an
  evidence-citing comment; the human stamps and merges.
- revise: verdict comment (revised plan + resume commit) first, then
  attempt-N, then pr_ready comes off, then the issue claim is stripped and
  the issue re-queued to plan_ready — explicitly delegated human authority.
- escalate: blocked + needs_human with the evidence bundle link; also forced
  deterministically when the round budget is exhausted.

A missing or unparseable verdict file — including a revise at the final
budgeted round, which would commission a Run with no remaining review round —
applies NO PR-side state; the round is retried on the next poll.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time

from theozolith_worker import evidence, gitops, jobdir, runner, verdict
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
)
from theozolith_worker.config import ConfigError, DriverConfig, load_config
from theozolith_worker.containers import (
    ContainerSpec,
    DockerEngine,
    container_labels,
    review_container_name,
    review_session_name,
)
from theozolith_worker.decisions import section_text
from theozolith_worker.githubapi import GitHubClient, PullRequest
from theozolith_worker.sessions import SessionFactory
from theozolith_worker.signals import compute_signals
from theozolith_worker.worker import container_session_factory

DIFF_LIMIT = 200_000

REVIEW_PROMPT = """\
You are the Reviewer in TheOzolith agentic coding pipeline. A Worker shipped \
a best-effort PR; you own the verdict. You never implement — you judge the \
diff against the issue's stated intent and acceptance criteria, and you judge \
the decisions the Worker recorded, not just the code.

Your working directory contains the review inputs as files:

- `issue.md` — the issue: stated intent and acceptance criteria
- `diff.patch` — the full diff of the PR as shipped
- `decisions.md` — the PR's Decisions Section (recorded by the Worker)
- `signals.md` — mechanical diff signals (computed evidence — weigh it, it \
is not a grader)

## Review round

This verdict closes round {round} of {budget}. {round_rule}

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
  "cherry_pick": []}}

deviation grades divergence from the plan (files outside the plan's \
footprint, unrequested behavior, new dependencies, size far beyond \
expectation). risk is your own overall read of the change as implemented, \
independent of the issue's baseline label. approve only when the acceptance \
criteria are met by the diff as shipped. Escalate when a decision only a \
human may make is blocking (contradictory acceptance criteria, an open \
question the Worker flagged that you cannot settle).
"""

MIDDLE_ROUND_RULE = (
    "A revise verdict re-queues the issue for another Run that a later round reviews."
)
FINAL_ROUND_RULE = (
    "This is the LAST budgeted round: you must NOT emit revise — approve or "
    "escalate only. A revise would commission a Run with no remaining review "
    "round and will be rejected."
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
    diff = "\n".join(f"--- {f.path} ({f.status})\n{f.patch}" for f in files)
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n[diff truncated]"
    return {
        "issue.md": f"# Issue #{issue.number}: {issue.title}\n\n{issue.body or '(no body)'}\n",
        "diff.patch": (diff or "(empty diff)") + "\n",
        "decisions.md": (section_text(pr.body) or "(missing — judge accordingly)") + "\n",
        "signals.md": signals.render() + "\n",
    }


def review_pr(
    config: DriverConfig,
    client: GitHubClient,
    pr: PullRequest,
    session_factory: SessionFactory,
    *,
    log=_log,
) -> verdict.Verdict | None:
    """Run one review round and apply the verdict. None = no state this cycle."""
    issue_number = runner.issue_for_branch(pr.head_ref)
    if issue_number is None:
        log(f"PR #{pr.number} head {pr.head_ref!r} is not a pipeline branch; skipping")
        return None
    rounds_spent = attempts_on(pr.labels)
    round_number = rounds_spent + 1
    bundle_url = evidence.issue_evidence_url(config.repo, issue_number)

    if rounds_spent >= ROUND_BUDGET:
        # The budget check cannot be argued with: no model call.
        result = verdict.Verdict(
            verdict=verdict.ESCALATE,
            round=round_number,
            evidence=(
                f"Round budget exhausted: {ROUND_BUDGET} review rounds spent on this "
                "issue. A human decision is required to continue."
            ),
            bundle_url=bundle_url,
        )
        _apply(config, client, pr, issue_number, result, log)
        return result

    review_id = f"review-{pr.number}-round-{round_number}"
    job = jobdir.create_job_dir(config.jobs_dir, review_id)
    try:
        work = job / jobdir.WORK_DIR
        work.mkdir(parents=True, exist_ok=True)
        for name, content in _review_inputs(client, pr, issue_number).items():
            (work / name).write_text(content, encoding="utf-8")

        round_rule = FINAL_ROUND_RULE if round_number >= ROUND_BUDGET else MIDDLE_ROUND_RULE
        prompt = REVIEW_PROMPT.format(
            round=round_number, budget=ROUND_BUDGET, round_rule=round_rule
        )
        jobdir.atomic_write(job / jobdir.PROMPT_FILE, prompt)
        manifest = jobdir.Manifest(
            run_id=review_id,
            mode=jobdir.MODE_REVIEW,
            session=review_session_name(pr.number, round_number),
            adapter=config.adapter,
            model=config.model,  # the Reviewer's stronger model (ADR-0008)
            workdir=jobdir.WORK_DIR,
            agent_timeout_seconds=config.agent_timeout_seconds,
            settle_seconds=config.settle_seconds,
        )
        jobdir.write_manifest(job, manifest)
        spec = ContainerSpec(
            name=review_container_name(pr.number, round_number),
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
            final_round=round_number >= ROUND_BUDGET,
            default_resume=pr.head_sha,
            bundle_url=bundle_url,
        )
        if result is None:
            # No PR-side state whatsoever; the round retries next poll.
            log(f"PR #{pr.number}: verdict rejected ({reason}); will retry next poll")
            return None

        transcript = _read_transcript(job)
        _apply(config, client, pr, issue_number, result, log, transcript=transcript)
        return result
    finally:
        shutil.rmtree(job, ignore_errors=True)


def _read_transcript(job) -> str:
    try:
        return (job / jobdir.TRANSCRIPT_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""


def _apply(
    config: DriverConfig,
    client: GitHubClient,
    pr: PullRequest,
    issue_number: int,
    result: verdict.Verdict,
    log,
    *,
    transcript: str = "",
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
    _push_review_evidence(config, pr, issue_number, result, transcript)


def _push_review_evidence(
    config: DriverConfig,
    pr: PullRequest,
    issue_number: int,
    result: verdict.Verdict,
    transcript: str,
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
        "model": config.model,
        "container": review_container_name(pr.number, result.round),
    }
    prefix = f"runs/issue-{issue_number}/reviews/round-{result.round}-{pr.head_sha[:12]}"
    files = {f"{prefix}.json": json.dumps(record, indent=2, sort_keys=True) + "\n"}
    if transcript:
        files[f"{prefix}-transcript.txt"] = transcript
    try:  # noqa: SIM105 - traceability never blocks coordination
        evidence.push_bundle(
            config.clone_url,
            files,
            message=f"Evidence: review round {result.round} (issue #{issue_number})",
            author_name=config.worker_id,
            author_email=f"{config.worker_id}@theozolith.invalid",
            env=gitops.auth_env(config.token),
        )
    except Exception:
        pass


def reviewable(labels: set[str]) -> bool:
    return PR_READY in labels and NEEDS_HUMAN not in labels and BLOCKED not in labels


def run_reviewer(
    config: DriverConfig,
    client: GitHubClient | None = None,
    session_factory: SessionFactory | None = None,
    *,
    sleep=time.sleep,
    once: bool = False,
    log=_log,
) -> int:
    """The Reviewer poll loop; returns the number of verdicts applied."""
    client = client or GitHubClient(config.repo, config.token, api_url=config.api_url)
    session_factory = session_factory or container_session_factory(DockerEngine())
    me = client.viewer_login()
    log(f"reviewer driver ({me}) polling {config.repo} for {PR_READY} without {NEEDS_HUMAN}")

    verdicts = 0
    while True:
        try:
            for candidate in client.list_open_prs_by_label(PR_READY):
                if not reviewable(candidate.labels):
                    continue
                pr = client.get_pull(candidate.number)
                if review_pr(config, client, pr, session_factory, log=log) is not None:
                    verdicts += 1
        except Exception as exc:
            log(f"review pass failed: {exc}")
        if once:
            return verdicts
        sleep(config.poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-reviewer",
        description=(
            "TheOzolith Reviewer driver: poll pr_ready PRs, run review rounds in "
            "ephemeral containers, own all post-PR state."
        ),
    )
    parser.add_argument("--once", action="store_true", help="One poll pass, then exit.")
    args = parser.parse_args(argv)
    try:
        config = load_config(role="reviewer")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        run_reviewer(config, once=args.once)
    except KeyboardInterrupt:
        print("reviewer driver stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
