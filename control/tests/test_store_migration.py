"""Store schema migration: an old-schema cache DB opens on the current code.

The store is a cache, not an archive (ADR-0016), but an in-place upgrade must
never drop the live Run events a restart depends on. The ADR-0020 sweep
renamed the events ``worker`` column to ``driver``; an old-schema database must
open with that column present and backfilled from the old values.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from controlrig import make_rig
from theozolith_control.store import Store

# The events table exactly as it shipped before the ADR-0020 sweep: a `worker`
# column, and no `driver`/`component` yet (an even earlier schema).
_OLD_EVENTS = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    received_at REAL NOT NULL,
    payload TEXT NOT NULL,
    node TEXT,
    worker TEXT,
    issue INTEGER,
    run_id TEXT,
    attempt INTEGER,
    phase TEXT,
    pr INTEGER,
    round INTEGER,
    verdict TEXT
);
"""


def _seed_old_db(path: Path) -> None:
    db = sqlite3.connect(str(path))
    db.executescript(_OLD_EVENTS)
    payload = {"type": "theozolith.run", "worker": "worker-a", "issue": 5, "phase": "claimed"}
    db.execute(
        "INSERT INTO events (type, received_at, payload, node, worker, issue, run_id, phase)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("theozolith.run", 1000.0, json.dumps(payload), "box1", "worker-a", 5, "r1", "claimed"),
    )
    db.commit()
    db.close()


def test_old_schema_store_opens_with_backfilled_driver_column(tmp_path):
    path = tmp_path / "store.db"
    _seed_old_db(path)

    store = Store(path, clock=lambda: 2000.0)

    columns = {r["name"] for r in store._db.execute("PRAGMA table_info(events)")}
    assert "driver" in columns and "worker" not in columns  # renamed, not duplicated
    assert "repo" in columns  # ALTER-added (ADR-0056), NULL on legacy rows

    # The old `worker` value is carried over as the backfill: the driver-keyed
    # liveness query sees it under `driver`.
    assert store.driver_last_seen("worker-a") == 1000.0
    # The legacy fence (ADR-0056): a pre-ADR-0056 run event has no repo, so
    # it never enters claim logic — yet stays readable as events history.
    assert store.live_claims() == []
    events = store.events(type="theozolith.run", issue=5)
    assert len(events) == 1 and events[0]["phase"] == "claimed"
    # Legacy rows surface in the display read model with repo None.
    (state,) = store.run_states()
    assert state["issue"] == 5 and state["repo"] is None


# A nodes table as it shipped in the FIRST config-distribution cut (ADR-0042):
# drivers_hash present, but no drivers_hash_reported presence bit yet.
_OLD_NODES = """
CREATE TABLE nodes (
    name TEXT PRIMARY KEY,
    version TEXT NOT NULL DEFAULT '',
    drivers_hash TEXT NOT NULL DEFAULT '',
    drivers_built_against TEXT NOT NULL DEFAULT '',
    registered_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
"""


def test_old_nodes_schema_gains_the_presence_bit_and_reads_fail_open(tmp_path):
    """An old-schema nodes row (no drivers_hash_reported) opens on the current
    code with the column added, defaulting to 0 — 'field never reported', the
    fail-open reading — so a stale cache never blocks dispatch until the next
    heartbeat records what the daemon actually reports (ADR-0042)."""
    path = tmp_path / "cache.db"
    db = sqlite3.connect(str(path))
    db.executescript(_OLD_NODES)
    # A pre-column row that even carried an empty drivers_hash: without the bit,
    # empty was ambiguous; the migration must NOT read it as "explicit none".
    db.execute(
        "INSERT INTO nodes (name, version, drivers_hash, drivers_built_against,"
        " registered_at, last_seen) VALUES ('box1', '0.3.0', '', '', 100.0, 100.0)",
    )
    db.commit()
    db.close()

    store = Store(path, clock=lambda: 200.0)
    columns = {r["name"] for r in store._db.execute("PRAGMA table_info(nodes)")}
    assert "drivers_hash_reported" in columns
    # Fail-open: the migrated row reads as "field not reported" (None), never ''.
    assert store.node_drivers_hash("box1") is None
    # The next heartbeat that explicitly reports '' flips it to a real report.
    store.touch_node("box1", "0.3.0", drivers_hash="")
    assert store.node_drivers_hash("box1") == ""


# The six coordination cache tables exactly as they shipped before ADR-0056:
# keyed by bare issue number, no repo anywhere — plus the pre-ADR-0056 drivers
# registry.
_OLD_COORDINATION = """
CREATE TABLE grants (
    issue INTEGER PRIMARY KEY,
    worker TEXT NOT NULL,
    node TEXT NOT NULL DEFAULT '',
    login TEXT NOT NULL,
    granted_at REAL NOT NULL
);
CREATE TABLE malformed_states (
    issue INTEGER PRIMARY KEY,
    detail TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE dispatch_waits (
    issue INTEGER PRIMARY KEY,
    reason TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE zombie_flags (
    issue INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    worker TEXT NOT NULL DEFAULT '',
    node TEXT NOT NULL DEFAULT '',
    flagged_at REAL NOT NULL,
    PRIMARY KEY (issue, run_id)
);
CREATE TABLE janitor_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    worker TEXT NOT NULL,
    reason TEXT NOT NULL,
    acted_at REAL NOT NULL
);
CREATE TABLE chained_dependents (
    dependent_pr INTEGER PRIMARY KEY,
    blocker_issue INTEGER NOT NULL,
    blocker_pr INTEGER NOT NULL,
    blocker_state TEXT NOT NULL,
    recorded_sha TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE drivers (
    worker TEXT PRIMARY KEY,
    node TEXT NOT NULL DEFAULT '',
    login TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    registered_at REAL NOT NULL,
    last_dispatch_at REAL NOT NULL
);
"""

