"""Claim dispatch: the Control Node as the single writer of claim creation.

ADR-0017 write-through: a Worker driver asks for work; the Control Node
selects an eligible plan_ready issue, writes the claim to GitHub itself —
assigns the driver's GitHub login, adds in_progress, removes plan_ready —
and returns the issue in the same response. Grants are serialized on one
lock, so claim races are structurally impossible; assign-and-verify is
gone. The Reviewer side of the same path is discovery-only (no claim label
exists on PRs).

Gates applied before any grant (ADR-0016 + the queue-behind rule + the
pin-eligibility rule of the ADR-0015 revision):

- a quarantined node gets nothing until a human releases it;
- a node whose last heartbeat-reported product version differs from the
  recorded pin gets nothing until it converges — keyed on the REPORTED
  version, never on command acks (a node can ack, fail the install, and
  never converge). Issuing an update therefore pauses dispatch fleet-wide;
  capacity returns node by node as versions converge. Nodes with no
  reported version (the daemon-less dev shape) stay eligible;
- a node with a pending drain/recycle/update/restart gets nothing, which
  bounds a queued-behind command by the current Run;
- an issue carrying ``failed`` is refused even with plan_ready present and
  surfaced on the dashboard as a malformed state — a visibly stalled grant
  beats a laundered failure loop;
- issues already granted (awaiting activation) or with a live Run are
  skipped;
- an issue with unsatisfied Dependency Edges is refused (ADR-0053): every
  ``blocked by`` edge must be satisfied — the blocker closed as completed,
  or the Chained Base go-ahead holding under the repo's merge-setting
  preconditions. Cycles, cross-repo edges, and not_planned blockers surface
  as malformed states; healthy unsatisfied chains surface as visible
  dispatch waits. The graph is read live per grant pass — never a stored
  graph (the ``dispatch_waits`` table is advisory display only).

GitHub remains the sole source of coordination truth: everything here
reconciles to what GitHub answers at grant time, never the reverse.
"""

from __future__ import annotations

import threading
from typing import Any

from theozolith_worker import deps
from theozolith_worker.bootstrap.vocabulary import (
    FAILED,
    IN_PROGRESS,
    PLAN_READY,
    PR_READY,
    reviewable,
)
from theozolith_worker.githubapi import (
    GitHubClient,
    GitHubError,
    Issue,
    PullRequest,
    RepoMergeSettings,
)

from theozolith_control.store import Store


def _log(message: str) -> None:
    print(message, flush=True)


class _PassReads:
    """Pass-scoped memo over the dependency reads deps.walk_closure and
    deps.resolve perform. ADR-0053 forbids cross-pass storage (the graph
    is read live per grant pass), not per-pass reuse: candidates sharing a
    blocker chain would otherwise re-fetch the same edges and PRs once per
    dependent — all while holding the lock that serializes every worker's
    grants."""

    def __init__(self, client: GitHubClient):
        self._client = client
        self.repo = client.repo
        self._blocked: dict[int, list[Issue]] = {}
        self._prs: dict[str, PullRequest | None] = {}

    def list_blocked_by(self, number: int) -> list[Issue]:
        if number not in self._blocked:
            self._blocked[number] = self._client.list_blocked_by(number)
        return self._blocked[number]

    def find_open_pr_by_head(self, head_ref: str) -> PullRequest | None:
        if head_ref not in self._prs:
            self._prs[head_ref] = self._client.find_open_pr_by_head(head_ref)
        return self._prs[head_ref]


