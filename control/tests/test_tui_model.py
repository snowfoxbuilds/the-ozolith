"""tui.model (M9, ADR-0040): pure derivations from the two read documents —
health precedence, run-state reduction, attach resolution over server-clock
heartbeat evidence, follow-by-cursor, eviction honesty, and the pinned
mirrors of every constant the TUI redeclares to keep its import tree clean.
"""

from __future__ import annotations

from typing import Any

from theozolith_control.tui import model
from tuirig import NOW, page, progress_event, run_event, state_doc

# -- nodes: per-row health precedence (ADR-0039) ----------------------------


def test_node_health_precedence_quarantined_stale_offpin_ok():
    state = state_doc(
        product_pin="0.4.0",
        nodes=[
            {"name": "a-quarantined", "version": "0.4.0", "last_seen": NOW - 500},
            {"name": "b-stale", "version": "0.4.0", "last_seen": NOW - 151},
            {"name": "c-offpin", "version": "0.3.0", "last_seen": NOW - 10},
            {"name": "d-ok", "version": "0.4.0", "last_seen": NOW - 10},
        ],
        node_health=[{"node": "a-quarantined", "quarantined": 1, "reason": "2 failed Runs"}],
    )
    rows = {r.name: r for r in model.node_rows(state)}
    assert rows["a-quarantined"].health == "quarantined"  # beats its own staleness
    assert rows["a-quarantined"].quarantine_reason == "2 failed Runs"
    assert rows["b-stale"].health == "stale"
    assert rows["c-offpin"].health == "off-pin"
    assert rows["d-ok"].health == "ok"


def test_staleness_uses_the_server_clock_only():
    """A skewed local clock cannot change the verdict: everything derives
    from state['now'] vs last_seen (ADR-0039 — no local time anywhere)."""
    state = state_doc()
    state["nodes"][0]["last_seen"] = NOW - 149
    assert model.node_rows(state)[0].health == "ok"
    state["nodes"][0]["last_seen"] = NOW - 151
    assert model.node_rows(state)[0].health == "stale"


# -- stacks and commands ----------------------------------------------------


def test_stack_rows_convergence_and_not_reported():
    state = state_doc(stacks=[])
    row = model.stack_rows(state)[0]
    assert row.actual == "not reported" and not row.converged
    state = state_doc()
    row = model.stack_rows(state)[0]
    assert row.actual == "running" and row.converged


def test_stack_rows_render_the_union_actual_only_is_off_desired():
    """The frozen web surface's drift behavior: rows come from the UNION of
    desired and reported (node, stack) keys. An actual-only running Stack —
    reported by a heartbeat but absent from desired state — renders desired
    '(unplaced)' with the reported kind and detail preserved, and is NEVER
    converged."""
    state = state_doc(
        stacks=[
            {
                "node": "box1",
                "name": "ghost",
                "kind": "container",
                "state": "running",
                "detail": "Up 3 hours",
                "updated_at": NOW - 10,
            }
        ]
    )
    rows = {r.name: r for r in model.stack_rows(state)}
    assert set(rows) == {"deck", "ghost"}  # union: desired-only AND actual-only
    ghost = rows["ghost"]
    assert ghost.desired == "(unplaced)"
    assert ghost.actual == "running" and not ghost.converged
    assert ghost.kind == "container" and ghost.detail == "Up 3 hours"  # reported, preserved
    assert rows["deck"].actual == "not reported" and not rows["deck"].converged


def test_stack_rows_desired_stopped_but_actual_running_is_divergent():
    state = state_doc()
    state["desired_stacks"][0]["state"] = "stopped"
    (row,) = model.stack_rows(state)
    assert row.desired == "stopped" and row.actual == "running" and not row.converged


def test_stack_rows_deletion_transition():
    """Desired definition deleted while the node still reports the Stack:
    the row survives as actual-only (off desired) until the node reconciles
    and stops reporting it — then it disappears."""
    state = state_doc(desired_stacks=[])
    (row,) = model.stack_rows(state)
    assert row.name == "deck" and row.desired == "(unplaced)" and not row.converged
    assert row.actual == "running" and row.kind == "container"
    reconciled = state_doc(desired_stacks=[], stacks=[])
    assert model.stack_rows(reconciled) == []


