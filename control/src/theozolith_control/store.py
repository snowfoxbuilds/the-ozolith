"""cache.db: Control Node cache persistence — deletable by construction.

The database is a cache, never an archive, with no exceptions left
(ADR-0016 as made true by ADR-0024's split: secrets and per-node tokens
live in store.db, see secretstore.py). Deleting this file mid-operation
costs the operator a re-login and one heartbeat round of state re-warm,
nothing else. What it caches: node/stack/container/image status from
heartbeats, namespaced events from drivers (terminal Run events kept
indefinitely — they are tiny and are the metrics substrate; progress
telemetry evicted oldest-first under a byte budget), queued infrastructure
commands, dispatch grants awaiting activation (ADR-0017), node health for
the quarantine gate, dashboard flags, browser sessions (ADR-0023),
outstanding join tokens, and unregistered-node sightings.

The connection is shared across FastAPI handlers and the sweep threads, so
every access serializes on one lock; at control-plane cadence (a heartbeat
per node per minute) contention is irrelevant.
"""

from __future__ import annotations

import hashlib
import json
import secrets as _secrets
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
    -- Reported config-distribution convergence (ADR-0042): the drivers-hash the
    -- node last applied ('' = none), and the product version that artifact was
    -- built against (advisory stamp skew, never a health downgrade).
    drivers_hash TEXT NOT NULL DEFAULT '',
    drivers_built_against TEXT NOT NULL DEFAULT '',
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
-- Container-kind Stack containers reported by heartbeat (ADR-0019): the
-- web terminal's attach targets (the Flight Deck lives here); run
-- containers above are never attach targets.
CREATE TABLE IF NOT EXISTS stack_containers (
    node TEXT NOT NULL,
    name TEXT NOT NULL,
    stack TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
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
    driver TEXT,
    issue INTEGER,
    run_id TEXT,
    attempt INTEGER,
    phase TEXT,
    pr INTEGER,
    round INTEGER,
    verdict TEXT,
    component TEXT
);
CREATE INDEX IF NOT EXISTS events_issue ON events (issue, id);
CREATE INDEX IF NOT EXISTS events_type ON events (type, id);
CREATE INDEX IF NOT EXISTS events_received ON events (received_at, id);
-- Legacy eviction watermark (ADR-0038, first shape): a single row whose
-- scope is unknowable. Kept read-only as conservative evidence from
-- pre-amendment caches — any filtered query reads as incomplete against
-- it. New evictions write event_eviction_scopes below instead.
CREATE TABLE IF NOT EXISTS event_evictions (
    scope TEXT PRIMARY KEY,
    last_evicted_id INTEGER NOT NULL,
    evicted_count INTEGER NOT NULL,
    evicted_at REAL NOT NULL,
    last_evicted_received_at REAL NOT NULL DEFAULT 0
);
-- Scoped eviction evidence (ADR-0038, filter-relative): one bounded row
-- per (type, node, component) combination seen among evicted rows —
-- enough to decide whether an eviction could have matched a filtered
-- query, never a row-level archive (cache-not-archive, ADR-0016). node
-- and component are '' when the evicted row had none; the '*' row is the
-- overflow sentinel: past the scope cap, evidence collapses to
-- match-everything (the conservative direction). Everything here dies
-- with cache.db by design.
CREATE TABLE IF NOT EXISTS event_eviction_scopes (
    type TEXT NOT NULL,
    node TEXT NOT NULL,
    component TEXT NOT NULL,
    -- first/last evicted id and newest timestamp bound the scope's evicted
    -- range: the CONSERVATIVE fallback evidence (each constraint checked
    -- against an extreme, which can only over-report incompleteness).
    -- 0 means unknown (a pre-amendment row) and reads conservatively.
    first_evicted_id INTEGER NOT NULL DEFAULT 0,
    last_evicted_id INTEGER NOT NULL,
    evicted_count INTEGER NOT NULL,
    evicted_at REAL NOT NULL,
    last_evicted_received_at REAL NOT NULL,
    -- Correlated (id, received_at) pairs of the evicted rows, JSON, bounded
    -- per scope: while samples_complete=1 they ARE the scope's evicted set,
    -- so a combined since+cursor query is answered as a true conjunction
    -- over single rows. Past the per-scope sample cap (or for the wildcard
    -- and migrated rows) samples are dropped and samples_complete=0 — the
    -- bounds above take over, conservative by construction. Evidence stays
    -- bounded: scope cap x sample cap, never an archive.
    samples TEXT NOT NULL DEFAULT '[]',
    samples_complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (type, node, component)
);
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
-- Browser sessions (ADR-0023: stateful, revocable). The cookie carries a
-- random 128-bit id; only its SHA-256 lands here — reading the cache never
-- yields a usable cookie. Absolute expiry; logout deletes the row; a
-- password change truncates the table.
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
-- Outstanding join tokens (ADR-0023): short-lived, use-counted, consumed on
-- successful exchange. Hash-stored like sessions. Losing this table only
-- invalidates unredeemed join strings — re-mint and re-paste.
CREATE TABLE IF NOT EXISTS join_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    uses_left INTEGER NOT NULL
);
-- Unregistered-node sightings (ADR-0023): heartbeats bearing unknown or
-- revoked tokens are rejected 401 and recorded here — advisory display
-- built from unauthenticated input, deduplicated and size-capped, never a
-- node record, never dispatch-eligible. After a stale-backup recovery this
-- is exactly the re-provision worklist.
CREATE TABLE IF NOT EXISTS unregistered_nodes (
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    beats INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (name, source)
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
-- Pin-convergence tracking (revision ruling amending ADR-0015): consecutive
-- off-pin heartbeats per node; a row exists only while the node is off-pin.
CREATE TABLE IF NOT EXISTS version_health (
    node TEXT PRIMARY KEY,
    offpin_beats INTEGER NOT NULL DEFAULT 0,
    restart_queued INTEGER NOT NULL DEFAULT 0,
    escalated INTEGER NOT NULL DEFAULT 0
);
-- Config-distribution convergence tracking (ADR-0042): the exact analog of
-- version_health for the drivers-hash — consecutive off-hash heartbeats per
-- node, a row only while off-hash. Same escalation ladder, same knob.
CREATE TABLE IF NOT EXISTS drivers_health (
    node TEXT PRIMARY KEY,
    offpin_beats INTEGER NOT NULL DEFAULT 0,
    restart_queued INTEGER NOT NULL DEFAULT 0,
    escalated INTEGER NOT NULL DEFAULT 0
);
"""

# Tables of earlier schema generations, dropped on open: the advisory
# claim-intent pre-filter (replaced by dispatch grants, ADR-0017) and the
# retry auditor's findings (died with the marker machinery, ADR-0016).
# The pre-split secrets table (moved to store.db, ADR-0024) is deliberately
# NOT dropped: pointing this class at a pre-split control.db must never
# destroy the one copy of the ciphertext — migration is manual.
_DROPPED_TABLES = ("claim_intents", "audit_findings")

# Unregistered-node sightings kept at most (dedup key: name + source).
UNREGISTERED_CAP = 64


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


EVENT_RUN = "theozolith.run"
EVENT_REVIEW = "theozolith.review"
EVENT_PROGRESS = "theozolith.run.progress"
EVENT_ERROR = "theozolith.error"

RUN_PHASES = ("claimed", "gate", "pr-open", "failed", "escalated")
# Phases meaning "the driver still holds the claim" — what the zombie
# janitor watches. failed is live (ADR-0016): the driver keeps the claim
# through the local retry, so only pr-open and escalated end its watch —
# a driver that dies between the failed Run and its retry stays visible.
LIVE_RUN_PHASES = ("claimed", "gate", "failed")

# The command verbs (ADR-0015; restart is the off-pin escalation step), and
# the subset whose pending presence closes the dispatch gate for a node
# (queue-behind, ADR-0018).
COMMAND_VERBS = ("drain", "recycle", "update", "rebuild", "restart")
LIFECYCLE_VERBS = ("drain", "recycle", "update", "restart")

# Consecutive failed Runs on one node before the dispatch gate closes.
QUARANTINE_AFTER_FAILURES = 2

# Distinct (type, node, component) eviction-evidence rows kept before the
# evidence collapses into the '*' match-everything sentinel (ADR-0038):
# bounded by decision — evidence about evictions must never itself grow
# into an archive inside the cache.
EVICTION_SCOPE_CAP = 64
# Correlated (id, received_at) samples kept per scope: while under the cap
# they are the scope's complete evicted set and combined since+cursor
# queries are answered exactly; past it, evidence falls back to the
# conservative bounds. Bounded by decision (ADR-0038): at most
# EVICTION_SCOPE_CAP x EVICTION_SAMPLE_CAP pairs ever exist.
EVICTION_SAMPLE_CAP = 32
EVICTION_WILDCARD = "*"


@dataclass(frozen=True)
class LiveClaim:
    """The latest non-terminal Run event for one issue."""

    issue: int
    driver: str  # the worker_id that emitted the Run event (ADR-0020 field)
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
        node_columns = {r["name"] for r in self._db.execute("PRAGMA table_info(nodes)")}
        for column in ("drivers_hash", "drivers_built_against"):  # ADR-0042
            if column not in node_columns:
                self._db.execute(f"ALTER TABLE nodes ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        present = {r["name"] for r in self._db.execute("PRAGMA table_info(commands)")}
        for column, decl in (
            ("force", "INTEGER NOT NULL DEFAULT 0"),
            ("deferred_reason", "TEXT"),
        ):
            if column not in present:
                self._db.execute(f"ALTER TABLE commands ADD COLUMN {column} {decl}")
        event_columns = {r["name"] for r in self._db.execute("PRAGMA table_info(events)")}
        if "component" not in event_columns:  # theozolith.error filter column
            self._db.execute("ALTER TABLE events ADD COLUMN component TEXT")
        if "driver" not in event_columns:
            # ADR-0020 sweep: the run/progress-event actor field became `driver`
            # and the column follows. RENAME carries the old `worker` values
            # over as the backfill, so an old-schema store opens with a
            # populated `driver` column (review events already coalesced their
            # reviewer into this column, so they need no separate backfill).
            if "worker" in event_columns:
                self._db.execute("ALTER TABLE events RENAME COLUMN worker TO driver")
            else:
                self._db.execute("ALTER TABLE events ADD COLUMN driver TEXT")
        eviction_columns = {
            r["name"] for r in self._db.execute("PRAGMA table_info(event_evictions)")
        }
        if "last_evicted_received_at" not in eviction_columns:
            # 0 = recorded before the column existed: the timestamp is
            # unknown, so window reads treat it as incomplete (conservative).
            self._db.execute(
                "ALTER TABLE event_evictions ADD COLUMN"
                " last_evicted_received_at REAL NOT NULL DEFAULT 0"
            )
        scope_columns = {
            r["name"] for r in self._db.execute("PRAGMA table_info(event_eviction_scopes)")
        }
        if "first_evicted_id" not in scope_columns:
            # 0 = unknown lower bound: cursor reads treat the scope as
            # possibly reaching any page (conservative).
            self._db.execute(
                "ALTER TABLE event_eviction_scopes ADD COLUMN"
                " first_evicted_id INTEGER NOT NULL DEFAULT 0"
            )
        if "samples" not in scope_columns:
            # Migrated rows carry no correlated samples: samples_complete
            # stays 0 and the bounds-only conservative path answers.
            self._db.execute(
                "ALTER TABLE event_eviction_scopes ADD COLUMN samples TEXT NOT NULL DEFAULT '[]'"
            )
            self._db.execute(
                "ALTER TABLE event_eviction_scopes ADD COLUMN"
                " samples_complete INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self._db.close()

    def now(self) -> float:
        """The store's clock — the same one every recorded timestamp used,
        exposed so read models can ship a server ``now`` (ADR-0039) and
        clients never mix their clock into staleness math."""
        return self._clock()

    # -- nodes and heartbeat status ----------------------------------------

    def touch_node(
        self,
        name: str,
        version: str = "",
        *,
        drivers_hash: str = "",
        drivers_built_against: str = "",
    ) -> None:
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO nodes (name, version, drivers_hash, drivers_built_against,"
                " registered_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET last_seen = ?, version = ?,"
                " drivers_hash = ?, drivers_built_against = ?",
                (
                    name,
                    version,
                    drivers_hash,
                    drivers_built_against,
                    now,
                    now,
                    now,
                    version,
                    drivers_hash,
                    drivers_built_against,
                ),
            )

    def node_last_seen(self, name: str) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT last_seen FROM nodes WHERE name = ?", (name,)).fetchone()
        return row["last_seen"] if row else None

    def node_version(self, name: str) -> str:
        """The last heartbeat-reported product version ('' when unknown)."""
        with self._lock:
            row = self._db.execute("SELECT version FROM nodes WHERE name = ?", (name,)).fetchone()
        return row["version"] if row else ""

    def node_drivers_hash(self, name: str) -> str:
        """The last heartbeat-reported config-distribution hash ('' when unknown
        or none applied) — the dispatch gate's off-hash input (ADR-0042)."""
        with self._lock:
            row = self._db.execute(
                "SELECT drivers_hash FROM nodes WHERE name = ?", (name,)
            ).fetchone()
        return row["drivers_hash"] if row else ""

    def drivers_skew(self) -> list[dict[str, Any]]:
        """Advisory stamp skew (ADR-0042): nodes whose applied distribution was
        built against a product version other than the one they now run. Both
        non-empty and different — never a health downgrade, surfaced only."""
        with self._lock:
            rows = self._db.execute(
                "SELECT name, version, drivers_built_against FROM nodes"
                " WHERE drivers_built_against != '' AND version != ''"
                " AND drivers_built_against != version ORDER BY name"
            ).fetchall()
        return [
            {
                "node": r["name"],
                "built_against": r["drivers_built_against"],
                "product_version": r["version"],
            }
            for r in rows
        ]

    def observe_version(self, node: str, reported: str, pin: str, threshold: int) -> str | None:
        """Track pin convergence per heartbeat (revision ruling amending
        ADR-0015). On-pin (or no pin / no report): reset. Off-pin: count;
        at ``threshold`` consecutive off-pin beats queue one restart
        command; still off-pin ``threshold`` beats after that, mark the
        node escalated (the caller emits the theozolith.error event).
        Returns the action taken: None | "restart-queued" | "escalated"."""
        return self._observe_convergence("version_health", node, reported, pin, threshold)

    def observe_drivers(self, node: str, reported: str, desired: str, threshold: int) -> str | None:
        """Track config-distribution convergence per heartbeat (ADR-0042): the
        exact analog of ``observe_version`` for the drivers-hash. Reuses the
        same restart ladder and the same ``offpin_beats`` knob — one concept,
        convergence patience. Returns None | "restart-queued" | "escalated"."""
        return self._observe_convergence("drivers_health", node, reported, desired, threshold)

    def _observe_convergence(
        self, table: str, node: str, reported: str, desired: str, threshold: int
    ) -> str | None:
        """The shared convergence ladder over a ``(node, offpin_beats,
        restart_queued, escalated)`` health table (version_health /
        drivers_health): reset on match / no desired / no report; count
        otherwise; queue one restart at ``threshold``; mark escalated at
        ``2 * threshold``. ``table`` is a fixed module constant, never input."""
        with self._lock, self._db:
            if not desired or not reported or reported == desired:
                self._db.execute(f"DELETE FROM {table} WHERE node = ?", (node,))
                return None
            self._db.execute(
                f"INSERT INTO {table} (node, offpin_beats) VALUES (?, 0)"
                " ON CONFLICT (node) DO NOTHING",
                (node,),
            )
            self._db.execute(
                f"UPDATE {table} SET offpin_beats = offpin_beats + 1 WHERE node = ?",
                (node,),
            )
            row = self._db.execute(f"SELECT * FROM {table} WHERE node = ?", (node,)).fetchone()
            if not row["restart_queued"] and row["offpin_beats"] >= threshold:
                self._db.execute(
                    "INSERT INTO commands (node, verb, target, force, created_at)"
                    " VALUES (?, 'restart', NULL, 0, ?)",
                    (node, self._clock()),
                )
                self._db.execute(f"UPDATE {table} SET restart_queued = 1 WHERE node = ?", (node,))
                return "restart-queued"
            if (
                row["restart_queued"]
                and not row["escalated"]
                and row["offpin_beats"] >= 2 * threshold
            ):
                self._db.execute(f"UPDATE {table} SET escalated = 1 WHERE node = ?", (node,))
                return "escalated"
            return None

    def container_record(self, node: str, name: str) -> dict[str, Any] | None:
        """The live-container row a heartbeat last reported for (node, name):
        the terminal's authority for target resolution (ADR-0019 — the
        ``owner`` field is where the Stack is derived from, and
        ``age_seconds`` is the heartbeat-freshness evidence, computed here
        so callers share this store's clock)."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM containers WHERE node = ? AND name = ?", (node, name)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["age_seconds"] = self._clock() - record["updated_at"]
        return record

    def stack_container_record(self, node: str, name: str) -> dict[str, Any] | None:
        """The live stack-container row a heartbeat last reported for
        (node, name): the terminal's target authority for container-kind
        Stacks (ADR-0019 — the Flight Deck attaches through this record;
        ``stack`` is where the owning Stack is derived from)."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM stack_containers WHERE node = ? AND name = ?", (node, name)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["age_seconds"] = self._clock() - record["updated_at"]
        return record

    def record_status(
        self,
        node: str,
        stacks: list[dict[str, Any]],
        containers: list[dict[str, Any]],
        images: list[dict[str, Any]],
        stack_containers: list[dict[str, Any]] | None = None,
    ) -> None:
        """Replace the node's reported status with this heartbeat's."""
        now = self._clock()
        with self._lock, self._db:
            for table in ("stacks", "containers", "images", "stack_containers"):
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
            self._db.executemany(
                "INSERT INTO stack_containers (node, name, stack, state, status, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        node,
                        str(c.get("name", "")),
                        str(c.get("stack", "")),
                        str(c.get("state", "")),
                        str(c.get("status", "")),
                        now,
                    )
                    for c in (stack_containers or [])
                    if c.get("name")
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
                "INSERT INTO events (type, received_at, payload, node, driver, issue,"
                " run_id, attempt, phase, pr, round, verdict, component)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_type,
                    self._clock(),
                    json.dumps(event, sort_keys=True),
                    node,
                    # run/progress events carry `driver`; review events carry
                    # `reviewer` (its own accurate vocabulary, ADR-0020) — both
                    # land in the driver column.
                    _str("driver") or _str("reviewer"),
                    issue,
                    _str("run_id"),
                    _int("attempt"),
                    phase,
                    _int("pr"),
                    _int("round"),
                    _str("verdict"),
                    _str("component"),
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

    def error_events(
        self, *, node: str | None = None, component: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Newest-first theozolith.error feed, filterable by node/component."""
        query = "SELECT id, received_at, node, component, payload FROM events WHERE type = ?"
        params: list[Any] = [EVENT_ERROR]
        if node:
            query += " AND node = ?"
            params.append(node)
        if component:
            query += " AND component = ?"
            params.append(component)
        with self._lock:
            rows = self._db.execute(
                query + " ORDER BY id DESC LIMIT ?", [*params, limit]
            ).fetchall()
        return [
            {
                "id": r["id"],
                "received_at": r["received_at"],
                "node": r["node"] or "",
                "component": r["component"] or "",
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    def error_filters(self) -> tuple[list[str], list[str]]:
        """Distinct (nodes, components) seen on error events, for the panel."""
        with self._lock:
            nodes = [
                r["node"]
                for r in self._db.execute(
                    "SELECT DISTINCT node FROM events WHERE type = ? AND node IS NOT NULL"
                    " ORDER BY node",
                    (EVENT_ERROR,),
                )
            ]
            components = [
                r["component"]
                for r in self._db.execute(
                    "SELECT DISTINCT component FROM events"
                    " WHERE type = ? AND component IS NOT NULL ORDER BY component",
                    (EVENT_ERROR,),
                )
            ]
        return nodes, components

    def live_claims(self) -> list[LiveClaim]:
        """Issues whose LATEST Run event is non-terminal (claimed/gate)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT e.issue, e.driver, e.node, e.run_id, e.received_at, e.phase"
                " FROM events e JOIN ("
                "   SELECT issue, MAX(id) AS latest FROM events"
                "   WHERE type = ? AND issue IS NOT NULL GROUP BY issue"
                " ) last ON e.id = last.latest",
                (EVENT_RUN,),
            ).fetchall()
        return [
            LiveClaim(
                issue=r["issue"],
                driver=r["driver"] or "",
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
                "SELECT e.issue, e.driver, e.node, e.run_id, e.attempt, e.phase, e.pr,"
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
                "driver": r["driver"] or "",
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

    def driver_last_seen(self, driver: str) -> float | None:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(received_at) AS seen FROM events WHERE driver = ?", (driver,)
            ).fetchone()
        return row["seen"] if row and row["seen"] is not None else None

    def evict_progress(self, budget_bytes: int) -> int:
        """Cache, never archive (ADR-0016): drop the oldest progress and
        error events until their payloads fit the byte budget. Terminal Run
        events are never touched. Returns the number of rows evicted."""
        evictable = (EVENT_PROGRESS, EVENT_ERROR)
        with self._lock, self._db:
            total = self._db.execute(
                "SELECT COALESCE(SUM(LENGTH(payload)), 0) AS total FROM events"
                " WHERE type IN (?, ?)",
                evictable,
            ).fetchone()["total"]
            excess = total - budget_bytes
            if excess <= 0:
                return 0
            doomed: list[int] = []
            scopes: dict[tuple[str, str, str], dict[str, Any]] = {}
            for row in self._db.execute(
                "SELECT id, type, node, component, received_at, LENGTH(payload) AS size"
                " FROM events WHERE type IN (?, ?) ORDER BY id",
                evictable,
            ):
                doomed.append(row["id"])
                key = (row["type"], row["node"] or "", row["component"] or "")
                scope = scopes.setdefault(
                    key,
                    {
                        "count": 0,
                        "min_id": row["id"],
                        "max_id": 0,
                        "max_received": 0.0,
                        "samples": [],
                    },
                )
                scope["count"] += 1
                scope["min_id"] = min(scope["min_id"], row["id"])
                scope["max_id"] = max(scope["max_id"], row["id"])
                scope["max_received"] = max(scope["max_received"], row["received_at"])
                scope["samples"].append((row["id"], row["received_at"]))
                excess -= row["size"]
                if excess <= 0:
                    break
            self._db.executemany("DELETE FROM events WHERE id = ?", [(id_,) for id_ in doomed])
            # Scoped evidence rides the same transaction (ADR-0038):
            # eviction and its evidence are one atomic fact, recorded per
            # (type, node, component) so a filtered read can tell whether
            # an eviction could have matched it. Past the cap — or once
            # collapsed — everything folds into the '*' sentinel.
            self._record_eviction_scopes(scopes)
            return len(doomed)

    def _record_eviction_scopes(self, scopes: dict[tuple[str, str, str], dict[str, Any]]) -> None:
        wildcard = (EVICTION_WILDCARD, EVICTION_WILDCARD, EVICTION_WILDCARD)
        prior_rows = {
            (r["type"], r["node"], r["component"]): dict(r)
            for r in self._db.execute("SELECT * FROM event_eviction_scopes")
        }
        collapse = wildcard in prior_rows or len(set(prior_rows) | set(scopes)) > EVICTION_SCOPE_CAP
        if collapse and set(prior_rows) != {wildcard}:
            # Absorb every existing scope's bounds into the sentinel, then
            # drop the per-scope rows — bounded evidence, still honest.
            # Correlated samples cannot survive a collapse: the sentinel is
            # bounds-only, conservative by construction.
            absorbed = self._db.execute(
                "SELECT MIN(first_evicted_id) AS min_id, MAX(last_evicted_id) AS max_id,"
                " SUM(evicted_count) AS count,"
                " MAX(last_evicted_received_at) AS max_received"
                " FROM event_eviction_scopes"
            ).fetchone()
            self._db.execute("DELETE FROM event_eviction_scopes")
            prior_rows = {}
            if absorbed["max_id"] is not None:
                scopes = dict(scopes)
                merged = scopes.setdefault(
                    wildcard,
                    {
                        "count": 0,
                        "min_id": absorbed["min_id"],
                        "max_id": 0,
                        "max_received": 0.0,
                        "samples": [],
                    },
                )
                merged["count"] += absorbed["count"]
                merged["min_id"] = min(merged["min_id"], absorbed["min_id"])
                merged["max_id"] = max(merged["max_id"], absorbed["max_id"])
                merged["max_received"] = max(merged["max_received"], absorbed["max_received"])
        if collapse:
            folded: dict[str, Any] = {"count": 0, "min_id": 0, "max_id": 0, "max_received": 0.0}
            for scope in scopes.values():
                folded["count"] += scope["count"]
                folded["min_id"] = (
                    scope["min_id"]
                    if folded["min_id"] == 0
                    else min(folded["min_id"], scope["min_id"])
                )
                folded["max_id"] = max(folded["max_id"], scope["max_id"])
                folded["max_received"] = max(folded["max_received"], scope["max_received"])
            folded["samples"] = []
            scopes = {wildcard: folded}
        now = self._clock()
        for (type_, node, component), scope in scopes.items():
            prior = prior_rows.get((type_, node, component))
            if prior is None:
                first = scope["min_id"]
                last = scope["max_id"]
                count = scope["count"]
                max_received = scope["max_received"]
                samples: list = list(scope.get("samples") or [])
                complete = True
            else:
                first = (
                    0
                    if prior["first_evicted_id"] == 0 or scope["min_id"] == 0
                    else min(prior["first_evicted_id"], scope["min_id"])
                )
                last = max(prior["last_evicted_id"], scope["max_id"])
                count = prior["evicted_count"] + scope["count"]
                max_received = max(prior["last_evicted_received_at"], scope["max_received"])
                complete = bool(prior["samples_complete"])
                samples = (
                    json.loads(prior["samples"]) + list(scope.get("samples") or [])
                    if complete
                    else []
                )
            # The wildcard is bounds-only; a scope past the sample cap drops
            # its samples and answers conservatively from bounds — the
            # evidence never grows into an archive (ADR-0038).
            if (type_, node, component) == wildcard or len(samples) > EVICTION_SAMPLE_CAP:
                samples, complete = [], False
            self._db.execute(
                "INSERT OR REPLACE INTO event_eviction_scopes"
                " (type, node, component, first_evicted_id, last_evicted_id, evicted_count,"
                "  evicted_at, last_evicted_received_at, samples, samples_complete)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    type_,
                    node,
                    component,
                    first,
                    last,
                    count,
                    now,
                    max_received,
                    json.dumps(samples),
                    1 if complete else 0,
                ),
            )

    def eviction_mark(self) -> dict[str, Any] | None:
        """The LEGACY single-row watermark (pre-amendment caches), or None.
        Its scope is unknowable, so any filtered query reads conservatively
        against it; new evictions record scoped evidence instead."""
        with self._lock:
            row = self._db.execute(
                "SELECT last_evicted_id, evicted_count, evicted_at, last_evicted_received_at"
                " FROM event_evictions WHERE scope = 'events'"
            ).fetchone()
        return dict(row) if row is not None else None

    def eviction_evidence(
        self,
        *,
        type: str | None = None,
        node: str | None = None,
        component: str | None = None,
        since: float | None = None,
        before_id: int | None = None,
    ) -> tuple[bool, bool]:
        """(any eviction ever, THIS query incomplete) — ADR-0038. The query
        is incomplete when an evicted row MAY have matched the whole
        conjunction: type/node/component filters AND ``received_at >=
        since`` AND ``id < before_id`` — evaluated over SINGLE rows, never
        assembled from different rows' extremes. While a scope's
        correlated samples are complete (bounded per scope) the answer is
        exact; bounds-only evidence — the '*' sentinel, overflowed scopes,
        migrated rows, and the legacy single-row watermark — answers
        conservatively, over-reporting incompleteness only."""
        any_evicted = False
        incomplete = False
        legacy = self.eviction_mark()
        if legacy is not None:
            any_evicted = True
            cutoff = legacy["last_evicted_received_at"]
            incomplete = since is None or cutoff == 0 or since <= cutoff
        with self._lock:
            rows = self._db.execute("SELECT * FROM event_eviction_scopes").fetchall()
        for row in rows:
            any_evicted = True
            if incomplete:
                break
            if row["type"] != EVICTION_WILDCARD:
                if type is not None and row["type"] != type:
                    continue
                if node is not None and row["node"] != node:
                    continue
                if component is not None and row["component"] != component:
                    continue
            if row["samples_complete"]:
                # Complete correlated samples: the conjunction must hold on
                # ONE recorded evicted row (id, received_at), never on the
                # id-minimum of one row and the timestamp-maximum of another.
                pairs = json.loads(row["samples"])
                if not any(
                    (since is None or received >= since)
                    and (before_id is None or evicted_id < before_id)
                    for evicted_id, received in pairs
                ):
                    continue
            else:
                # Bounds-only evidence: each constraint against the scope's
                # extremes — conservative, may over-report incompleteness.
                if since is not None and since > row["last_evicted_received_at"]:
                    continue
                first = row["first_evicted_id"]
                if before_id is not None and first > 0 and first >= before_id:
                    continue  # every evicted row in this scope sits at/after the bound
            incomplete = True
        return any_evicted, incomplete

    def events_page(
        self,
        *,
        node: str | None = None,
        component: str | None = None,
        type: str | None = None,
        since: float | None = None,
        before_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """One newest-first page for GET /api/v1/events (ADR-0038): stored
        payloads verbatim — known and unknown types alike, rendering is the
        client's job — plus the continuation cursor and the store-level
        eviction indicator. ``before_id`` is the decoded cursor: rows
        strictly older than it."""
        query = "SELECT id, type, received_at, node, component, payload FROM events"
        clauses: list[str] = []
        params: list[Any] = []
        for clause, value in (
            ("type = ?", type),
            ("node = ?", node),
            ("component = ?", component),
        ):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        if since is not None:
            clauses.append("received_at >= ?")
            params.append(since)
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._db.execute(
                query + " ORDER BY id DESC LIMIT ?", [*params, limit]
            ).fetchall()
        # The eviction indicator is QUERY-relative (ADR-0038): `evicted` is
        # true exactly when an eviction could have matched this call's
        # filters, window, AND cursor bound; `any_evicted` is the global
        # fact (any eviction ever in this cache). Legacy pre-amendment
        # watermarks have unknowable scope and read conservatively.
        any_evicted, query_incomplete = self.eviction_evidence(
            type=type, node=node, component=component, since=since, before_id=before_id
        )
        return {
            "events": [
                {
                    "id": r["id"],
                    "type": r["type"],
                    "received_at": r["received_at"],
                    "node": r["node"],
                    "component": r["component"],
                    "payload": json.loads(r["payload"]),
                }
                for r in rows
            ],
            # A full page may have more behind it; a short page is the end.
            # The cursor is the last id as a decimal string — contractually
            # opaque to clients (ADR-0038).
            "next_cursor": str(rows[-1]["id"]) if len(rows) == limit else None,
            "evicted": query_incomplete,
            "any_evicted": any_evicted,
        }

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

    # -- browser sessions (ADR-0023) -------------------------------------------

    def create_session(self, ttl_seconds: float) -> str:
        """A fresh session: returns the random 128-bit id the cookie will
        carry; only its digest is stored."""
        raw = _secrets.token_hex(16)
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions (id_hash, created_at, expires_at) VALUES (?, ?, ?)",
                (_digest(raw), now, now + ttl_seconds),
            )
        return raw

    def session_active(self, raw: str) -> bool:
        """True for a live, unexpired session id. Expired rows are reaped
        on sight (absolute expiry, ADR-0023 — no sliding renewal)."""
        if not raw:
            return False
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT expires_at FROM sessions WHERE id_hash = ?", (_digest(raw),)
            ).fetchone()
            if row is None:
                return False
            if self._clock() >= row["expires_at"]:
                self._db.execute("DELETE FROM sessions WHERE id_hash = ?", (_digest(raw),))
                return False
        return True

    def delete_session(self, raw: str) -> None:
        """Logout: immediate server-side invalidation."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE id_hash = ?", (_digest(raw),))

    def truncate_sessions(self) -> None:
        """A password change invalidates every session (ADR-0023)."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions")

    # -- join tokens (ADR-0023) -------------------------------------------------

    def create_join_token(self, *, ttl_seconds: float, uses: int) -> tuple[str, bytes, float]:
        """Mint one join token: (short id for revocation, 16 raw bytes for
        the join-string payload, expiry). Default policy — 1h TTL, single
        use — belongs to the callers; this stores what it is told."""
        raw = _secrets.token_bytes(16)
        token_id = _secrets.token_hex(4)
        now = self._clock()
        expires_at = now + ttl_seconds
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO join_tokens (id, token_hash, created_at, expires_at, uses_left)"
                " VALUES (?, ?, ?, ?, ?)",
                (token_id, hashlib.sha256(raw).hexdigest(), now, expires_at, max(1, uses)),
            )
        return token_id, raw, expires_at

    def redeem_join_token(self, raw: bytes) -> bool:
        """Consume one use; False for unknown, expired, revoked, or spent
        tokens (one answer — the node-side error text stays generic by
        design: expired/consumed/revoked are indistinguishable off-box)."""
        token_hash = hashlib.sha256(raw).hexdigest()
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id, expires_at, uses_left FROM join_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return False
            if self._clock() >= row["expires_at"] or row["uses_left"] <= 0:
                self._db.execute("DELETE FROM join_tokens WHERE id = ?", (row["id"],))
                return False
            if row["uses_left"] == 1:
                self._db.execute("DELETE FROM join_tokens WHERE id = ?", (row["id"],))
            else:
                self._db.execute(
                    "UPDATE join_tokens SET uses_left = uses_left - 1 WHERE id = ?", (row["id"],)
                )
        return True

    def revoke_join_token(self, token_id: str) -> bool:
        """The oops backstop; True when an outstanding token was revoked."""
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM join_tokens WHERE id = ?", (token_id,))
        return cursor.rowcount > 0

    def join_tokens(self) -> list[dict[str, Any]]:
        """Outstanding (unexpired) tokens for the dashboard/CLI — ids and
        windows only, never token material."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM join_tokens WHERE expires_at <= ?", (self._clock(),))
            rows = self._db.execute(
                "SELECT id, created_at, expires_at, uses_left FROM join_tokens ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- unregistered-node sightings (ADR-0023) ---------------------------------

    def record_unregistered(self, name: str, source: str) -> None:
        """One rejected-heartbeat sighting: deduplicated on (self-declared
        name, source address) and size-capped — unauthenticated input must
        never grow the cache unboundedly. Past the cap, the oldest sighting
        is evicted (advisory display; losing one row costs nothing)."""
        name = (name or "(unnamed)")[:64]
        source = (source or "?")[:64]
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO unregistered_nodes (name, source, first_seen, last_seen, beats)"
                " VALUES (?, ?, ?, ?, 1)"
                " ON CONFLICT (name, source) DO UPDATE SET last_seen = ?, beats = beats + 1",
                (name, source, now, now, now),
            )
            for row in self._db.execute(
                "SELECT name, source FROM unregistered_nodes ORDER BY last_seen DESC"
                " LIMIT -1 OFFSET ?",
                (UNREGISTERED_CAP,),
            ).fetchall():
                self._db.execute(
                    "DELETE FROM unregistered_nodes WHERE name = ? AND source = ?",
                    (row["name"], row["source"]),
                )

    def clear_unregistered(self, name: str) -> None:
        """A successful provisioning settles every sighting of that name —
        the re-provision worklist shrinks by itself."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM unregistered_nodes WHERE name = ?", (name,))

    def unregistered_nodes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT name, source, first_seen, last_seen, beats FROM unregistered_nodes"
                " ORDER BY last_seen DESC"
            ).fetchall()
        return [dict(r) for r in rows]

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
            stack_containers = self._db.execute(
                "SELECT * FROM stack_containers ORDER BY node, name"
            ).fetchall()
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
            "stack_containers": [dict(r) for r in stack_containers],
            "images": [dict(r) for r in images],
            "commands": [dict(r) for r in commands],
            "node_health": [dict(r) for r in health],
        }
