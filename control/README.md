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
- **Typed event API** — namespaced events; `theozolith.run`, `theozolith.review`, and
  `theozolith.run.progress` are the known pipeline types; unknown types are accepted,
  stored, and rendered generically on the dashboard (extension point). Telemetry
  payloads are size-capped at ingestion; progress events evict oldest-first under a
  ~10GB budget (the database is a cache, never the archive — ADR-0016).
- **Zombie-claim janitor** (ADR-0016) — two-phase, evidence-first: silence past the
  grace period flags the dashboard only; once the Run's evidence bundle lands the claim
  is released and escalated `failed` + `needs_human` with the evidence link. Also
  releases never-activated dispatch grants (no claimed event within the activation
  window).
- **Secret store** — values entered once via `theozolith-control secret set` or the
  dashboard's web form (both write through the same store), encrypted at rest (Fernet,
  file-held master key), pull-only and node-scoped, TLS mandatory, never displayed.
- **Dashboard + web terminal** (M4, ADR-0018; hardened by ADR-0019) — read-only fleet
  view (Jinja + HTMX polling, no build step) and a PTY-bridge terminal running each
  Stack's config-supplied `attach` argv against live run containers (target and owner
  derived server-side from fresh heartbeats; identifiers validated; output bounded),
  audit-logged to `<data>/terminal-audit.log`. One admin credential fronts all of it,
  behind a randomized public origin with exact Host/Origin enforcement (derived from
  the origin URL alone — independent of the Uvicorn bind host/port).

Availability (ADR-0017): with this service down, in-flight Runs finish and publish;
new claims and review rounds pause. Drivers hold their own PATs for all non-claim
GitHub writes.

```sh
theozolith-control origin-init                       # once: mint the public origin (ADR-0019)
theozolith-control tls-init                          # once: TLS covering the origin's hostname
theozolith-control serve                             # the service (+ dashboard)
theozolith-control secret set github-worker          # operator entry
theozolith-control command recycle --node box1 --target worker   # queues behind a Run
theozolith-control command recycle --node box1 --target worker --force
theozolith-control unquarantine --node box1          # human-only release
theozolith-control status                            # fleet state JSON
theozolith-control flags                             # zombie/malformed/quarantine flags
```
