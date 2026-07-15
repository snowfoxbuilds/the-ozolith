"""The Reviewer actor: a separate long-lived process owning all post-PR state.

Own container, own GitHub identity, stronger model than the Worker adapters —
no self-grading by construction (ADR-0008). Polls one label on one object
type (pr_ready PRs without needs_human), judges the diff against the issue's
stated intent, its acceptance criteria, and the PR's Decisions Section, with
mechanical diff signals fed in as evidence, and applies exactly one verdict:

- approve: needs_human (keeping pr_ready) + deviation:* + risk:* + an
  evidence-citing comment; the human stamps and merges.
- revise: verdict comment (revised plan + resume commit) first, then
  attempt-N, then pr_ready comes off, then the issue claim is stripped and
  the issue re-queued to plan_ready — explicitly delegated human authority.
- escalate: blocked + needs_human with the evidence bundle link; also forced
  deterministically when the round budget is exhausted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from theozolith_worker import evidence, runner, verdict
from theozolith_worker.adapters import Adapter, make_adapter
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
from theozolith_worker.config import ActorConfig, ConfigError, load_config
from theozolith_worker.decisions import section_text
from theozolith_worker.githubapi import GitHubClient, PullRequest
from theozolith_worker.signals import compute_signals

GRADES = ("low", "medium", "high")
DIFF_LIMIT = 60_000

REVIEW_PROMPT = """\
You are the Reviewer in TheOzolith agentic coding pipeline. A Worker shipped \
a best-effort PR; you own the verdict. You never implement — you judge the \
diff against the issue's stated intent and acceptance criteria, and you judge \
the decisions the Worker recorded, not just the code.

## Issue #{issue_number}: {issue_title}

{issue_body}

## The PR's Decisions Section (recorded by the Worker)

{decisions}

## Mechanical diff signals (computed evidence — weigh it, it is not a grader)

{signals}

## Review round

This verdict closes round {round} of {budget}. Escalate instead of revising \
when a decision only a human may make is blocking (contradictory acceptance \
criteria, an open question the Worker flagged that you cannot settle).

## Diff

{diff}

## Your reply

Reply with exactly one JSON object and nothing else (no fences, no prose):
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
criteria are met by the diff as shipped.
"""


def _log(message: str) -> None:
    print(message, flush=True)


def parse_model_verdict(text: str) -> dict | None:
    """The model's JSON verdict, tolerant of stray prose around it."""
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1]):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("verdict") in (
            verdict.APPROVE,
            verdict.REVISE,
            verdict.ESCALATE,
        ):
            return data
    return None


def _grade(raw: object) -> str:
    return raw if raw in GRADES else "medium"


def _strip_claim_and_requeue(client: GitHubClient, issue_number: int) -> None:
    """Delegated authority: return the issue to the claimable pool."""
    issue = client.get_issue(issue_number)
    for login in issue.assignees:
        client.remove_assignee(issue_number, login)
    client.remove_label(issue_number, IN_PROGRESS)
    client.add_labels(issue_number, PLAN_READY)


def review_pr(
    config: ActorConfig,
    client: GitHubClient,
    adapter: Adapter,
    pr: PullRequest,
    *,
    log=_log,
) -> verdict.Verdict | None:
    """Review one PR and apply the verdict. None = no verdict this cycle."""
    issue_number = runner.issue_for_branch(pr.head_ref)
    if issue_number is None:
        log(f"PR #{pr.number} head {pr.head_ref!r} is not a pipeline branch; skipping")
        return None
    rounds_spent = attempts_on(pr.labels)
    round_number = rounds_spent + 1
    bundle_url = evidence.issue_evidence_url(config.repo, issue_number)

    if rounds_spent >= ROUND_BUDGET:
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

    issue = client.get_issue(issue_number)
    files = client.pr_files(pr.number)
    signals = compute_signals(files)
    diff = "\n".join(f"--- {f.path} ({f.status})\n{f.patch}" for f in files)
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n[diff truncated]"

    prompt = REVIEW_PROMPT.format(
        issue_number=issue_number,
        issue_title=issue.title,
        issue_body=issue.body or "(no body)",
        decisions=section_text(pr.body) or "(missing — judge accordingly)",
        signals=signals.render(),
        round=round_number,
        budget=ROUND_BUDGET,
        diff=diff or "(empty diff)",
    )
    reply = adapter.complete(prompt)
    data = parse_model_verdict(reply.text) if reply.ok else None
    if data is None:
        log(f"PR #{pr.number}: no usable verdict from the model; will retry next poll")
        return None

    result = verdict.Verdict(
        verdict=data["verdict"],
        round=round_number,
        evidence=str(data.get("evidence", "")),
        deviation=_grade(data.get("deviation")),
        risk=_grade(data.get("risk")),
        revised_plan=str(data.get("revised_plan", "")),
        resume_commit=str(data.get("resume_commit", "")) or pr.head_sha,
        cherry_pick=[str(sha) for sha in data.get("cherry_pick", []) if sha],
        bundle_url=bundle_url,
    )
    _apply(config, client, pr, issue_number, result, log)
    return result


def _apply(
    config: ActorConfig,
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
    _push_review_evidence(config, client, pr, issue_number, result)


def _push_review_evidence(
    config: ActorConfig,
    client: GitHubClient,
    pr: PullRequest,
    issue_number: int,
    result: verdict.Verdict,
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
    }
    path = f"runs/issue-{issue_number}/reviews/round-{result.round}-{pr.head_sha[:12]}.json"
    try:  # noqa: SIM105 - traceability never blocks coordination
        evidence.push_bundle(
            config.clone_url,
            {path: json.dumps(record, indent=2, sort_keys=True) + "\n"},
            message=f"Evidence: review round {result.round} (issue #{issue_number})",
            author_name=client.viewer_login(),
            author_email=f"{client.viewer_login()}@users.noreply.github.com",
        )
    except Exception:
        pass


def reviewable(labels: set[str]) -> bool:
    return PR_READY in labels and NEEDS_HUMAN not in labels and BLOCKED not in labels


def run_reviewer(
    config: ActorConfig,
    client: GitHubClient | None = None,
    adapter: Adapter | None = None,
    *,
    sleep=time.sleep,
    once: bool = False,
    log=_log,
) -> int:
    """The Reviewer poll loop; returns the number of verdicts issued."""
    client = client or GitHubClient(config.repo, config.token, api_url=config.api_url)
    adapter = adapter or make_adapter(config.adapter, config.model)
    me = client.viewer_login()
    log(f"reviewer ({me}) polling {config.repo} for {PR_READY} without {NEEDS_HUMAN}")

    verdicts = 0
    while True:
        for candidate in client.list_open_prs_by_label(PR_READY):
            if not reviewable(candidate.labels):
                continue
            pr = client.get_pull(candidate.number)
            if review_pr(config, client, adapter, pr, log=log) is not None:
                verdicts += 1
        if once:
            return verdicts
        sleep(config.poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="theozolith-reviewer",
        description="TheOzolith Reviewer: poll pr_ready PRs and own all post-PR state.",
    )
    parser.add_argument("--once", action="store_true", help="One poll pass, then exit.")
    args = parser.parse_args(argv)
    try:
        config = load_config(role="reviewer")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run_reviewer(config, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
