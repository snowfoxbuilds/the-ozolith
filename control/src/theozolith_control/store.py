"""Control Node persistence: one SQLite database, one writer process.

The database is a cache, never an archive (ADR-0016): the evidence bundle is
the sole durable audit trail, nothing here may ever be the only copy of
anything, and everything is deletable by policy. What it caches: node/stack/
container/image status from heartbeats, namespaced events from drivers
(terminal Run events kept indefinitely — they are tiny and are the metrics
substrate; progress telemetry evicted oldest-first under a byte budget),
queued infrastructure commands, dispatch grants awaiting activation
(ADR-0017), node health for the quarantine gate, dashboard flags, and the
encrypted secret values.

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
CREATE INDEX IF NOT EXISTS events_type ON events (type, id);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node TEXT NOT NULL,
    verb TEXT NOT NULL,
    target TEXT,
    force INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    delivered_at REAL,
    completed_at REAL,
    deferred_reason TEXT
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
-- Write-through claim grants awaiting activation (ADR-0017): a row lives
-- from the GitHub claim write until the driver's claimed event lands; the
-- janitor releases rows that outlive the activation window.
CREATE TABLE IF NOT EXISTS grants (
    issue INTEGER PRIMARY KEY,
    worker TEXT NOT NULL,
    node TEXT NOT NULL DEFAULT '',
    login TEXT NOT NULL,
    granted_at REAL NOT NULL
);
-- Driver registration (ADR-0017 prerequisite): each dispatch request
-- carries the driver's GitHub login; the registry feeds the dashboard.
CREATE TABLE IF NOT EXISTS drivers (
    worker TEXT PRIMARY KEY,
    node TEXT NOT NULL DEFAULT '',
    login TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    registered_at REAL NOT NULL,
    last_dispatch_at REAL NOT NULL
);
-- The quarantine gate (ADR-0016): 2 consecutive failed Runs stop grants to
-- a node; release is human action only, never a timer.
CREATE TABLE IF NOT EXISTS node_health (
    node TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    quarantined INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    since REAL
);
-- Phase-1 zombie escalation (ADR-0016): a silent claim is flagged for the
-- dashboard without touching GitHub; the row clears when the Worker
-- resurfaces or the janitor escalates with evidence.
CREATE TABLE IF NOT EXISTS zombie_flags (
    issue INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    worker TEXT NOT NULL DEFAULT '',
    node TEXT NOT NULL DEFAULT '',
    flagged_at REAL NOT NULL,
    PRIMARY KEY (issue, run_id)
);
-- Malformed coordination states dispatch refuses to launder (ADR-0016):
-- failed + plan_ready stays refused and visible until a human fixes it.
CREATE TABLE IF NOT EXISTS malformed_states (
    issue INTEGER PRIMARY KEY,
    detail TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
"""

# Tables of earlier schema generations, dropped on open: the advisory
# claim-intent pre-filter (replaced by dispatch grants, ADR-0017) and the
# retry auditor's findings (died with the marker machinery, ADR-0016).
_DROPPED_TABLES = ("claim_intents", "audit_findings")

EVENT_RUN = "theozolith.run"
EVENT_REVIEW = "theozolith.review"
EVENT_PROGRESS = "theozolith.run.progress"

RUN_PHASES = ("claimed", "gate", "pr-open", "failed", "escalated")
# Phases meaning "the driver still holds the claim" — what the zombie
# janitor watches. failed is live (ADR-0016): the driver keeps the claim
# through the local retry, so only pr-open and escalated end its watch —
# a driver that dies between the failed Run and its retry stays visible.
LIVE_RUN_PHASES = ("claimed", "gate", "failed")

# The command verbs (ADR-0015), and the subset whose pending presence
# closes the dispatch gate for a node (queue-behind, ADR-0018).
COMMAND_VERBS = ("drain", "recycle", "update", "rebuild")
LIFECYCLE_VERBS = ("drain", "recycle", "update")

