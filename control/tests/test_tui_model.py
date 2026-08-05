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


# -- runs: the client-side run_states() twin --------------------------------


def test_run_rows_latest_per_issue_joined_with_latest_progress():
    runs = page(
        [
            run_event(30, 7, "gate", run_id="r2", attempt=2),
            run_event(20, 7, "claimed", run_id="r2", attempt=2),
            run_event(10, 5, "pr-open", run_id="r9", pr=41),
        ]
    )
    progress = page(
        [
            progress_event(31, "r2", tool_calls=44, elapsed_seconds=500.0),
            progress_event(21, "r2", tool_calls=14),
        ]
    )
    rows = model.run_rows(runs, progress, "acme/sandbox")
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
    # The channel gap (ADR-0040): failed events carry no failure class
    # today — the field is empty, rendered honestly absent, and lights up
    # without TUI changes the day the event grows the field.
    assert done.failure_class == ""


def test_run_rows_without_a_repo_omit_links_but_keep_the_reference():
    rows = model.run_rows(page([run_event(10, 5, "failed", run_id="r9")]), page([]), None)
    assert rows[0].pr_url == "" and rows[0].evidence_url == ""
    assert rows[0].evidence_ref == "theozolith/evidence: runs/issue-5/r9"


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