def test_command_rows_surface_queue_behind_deferrals():
    state = state_doc(
        commands=[
            {
                "id": 1,
                "node": "box1",
                "verb": "recycle",
                "target": "deck",
                "completed_at": None,
                "deferred_reason": "queued behind run r9",
            },
            {
                "id": 2,
                "node": "box1",
                "verb": "drain",
                "target": None,
                "completed_at": None,
                "deferred_reason": None,
            },
            {
                "id": 3,
                "node": "box1",
                "verb": "update",
                "target": None,
                "completed_at": NOW - 5,
                "deferred_reason": None,
            },
        ]
    )
    rows = model.command_rows(state)
    assert [(r.id, r.state) for r in rows] == [(1, "deferred"), (2, "pending")]
    assert rows[0].deferred_reason == "queued behind run r9"


# -- runs: the client-side run_states() twin (complete across pages) --------


def pager(pages: dict[str | None, dict[str, Any]]):
    """A fetch callable over cursor-keyed pages (None = the head fetch)."""

    def fetch(cursor: str | None) -> dict[str, Any]:
        return pages.get(cursor, page([]))

    return fetch


def make_index(
    runs_pages: dict[str | None, dict[str, Any]],
    progress_pages: dict[str | None, dict[str, Any]] | None = None,
) -> model.RunIndex:
    index = model.RunIndex()
    index.refresh(pager(runs_pages), pager(progress_pages or {}))
    return index


def test_run_rows_latest_per_issue_joined_with_latest_progress():
    index = make_index(
        {
            None: page(
                [
                    run_event(30, 7, "gate", run_id="r2", attempt=2),
                    run_event(20, 7, "claimed", run_id="r2", attempt=2),
                    run_event(10, 5, "pr-open", run_id="r9", pr=41),
                ]
            )
        },
        {
            None: page(
                [
                    progress_event(31, "r2", tool_calls=44, elapsed_seconds=500.0),
                    progress_event(21, "r2", tool_calls=14),
                ]
            )
        },
    )
    rows = model.run_rows(index)
    by_issue = {r.issue: r for r in rows}
    live = by_issue[7]
    assert live.phase == "gate" and not live.terminal
    assert live.tool_calls == 44 and live.elapsed_seconds == 500.0  # the LATEST progress
    assert live.transcript_bytes == 65536 and live.transcript_tail == "...tail..."
    done = by_issue[5]
    assert done.terminal and done.phase == "pr-open" and done.pr == 41
    assert done.pr_url == "https://github.com/acme/sandbox/pull/41"
    assert done.evidence_ref == "theozolith/evidence: runs/issue-5/r9"
    assert (
        done.evidence_url
        == "https://github.com/acme/sandbox/tree/theozolith/evidence/runs/issue-5/r9"
    )
    # A pr-open event carries no failure class — there is no failure; the
    # renderer shows "not applicable", never a defect (ADR-0040 amendment).
    assert done.failure_class == ""


def test_run_rows_carry_the_canonical_failure_class_from_the_event():
    index = make_index(
        {None: page([run_event(10, 5, "failed", run_id="r9", failure_class="timeout")])}
    )
    (row,) = model.run_rows(index)
    assert row.failure_class == "timeout"


def test_repo_less_legacy_events_are_skipped_never_rows():
    """A pre-ADR-0056 run event carries no repo: it stays metrics history and
    never becomes a Runs row (the client-side legacy fence). A repo-bearing
    event beside it is unaffected."""
    index = make_index(
        {
            None: page(
                [
                    run_event(20, 6, "gate", run_id="r6"),
                    run_event(10, 5, "failed", run_id="r9", repo=None),
                ]
            )
        }
    )
    rows = model.run_rows(index)
    assert {r.issue for r in rows} == {6}  # issue 5's repo-less event is gone
    assert rows[0].repo == "acme/sandbox"


