# theozolith-control

Control Node: the product's central service (see `docs/specs/NODE-SUBSTRATE.md` and
ADR-0015/0017/0018 for the contracts). FastAPI + SQLite, shipped as the `control`
built-in Stack (a container Stack; `control/docker/Dockerfile`, compose in
`deploy/compose/`).

- **Heartbeat/command channel** — Node Daemons report node, Stack, labeled run-container,
  and build status every 60s; responses carry infrastructure commands (drain, recycle,
  update, rebuild — recycle/update queue behind an in-flight Run unless `--force`) and
  the node's desired state from the Config Repo (ADR-0006).
- **Claim dispatch** (ADR-0017) — the single writer of claim creation: Workers request
  work, the Control Node claims the issue on GitHub itself (assign + `in_progress`,
  write-through) and returns it; the Reviewer side is discovery-only. Gates: node
  quarantine (2 consecutive failed Runs, human-only release), pending lifecycle
  commands, and the `failed` label (refused and surfaced as a malformed state, never
  laundered). Requires the control PAT; without it the pipeline pauses.
- **Typed event API** — namespaced events; `theozolith.run`, `theozolith.review`,
  `theozolith.run.progress`, and `theozolith.error` are the known pipeline types;
  unknown types are accepted, stored, and rendered generically on the dashboard
  (extension point). Telemetry payloads are size-capped at ingestion (error context is
  truncated, never refused); progress and error events evict oldest-first under a
  ~10GB budget (the database is a cache, never the archive — ADR-0016). The dashboard's
  errors panel filters `theozolith.error` summaries by node and component.
- **Zombie-claim janitor** (ADR-0016) — two-phase, evidence-first: silence past the
  grace period flags the dashboard only; once the Run's evidence bundle lands the claim
  is released and escalated `failed` + `needs_human` with the evidence link. Also
  releases never-activated dispatch grants (no claimed event within the activation
  window).
- **Secret store** — values entered once via `theozolith secret set` or the
  dashboard's web form (both write through the same store), encrypted at rest (Fernet,
  file-held master key), pull-only and node-scoped, TLS mandatory, never displayed.
- **Dashboard + web terminal** (M4, ADR-0018; hardened by ADR-0022) — read-only fleet
  view (Jinja + HTMX polling, no build step) and a PTY-bridge terminal running a
  container Stack's config-supplied `attach` argv against its live stack container
  (target and owning Stack derived server-side from fresh heartbeats; identifiers
  validated; output bounded), audit-logged to `<data>/terminal-audit.log`. The Flight
  Deck is the primary target; run containers are headless and never attach targets
  (ADR-0019), audit log under `~/.theozolith/logs/`. The admin password (scrypt hash,
  stateful revocable sessions in cache.db, rate-limited login — ADR-0023) fronts the
  browser surface; the admin bearer token stays the machine credential. Everything
  sits behind a randomized public origin with exact Host/Origin enforcement (derived
  from the origin URL alone — independent of the Uvicorn bind host/port). Tier-2
  settings edit `control.toml` in the Config Repo via fixed-schema commits; join
  tokens mint the one paste that provisions a node (per-node bearer tokens; rejected
  heartbeats surface as the unregistered-nodes view). A plaintext bootstrap listener
  (port 6965) serves exactly the CA cert, origin, and control URL.
- **Operator TUI** (M9, ADR-0040) — `theozolith top`, the primary routine-operations
  surface while the web UI is frozen: a full-screen Textual app run on the Control
  Node (`sudo theozolith top`) or against an SSH-forwarded socket (`CONTROL_NODE_URL`
  + `THEOZOLITH_ADMIN_TOKEN` + `THEOZOLITH_TLS_CA`) — a pure API consumer over the
  bearer `/api/v1` reads (state + events), never the databases, working from the
  moment init finishes (the browser surface's enabled state is irrelevant to it).
  Panels: `1` Fleet (nodes with quarantine/stale/off-pin health on the server clock,
  live containers, the command queue with queue-behind deferrals), `2` Stacks & Runs
  (the UNION of desired and reported Stacks — desired-only rows read "not reported",
  actual-only rows read desired "(unplaced)" and are always off desired; run detail
  with phase, attempt, elapsed vs timeout budget, tool calls, and the advisory
  transcript tail labeled with its byte count — outcome, the worker's canonical
  failure class, PR, and evidence-bundle reference once terminal; the Runs listing
  stays complete across event pages via a client-side index, and ever crossing its
  defensive history bound shows an explicit incomplete-data notice), `3` Events
  (exact node/component/type filters, follow mode by cursor, per-query eviction
  honesty, and a per-panel "history incomplete" warning when a follow overflow
  skipped events client-side — distinct from server eviction, cleared only by a
  filter change), `4` Errors, `5` Settings (control.toml read-only — editing stays
  git-native). Keys: `c` queue a command (destructive verbs demand the target name
  typed back), `x` release a quarantine, `s` masked secret entry (value never
  displayed), `a` print the pastable attach command resolved from fresh heartbeat
  evidence (print-only — no embedded terminal, no PTY, no websocket, by ruling),
  `f` follow on/off, `r` refresh, `q` quit. Polls every 5 s; an unreachable control
  degrades to a banner over the last documents and keeps polling — and attach
  assistance fails CLOSED while degraded (a retained snapshot's frozen server clock
  cannot prove heartbeat freshness; the refusal stands until a refresh succeeds).
- **Product updates** (ADR-0015 as amended) — `theozolith update` pins a published
  release; `theozolith build` pins a CLEAN source checkout's git SHA (a dirty tree is
  refused) and uploads wheels the Control Node serves for node pulls; `theozolith
  test` is the local-development signal. Both update paths commit the pin to
  product.toml; the pin is desired state that nodes converge to on every heartbeat
  (failed installs self-retry; the fanned-out command is only a nudge), with the
  control-hosting node queued last. Dispatch grants only to on-pin nodes — an update
  pauses new dispatch fleet-wide until versions converge; persistently off-pin nodes
  get a restart command, then a theozolith.error. The dashboard surfaces version skew
  against the recorded pin.

Availability (ADR-0017): with this service down, in-flight Runs finish and publish;
new claims and review rounds pause. Drivers hold their own PATs for all non-claim
GitHub writes.

```sh
theozolith init                              # the unified first run (ADR-0023/0036):
                                                     # key, token, address, CA/TLS, handoff
theozolith init --with-local-node            # + Node Daemon and the internal join (ADR-0037)
theozolith origin-init                       # opt-in browser: origin + password (ADR-0036)
theozolith serve                             # the service (+ dashboard + bootstrap listener)
theozolith recover                           # after restoring ~/.theozolith minus cache/
theozolith join-token create                         # print the one paste that provisions a node
theozolith secret set github-worker          # operator entry
theozolith command recycle --node box1 --target worker   # queues behind a Run
theozolith command recycle --node box1 --target worker --force
theozolith unquarantine --node box1          # human-only release
theozolith status                            # fleet health (exit 0/1/2; --json to parse)
theozolith top                               # the Operator TUI (M9; see --help for keys)
theozolith flags                             # zombie/malformed/quarantine flags
```
