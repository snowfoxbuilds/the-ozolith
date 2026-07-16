# theozolith-control

Control Node: the product's central service (see `docs/specs/NODE-SUBSTRATE.md` and
ADR-0015 for the contracts). FastAPI + SQLite, shipped as the `control` built-in Stack
(a container Stack; `control/docker/Dockerfile`, compose in `deploy/compose/`).

- **Heartbeat/command channel** — Node Daemons report node, Stack, labeled run-container,
  and build status every 60s; responses carry infrastructure commands (drain, recycle,
  update, rebuild) and the node's desired state from the Config Repo (ADR-0006).
- **Typed event API** — namespaced events; `theozolith.run` and `theozolith.review` are
  the known pipeline types; unknown types are accepted and stored (extension point).
- **Claim pre-filter** — advisory race filter for Workers; never a claim. GitHub
  assign-and-verify remains the only authority (ADR-0002).
- **Zombie-claim janitor** — returns issues whose Worker went silent past the grace
  period to `plan_ready`. **Retry auditor** — flags `attempt-N` label mismatches against
  events; never auto-corrects.
- **Secret store** — values entered once via `theozolith-control secret set` (the M4 web
  form writes through the same API), encrypted at rest (Fernet, file-held master key),
  pull-only and node-scoped, TLS mandatory.

The pipeline must keep working with this service down (ADR-0002): drivers skip events
and the pre-filter cleanly when it is unreachable.

```sh
theozolith-control tls-init --host controlnode.lan   # once
theozolith-control serve                             # the service
theozolith-control secret set github-worker          # operator entry
theozolith-control command drain --node box1 --target worker
theozolith-control status                            # fleet state JSON
```
