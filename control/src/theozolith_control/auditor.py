"""The retry auditor: attempt-N labels cross-checked against events.

The attempt-N label on a PR is Reviewer-owned coordination state; Run and
review events are the Control Node's independent record of what actually
happened. When they disagree — a label someone hand-edited, a lost verdict,
a driver bug — the auditor records a finding for a human. It NEVER corrects
GitHub (ADR-0002; the brief): flag, don't fix. The whole sweep is read-only
on the GitHub side by construction — it performs GET requests only.

Expected attempts for an issue = max(highest revise-verdict round from
review events, highest Run attempt - 1): a revise on round N stamps
attempt-N, and a Run executing at attempt K implies K-1 spent rounds
(ADR-0014). Issues with no events are skipped — a fresh Control Node has no
grounds to audit history it never saw.
"""

from __future__ import annotations

from theozolith_worker.bootstrap.vocabulary import ROUND_BUDGET, attempt_label, attempts_on
from theozolith_worker.githubapi import GitHubClient
from theozolith_worker.runner import issue_for_branch

from theozolith_control.store import EVENT_REVIEW, EVENT_RUN, Store


def _log(message: str) -> None:
    print(message, flush=True)


def expected_attempts(store: Store, issue: int) -> int | None:
    """What the events imply; None when there is no event record at all."""
    revise_rounds = [0]
    run_attempts = [0]
    seen = False
    for event in store.events(type=EVENT_REVIEW, issue=issue):
        seen = True
        if event.get("verdict") == "revise" and isinstance(event.get("round"), int):
            revise_rounds.append(event["round"])
    for event in store.events(type=EVENT_RUN, issue=issue):
        seen = True
        if isinstance(event.get("attempt"), int):
            run_attempts.append(event["attempt"])
    if not seen:
        return None
    return max(max(revise_rounds), max(run_attempts) - 1)


def _candidate_prs(store: Store, client: GitHubClient) -> dict[int, int | None]:
    """PR number -> issue number, from events and from labeled PRs."""
    candidates: dict[int, int | None] = {}
    for event in store.events(type=EVENT_REVIEW):
        if isinstance(event.get("pr"), int):
            candidates[event["pr"]] = (
                event.get("issue") if isinstance(event.get("issue"), int) else None
            )
    for event in store.events(type=EVENT_RUN):
        if isinstance(event.get("pr"), int) and isinstance(event.get("issue"), int):
            candidates[event["pr"]] = event["issue"]
    for round_number in range(1, ROUND_BUDGET + 1):
        for pr in client.list_open_prs_by_label(attempt_label(round_number)):
            candidates.setdefault(pr.number, None)
    return candidates


def sweep(store: Store, client: GitHubClient, *, log=_log) -> list[dict]:
    """One audit pass; returns the NEW findings recorded this pass."""
    findings: list[dict] = []
    for pr_number, issue in sorted(_candidate_prs(store, client).items()):
        pull = client.get_pull(pr_number)
        if issue is None:
            issue = issue_for_branch(pull.head_ref)
        if issue is None:
            continue  # not a pipeline PR
        expected = expected_attempts(store, issue)
        if expected is None:
            continue  # no event record: nothing to cross-check against
        actual = attempts_on(pull.labels)
        if actual == expected:
            continue
        detail = (
            f"PR #{pr_number} (issue #{issue}) carries attempt-{actual} but events imply"
            f" attempt-{expected}; flagged only — never auto-corrected (ADR-0002)"
        )
        if store.record_audit_finding(pr_number, issue, expected, actual, detail):
            findings.append(
                {"pr": pr_number, "issue": issue, "expected": expected, "actual": actual}
            )
            log(f"auditor: {detail}")
    return findings
