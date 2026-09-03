Status: ACCEPTED

Date: 2026-07-28

# ADR-0028: Unregistered-view dedupe key, size cap, and eviction

## Context

Heartbeats bearing unknown or revoked tokens are rejected 401 and surfaced as an advisory "unregistered nodes" view — display built from unauthenticated input, so it must be deduplicated and size-capped by construction (ADR-0023). Key, cap, and eviction were delegated (M7 brief).

## Decision

- **Dedupe key: (self-declared name, source address)** — both truncated to 64 characters at ingestion. A repeat sighting updates `last_seen` and a `beats` counter instead of inserting. Name alone would let one forged name shadow a real node's sighting from a different box; source alone would merge distinctly named daemons behind one NAT address.
- **Cap: 64 rows** (`unregistered_nodes` in `cache.db`). Real fleets are single digits; 64 covers a whole re-provision worklist after a stale-backup recovery with an order of magnitude to spare, while bounding what a hostile LAN peer can grow to one SQLite page's worth of text.
- **Eviction: oldest `last_seen` first**, applied at insertion time past the cap. The view's value is "what is heartbeating at me *now*"; anything evicted while still live re-inserts itself within one heartbeat interval, so eviction can never lose a live signal for longer than that.
- A successful provisioning (join exchange) and every authorized heartbeat delete sightings under that node's name — the worklist shrinks by itself.

## Alternatives Considered

- **Unbounded table with a janitor sweep**: unauthenticated input must be bounded at the write, not cleaned up on a cadence.
- **LRU by first_seen**: evicts the longest-suffering node — exactly the one the operator most needs to see.
