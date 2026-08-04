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


def test_eviction_indicator_appears_exactly_when_rows_were_evicted(control: ControlRig):
    """ADR-0038: the indicator is a store-level fact recorded by the one
    deleter in the same transaction — false before any eviction, true on
    every response after one, so clients exhausting pagination report the
    history as incomplete."""
    progress = {
        "type": "theozolith.run.progress",
        "worker": "worker-a",
        "node": "box1",
        "issue": 1,
        "run_id": "r1",
        "transcript_tail": "x" * 1000,
    }
    control.node_post("/api/v1/events", progress)
    control.node_post("/api/v1/events", run_event(1, "claimed"))
    assert _read(control)["evicted"] is False

    assert control.store.evict_progress(budget_bytes=10) == 1
    page = _read(control)
    assert page["evicted"] is True
    # Terminal events survive eviction (ADR-0016); only the progress row died.
    assert [e["type"] for e in page["events"]] == ["theozolith.run"]