def test_two_bound_workspaces_sharing_an_issue_number_are_distinct_rows():
    """Two Bound Workspaces can share an issue number (ADR-0056): the index is
    keyed by (repo, issue), so each becomes its own row with links built from
    its OWN repo — neither shadows the other."""
    index = make_index(
        {
            None: page(
                [
                    run_event(20, 7, "pr-open", run_id="rA", pr=11, repo="acme/app"),
                    run_event(10, 7, "claimed", run_id="rB", repo="acme/infra"),
                ]
            )
        }
    )
    rows = {r.repo: r for r in model.run_rows(index)}
    assert set(rows) == {"acme/app", "acme/infra"}
    assert rows["acme/app"].issue == 7 and rows["acme/infra"].issue == 7
    assert rows["acme/app"].pr_url == "https://github.com/acme/app/pull/11"
    assert rows["acme/infra"].issue_url == "https://github.com/acme/infra/issues/7"


def test_pause_notice_lists_each_repo_and_is_empty_when_none():
    assert model.pause_notice(state_doc(dispatch_pauses=[])) == ""
    text = model.pause_notice(
        state_doc(
            dispatch_pauses=[
                {"repo": "acme/app", "reason": "GitHub 503", "first_seen": NOW, "last_seen": NOW},
                {"repo": "acme/infra", "reason": "", "first_seen": NOW, "last_seen": NOW},
            ]
        )
    )
    assert "dispatch paused: acme/app — GitHub 503" in text
    assert "dispatch paused: acme/infra — unspecified" in text  # blank reason is labeled


def test_unbound_notice_lists_each_obligation_and_is_empty_when_none():
    assert model.unbound_notice(state_doc(unbound_obligations=[])) == ""
    text = model.unbound_notice(
        state_doc(
            unbound_obligations=[
                {
                    "kind": "grant",
                    "repo": "acme/gone",
                    "ref": "acme/gone#7",
                    "reason": "pending grant to worker-a awaiting activation",
                    "since": NOW - 600,
                },
                {
                    "kind": "chained",
                    "repo": "acme/gone",
                    "ref": "acme/gone PR #12",
                    "reason": "chained dependent of #3 (closed unmerged)",
                    "since": NOW - 60,
                },
            ]
        )
    )
    assert "operator-owned, no GitHub cleanup" in text
    assert "unbound grant acme/gone#7 — pending grant to worker-a awaiting activation" in text
    assert "unbound chained acme/gone PR #12 — chained dependent of #3 (closed unmerged)" in text


def test_run_index_bootstrap_is_complete_across_page_boundaries():
    """The 500-row bound is on EVENTS, not issues: an older active issue
    whose latest event fell off the first page must stay visible. Two pages,
    multiple events per issue crossing the boundary, issue 3's only (still
    live) event on page two."""
    index = make_index(
        {
            None: page(
                [
                    run_event(60, 7, "gate", run_id="r7b", attempt=2),
                    run_event(50, 9, "pr-open", run_id="r9", pr=41),
                    run_event(40, 7, "claimed", run_id="r7b", attempt=2),
                ],
                next_cursor="40",
            ),
            "40": page(
                [
                    run_event(30, 7, "failed", run_id="r7a", failure_class="timeout"),
                    run_event(20, 3, "claimed", run_id="r3"),
                    run_event(10, 9, "gate", run_id="r9"),
                ]
            ),
        }
    )
    rows = {r.issue: r for r in model.run_rows(index)}
    assert set(rows) == {3, 7, 9}  # the older active issue survives the boundary
    assert rows[3].phase == "claimed" and not rows[3].terminal
    assert rows[7].phase == "gate" and rows[7].run_id == "r7b"  # latest event wins
    assert rows[9].phase == "pr-open"
    assert not index.truncated


def test_run_index_advances_incrementally_without_rescanning_history():
    pages = {
        None: page([run_event(20, 3, "claimed", run_id="r3")]),
    }
    calls: list[str | None] = []

    def fetch_runs(cursor):
        calls.append(cursor)
        return pages.get(cursor, page([]))

    index = model.RunIndex()
    index.refresh(fetch_runs, pager({}))
    assert calls == [None] and index.max_run_id == 20
    # Next poll: only new head events land; overlap stops the walk on the
    # head page — history is never re-walked.
    pages[None] = page(
        [run_event(40, 3, "gate", run_id="r3"), run_event(20, 3, "claimed", run_id="r3")],
        next_cursor="20",
    )
    calls.clear()
    index.refresh(fetch_runs, pager({}))
    assert calls == [None]
    (row,) = model.run_rows(index)
    assert row.phase == "gate" and index.max_run_id == 40


