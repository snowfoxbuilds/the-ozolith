Status: ACCEPTED

Date: 2026-07-28

Provenance: delegated decision from the M7 brief; implements ADR-0023's admin-password-and-sessions contract (superseding ADR-0018's session section).

# ADR-0027: Session-table schema and login rate-limit parameters

## Context

ADR-0023 fixed the policy: server-side revocable sessions in `cache.db`, a cookie carrying only a random 128-bit id, absolute 30-day default expiry, logout deletes the row, password change truncates the table, constant-time comparison plus rate limiting on the login form. Schema and parameters were delegated.

## Decision

### Session table (`cache.db`)

```sql
CREATE TABLE sessions (
    id_hash    TEXT PRIMARY KEY,   -- SHA-256 hex of the cookie's 128-bit id
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL       -- absolute; no sliding renewal
);
```

- The cookie carries the raw id (32 hex chars from `secrets.token_hex(16)`); only its digest is stored, so reading `cache.db` never yields a usable cookie. Lookup by digest is the comparison — no plaintext comparisons exist to get wrong.
- Expired rows are reaped on sight during validation; logout deletes by digest; `set-password` (and init) truncates.
- `expires_at = login time + session_days × 86400` (`session_days` is a tier-2 `control.toml` setting, default 30).

### Password hashing

`hashlib.scrypt`, n=2^14, r=8, p=1, 32-byte salt, 64-byte key — interactive-login cost (~tens of ms) with memory-hardness, stdlib-only per ADR-0023. Stored self-describing (`scrypt$n$r$p$salt$hash` at `secrets/admin-password`), so parameter upgrades are a rehash on next `set-password`, not a migration.

### Login rate limit

**At most 5 failed attempts per rolling 60-second window, globally**; while exceeded, the form answers 429 with `Retry-After`. The check runs before the scrypt work, so a throttled flood costs the server nothing. One global bucket, not per-IP: there is exactly one credential to defend and one legitimate operator (ADR-0022 trust model), and per-source buckets would only let an attacker spread the same guess budget across addresses. The lockout-as-DoS cost is bounded at 60 s on a trusted network and is accepted; the 128-bit origin slug remains defense in depth in front of the form.

The failure window is **per-process memory** (2026-07-28 note, PR #9 review): a `serve` restart clears it, and the design assumes the single-process `serve` the product ships — a multi-worker deployment would multiply the budget by the worker count. Accepted: restarting the Control Node to reset the limiter is operator action, sessions themselves are durable in `cache.db`, and the scrypt cost plus the 60-second window still bound online guessing to a rate that never threatens the password space.

## Alternatives rejected

- **Storing raw session ids**: a cache.db copy (backups explicitly exclude it, but still) would be a cookie jar.
- **Sliding expiry**: converts "absolute expiry, default 30 days" into "forever for a daily user" — revocation semantics stay crisp with one absolute stamp.
- **Per-IP token buckets with persistence**: state and eviction machinery defending against a threat model (distributed online guessing on a trusted LAN) the deployment shape excludes.
