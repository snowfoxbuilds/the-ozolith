"""Dependency Edges: the blocked-by closure walk and Chained Base resolution.

ADR-0053: GitHub-native ``blocked by`` relations between workspace-repo
issues are the machine-readable ordering truth. This module is pure reads —
no GitHub writes — shared by dispatch (claim eligibility) and the
Implementer driver (checkout-time base derivation), so the two sides can
never drift on what "satisfied" means. Every walk is live per call: the
reads are the authority, never a cache (ADR-0053 rejects store-backed
graphs as authority).

The branch-naming convention lives here too: ``ozolith/issue-N`` is how a
blocker's PR is resolved deterministically (the go-ahead never parses
comments), and how a chained base branch is traced back to its issue.
"""

from __future__ import annotations

from dataclasses import dataclass

from theozolith_worker.bootstrap.vocabulary import BLOCKED, NEEDS_HUMAN, PR_READY
from theozolith_worker.githubapi import GitHubClient, Issue, RepoMergeSettings

BRANCH_PREFIX = "ozolith/issue-"

# ChainDecision kinds.
READY = "ready"  # every edge satisfied by a completed close: base = default
CHAINED = "chained"  # go-ahead holds: base = the chain tip's branch
WAIT = "wait"  # edges unsatisfied but healthy: skip, re-examine next pass
MALFORMED = "malformed"  # the graph needs a human (the failed+plan_ready lane)


def branch_for(issue_number: int) -> str:
    return f"{BRANCH_PREFIX}{issue_number}"


def issue_for_branch(head_ref: str) -> int | None:
    suffix = head_ref.removeprefix(BRANCH_PREFIX)
    return int(suffix) if head_ref != suffix and suffix.isdigit() else None


class DependencyCycleError(RuntimeError):
    """The blocked-by closure contains a cycle: a malformed state a human
    must re-edge (ADR-0053) — the message doubles as dispatch's malformed
    detail and the driver's infra-class failure reason."""

    def __init__(self, cycle: tuple[int, ...]):
        path = " -> ".join(f"#{n}" for n in cycle)
        super().__init__(
            f"dependency cycle in the blocked-by closure: {path}"
            " — re-edge the relations on GitHub (ADR-0053)"
        )
        self.cycle = cycle


class CrossRepoEdgeError(RuntimeError):
    """A claimable work issue carries a cross-repo Dependency Edge — a
    malformed state (ADR-0053): cross-repo work enters ordering only
    through a locally created stand-in sub-issue."""

    def __init__(self, issue: int, foreign_repo: str):
        super().__init__(
            f"issue #{issue} is blocked by an issue in {foreign_repo!r} — a cross-repo"
            " Dependency Edge on a claimable issue is malformed (ADR-0053); replace it"
            " with a locally created stand-in issue"
        )
        self.issue = issue
        self.foreign_repo = foreign_repo


@dataclass(frozen=True)
class DependencyClosure:
    """The COMPLETE transitive blocked-by closure of one issue.

    ``order`` is topological — blockers before dependents, the walked issue
    last — and deterministic (ties resolve by ascending issue number).
    ``edges`` maps each issue to its direct blockers; ``issues`` holds the
    blocker payloads as the walk read them (the walked issue itself is not
    fetched — callers already hold it). ``order == (number,)`` means
    edge-less.

    Complete means closed and merged blockers keep their real edges: this
    closure is the truthful graph — issue #82's ``input/deps/`` serialization
    consumes it and must never rediscover omitted edges — and eligibility
    (``resolve``) is an interpretation OVER it, never baked into the walk. A
    completed blocker can sit above a still-open ancestor whose branch
    carries the merged work (a chain layer merged into its parent branch);
    pruning beneath the completed blocker would hide that live ancestor and
    base the dependent on the default branch, silently dropping its work."""

    order: tuple[int, ...]
    edges: dict[int, tuple[int, ...]]
    issues: dict[int, Issue]


def walk_closure(client: GitHubClient, number: int) -> DependencyClosure:
    """DFS over ``list_blocked_by``, live per call — the reads are the
    authority (ADR-0053). The walk is FULL: every reachable same-repo edge
    is followed, closed/merged blockers included, so cycle detection
    (:class:`DependencyCycleError`, carrying the cycle path) and cross-repo
    detection (:class:`CrossRepoEdgeError`) cover the whole graph — a
    transitive cycle or foreign edge is never hidden beneath a closed
    blocker. A diamond is walked once."""
    order: list[int] = []
    edges: dict[int, tuple[int, ...]] = {}
    issues: dict[int, Issue] = {}
    on_path: list[int] = []
    done: set[int] = set()

    def visit(current: int) -> None:
        if current in on_path:
            cycle = (*on_path[on_path.index(current) :], current)
            raise DependencyCycleError(cycle)
        if current in done:
            return
        on_path.append(current)
        blockers = sorted(client.list_blocked_by(current), key=lambda issue: issue.number)
        for blocker in blockers:
            if blocker.repo and blocker.repo != client.repo:
                raise CrossRepoEdgeError(current, blocker.repo)
            issues.setdefault(blocker.number, blocker)
        edges[current] = tuple(blocker.number for blocker in blockers)
        for blocker in blockers:
            visit(blocker.number)
        on_path.pop()
        done.add(current)
        order.append(current)

    visit(number)
    return DependencyClosure(order=tuple(order), edges=edges, issues=issues)


