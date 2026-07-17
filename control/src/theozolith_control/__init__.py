"""Control Node: the product's central service (NODE-SUBSTRATE.md).

Heartbeat/command channel, typed event ingestion, advisory claim pre-filter,
zombie-claim janitor, retry auditor, and the encrypted node-scoped secret
store. Advisory in coordination (ADR-0002) — the pipeline ships PRs with
this service down; authoritative for node and docker lifecycle.
"""

__version__ = "0.3.0"