def test_run_index_gap_closed_within_the_bound_stays_complete():
    """A large unseen backlog that the bounded advance CAN cover: every new
    event lands and the pre-existing older issue survives untouched."""
    index = model.RunIndex()
    index.refresh(pager({None: page([run_event(1, 3, "claimed", run_id="r3")])}), pager({}))
    backlog: dict[str | None, dict[str, Any]] = {
        None: page([run_event(30, 7, "gate", run_id="r7")], next_cursor="30")
    }
    for event_id in range(29, 1, -1):  # 28 more pages, one unseen event each
        backlog[str(event_id + 1)] = page(
            [run_event(event_id, 7, "claimed", run_id="r7")], next_cursor=str(event_id)
        )
    backlog["2"] = page([run_event(1, 3, "claimed", run_id="r3")])
    index.refresh(pager(backlog), pager({}))
    rows = {r.issue: r for r in model.run_rows(index)}
    assert set(rows) == {3, 7}
    assert rows[7].phase == "gate" and not index.truncated


def test_run_index_unclosable_gap_rebootstraps_and_discloses():
    """More unseen run events than even the bounded advance walk covers:
    the index re-bootstraps from the head. The rebuilt window is complete
    for what it holds; anything beyond it is DISCLOSED via the truncated
    flag (runs_notice) — an issue can only leave the listing with the
    incomplete-data notice showing, never silently."""
    index = model.RunIndex()
    index.refresh(pager({None: page([run_event(1, 3, "claimed", run_id="r3")])}), pager({}))
    flood: dict[str | None, dict[str, Any]] = {
        None: page([run_event(9000, 7, "gate", run_id="r7")], next_cursor="9000")
    }
    for event_id in range(8999, 9000 - 3 * model.INDEX_MAX_PAGES, -1):
        flood[str(event_id + 1)] = page(
            [run_event(event_id, 7, "claimed", run_id="r7")], next_cursor=str(event_id)
        )
    index.refresh(pager(flood), pager({}))
    rows = {r.issue: r for r in model.run_rows(index)}
    assert rows[7].phase == "gate"  # the newest window is correct
    assert index.truncated  # and the missing tail is disclosed
    assert model.runs_notice(index.truncated) != ""


def test_run_index_truncated_bootstrap_is_disclosed_never_silent():
    """A bootstrap that exhausts INDEX_MAX_PAGES with history remaining
    marks the index incomplete — runs_notice renders the disclosure."""
    endless: dict[str | None, dict[str, Any]] = {}
    top_id = 100_000
    endless[None] = page([run_event(top_id, 1, "claimed")], next_cursor=str(top_id))
    for i in range(model.INDEX_MAX_PAGES + 5):
        event_id = top_id - 1 - i
        endless[str(event_id + 1)] = page(
            [run_event(event_id, 1 + i, "claimed")], next_cursor=str(event_id)
        )
    index = make_index(endless)
    assert index.truncated
    notice = model.runs_notice(True)
    assert "incomplete" in notice and "missing" in notice
    assert model.runs_notice(False) == ""


def test_run_index_progress_eviction_never_removes_the_run():
    """Progress is evictable advisory telemetry (ADR-0016): a live Run whose
    progress records were all evicted keeps its row with telemetry fields
    empty; a terminal Run needs no progress at all."""
    index = make_index(
        {
            None: page(
                [
                    run_event(30, 7, "gate", run_id="r2", attempt=2),
                    run_event(10, 5, "failed", run_id="r9", failure_class="infra"),
                ]
            )
        },
        {None: page([], evicted=True)},  # every progress record evicted
    )
    rows = {r.issue: r for r in model.run_rows(index)}
    assert set(rows) == {5, 7}
    assert rows[7].elapsed_seconds is None and rows[7].tool_calls is None
    assert rows[5].terminal and rows[5].failure_class == "infra"


