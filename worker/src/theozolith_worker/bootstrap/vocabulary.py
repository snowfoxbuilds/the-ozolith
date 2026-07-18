"""The pipeline substrate vocabulary: labels and issue forms.

The verbatim label set from AGENTIC-CODING-PIPELINE.md. attempt-N is
concretized as attempt-1..attempt-3: the round budget is 3 review rounds per
issue, tracked on the PR by the Reviewer (ADR-0008).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

ROUND_BUDGET = 3

# Label names as constants for the actors (names must match LABELS below).
DRAFT = "draft"
PLAN_READY = "plan_ready"
IN_PROGRESS = "in_progress"
PR_READY = "pr_ready"
BLOCKED = "blocked"
FAILED = "failed"
NEEDS_HUMAN = "needs_human"
RISK_PREFIX = "risk:"
DEVIATION_PREFIX = "deviation:"
ATTEMPT_PREFIX = "attempt-"


def attempt_label(round_number: int) -> str:
    return f"{ATTEMPT_PREFIX}{round_number}"


def attempts_on(labels: set[str]) -> int:
    """Highest attempt-N present in ``labels`` (0 if none)."""
    rounds = [0]
    for name in labels:
        if name.startswith(ATTEMPT_PREFIX):
            suffix = name.removeprefix(ATTEMPT_PREFIX)
            if suffix.isdigit():
                rounds.append(int(suffix))
    return max(rounds)


def reviewable(labels: set[str]) -> bool:
    """A PR the Reviewer may judge: pr_ready without a human hold. The one
    predicate shared by the Control Node's discovery and the Reviewer's own
    re-check (ADR-0017) so the two can never drift."""
    return PR_READY in labels and NEEDS_HUMAN not in labels and BLOCKED not in labels


@dataclass(frozen=True)
class Label:
    name: str
    color: str  # hex, no leading '#', as the GitHub API expects
    description: str


LABELS: tuple[Label, ...] = (
    Label("draft", "ededed", "Planning in progress; not claimable."),
    Label(
        "plan_ready",
        "0e8a16",
        "Plan approved and claimable; asserts acceptance criteria and a baseline risk label.",
    ),
    Label("in_progress", "fbca04", "Claimed by a Worker; a Run is in flight."),
    Label("pr_ready", "1d76db", "PR ready for the automated Reviewer."),
    Label("blocked", "b60205", "Progress needs a decision; paired with needs_human."),
    Label(
        "failed",
        "6e0000",
        "Execution broke twice; autopsy the evidence. Overrides plan_ready at dispatch;"
        " only a human removes it, as part of re-queueing (ADR-0016).",
    ),
    Label("needs_human", "d93f0b", "Awaiting human review or decision."),
    Label("risk:low", "c2e0c6", "Risk grade: low (sensitive paths, blast radius, reversibility)."),
    Label(
        "risk:medium",
        "fef2c0",
        "Risk grade: medium (sensitive paths, blast radius, reversibility).",
    ),
    Label(
        "risk:high", "f9d0c4", "Risk grade: high (sensitive paths, blast radius, reversibility)."
    ),
    Label("deviation:low", "c5def5", "Reviewer grade: implementation stayed on plan."),
    Label("deviation:medium", "bfd4f2", "Reviewer grade: notable divergence from the plan."),
    Label("deviation:high", "e99695", "Reviewer grade: major divergence from the plan."),
    *(
        Label(
            f"attempt-{n}",
            "d4c5f9",
            f"Review round {n} of {ROUND_BUDGET} (ADR-0008).",
        )
        for n in range(1, ROUND_BUDGET + 1)
    ),
)


def form_files() -> dict[str, bytes]:
    """Issue-form files to place in the target repo, keyed by repo path."""
    data = resources.files("theozolith_worker.bootstrap") / "data"
    return {
        ".github/ISSUE_TEMPLATE/task.yml": (data / "task.yml").read_bytes(),
    }