@dataclass(frozen=True)
class ChainDecision:
    """What the closure resolves to for the walked issue (ADR-0053)."""

    kind: str  # READY | CHAINED | WAIT | MALFORMED
    reason: str = ""  # WAIT/MALFORMED: the human-readable why
    base_branch: str = ""  # READY/CHAINED: the checkout and PR base
    base_is_chained: bool = False
    tip_issue: int = 0  # CHAINED: the blocker whose branch is the base


def resolve(client: GitHubClient, closure: DependencyClosure, default_branch: str) -> ChainDecision:
    """The ADR-0053 go-ahead, over the COMPLETE closure minus the walked
    issue — nodes beneath closed blockers included.

    Every blocker closed as completed -> READY (base = the default branch).
    Any blocker closed for another reason -> MALFORMED (only completed
    satisfies an edge, anywhere in the graph — a not_planned ancestor is a
    graph a human must re-edge). Open blockers qualify only when each has an
    approved-and-awaiting-merge PR — ``pr_ready`` + ``needs_human`` without
    ``blocked``, labels only, never comment parsing — and those PRs form a
    single chain by base_ref linkage with exactly one tip; the dependent
    then bases on the tip (CHAINED). An open ancestor beneath a completed
    blocker joins that chain check like any other open blocker: its branch
    is where the completed layer's work merged to, so resolving READY over
    its head would base the dependent on the default branch and drop that
    work. Anything else healthy -> WAIT.

    Preconditions (:func:`chain_preconditions`) are deliberately NOT
    checked here: dispatch must check them; the driver at checkout never
    re-checks them (the grant already asserted them)."""
    dependent = closure.order[-1]
    open_blockers: list[Issue] = []
    for number in closure.order:
        if number == dependent:
            continue
        blocker = closure.issues[number]
        if blocker.state == "closed":
            if blocker.state_reason != "completed":
                return ChainDecision(
                    kind=MALFORMED,
                    reason=(
                        f"blocker #{blocker.number} is closed as"
                        f" {blocker.state_reason or '(no reason)'} — only a completed"
                        " close satisfies a Dependency Edge (ADR-0053); re-edge or"
                        " abandon the dependent"
                    ),
                )
        else:
            open_blockers.append(blocker)
    if not open_blockers:
        return ChainDecision(kind=READY, base_branch=default_branch)

    prs = {}
    for blocker in open_blockers:
        pr = client.find_open_pr_by_head(branch_for(blocker.number))
        if pr is None:
            return ChainDecision(kind=WAIT, reason=f"open blocker #{blocker.number} has no PR yet")
        if not (PR_READY in pr.labels and NEEDS_HUMAN in pr.labels and BLOCKED not in pr.labels):
            return ChainDecision(
                kind=WAIT,
                reason=(
                    f"blocker #{blocker.number} PR #{pr.number} is not approved-and-awaiting-merge"
                ),
            )
        prs[blocker.number] = pr

    # Single chain by base_ref linkage: each PR targets the default branch
    # or another open blocker's branch, exactly one tip, and the tip's base
    # path covers every open blocker — fan-in, fan-out, disconnected
    # cycles, and foreign bases all mean wait for merges.
    branch_of = {branch_for(number): number for number in prs}
    for number, pr in prs.items():
        if pr.base_ref != default_branch and pr.base_ref not in branch_of:
            return ChainDecision(
                kind=WAIT,
                reason=(
                    f"blocker #{number} PR #{pr.number} targets {pr.base_ref!r}, outside"
                    " the blocker chain; waiting for merges"
                ),
            )
    based_on = {pr.base_ref for pr in prs.values()}
    tips = [number for number in prs if branch_for(number) not in based_on]
    if len(tips) != 1:
        return ChainDecision(kind=WAIT, reason="parallel open blocker lines; waiting for merges")
    (tip,) = tips
    chain = [tip]
    while prs[chain[-1]].base_ref != default_branch:
        nxt = branch_of[prs[chain[-1]].base_ref]
        if nxt in chain:
            return ChainDecision(
                kind=WAIT, reason="parallel open blocker lines; waiting for merges"
            )
        chain.append(nxt)
    if len(chain) != len(prs):
        return ChainDecision(kind=WAIT, reason="parallel open blocker lines; waiting for merges")
    return ChainDecision(
        kind=CHAINED,
        base_branch=branch_for(tip),
        base_is_chained=True,
        tip_issue=tip,
    )


def chain_preconditions(settings: RepoMergeSettings) -> str | None:
    """None when the workspace supports chaining (ADR-0053: merge commits
    on, squash and rebase off — both rewrite SHAs and break chained diffs —
    and delete-branch-on-merge on, the retarget trigger); otherwise the
    visible "chaining off" reason naming each failing setting. An
    incomplete read fails safe: chaining off, never a silent pass."""
    if not settings.complete:
        return "chaining off: merge settings unreadable with this token"
    failing = []
    if not settings.merge_commit_allowed:
        failing.append("merge commits disabled")
    if settings.squash_allowed:
        failing.append("squash merge enabled")
    if settings.rebase_allowed:
        failing.append("rebase merge enabled")
    if not settings.delete_branch_on_merge:
        failing.append("delete-branch-on-merge disabled")
    if failing:
        return "chaining off: " + ", ".join(failing)
    return None