# Consecutive failed Runs on one node before the dispatch gate closes.
QUARANTINE_AFTER_FAILURES = 2


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
            self._migrate()

    def _migrate(self) -> None:
        """Bring an M3-era database up to this schema. The store is a cache
        (ADR-0016), so dropped tables lose nothing durable."""
        for table in _DROPPED_TABLES:
            self._db.execute(f"DROP TABLE IF EXISTS {table}")
        present = {r["name"] for r in self._db.execute("PRAGMA table_info(commands)")}
        for column, decl in (
            ("force", "INTEGER NOT NULL DEFAULT 0"),
            ("deferred_reason", "TEXT"),
        ):
            if column not in present:
                self._db.execute(f"ALTER TABLE commands ADD COLUMN {column} {decl}")

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
        """Store any namespaced event; known types get extracted columns.

        Run events also drive the coordination caches that hang off them:
        a claimed event activates (and retires) its dispatch grant, and
        failed/pr-open events move the node's consecutive-failure counter
        for the quarantine gate (ADR-0016/0017).
        """

        def _int(key: str) -> int | None:
            value = event.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        def _str(key: str) -> str | None:
            value = event.get(key)
            return str(value) if isinstance(value, str) and value else None

        event_type = str(event.get("type", ""))
        issue, phase, node = _int("issue"), _str("phase"), _str("node")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO events (type, received_at, payload, node, worker, issue,"
                " run_id, attempt, phase, pr, round, verdict)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_type,
                    self._clock(),
                    json.dumps(event, sort_keys=True),
                    node,
                    _str("worker") or _str("reviewer"),
                    issue,
                    _str("run_id"),
                    _int("attempt"),
                    phase,
                    _int("pr"),
                    _int("round"),
                    _str("verdict"),
                ),
            )
            if event_type != EVENT_RUN:
                return
            if phase == "claimed" and issue is not None:
                # Activation: the grant did its job; the events record owns
                # claim tracking from here (ADR-0017).
                self._db.execute("DELETE FROM grants WHERE issue = ?", (issue,))
            if node and phase == "failed":
                self._bump_failures(node, issue, _str("run_id"))
            if node and phase == "pr-open":
                # A consecutive completed Run resets the counter — but never
                # releases a quarantine (human-only, ADR-0016).
                self._db.execute(
                    "UPDATE node_health SET consecutive_failures = 0 WHERE node = ?", (node,)
                )

    def _bump_failures(self, node: str, issue: int | None, run_id: str | None) -> None:
        self._db.execute(
            "INSERT INTO node_health (node, consecutive_failures) VALUES (?, 1)"
            " ON CONFLICT (node) DO UPDATE SET consecutive_failures = consecutive_failures + 1",
            (node,),
        )
        row = self._db.execute(
            "SELECT consecutive_failures, quarantined FROM node_health WHERE node = ?", (node,)
        ).fetchone()
        if row["consecutive_failures"] >= QUARANTINE_AFTER_FAILURES and not row["quarantined"]:
            reason = (
                f"{row['consecutive_failures']} consecutive failed Runs"
                f" (latest run {run_id or 'unknown'}"
                + (f", issue #{issue}" if issue is not None else "")
                + ")"
            )
            self._db.execute(
                "UPDATE node_health SET quarantined = 1, reason = ?, since = ? WHERE node = ?",
                (reason, self._clock(), node),
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

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first feed for the dashboard."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, type, received_at, payload FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "type": r["type"],
                "received_at": r["received_at"],
                "payload": json.loads(r["payload"]),
            }
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

    def run_states(self) -> list[dict[str, Any]]:
        """Per issue: the latest Run event, joined with the latest progress
        telemetry for that run_id (the dashboard's Runs view)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT e.issue, e.worker, e.node, e.run_id, e.attempt, e.phase, e.pr,"
                " e.received_at FROM events e JOIN ("
                "   SELECT issue, MAX(id) AS latest FROM events"
                "   WHERE type = ? AND issue IS NOT NULL GROUP BY issue"
                " ) last ON e.id = last.latest ORDER BY e.issue",
                (EVENT_RUN,),
            ).fetchall()
            progress: dict[str, dict[str, Any]] = {}
            for row in self._db.execute(
                "SELECT p.run_id, p.payload FROM events p JOIN ("
                "   SELECT run_id, MAX(id) AS latest FROM events"
                "   WHERE type = ? AND run_id IS NOT NULL GROUP BY run_id"
                " ) last ON p.id = last.latest",
                (EVENT_PROGRESS,),
            ).fetchall():
                progress[row["run_id"]] = json.loads(row["payload"])
        return [
            {
                "issue": r["issue"],
                "worker": r["worker"] or "",
                "node": r["node"] or "",
                "run_id": r["run_id"] or "",
                "attempt": r["attempt"],
                "phase": r["phase"] or "",
                "pr": r["pr"],
                "last_event_at": r["received_at"],
                "progress": progress.get(r["run_id"] or ""),
            }
            for r in rows
        ]

    def worker_last_seen(self, worker: str) -> float | None:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(received_at) AS seen FROM events WHERE worker = ?", (worker,)
            ).fetchone()
        return row["seen"] if row and row["seen"] is not None else None

    def evict_progress(self, budget_bytes: int) -> int:
        """Cache, never archive (ADR-0016): drop the oldest progress events
        until their payloads fit the byte budget. Terminal Run events are
        never touched. Returns the number of rows evicted."""
        with self._lock, self._db:
            total = self._db.execute(
                "SELECT COALESCE(SUM(LENGTH(payload)), 0) AS total FROM events WHERE type = ?",
                (EVENT_PROGRESS,),
            ).fetchone()["total"]
            excess = total - budget_bytes
            if excess <= 0:
                return 0
            doomed: list[int] = []
            for row in self._db.execute(
                "SELECT id, LENGTH(payload) AS size FROM events WHERE type = ? ORDER BY id",
                (EVENT_PROGRESS,),
            ):
                doomed.append(row["id"])
                excess -= row["size"]
                if excess <= 0:
                    break
            self._db.executemany("DELETE FROM events WHERE id = ?", [(id_,) for id_ in doomed])
            return len(doomed)

    # -- dispatch grants and driver registry (ADR-0017) ------------------------

    def upsert_driver(self, worker: str, node: str, login: str, role: str) -> None:
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO drivers (worker, node, login, role, registered_at, last_dispatch_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (worker) DO UPDATE SET node = ?, login = ?, role = ?,"
                " last_dispatch_at = ?",
                (worker, node, login, role, now, now, node, login, role, now),
            )

    def drivers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM drivers ORDER BY worker").fetchall()
        return [dict(r) for r in rows]

    def record_grant(self, issue: int, worker: str, node: str, login: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO grants (issue, worker, node, login, granted_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (issue) DO UPDATE SET worker = ?, node = ?, login = ?,"
                " granted_at = ?",
                (issue, worker, node, login, self._clock(), worker, node, login, self._clock()),
            )

    def granted_issues(self) -> set[int]:
        with self._lock:
            rows = self._db.execute("SELECT issue FROM grants").fetchall()
        return {r["issue"] for r in rows}

    def expired_grants(self, window_seconds: float) -> list[dict[str, Any]]:
        """Grants past the activation window with no claimed event seen."""
        cutoff = self._clock() - window_seconds
        with self._lock:
            rows = self._db.execute(
                "SELECT issue, worker, node, login, granted_at FROM grants"
                " WHERE granted_at <= ? ORDER BY issue",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def release_grant(self, issue: int) -> bool:
        """True when the grant row was still there — i.e. the caller won
        the race against a late activation and owns the GitHub unwind."""
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM grants WHERE issue = ?", (issue,))
        return cursor.rowcount > 0

    # -- node health: the quarantine gate (ADR-0016) ---------------------------

    def node_quarantine(self, node: str) -> str | None:
        """The quarantine reason, or None when the node may receive work."""
        with self._lock:
            row = self._db.execute(
                "SELECT quarantined, reason FROM node_health WHERE node = ?", (node,)
            ).fetchone()
        if row and row["quarantined"]:
            return row["reason"] or "quarantined"
        return None

    def quarantines(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT node, consecutive_failures, reason, since FROM node_health"
                " WHERE quarantined = 1 ORDER BY node"
            ).fetchall()
        return [dict(r) for r in rows]

    def release_quarantine(self, node: str) -> bool:
        """Human-only release (ADR-0016); True when a quarantine was lifted."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE node_health SET quarantined = 0, reason = '', since = NULL,"
                " consecutive_failures = 0 WHERE node = ? AND quarantined = 1",
                (node,),
            )
        return cursor.rowcount > 0

    # -- dashboard flags -------------------------------------------------------

    def flag_zombie(self, issue: int, run_id: str, worker: str, node: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO zombie_flags (issue, run_id, worker, node, flagged_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (issue, run_id, worker, node, self._clock()),
            )

    def clear_zombie_flag(self, issue: int, run_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM zombie_flags WHERE issue = ? AND run_id = ?", (issue, run_id)
            )

    def zombie_flags(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT issue, run_id, worker, node, flagged_at FROM zombie_flags ORDER BY issue"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_malformed(self, issue: int, detail: str) -> None:
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO malformed_states (issue, detail, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (issue) DO UPDATE SET detail = ?, last_seen = ?",
                (issue, detail, now, now, detail, now),
            )

    def clear_malformed(self, issue: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM malformed_states WHERE issue = ?", (issue,))

    def malformed_states(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT issue, detail, first_seen, last_seen FROM malformed_states ORDER BY issue"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- commands ------------------------------------------------------------

    def queue_command(self, node: str, verb: str, target: str | None, force: bool = False) -> int:
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO commands (node, verb, target, force, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (node, verb, target, 1 if force else 0, self._clock()),
            )
        return int(cursor.lastrowid or 0)

    def pending_commands(self, node: str) -> list[dict[str, Any]]:
        """Undelivered AND unacknowledged commands: re-delivered every
        heartbeat until the node reports them completed (a deferred command
        — queue-behind — simply stays here and is re-delivered)."""
        now = self._clock()
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT id, verb, target, force FROM commands"
                " WHERE node = ? AND completed_at IS NULL ORDER BY id",
                (node,),
            ).fetchall()
            self._db.execute(
                "UPDATE commands SET delivered_at = ?"
                " WHERE node = ? AND completed_at IS NULL AND delivered_at IS NULL",
                (now, node),
            )
        return [
            {"id": r["id"], "verb": r["verb"], "target": r["target"], "force": bool(r["force"])}
            for r in rows
        ]

    def complete_commands(self, node: str, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE commands SET completed_at = ?, deferred_reason = NULL"
                " WHERE id = ? AND node = ?",
                [(self._clock(), int(i), node) for i in ids],
            )

    def record_deferrals(self, node: str, deferrals: list[dict[str, Any]]) -> None:
        """Heartbeat-reported queue-behind state: mark the named pending
        commands deferred and clear the mark on every other pending one."""
        reasons = {
            int(d["id"]): str(d.get("reason", ""))
            for d in deferrals
            if isinstance(d, dict) and isinstance(d.get("id"), int)
        }
        with self._lock, self._db:
            self._db.execute(
                "UPDATE commands SET deferred_reason = NULL"
                " WHERE node = ? AND completed_at IS NULL",
                (node,),
            )
            self._db.executemany(
                "UPDATE commands SET deferred_reason = ?"
                " WHERE id = ? AND node = ? AND completed_at IS NULL",
                [(reason, id_, node) for id_, reason in reasons.items()],
            )

    def pending_lifecycle_commands(self, node: str) -> list[str]:
        """Pending drain/recycle/update verbs for the dispatch gate: a node
        about to be drained, recycled, or updated gets no new work, which
        bounds a queued-behind command by the current Run (NODE-SUBSTRATE)."""
        placeholders = ", ".join("?" for _ in LIFECYCLE_VERBS)
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT verb FROM commands WHERE node = ? AND completed_at IS NULL"
                f" AND verb IN ({placeholders}) ORDER BY verb",
                (node, *LIFECYCLE_VERBS),
            ).fetchall()
        return [r["verb"] for r in rows]

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

    # -- janitor records --------------------------------------------------------

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

    # -- the fleet read model (CLI + dashboard) --------------------------------

    def fleet_state(self) -> dict[str, Any]:
        with self._lock:
            nodes = self._db.execute("SELECT * FROM nodes ORDER BY name").fetchall()
            stacks = self._db.execute("SELECT * FROM stacks ORDER BY node, name").fetchall()
            containers = self._db.execute("SELECT * FROM containers ORDER BY node, name").fetchall()
            images = self._db.execute("SELECT * FROM images ORDER BY node, name").fetchall()
            commands = self._db.execute(
                "SELECT id, node, verb, target, force, created_at, delivered_at, completed_at,"
                " deferred_reason FROM commands ORDER BY id"
            ).fetchall()
            health = self._db.execute("SELECT * FROM node_health ORDER BY node").fetchall()
        return {
            "nodes": [dict(r) for r in nodes],
            "stacks": [dict(r) for r in stacks],
            "run_containers": [dict(r) for r in containers],
            "images": [dict(r) for r in images],
            "commands": [dict(r) for r in commands],
            "node_health": [dict(r) for r in health],
        }
