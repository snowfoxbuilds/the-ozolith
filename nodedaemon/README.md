# theozolith-nodedaemon

Node Daemon (renamed from "Node Agent" — Agent is reserved for LLM tool configs;
ADR-0013): the uncontainerized TheOzolith daemon installed on every physical node,
making it a Container-Host. Stdlib-only; installed by `deploy/install-nodedaemon.sh`
as a systemd unit with `KillMode=control-group` — every TheOzolith process on the
node is a live descendant of this daemon or does not exist. A box joins the fleet
with one pasted join string: `theozolith-nodedaemon provision` verifies the pinned
CA fingerprint before transmitting anything, exchanges the short-lived join token
for this node's own non-expiring bearer token, and persists everything under the
state dir — no environment configuration remains (ADR-0023; provisioning IS
registration).

Each pass (60s):

- **Heartbeat** node, Stack, labeled run-container (`theozolith.run-id`/`theozolith.owner`)
  and derived-image build status to the Control Node over HTTPS + bearer token; the
  response carries infrastructure commands and this node's desired state (ADR-0006).
- **Reconcile commands**: drain (stop + persistently mark down), recycle (kill the whole
  driver tree incl. its run containers, restart), rebuild (force derived-image rebuild),
  update (stop Stacks, install the Config-Repo-pinned product version, re-exec).
- **Converge Stacks** — two kinds, one declarative format (no hardcoded workload
  knowledge): process Stacks run a product command as a supervised child in its own
  process group (how the Worker/Reviewer drivers deploy); container Stacks run an image
  or compose + overlays.
- **Build derived images** locally: digest-pinned base + setup instructions + optional
  Knowledge Source (baked via the M1 `theozolith-knowledge bake` CLI at build time);
  deterministic tag (base tag + instruction hash); metadata stamped as labels and
  reported in heartbeats.
- **Materialize secrets** pulled node-scoped from the Control Node into tmpfs
  (`/run/theozolith/secrets`, 0600), wired via the VAR_FILE convention — never on disk.
- **Reap orphans**: run containers whose owning driver is gone.

Degraded mode is first-class: with the Control Node unreachable (or no
`CONTROL_NODE_URL` at all) the daemon reconciles from its cached last-applied config
forever; the pipeline keeps shipping PRs via GitHub alone (ADR-0002).

```sh
theozolith-nodedaemon          # the supervised loop (systemd runs this)
theozolith-nodedaemon --once   # one heartbeat + reconcile pass (dev)
```
