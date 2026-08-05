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

### Eviction indicator: query-relative, with bounded scoped evidence

`evict_progress` records eviction evidence in the same transaction as its deletes — one bounded row per **(type, node, component)** combination seen among the evicted rows (the extracted columns the read filters match against), each carrying two tiers of evidence: **correlated `(id, received_at)` samples** of the evicted rows (bounded at 32 per scope; while `samples_complete` they *are* the scope's evicted set) and the **bounds fallback** (lowest/highest evicted id, newest `received_at`, count). The evidence is doubly capped — 64 scopes × 32 samples, far above the real cardinality of evictable types × fleet nodes × components — and degrades conservatively at every boundary: a scope past its sample cap drops to bounds-only, and past the scope cap everything collapses into a single bounds-only `'*'` sentinel row that matches every filter. Evidence about evictions is never allowed to grow into an archive inside the cache (ADR-0016).

The response contract is explicit about the two questions a client can ask:

- **`evicted`** — *query-relative*: true when an evicted row **may have matched this call's whole conjunction** — `type`/`node`/`component` filters AND `received_at >= since` AND `id < cursor` — evaluated over **single rows**, never assembled from one row's id minimum and another row's timestamp maximum. While a scope's correlated samples are complete the answer is **exact**: crossed constraints that no single evicted row satisfies read complete, evicting the cursor-boundary event never marks the *next* page incomplete, and an eviction matching the conjunction always reads incomplete. Bounds-only evidence (an overflowed scope, the `'*'` sentinel, a migrated row, the legacy watermark) answers each constraint against the scope's extremes — conservative, over-reporting incompleteness only. This is the field consumers act on — `theozolith status` degrades exactly when **its own** `theozolith.error` query (which carries no cursor) reads incomplete (ADR-0039), and an unrelated progress eviction never produces a false verdict.
- **`any_evicted`** — *global*: true once anything was ever evicted from this cache, whatever the filters.

A **legacy** pre-amendment watermark row (the earlier single-row shape) has unknowable scope and reads conservatively: any filtered query is incomplete against it, and its 0 timestamp (unknown reach) is incomplete for any window. All evidence lives in `cache.db` and dies with it: deleting the cache is the documented recovery move (ADR-0024) and yields an honestly empty store, not a false incompleteness claim about rows that no longer exist to be missing.

## Alternatives rejected

- **A page-relative eviction flag** (recomputed per returned page against the evicted id range): eviction interleaves with kept terminal events, so the id range has holes and the flag becomes a per-page computation clients would have to trust blindly; the per-scope timestamp comparison is O(scopes), exact for the filter + `since` shape status depends on, and conservative everywhere else.
- **A purely global flag** (true forever after any eviction, for any query): overstates incompleteness — a progress eviction would degrade an error-only `status` verdict for the rest of the cache's life over one long-past budget sweep. Kept only as the separate, honestly-named `any_evicted` field.
- **Row-level eviction tombstones** (record each evicted row's key columns, unbounded): an archive of the evicted growing inside the cache — exactly what cache-not-archive forbids; the doubly-capped scope rows answer the only question clients ask ("could my query have lost rows?") at fixed size.
- **Extremes-only conjunction** (answer `since` from the scope's newest timestamp and the cursor from its lowest id): the two extremes can belong to *different* rows, claiming incompleteness for crossed constraints no single evicted row satisfies — the round-5 defect. A conjunction is a property of one row; only correlated evidence (or an honest "may have matched") can state it.
- **Inferring eviction from `sqlite_sequence` vs. `MIN(id)`**: indistinguishable from other deletes and unqueryable per-filter; an explicit watermark written by the one deleter is boring and exact.
- **Timestamp cursors**: `received_at` collides (same-second bursts) and would skip or repeat rows at page seams; the id is the order.
- **Offset pagination**: pages shift as new events land; cursors are stable under concurrent ingest.
- **base64-wrapping the cursor**: obscurity without opacity — the contract ("pass it back untouched") is what makes it opaque, not the alphabet.
