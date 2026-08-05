Status: ACCEPTED

Date: 2026-08-04

Provenance: delegated decision from the M8 brief — the `--json` schema and the degraded-reason precedence order for `theozolith status`. Implements the "Grilling 2026-08-04 (TUI scope, contracts, sequencing)" ruling in NODE-SUBSTRATE.md; consumes ADR-0038 (the events read endpoint) and ADR-0022's 150-second staleness threshold.

# ADR-0039: `theozolith status` — read model, `--json` schema, degraded precedence, exit codes

## Context

The ruling fixed the behavior: human table on stdout, `--json` as the only parsing contract, exit 0 healthy / 1 degraded / 2 Control Node unreachable, pure API consumer (no systemd/docker/subprocess probing), stdlib-only. It delegated the `--json` schema and the precedence order of degraded reasons. Two gaps blocked a pure API consumer: `/api/v1/state` carried neither the product pin nor desired Stack state (both lived only in the dashboard's server-rendered view), and clock skew would poison client-side staleness math. The subcommand replaces the previous `status` (a raw `/api/v1/state` JSON dump) — sanctioned by the ruling, which makes `--json` the sole parsing contract.

## Decision

### The read model rides `/api/v1/state`

`/api/v1/state` gains three keys so status (and the M9 TUI) can stay a pure API consumer: `now` (server unix seconds — all staleness math uses the server clock), `product_pin` (the Config Repo pin, null when unpinned), and `desired_stacks` (`[{node, name, kind, state}]` from the Config Repo). Everything else status reads already exists: `nodes`, `stacks` (actual), `node_health`. Recent errors come from `GET /api/v1/events?type=theozolith.error&since=<now − 900>` — the error window is **15 minutes**, wide enough to catch a failing reconcile loop across several heartbeats, narrow enough that a resolved incident ages out of "degraded" without human bookkeeping.

### Degraded conditions and precedence

Exit 1 when any of the following holds; reasons are reported in this order, and the **first line of output is the highest-precedence reason**:

1. **quarantined** — any `node_health` row with `quarantined` set. Highest: the fleet is deliberately halted and only a human releases it.
2. **stale** — any node with `now − last_seen > 150` (ADR-0022's threshold, ~2.5 missed heartbeats). The fleet's true state is unknown, which outranks everything knowable below.
3. **off-pin** — any node whose reported version differs from `product_pin` (skipped when either side is empty). Dispatch is paused fleet-wide until convergence.
4. **stack off desired state** — any Stack whose actual state differs from its desired state, in either direction, including a desired-running Stack its node has not reported. A drained Stack reads as off-desired: a drained fleet is legitimately not healthy-idle. Stopped-by-desire is healthy.
5. **recent errors** — any `theozolith.error` event inside the 15-minute window.
6. **incomplete error history** — the events response reports status's **own query** incomplete (ADR-0038's query-relative `evicted`: an eviction that could have matched `theozolith.error` rows inside the 15-minute window; unrelated progress evictions never trip it). Lowest precedence: it is an epistemic qualifier, not an observed fault — but it is a full degraded reason, because exit 0 is the unqualified "healthy" answer and status must never give it from incomplete evidence. The human line states it plainly ("this assessment may miss failures"); with query-relative eviction it fires only when error evidence in the window was actually lost, which is itself alarming.

The order runs from "a human already decided to halt" through "we cannot know" and "we know and it is converging" down to "advisory telemetry" and, last, "the telemetry itself is incomplete" — each reason outranks the ones a fixed version of it would subsume.

### Exit code 2: the read failed

Any failure to complete both reads — connection refused, timeout, TLS failure, or a non-2xx answer — exits 2, printing the dial target, the error class (`ConnectionRefusedError`, `HTTP 401`, …), and the static hints `systemctl status theozolith-control` / `docker ps`. No local probing of any kind: status runs no subprocess ever (test-asserted), and the hints are strings, not diagnoses. Reachable-but-refusing (an HTTP error) folds into 2 rather than a third code: either way status could not assess the fleet, and the error class says which case the operator has.

### `--json` schema — the only parsing contract

```json
{
  "status": "healthy" | "degraded",
  "reasons": ["<one human-readable string per finding, precedence order>"],
  "state":  { …the raw /api/v1/state document, verbatim… },
  "errors": { …the raw /api/v1/events response for the error window, verbatim… }
}
```

Unreachable emits `{"status": "unreachable", "dial_target": "<url>", "error_class": "<class>", "hints": ["…"]}` on stdout and exits 2. The raw documents are passed through untouched — the server documents evolve under their own contract and status never rewrites them; `status`/`reasons` are the only computed fields. The human table renders nodes (name, health, version, last-seen age) and Stacks (node, name, desired, actual) from the same data and is explicitly not parseable surface.

### stdlib-only, enforced

The implementation lives in a module that is stdlib-only for its entire import closure — module-level *and* function-scope (the nodedaemon AST check's discipline, extended to this module) — and the URL/token/CA resolution it defines is the one implementation the rest of the CLI delegates to. A test imports the module in a clean interpreter and asserts none of the control dependency exceptions (`cryptography`, `fastapi`, `uvicorn`, `jinja2`) were loaded; another runs the command with subprocess machinery poisoned to prove the no-probing rule.

## Alternatives rejected

- **A server-side `/api/v1/status` verdict endpoint**: moves the health policy into the server where the TUI would re-implement it anyway for per-row display; the read model belongs on the server, the verdict in the client, and `--json` shipping both raw documents keeps every downstream consumer un-lied-to.
- **Staleness before quarantine in precedence**: a quarantined node is often also stale; leading with "stale" would bury the one reason that names the human action (release) behind the symptom.
- **A third exit code for reachable-but-refusing**: multiplies the contract for a distinction the printed error class already carries.
- **Excluding drained Stacks from off-desired**: requires the wire to distinguish drain from stop (it deliberately does not today) and would make a forgotten drain read healthy forever.
- **A configurable error window**: a tier-2 tunable for a v1 constant nobody has asked to tune; 15 minutes is recorded here and can graduate to `control.toml` when a real deployment needs it.