def test_run_index_progress_paginates_until_live_runs_are_covered():
    """The latest progress for a live Run can sit beyond the first progress
    page (busier runs emit more): the walk continues until every live
    run_id is covered."""
    index = make_index(
        {
            None: page(
                [
                    run_event(90, 7, "gate", run_id="r7"),
                    run_event(80, 3, "gate", run_id="r3"),
                ]
            )
        },
        {
            None: page(
                [progress_event(95, "r7", tool_calls=50)],
                next_cursor="95",
            ),
            "95": page([progress_event(70, "r3", tool_calls=9)]),
        },
    )
    rows = {r.issue: r for r in model.run_rows(index)}
    assert rows[7].tool_calls == 50
    assert rows[3].tool_calls == 9  # found beyond the first page


def test_run_index_prunes_progress_for_unreferenced_runs():
    index = make_index(
        {None: page([run_event(30, 7, "gate", run_id="r2")])},
        {
            None: page(
                [progress_event(40, "r2", tool_calls=4), progress_event(35, "r-old", tool_calls=9)]
            )
        },
    )
    assert set(index.latest_progress_by_run) == {"r2"}


def test_timeout_budget_env_override_and_default():
    state = state_doc()
    assert model.timeout_budget_seconds(state, "worker") == 3600.0  # unknown stack: default
    state["desired_stacks"].append(
        {
            "node": "box1",
            "name": "worker",
            "kind": "process",
            "state": "running",
            "env": {"THEOZOLITH_AGENT_TIMEOUT_SECONDS": "7200"},
            "attach": [],
        }
    )
    assert model.timeout_budget_seconds(state, "worker") == 7200.0
    state["desired_stacks"][-1]["env"]["THEOZOLITH_AGENT_TIMEOUT_SECONDS"] = "not-a-number"
    assert model.timeout_budget_seconds(state, "worker") == 3600.0


# -- attach assistance (acceptance 6) ---------------------------------------


def test_attach_command_substitutes_exactly_the_configured_argv():
    result = model.attach_command(state_doc(), "box1", "flight-deck-1")
    assert result.ok and result.stack == "deck"
    assert result.command == "ssh box1 -t docker exec -it flight-deck-1 tmux attach"


def test_attach_refuses_stale_heartbeat_evidence_on_the_server_clock():
    state = state_doc()
    state["stack_containers"][0]["updated_at"] = NOW - 151
    result = model.attach_command(state, "box1", "flight-deck-1")
    assert not result.ok
    assert "stale" in result.reason and "151s" in result.reason and "150" in result.reason
    # Freshness is judged against state['now'], not the local clock: the
    # same document is fresh when the server clock says so.
    state["now"] = NOW - 100
    assert model.attach_command(state, "box1", "flight-deck-1").ok


def test_attach_fails_closed_when_snapshot_freshness_is_unprovable():
    """A retained snapshot after a failed refresh has a FROZEN server clock:
    heartbeat evidence would look fresh forever. snapshot_fresh=False
    refuses before any heartbeat judgment — even perfectly fresh-looking
    evidence — naming the unprovable server-clock freshness; the local wall
    clock is never consulted (ADR-0040 amendment)."""
    state = state_doc()  # heartbeat evidence looks 30s fresh — but frozen
    refused = model.attach_command(state, "box1", "flight-deck-1", snapshot_fresh=False)
    assert not refused.ok
    assert "cannot be established" in refused.reason
    assert "refresh succeeds" in refused.reason
    # The same document with a provably-current clock resolves normally —
    # recovery restores the ordinary 150s server-clock evaluation.
    assert model.attach_command(state, "box1", "flight-deck-1", snapshot_fresh=True).ok


def test_freshness_degraded_tracks_failure_and_recovery():
    fresh = model.Freshness()
    assert fresh.degraded  # nothing received yet: nothing is provable
    fresh.succeed(NOW)
    assert not fresh.degraded
    fresh.fail("https://127.0.0.1:8443", "ConnectionRefusedError")
    assert fresh.degraded
    fresh.succeed(NOW + 10)
    assert not fresh.degraded


