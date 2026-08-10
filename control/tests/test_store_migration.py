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
