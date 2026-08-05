"""GET /api/v1/events (ADR-0038, amending ADR-0015): bearer-auth newest-
first read view over stored event rows — unknown types unrendered —
with node/component/type filters, since + cursor pagination, and the
eviction indicator that appears when (and only when) older rows have been
evicted."""

from __future__ import annotations

from controlrig import ControlRig, run_event


def _error_event(node: str = "box1", component: str = "node-daemon", message: str = "boom"):
    return {
        "type": "theozolith.error",
        "node": node,
        "component": component,
        "error_class": "RuntimeError",
        "message": message,
        "context": "",
    }


def _read(control: ControlRig, **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    answer = control.admin("GET", "/api/v1/events" + (f"?{query}" if query else ""))
    assert answer.status_code == 200
    return answer.json()


def test_events_read_requires_the_admin_bearer(control: ControlRig):
    assert control.client.get("/api/v1/events").status_code == 401
    node_token = control.node_token()
    refused = control.client.get(
        "/api/v1/events", headers={"Authorization": f"Bearer {node_token}"}
    )
    assert refused.status_code == 401  # node tokens are not admin tokens


def test_unknown_types_round_trip_unrendered(control: ControlRig):
    """The typed-event extension point survives the read view: an unknown
    namespaced type comes back payload-verbatim, no rendering, no schema."""
    custom = {"type": "acme.backup", "node": "box1", "shape": {"nested": [1, 2, 3]}}
    control.node_post("/api/v1/events", custom)
    page = _read(control, type="acme.backup")
    assert [e["payload"] for e in page["events"]] == [custom]
    assert page["events"][0]["type"] == "acme.backup"
    assert page["events"][0]["node"] == "box1"
    assert page["evicted"] is False


def test_filters_node_component_and_type(control: ControlRig):
    control.node_post("/api/v1/events", _error_event(node="box1", component="node-daemon"))
    control.node_post(
        "/api/v1/events", _error_event(node="box2", component="implementer-driver"), node="box2"
    )
    control.node_post("/api/v1/events", run_event(7, "claimed", node="box1"))

    by_type = _read(control, type="theozolith.error")
    assert len(by_type["events"]) == 2
    by_node = _read(control, type="theozolith.error", node="box2")
    assert [e["component"] for e in by_node["events"]] == ["implementer-driver"]
    by_component = _read(control, component="node-daemon")
    assert len(by_component["events"]) == 1
    everything = _read(control)
    assert len(everything["events"]) == 3


def test_cursor_pages_newest_first_until_exhausted(control: ControlRig):
    for issue in range(1, 6):
        control.node_post("/api/v1/events", run_event(issue, "claimed", run_id=f"r{issue}"))

    first = _read(control, limit=2)
    assert [e["payload"]["issue"] for e in first["events"]] == [5, 4]
    assert first["next_cursor"] is not None

    second = _read(control, limit=2, cursor=first["next_cursor"])
    assert [e["payload"]["issue"] for e in second["events"]] == [3, 2]

    third = _read(control, limit=2, cursor=second["next_cursor"])
    assert [e["payload"]["issue"] for e in third["events"]] == [1]
    assert third["next_cursor"] is None  # short page: the end

    malformed = control.admin("GET", "/api/v1/events?cursor=not-a-cursor")
    assert malformed.status_code == 400


def test_since_filters_on_the_server_clock(control: ControlRig):
    control.node_post("/api/v1/events", run_event(1, "claimed"))
    control.clock.advance(100)
    cutoff = control.clock.now
    control.node_post("/api/v1/events", run_event(2, "claimed"))

    page = _read(control, since=cutoff)
    assert [e["payload"]["issue"] for e in page["events"]] == [2]
    assert all(e["received_at"] >= cutoff for e in page["events"])


def test_limit_clamps_to_the_page_bounds(control: ControlRig):
    control.node_post("/api/v1/events", run_event(1, "claimed"))
    control.node_post("/api/v1/events", run_event(2, "claimed"))
    assert len(_read(control, limit=0)["events"]) == 1  # clamped up to 1
    assert len(_read(control, limit=99999)["events"]) == 2  # clamped to the max
    assert control.admin("GET", "/api/v1/events?limit=abc").status_code == 400
    assert control.admin("GET", "/api/v1/events?since=abc").status_code == 400


def _progress_event(node: str = "box1", issue: int = 1) -> dict:
    return {
        "type": "theozolith.run.progress",
        "worker": "worker-a",
        "node": node,
        "issue": issue,
        "run_id": f"r{issue}",
        "transcript_tail": "x" * 1000,
    }


def test_eviction_evidence_is_query_relative(control: ControlRig):
    """ADR-0038 (amended): `evicted` answers for THIS query — filters and
    window — while `any_evicted` is the global fact. A progress-only
    eviction never marks an error-only or other-node read incomplete."""
    control.node_post("/api/v1/events", _progress_event())
    control.node_post("/api/v1/events", run_event(1, "claimed"))
    evicted_at = control.clock.now
    before = _read(control)
    assert before["evicted"] is False and before["any_evicted"] is False

    assert control.store.evict_progress(budget_bytes=10) == 1
    page = _read(control)
    assert page["evicted"] is True  # unbounded read: all history is the window
    assert page["any_evicted"] is True
    # Terminal events survive eviction (ADR-0016); only the progress row died.
    assert [e["type"] for e in page["events"]] == ["theozolith.run"]

    # Filter-relative: the evicted row was progress on box1 — an error-only
    # or other-node query is complete and says so (any_evicted stays true).
    error_page = _read(control, type="theozolith.error")
    assert error_page["evicted"] is False and error_page["any_evicted"] is True
    assert _read(control, type="theozolith.run.progress")["evicted"] is True
    assert _read(control, type="theozolith.run.progress", node="box2")["evicted"] is False
    assert _read(control, type="theozolith.run.progress", node="box1")["evicted"] is True

    # Window-relative inside a matching filter…
    assert _read(control, type="theozolith.run.progress", since=evicted_at - 10)["evicted"] is True
    # …and complete again for a window entirely after the eviction.
    control.clock.advance(100)
    later = _read(control, since=control.clock.now - 50)
    assert later["evicted"] is False and later["any_evicted"] is True


def test_matching_error_eviction_marks_error_queries_incomplete(control: ControlRig):
    error = _error_event(node="box1", component="node-daemon", message="m" * 1500)
    control.node_post("/api/v1/events", error)
    assert control.store.evict_progress(budget_bytes=10) == 1
    assert _read(control, type="theozolith.error")["evicted"] is True
    assert _read(control, type="theozolith.error", node="box1")["evicted"] is True
    # Nonmatching node/component filters stay complete.
    assert _read(control, type="theozolith.error", node="box2")["evicted"] is False
    assert (
        _read(control, type="theozolith.error", component="implementer-driver")["evicted"] is False
    )


def test_legacy_watermark_reads_conservatively(control: ControlRig):
    """A pre-amendment cache carries only the single-row watermark: its
    scope is unknowable, so every filtered query reads incomplete — and a
    zero timestamp (unknown reach) is conservative for any window."""
    control.store._db.execute(
        "INSERT INTO event_evictions"
        " (scope, last_evicted_id, evicted_count, evicted_at, last_evicted_received_at)"
        " VALUES ('events', 5, 3, ?, 0)",
        (control.clock.now,),
    )
    page = _read(control, type="theozolith.error", node="box-unrelated")
    assert page["evicted"] is True and page["any_evicted"] is True
    assert _read(control, since=control.clock.now + 999)["evicted"] is True


def test_scope_cap_collapses_to_the_conservative_wildcard(control: ControlRig):
    """Eviction evidence is bounded (never an archive): past the scope cap
    it collapses to the '*' sentinel, which matches every filter — the
    conservative direction."""
    from theozolith_control.store import EVICTION_SCOPE_CAP

    for i in range(EVICTION_SCOPE_CAP + 5):
        control.store.record_event(_progress_event(node=f"n{i}", issue=i))
    assert control.store.evict_progress(budget_bytes=0) == EVICTION_SCOPE_CAP + 5
    scopes = control.store._db.execute("SELECT type FROM event_eviction_scopes").fetchall()
    assert [s["type"] for s in scopes] == ["*"]
    # The wildcard matches filters no evicted row ever carried.
    assert _read(control, type="theozolith.error", node="nowhere")["evicted"] is True
