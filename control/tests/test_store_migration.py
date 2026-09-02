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

    # The old `worker` value is carried over as the backfill: the driver-keyed
    # liveness query and the live-claim reader both see it under `driver`.
    assert store.driver_last_seen("worker-a") == 1000.0
    claims = store.live_claims()
    assert len(claims) == 1
    assert claims[0].driver == "worker-a" and claims[0].issue == 5


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


def test_old_db_gains_the_cli_status_table(tmp_path):
    """ADR-0055: opening a pre-CLI-Pin database creates cli_status (the
    CREATE IF NOT EXISTS migration lane) and the record/read round-trip
    works replace-per-beat."""
    path = tmp_path / "store.db"
    _seed_old_db(path)
    store = Store(path, clock=lambda: 2000.0)
    assert store.fleet_state()["cli_status"] == []
    store.record_cli_status(
        "box1",
        [
            {
                "worker_type": "flightdeck",
                "tool": "claude",
                "desired": "2.1.257",
                "applied": "2.1.250",
                "converged": False,
                "error_class": "CliDownloadFailed",
                "error": "claude 2.1.257: download failed after 0 bytes",
            }
        ],
    )
    rows = store.fleet_state()["cli_status"]
    assert len(rows) == 1 and rows[0]["node"] == "box1"
    assert rows[0]["desired"] == "2.1.257" and rows[0]["converged"] == 0
    assert rows[0]["error_message"].startswith("claude 2.1.257")
    store.record_cli_status("box1", [])
    assert store.fleet_state()["cli_status"] == []
