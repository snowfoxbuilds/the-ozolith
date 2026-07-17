"""Control Node persistence: one SQLite database, one writer process.

Everything the Control Node knows is observational or advisory (ADR-0002):
node/stack/container/image status from heartbeats, namespaced events from
drivers, queued infrastructure commands, short-lived claim intents, encrypted
secret values, and the janitor/auditor records. Nothing here is coordination
state — GitHub owns that.

The connection is shared across FastAPI handlers and the sweep threads, so
every access serializes on one lock; at control-plane cadence (a heartbeat
per node per minute) contention is irrelevant.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    name TEXT PRIMARY KEY,
    version TEXT NOT NULL DEFAULT '',
    registered_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stacks (
    node TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (node, name)
);
CREATE TABLE IF NOT EXISTS containers (
    node TEXT NOT NULL,
    name TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (node, name)
);
CREATE TABLE IF NOT EXISTS images (
    node TEXT NOT NULL,
    name TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT '',
    base_digest TEXT NOT NULL DEFAULT '',
    instruction_hash TEXT NOT NULL DEFAULT '',
    built_at TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (node, name)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    received_at REAL NOT NULL,
    payload TEXT NOT NULL,
    -- Extracted columns for the known namespaced types (NULL otherwise).
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
CREATE INDEX IF NOT EXISTS events_issue ON events (issue, id);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node TEXT NOT NULL,
    verb TEXT NOT NULL,
    target TEXT,
    created_at REAL NOT NULL,
    delivered_at REAL,
    completed_at REAL
);
CREATE TABLE IF NOT EXISTS claim_intents (
    issue INTEGER PRIMARY KEY,
    worker TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS janitor_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    worker TEXT NOT NULL,
    reason TEXT NOT NULL,
    acted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr INTEGER NOT NULL,
    issue INTEGER,
    expected INTEGER NOT NULL,
    actual INTEGER NOT NULL,
    detail TEXT NOT NULL,
    found_at REAL NOT NULL,
    UNIQUE (pr, expected, actual)
);
"""

EVENT_RUN = "theozolith.run"
EVENT_REVIEW = "theozolith.review"

RUN_PHASES = ("claimed", "gate", "pr-open", "failed", "escalated")
# Phases meaning "a Run is in flight" — what the zombie janitor watches.
LIVE_RUN_PHASES = ("claimed", "gate")


@dataclass(frozen=True)
class LiveClaim:
    """The latest non-terminal Run event for one issue."""

    issue: int
    worker: str
    node: str
    run_id: str
    last_event_at: float


