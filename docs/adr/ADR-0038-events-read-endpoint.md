Status: ACCEPTED

Date: 2026-08-04

Provenance: delegated decision from the M8 brief — the events read endpoint's shape, cursor encoding, default page size, and eviction indicator. Amends ADR-0015 (the control-plane API gains its first read view over stored events) under the "Grilling 2026-08-04" rulings in NODE-SUBSTRATE.md; consumes ADR-0016/0024 (cache-not-archive, cache.db).

# ADR-0038: `GET /api/v1/events` — read view, cursor, and the eviction indicator

## Context

ADR-0015 defined `POST /api/v1/events` (ingest) and no read surface; the dashboard reads events through server-rendered fragments. The Operator TUI ruling adds `GET /api/v1/events`: a bearer-auth JSON view over stored rows — known and unknown types alike, rendering is the client's job — with node/component/type filters and cursor + `since` pagination, honestly reporting when eviction has removed history. The events table's `AUTOINCREMENT` id is monotonic and never reused; eviction (`evict_progress`, ADR-0016's ~10 GB budget) deletes only `theozolith.run.progress` and `theozolith.error` rows, oldest-first, and today records no evidence it ever ran.

## Decision

### Endpoint

`GET /api/v1/events` — admin bearer token, JSON. Query parameters, all optional:

| Param | Meaning |
| --- | --- |
| `node` | exact match on the extracted node column |
| `component` | exact match on the extracted component column |
| `type` | exact match on the event type (`theozolith.error`, an unknown namespaced type, …) |
| `since` | unix seconds; only rows with `received_at >= since` |
| `cursor` | opaque continuation token from a prior response |
| `limit` | page size; **default 100**, clamped to 1–500 |

Rows return **newest-first** (the house `ORDER BY id DESC` convention — a fleet surface reads backward from now). Each element is `{id, type, received_at, node, component, payload}` where `payload` is the stored event JSON **verbatim** — unknown types round-trip unrendered; `node`/`component` are surfaced top-level (null when not extracted) so filter results are self-describing.

### Cursor encoding and page turn

The cursor is the decimal string of the last (lowest) event id in the returned page, but it is **contractually opaque**: clients pass it back untouched and never do arithmetic on it. A response carries `next_cursor` (string) when the page is full — meaning "there may be more"; a subsequent page answers `next_cursor: null` when short or empty. A malformed cursor is a 400. The id is already monotonic, never reused, and indexed with every filter the endpoint serves (`(type, id)` exists; the schema gains `(received_at, id)` for `since` scans); inventing a fancier encoding would add decode failure modes to a value SQLite already guarantees.

### Eviction indicator

`evict_progress` records a watermark in the same transaction as its deletes: a single-row `event_evictions` table carrying the highest evicted id, the running count, the last eviction time, and **the newest evicted row's `received_at`**. A response's `evicted` field is **window-relative**: with a `since` bound it is true exactly when the window reaches into the evicted range (`since <= last_evicted_received_at` — ids and timestamps advance together under the store's one clock, so everything evicted is at least as old as that mark); without `since` the whole history is the window, and any eviction ever makes it true. Clients that exhaust pagination with `evicted: true` report the history as incomplete rather than complete-looking, and `theozolith status` turns an incomplete recent-error window into an explicit degraded reason (ADR-0039) — never an unqualified healthy verdict on incomplete evidence. The watermark lives in `cache.db` and dies with it: deleting the cache is the documented recovery move (ADR-0024) and yields an honestly empty store, not a false incompleteness claim about rows that no longer exist to be missing. (A pre-amendment watermark row carries a 0 timestamp — unknown reach — and reads as incomplete for any window, the conservative direction.)

## Alternatives rejected

- **A page-relative eviction flag** (recomputed per returned page against the evicted id range): eviction interleaves with kept terminal events, so the id range has holes and the flag becomes a per-page computation clients would have to trust blindly; the window-relative timestamp comparison is O(1), exact for `since` bounds (the case status depends on), and conservative everywhere else.
- **A purely global flag** (true forever after any eviction): overstates incompleteness for windows entirely after the eviction — `status` would report degraded history for the rest of the cache's life over one long-past budget sweep.
- **Inferring eviction from `sqlite_sequence` vs. `MIN(id)`**: indistinguishable from other deletes and unqueryable per-filter; an explicit watermark written by the one deleter is boring and exact.
- **Timestamp cursors**: `received_at` collides (same-second bursts) and would skip or repeat rows at page seams; the id is the order.
- **Offset pagination**: pages shift as new events land; cursors are stable under concurrent ingest.
- **base64-wrapping the cursor**: obscurity without opacity — the contract ("pass it back untouched") is what makes it opaque, not the alphabet.