class Dispatcher:
    """One serialized grant path for the whole fleet (ADR-0017)."""

    def __init__(self, store: Store, client: GitHubClient, *, log=_log):
        self._store = store
        self._client = client
        self._log = log
        self._lock = threading.Lock()

    def _version_block(self, node: str, pin: str) -> str | None:
        """The pin-eligibility gate: a node reporting a version other than
        the recorded pin gets no work until it converges. Unreported
        versions stay eligible (the daemon-less dev shape heartbeats
        nothing)."""
        if not pin:
            return None
        reported = self._store.node_version(node)
        if not reported or reported == pin:
            return None
        return (
            f"node {node!r} runs product version {reported}, the pin is {pin};"
            " no new work until it converges"
        )

    def _drivers_block(self, node: str, drivers_hash: str) -> str | None:
        """The config-distribution gate (ADR-0042): a node whose reported
        drivers-hash differs from the recorded one gets no work until it
        converges. A node with no recorded distribution ("" desired) is always
        eligible.

        Field presence decides the two empty cases (never truthiness): a
        heartbeat that OMITS the field — ``node_drivers_hash`` returns ``None`` —
        is the legacy/daemon-less shape and stays fail-open eligible; a current
        daemon that reports ``''`` because it has no verified applied tree is
        off-hash and blocked exactly like a mismatching non-empty hash."""
        if not drivers_hash:
            return None
        reported = self._store.node_drivers_hash(node)
        if reported is None or reported == drivers_hash:
            return None
        running = reported[:12] if reported else "(none applied)"
        return (
            f"node {node!r} runs config distribution {running}, the deployment"
            f" is {drivers_hash[:12]}; no new work until it converges"
        )

    def _dependency_block(
        self, issue_number: int, reads: _PassReads, settings_cache: list[RepoMergeSettings]
    ) -> tuple[str, str] | None:
        """The ADR-0053 eligibility lanes: None when every Dependency Edge
        is satisfied (or none exist); otherwise ``("malformed" | "wait",
        detail)``. Claim selection only — no coordination write happens
        here — and the reads are live per pass: GitHub is the authority,
        the recorded rows are advisory display.

        A failing dependency READ (not a malformed graph) re-raises with
        the issue named: granting with unknown ordering is never an
        option, so dispatch pauses loudly — the same posture as a failing
        issue listing, and the deliberate fail-loud ruling for a GitHub
        without the dependencies feature (issue #81: never silently
        edge-less)."""
        try:
            closure = deps.walk_closure(reads, issue_number)
        except (deps.DependencyCycleError, deps.CrossRepoEdgeError) as exc:
            return ("malformed", str(exc))
        except GitHubError as exc:
            raise GitHubError(
                f"dependency read for issue #{issue_number} failed — dispatch pauses"
                f" rather than granting with unknown ordering (ADR-0053): {exc}",
                status=exc.status,
            ) from exc
        if closure.order == (issue_number,):
            return None  # edge-less: the exact pre-ADR-0053 path
        decision = deps.resolve(reads, closure, self._client.default_branch())
        if decision.kind == deps.MALFORMED:
            return ("malformed", decision.reason)
        if decision.kind == deps.WAIT:
            return ("wait", decision.reason)
        if decision.kind == deps.CHAINED:
            if not settings_cache:
                settings_cache.append(self._client.repo_merge_settings())
            off = deps.chain_preconditions(settings_cache[0])
            if off is not None:
                return ("wait", f"{off}; dependents wait for full merge (ADR-0053)")
        return None

    def grant_work(
        self, worker: str, node: str, login: str, *, pin: str = "", drivers_hash: str = ""
    ) -> dict[str, Any]:
        """Grant one issue to ``worker`` (write-through), or explain why not.

        Returns ``{"issue": {...}}`` on a grant and ``{"issue": None}``
        (with an optional ``reason``) otherwise.
        """
        with self._lock:
            self._store.upsert_driver(worker, node, login, "implementer")
            quarantine = self._store.node_quarantine(node)
            if quarantine is not None:
                return {"issue": None, "reason": f"node {node!r} quarantined: {quarantine}"}
            version_block = self._version_block(node, pin)
            if version_block is not None:
                return {"issue": None, "reason": version_block}
            drivers_block = self._drivers_block(node, drivers_hash)
            if drivers_block is not None:
                return {"issue": None, "reason": drivers_block}
            pending = self._store.pending_lifecycle_commands(node)
            if pending:
                return {
                    "issue": None,
                    "reason": f"node {node!r} has pending {'/'.join(pending)}; no new work",
                }

            granted = self._store.granted_issues()
            live = {claim.issue for claim in self._store.live_claims()}
            candidates = self._client.list_open_issues(PLAN_READY)
            # Advisory rows describe the plan_ready pool: an issue that
            # left it (closed, label removed, claimed) takes its wait and
            # malformed rows with it — otherwise the dashboard shows a
            # departed issue "waiting" forever. Rows for issues still in
            # the pool are reconciled lane by lane below.
            listed = {candidate.number for candidate in candidates}
            for row in self._store.malformed_states():
                if row["issue"] not in listed:
                    self._store.clear_malformed(row["issue"])
            for row in self._store.dispatch_waits():
                if row["issue"] not in listed:
                    self._store.clear_wait(row["issue"])
            flagged = {row["issue"] for row in self._store.malformed_states()}
            waiting = {row["issue"] for row in self._store.dispatch_waits()}
            # Pass-scoped read caches: the merge settings are fetched
            # lazily at most once per pass (only a pass that reaches a
            # chained candidate reads them), and dependency reads are
            # memoized across candidates sharing a blocker chain.
            settings_cache: list[RepoMergeSettings] = []
            reads = _PassReads(self._client)
            for issue in candidates:
                if FAILED in issue.labels:
                    detail = "carries failed + plan_ready; dispatch refuses to grant (ADR-0016)"
                    self._store.record_malformed(issue.number, detail)
                    if issue.number in waiting:
                        self._store.clear_wait(issue.number)  # superseded by the flag
                    self._log(f"dispatch: issue #{issue.number} {detail}")
                    continue
                if issue.assignees or IN_PROGRESS in issue.labels:
                    continue  # spoken for on GitHub (hand-edited labels stay meaningful)
                if issue.number in granted or issue.number in live:
                    continue
                block = self._dependency_block(issue.number, reads, settings_cache)
                if block is not None:
                    lane, detail = block
                    # The lanes reconcile each other: an issue is malformed
                    # OR waiting, never both — a waiting dependent whose
                    # blocker was closed not_planned moves lanes cleanly.
                    if lane == "malformed":
                        self._store.record_malformed(issue.number, detail)
                        if issue.number in waiting:
                            self._store.clear_wait(issue.number)
                    else:
                        self._store.record_wait(issue.number, detail)
                        if issue.number in flagged:
                            self._store.clear_malformed(issue.number)
                    self._log(f"dispatch: issue #{issue.number} {lane}: {detail}")
                    continue
                # The candidate passed every malformed and wait lane: stale
                # flags clear here (a human fixed the labels or the graph,
                # or the chain's go-ahead arrived) — and only here.
                if issue.number in flagged:
                    self._store.clear_malformed(issue.number)
                if issue.number in waiting:
                    self._store.clear_wait(issue.number)

                # The grant row lands BEFORE the GitHub writes: if any write
                # fails midway (or this process dies), the janitor's
                # never-activated release finds the row and unwinds whatever
                # landed — a half-written claim is never orphaned.
                self._store.record_grant(issue.number, worker, node, login)
                self._client.add_assignees(issue.number, login)
                self._client.add_labels(issue.number, IN_PROGRESS)
                self._client.remove_label(issue.number, PLAN_READY)
                self._log(f"dispatch: issue #{issue.number} granted to {worker} ({login})")
                return {
                    "issue": {
                        "number": issue.number,
                        "title": issue.title,
                        "body": issue.body,
                        "labels": sorted((issue.labels | {IN_PROGRESS}) - {PLAN_READY}),
                    }
                }
            return {"issue": None}

    def review_targets(
        self, worker: str, node: str, login: str, *, pin: str = "", drivers_hash: str = ""
    ) -> dict[str, Any]:
        """Discovery for the Reviewer: reviewable pr_ready PRs, no writes.
        The pin-eligibility gate applies here too — an off-pin node burns
        review rounds on the wrong product version — as does the identical
        config-distribution gate (ADR-0042).

        The listing arrives oldest-first (the client passes
        sort=created&direction=asc) and the order is passed through
        untouched: oldest-first Reviewer discovery is load-bearing for
        chains — a blocker must be reviewed before its dependents'
        Chained Base go-ahead can hold (ADR-0053)."""
        self._store.upsert_driver(worker, node, login, "reviewer")
        version_block = self._version_block(node, pin)
        if version_block is not None:
            return {"prs": [], "reason": version_block}
        drivers_block = self._drivers_block(node, drivers_hash)
        if drivers_block is not None:
            return {"prs": [], "reason": drivers_block}
        numbers = [
            candidate.number
            for candidate in self._client.list_open_prs_by_label(PR_READY)
            if reviewable(candidate.labels)
        ]
        return {"prs": numbers}