class Store:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- nodes and heartbeat status ----------------------------------------

    def touch_node(self, name: str, version: str = "") -> None:
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO nodes (name, version, registered_at, last_seen)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET last_seen = ?, version = ?",
                (name, version, now, now, now, version),
            )

    def node_last_seen(self, name: str) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT last_seen FROM nodes WHERE name = ?", (name,)).fetchone()
        return row["last_seen"] if row else None

    def record_status(
        self,
        node: str,
        stacks: list[dict[str, Any]],
        containers: list[dict[str, Any]],
        images: list[dict[str, Any]],
    ) -> None:
        """Replace the node's reported status with this heartbeat's."""
        now = self._clock()
        with self._lock, self._db:
            for table in ("stacks", "containers", "images"):
                self._db.execute(f"DELETE FROM {table} WHERE node = ?", (node,))
            self._db.executemany(
                "INSERT INTO stacks (node, name, kind, state, detail, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        node,
                        str(s.get("name", "")),
                        str(s.get("kind", "")),
                        str(s.get("state", "")),
                        str(s.get("detail", "")),
                        now,
                    )
                    for s in stacks
                    if s.get("name")
                ],
            )
            self._db.executemany(
                "INSERT INTO containers (node, name, run_id, owner, status, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        node,
                        str(c.get("name", "")),
                        str(c.get("run_id", "")),
                        str(c.get("owner", "")),
                        str(c.get("status", "")),
                        now,
                    )
                    for c in containers
                    if c.get("name")
                ],
            )
            self._db.executemany(
                "INSERT INTO images"
                " (node, name, tag, base_digest, instruction_hash, built_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        node,
                        str(i.get("name", "")),
                        str(i.get("tag", "")),
                        str(i.get("base_digest", "")),
                        str(i.get("instruction_hash", "")),
                        str(i.get("built_at", "")),
                        now,
                    )
                    for i in images
                    if i.get("name")
                ],
            )

    # -- typed events --------------------------------------------------------

    def record_event(self, event: dict[str, Any]) -> None:
        """Store any namespaced event; known types get extracted columns."""

        def _int(key: str) -> int | None:
            value = event.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        def _str(key: str) -> str | None:
            value = event.get(key)
            return str(value) if isinstance(value, str) and value else None

        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO events (type, received_at, payload, node, worker, issue,"
                " run_id, attempt, phase, pr, round, verdict)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(event.get("type", "")),
                    self._clock(),
                    json.dumps(event, sort_keys=True),
                    _str("node"),
                    _str("worker") or _str("reviewer"),
                    _int("issue"),
                    _str("run_id"),
                    _int("attempt"),
                    _str("phase"),
                    _int("pr"),
                    _int("round"),
                    _str("verdict"),
                ),
            )

    def events(self, *, type: str | None = None, issue: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT type, received_at, payload FROM events"
        clauses, params = [], []
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if issue is not None:
            clauses.append("issue = ?")
            params.append(issue)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY id", params).fetchall()
        return [
            {"type": r["type"], "received_at": r["received_at"], **json.loads(r["payload"])}
            for r in rows
        ]

    def live_claims(self) -> list[LiveClaim]:
        """Issues whose LATEST Run event is non-terminal (claimed/gate)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT e.issue, e.worker, e.node, e.run_id, e.received_at, e.phase"
                " FROM events e JOIN ("
                "   SELECT issue, MAX(id) AS latest FROM events"
                "   WHERE type = ? AND issue IS NOT NULL GROUP BY issue"
                " ) last ON e.id = last.latest",
                (EVENT_RUN,),
            ).fetchall()
        return [
            LiveClaim(
                issue=r["issue"],
                worker=r["worker"] or "",
                node=r["node"] or "",
                run_id=r["run_id"] or "",
                last_event_at=r["received_at"],
            )
            for r in rows
            if r["phase"] in LIVE_RUN_PHASES
        ]

    def worker_last_seen(self, worker: str) -> float | None:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(received_at) AS seen FROM events WHERE worker = ?", (worker,)
            ).fetchone()
        return row["seen"] if row and row["seen"] is not None else None

    # -- commands ------------------------------------------------------------

    def queue_command(self, node: str, verb: str, target: str | None) -> int:
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO commands (node, verb, target, created_at) VALUES (?, ?, ?, ?)",
                (node, verb, target, self._clock()),
            )
        return int(cursor.lastrowid or 0)

    def pending_commands(self, node: str) -> list[dict[str, Any]]:
        """Undelivered AND unacknowledged commands: re-delivered every
        heartbeat until the node reports them completed."""
        now = self._clock()
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT id, verb, target FROM commands"
                " WHERE node = ? AND completed_at IS NULL ORDER BY id",
                (node,),
            ).fetchall()
            self._db.execute(
                "UPDATE commands SET delivered_at = ?"
                " WHERE node = ? AND completed_at IS NULL AND delivered_at IS NULL",
                (now, node),
            )
        return [{"id": r["id"], "verb": r["verb"], "target": r["target"]} for r in rows]

    def complete_commands(self, node: str, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE commands SET completed_at = ? WHERE id = ? AND node = ?",
                [(self._clock(), int(i), node) for i in ids],
            )

    # -- claim intents (the advisory pre-filter, ADR-0002) --------------------

    def claim_intent(self, issue: int, worker: str, ttl_seconds: float) -> tuple[bool, str]:
        """Grant or refuse a short exclusive intent; (allow, holder)."""
        now = self._clock()
        with self._lock, self._db:
            self._db.execute("DELETE FROM claim_intents WHERE expires_at <= ?", (now,))
            row = self._db.execute(
                "SELECT worker FROM claim_intents WHERE issue = ?", (issue,)
            ).fetchone()
            if row is not None and row["worker"] != worker:
                return False, row["worker"]
            self._db.execute(
                "INSERT INTO claim_intents (issue, worker, expires_at) VALUES (?, ?, ?)"
                " ON CONFLICT (issue) DO UPDATE SET worker = ?, expires_at = ?",
                (issue, worker, now + ttl_seconds, worker, now + ttl_seconds),
            )
            return True, worker

    # -- secrets (encrypted values only; crypto lives in crypto.py) ----------

    def put_secret(self, name: str, token: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO secrets (name, token, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET token = ?, updated_at = ?",
                (name, token, self._clock(), token, self._clock()),
            )

    def get_secret_token(self, name: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT token FROM secrets WHERE name = ?", (name,)).fetchone()
        return row["token"] if row else None

    def secret_names(self) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT name FROM secrets ORDER BY name").fetchall()
        return [r["name"] for r in rows]

    def replace_secret_tokens(self, tokens: dict[str, str]) -> None:
        """Key rotation: swap every stored token in one transaction."""
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE secrets SET token = ? WHERE name = ?",
                [(token, name) for name, token in tokens.items()],
            )

    # -- janitor + auditor records --------------------------------------------

    def record_janitor_action(self, issue: int, run_id: str, worker: str, reason: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO janitor_actions (issue, run_id, worker, reason, acted_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (issue, run_id, worker, reason, self._clock()),
            )

    def janitor_acted(self, issue: int, run_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM janitor_actions WHERE issue = ? AND run_id = ?", (issue, run_id)
            ).fetchone()
        return row is not None

    def janitor_actions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT issue, run_id, worker, reason, acted_at FROM janitor_actions ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_audit_finding(
        self, pr: int, issue: int | None, expected: int, actual: int, detail: str
    ) -> bool:
        """Store a mismatch; False when this exact finding is already known."""
        with self._lock, self._db:
            try:
                self._db.execute(
                    "INSERT INTO audit_findings (pr, issue, expected, actual, detail, found_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (pr, issue, expected, actual, detail, self._clock()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def audit_findings(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT pr, issue, expected, actual, detail, found_at"
                " FROM audit_findings ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- the fleet read model (CLI now, M4 dashboard later) --------------------

    def fleet_state(self) -> dict[str, Any]:
        with self._lock:
            nodes = self._db.execute("SELECT * FROM nodes ORDER BY name").fetchall()
            stacks = self._db.execute("SELECT * FROM stacks ORDER BY node, name").fetchall()
            containers = self._db.execute("SELECT * FROM containers ORDER BY node, name").fetchall()
            images = self._db.execute("SELECT * FROM images ORDER BY node, name").fetchall()
            commands = self._db.execute(
                "SELECT id, node, verb, target, created_at, delivered_at, completed_at"
                " FROM commands ORDER BY id"
            ).fetchall()
        return {
            "nodes": [dict(r) for r in nodes],
            "stacks": [dict(r) for r in stacks],
            "run_containers": [dict(r) for r in containers],
            "images": [dict(r) for r in images],
            "commands": [dict(r) for r in commands],
        }