_SIX_CACHE_TABLES = (
    "grants",
    "malformed_states",
    "dispatch_waits",
    "zombie_flags",
    "janitor_actions",
    "chained_dependents",
)


def test_pre_adr0056_cache_tables_are_dropped_and_recreated_empty(tmp_path):
    """The ADR-0056 re-key: a pre-ADR-0056 cache.db opens with the six
    coordination cache tables recreated EMPTY with the repo column — never a
    backfill (a guessed repo would be the wrong-repo collision the re-key
    prevents; the next dispatch pass rebuilds the rows from GitHub, which is
    the ADR-0016 cache doctrine). drivers is a registry, so it keeps its
    rows and ALTER-gains repo/stack."""
    path = tmp_path / "cache.db"
    db = sqlite3.connect(str(path))
    db.executescript(_OLD_COORDINATION)
    db.execute(
        "INSERT INTO grants (issue, worker, node, login, granted_at)"
        " VALUES (7, 'worker-a', 'box1', 'ozolith-worker-a', 100.0)"
    )
    db.execute(
        "INSERT INTO malformed_states (issue, detail, first_seen, last_seen)"
        " VALUES (9, 'failed + plan_ready', 100.0, 100.0)"
    )
    db.execute(
        "INSERT INTO janitor_actions (issue, run_id, worker, reason, acted_at)"
        " VALUES (5, 'r1', 'worker-a', 'escalated', 100.0)"
    )
    db.execute(
        "INSERT INTO drivers (worker, node, login, role, registered_at, last_dispatch_at)"
        " VALUES ('worker-a', 'box1', 'ozolith-worker-a', 'implementer', 100.0, 100.0)"
    )
    db.commit()
    db.close()

    store = Store(path, clock=lambda: 200.0)
    for table in _SIX_CACHE_TABLES:
        info = {r["name"]: r for r in store._db.execute(f"PRAGMA table_info({table})")}
        assert "repo" in info, table
        # janitor_actions.repo is the ONE nullable cache column — NULL marks
        # a node-scoped act, never a '' sentinel; every other cache table's
        # repo is NOT NULL (ADR-0056).
        expect_notnull = 0 if table == "janitor_actions" else 1
        assert info["repo"]["notnull"] == expect_notnull, table
        count = store._db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert count == 0, table  # recreated empty, never backfilled
    # The registry survived with its rows, repo/stack defaulting to ''.
    (driver,) = store.drivers()
    assert driver["worker"] == "worker-a"
    assert driver["repo"] == "" and driver["stack"] == ""
    # The re-keyed accessors work against the recreated tables.
    store.record_grant("acme/sandbox", 7, "worker-a", "box1", "ozolith-worker-a")
    assert store.granted_issues("acme/sandbox") == {7}


def test_migrated_rig_rebuilds_grants_from_github_on_the_next_pass(tmp_path):
    """The drop is safe because the rebuild doctrine holds (ADR-0016): the
    app opens a pre-ADR-0056 cache.db (tables recreated empty, old grant
    gone) and the next dispatch pass re-derives the grant state from what
    GitHub answers — the cache was never the truth."""
    path = tmp_path / "data" / "cache" / "cache.db"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(str(path))
    db.executescript(_OLD_COORDINATION)
    db.execute(
        "INSERT INTO grants (issue, worker, node, login, granted_at)"
        " VALUES (7, 'worker-a', 'box1', 'ozolith-worker-a', 100.0)"
    )
    db.commit()
    db.close()

    control = make_rig(tmp_path)
    assert control.store.granted_issues("acme/sandbox") == set()  # never backfilled
    # GitHub still lists #7 as plan_ready and unassigned — the stale grant
    # row is gone, so the pass grants it afresh, write-through and keyed.
    control.github.add_issue(7, labels={"plan_ready"}, assignees=[])
    assert control.dispatch().json()["issue"]["number"] == 7
    assert control.store.granted_issues("acme/sandbox") == {7}


def test_current_schema_store_reopens_without_dropping(tmp_path):
    """The drop is a one-time migration: a store already carrying the repo
    key keeps its coordination rows across a reopen (a restart must never
    empty the caches)."""
    path = tmp_path / "cache.db"
    store = Store(path, clock=lambda: 100.0)
    store.record_grant("acme/sandbox", 7, "worker-a", "box1", "ozolith-worker-a")
    store.close()

    reopened = Store(path, clock=lambda: 200.0)
    assert reopened.granted_issues("acme/sandbox") == {7}