def test_attach_refusal_ladder():
    # Run containers are categorically headless (ADR-0019).
    state = state_doc(
        run_containers=[
            {
                "name": "ozolith-run-r1",
                "node": "box1",
                "run_id": "r1",
                "owner": "worker",
                "status": "Up",
                "updated_at": NOW - 5,
            }
        ]
    )
    refused = model.attach_command(state, "box1", "ozolith-run-r1")
    assert not refused.ok and "run container" in refused.reason
    # Unknown container.
    assert "not live" in model.attach_command(state_doc(), "box1", "ghost").reason
    # Owner not configured.
    state = state_doc(desired_stacks=[])
    refused = model.attach_command(state, "box1", "flight-deck-1")
    assert "no configured owning Stack" in refused.reason
    # Not container-kind.
    state = state_doc()
    state["desired_stacks"][0]["kind"] = "process"
    assert "not a container-kind" in model.attach_command(state, "box1", "flight-deck-1").reason
    # No attach argv configured.
    state = state_doc()
    state["desired_stacks"][0]["attach"] = []
    assert "exposes no terminal" in model.attach_command(state, "box1", "flight-deck-1").reason


def test_attach_validates_identifiers_before_printing():
    """Forged heartbeat values die in validation, never in a printed
    command (ADR-0022's shell-inert whitelists, mirrored)."""
    state = state_doc()
    state["stack_containers"][0]["name"] = "evil; rm -rf /"
    refused = model.attach_command(state, "box1", "evil; rm -rf /")
    assert not refused.ok and "invalid container name" in refused.reason
    bad_host = model.attach_command(state_doc(), "box1;x", "flight-deck-1")
    assert not bad_host.ok  # not live under that node name — never rendered


def test_attach_quotes_template_elements_that_need_it():
    state = state_doc()
    state["desired_stacks"][0]["attach"] = ["ssh", "{host}", "tmux", "attach", "-t", "my session"]
    result = model.attach_command(state, "box1", "flight-deck-1")
    assert result.command == "ssh box1 tmux attach -t 'my session'"


# -- events: follow mode + eviction honesty (acceptance 8) ------------------


def _event(event_id: int) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": "acme.tick",
        "received_at": NOW,
        "node": None,
        "component": None,
        "payload": {"type": "acme.tick", "n": event_id},
    }


def test_advance_events_first_fetch_takes_one_page():
    pages = [page([_event(5), _event(4)], next_cursor="4")]
    fetched: list[str | None] = []

    def fetch(cursor):
        fetched.append(cursor)
        return pages.pop(0)

    fresh, evicted, gap = model.advance_events(fetch, None)
    assert [e["id"] for e in fresh] == [5, 4]
    assert fetched == [None] and not gap and not evicted


def test_advance_events_stops_at_the_first_already_seen_row():
    calls: list[str | None] = []

    def fetch(cursor):
        calls.append(cursor)
        return page([_event(9), _event(8), _event(7)], next_cursor="7")

    fresh, _evicted, gap = model.advance_events(fetch, known_max_id=8)
    assert [e["id"] for e in fresh] == [9]
    assert calls == [None]  # overlap on the head page: history is never re-walked
    assert not gap


def test_advance_events_walks_the_cursor_across_an_unseen_gap_only():
    pages = {
        None: page([_event(20), _event(19)], next_cursor="19"),
        "19": page([_event(18), _event(17)], next_cursor="17"),
        "17": page([_event(16), _event(15)], next_cursor="15"),
    }
    calls: list[str | None] = []

    def fetch(cursor):
        calls.append(cursor)
        return pages[cursor]

    fresh, _evicted, gap = model.advance_events(fetch, known_max_id=16)
    assert [e["id"] for e in fresh] == [20, 19, 18, 17]
    assert calls == [None, "19"]  # stopped at the page containing known id 16
    assert not gap


def test_advance_events_reports_an_unclosed_gap():
    def fetch(cursor):
        base = 100 - 2 * int(cursor or 100 + 2) if cursor else 100
        base = int(cursor) if cursor else 100
        return page([_event(base), _event(base - 1)], next_cursor=str(base - 1))

    fresh, _evicted, gap = model.advance_events(fetch, known_max_id=1, max_pages=3)
    assert gap  # three full pages, all unseen: the caller must resync
    assert len(fresh) == 6


def test_eviction_notice_is_query_relative_never_global():
    """ADR-0038's split contract: the panel notice keys on the response's
    query-relative `evicted` — any_evicted alone flags nothing."""
    assert model.eviction_notice(False) == ""
    unaffected = page([], evicted=False)
    unaffected["any_evicted"] = True  # some other scope lost rows
    assert model.eviction_notice(bool(unaffected["evicted"])) == ""
    notice = model.eviction_notice(True)
    assert "may be evicted" in notice and "not complete history" in notice


def test_advance_events_propagates_the_evicted_flag():
    fresh, evicted, _gap = model.advance_events(lambda c: page([_event(3)], evicted=True), None)
    assert evicted and [e["id"] for e in fresh] == [3]


def test_continuity_notice_distinguishes_eviction_from_follow_gap():
    """The two incompleteness causes are different facts: server-side
    eviction (query-relative, ADR-0038) vs a client-side follow overflow.
    One combined 'history incomplete' line names whichever apply — and the
    gap wording explicitly disclaims being eviction."""
    assert model.continuity_notice(False, False) == ""
    evicted_only = model.continuity_notice(True, False)
    assert "may be evicted" in evicted_only and "follow mode" not in evicted_only
    gap_only = model.continuity_notice(False, True)
    assert "history incomplete" in gap_only
    assert "skipped" in gap_only and "not server eviction" in gap_only
    assert "may be evicted" not in gap_only  # never labeled as eviction
    both = model.continuity_notice(True, True)
    assert "may be evicted" in both and "skipped" in both
    assert both.startswith("history incomplete")


# -- settings + degraded banner ---------------------------------------------


def test_settings_rows_render_address_fields_then_tunables():
    rows = model.settings_rows(state_doc())
    assert rows[0] == ("control_ip", "203.0.113.5")
    assert rows[1] == ("control_port", "443")
    assert rows[2][0] == "browser_origin" and "disabled" in rows[2][1]
    assert ("heartbeat_seconds", "60.0") in rows


def test_freshness_banner_appears_on_failure_and_clears_on_success():
    fresh = model.Freshness()
    fresh.succeed(NOW - 30)
    assert fresh.banner(NOW) == ""
    fresh.fail("https://127.0.0.1:8443", "ConnectionRefusedError")
    banner = fresh.banner(NOW)
    assert "CONTROL UNREACHABLE" in banner
    assert "https://127.0.0.1:8443" in banner and "ConnectionRefusedError" in banner
    assert "30s stale" in banner
    fresh.succeed(NOW)
    assert fresh.banner(NOW) == ""


# -- mirrored constants track their owners ----------------------------------


def test_mirrored_constants_match_their_sources():
    """Every constant the TUI redeclares (to keep web/, worker internals,
    and the store out of its import tree) is pinned to the owning module."""
    from pathlib import Path

    from theozolith_control import configrepo
    from theozolith_control.web import terminal, views
    from theozolith_worker import config as worker_config
    from theozolith_worker import evidence

    assert model.STALE_AFTER_SECONDS == views.STALE_AFTER_SECONDS
    assert model._HOST_RE.pattern == terminal._HOST_RE.pattern
    assert model._CONTAINER_RE.pattern == terminal._CONTAINER_RE.pattern
    assert (model.ATTACH_HOST, model.ATTACH_CONTAINER) == configrepo.ATTACH_PLACEHOLDERS
    assert model.EVIDENCE_BRANCH == evidence.EVIDENCE_BRANCH
    assert evidence.run_dir(5, "r9") == "runs/issue-5/r9"  # run_rows composes this shape
    source = Path(worker_config.__file__).read_text(encoding="utf-8")
    assert f'"{model.AGENT_TIMEOUT_ENV}", default="3600"' in source
    assert model.AGENT_TIMEOUT_DEFAULT_SECONDS == 3600.0
