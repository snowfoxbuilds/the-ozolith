Status: ACCEPTED

Date: 2026-08-04

Provenance: delegated decision from the M8 brief — listener/serve orchestration inside `init --with-local-node`, plus the scaffold's contents, README wording, and example worker-type choice. Implements the "Grilling 2026-08-04 (single-node)" and "(late)" rulings in NODE-SUBSTRATE.md and ADR-0035's scaffold consequence. Builds on ADR-0023/0025 (join flow, unchanged), ADR-0026 (bootstrap listener), ADR-0034 (root-mediated setup), ADR-0036 (init composes no browser surface).

# ADR-0037: Single-node bootstrap — orchestration inside `init --with-local-node` and the stage-don't-deploy scaffold

## Context

`theozolith init --with-local-node` must install the Node Daemon on the same box and execute the **unmodified** join flow end-to-end internally: join-token create → machine-composed provision line → loopback bootstrap listener → per-node token minted, join token consumed. The human never sees the join string; no second provisioning code path may exist; the local daemon persists a loopback dial address. The ruling delegated the orchestration (temporary bootstrap listener vs. early serve start) and the scaffold's concrete shape to this PR.

Two existing mechanisms make the loopback contract fall out without touching provisioning: the join exchange answers `control_url = https://<the Host the node dialed>` (provisioning persists that answer), and the node-side provision takes its exchange **host from the join string** and only the **port from the listener's `/control-url`**. A node that dials loopback therefore persists loopback; the nodedaemon suite's `LiveControl` rig already proves the whole chain against `127.0.0.1`.

## Decision

### Orchestration: early serve start *and* a temporary loopback listener

Both delegated options are used, each where it is honest:

1. **Pre-flight, before any state is written** (extending init's existing posture): `--with-local-node` requires root on bare metal with systemd, refuses inside a container, and requires `docker` and a resolvable `theozolith-nodedaemon` executable (the bare-metal build installs all four distributions into one venv; a control-only install is refused with remediation naming `build.py` / the nodedaemon package — a root path must not start `pip install`ing on its own).
2. **Standard init runs unmodified** (ADR-0036 scope), including the systemd unit install; the scaffold (below) is committed into the fresh Config Repo.
3. **Early serve start**: init starts the just-installed `theozolith-control.service` and waits for readiness by polling `/api/v1/healthz` over TLS at `https://127.0.0.1:<control_port>`, verified against the freshly minted CA (the loopback IP SAN is unconditional since ADR-0036). The service is the real supervisor from the first heartbeat — no throwaway in-process serve whose lifecycle would differ from production, and ADR-0035's "control is always its own systemd unit" holds from the very first boot.
4. **Temporary loopback bootstrap listener**: the production listener inside serve answers the LAN control URL — correct for every remote node, wrong for the local one. Init therefore starts a second `BootstrapServer` instance (the same class serve runs, ADR-0026) bound to `127.0.0.1` on an ephemeral port, serving the same CA and `control_url = https://127.0.0.1:<control_port>`. It lives only for the exchange and is stopped in a `finally`. The listener's route table and semantics are untouched — this is a second *instance*, not a second implementation.
5. **Join, exactly as a human would**: init mints the join token through `POST /api/v1/join-tokens` with the admin bearer token and an explicit `addr = 127.0.0.1:<listener port>` (the same endpoint and `--addr` mechanism `join-token create` uses), installs the node-side prerequisites (the `ozolith` system user in the `docker` group, `/var/lib/theozolith` at 0750, the `theozolith-nodedaemon.service` unit, daemon-reload — the same steps `install-nodedaemon.sh` performs, minus the venv install that already happened; a test pins the two unit bodies against drift), then runs the provision line as a child process: `theozolith-nodedaemon provision '<join string>' --node <hostname>`. That is byte-for-byte the grammar a human paste runs — parse, fingerprint check against the temporary listener, TLS exchange against the real serve at loopback, persist, enable, restart. The join string is never printed.
6. **Verification before the handoff**: init confirms the join token was consumed and waits for the node's first heartbeat to land in `/api/v1/state`, then prints a local-node handoff (service running, node registered, Stack staged — no "start serving" step, since serve already runs).

Failure anywhere fails loudly with the completed steps named; re-running requires `--force` like any re-init.

### The scaffold: complete, commented, stopped

`--with-local-node` seeds the Config Repo (committed with the fixed machine identity) with:

- `stacks/worker.toml` — a process-kind Implementer Stack placed on the local node, `state = "stopped"`, `command = "theozolith-worker"`, `run_image = "claude-dev"`, commented `env` entries (`THEOZOLITH_REPO` placeholder, model, worker id) and the two conventional secret references (`WORKER_GITHUB_TOKEN`, `ANTHROPIC_API_KEY`). Every field carries a comment saying what it is and what to change.
- `images/claude-dev.toml` — the example worker type: the product's Claude run image as `base` with a placeholder digest that the README's first step replaces, plus commented `setup` and Knowledge Source examples. **The Implementer on the Claude harness is the example worker type**: it is the flagship built-in, it exercises the full derived-image path (base + setup + knowledge), and it matches `deploy/configs-example/` so the two never teach different shapes.
- `README.md` — names the finish line in three steps: **pin the base image digest** (one `docker pull` + `docker inspect` line shown), **enter secrets** (`theozolith secret set …`, or the TUI once M9 lands), **flip `state` to `"running"` and commit** — then the daemon builds the derived image and brings the worker up, visible in `theozolith status`.

### Stage-don't-deploy is enforced, not hoped for

Desired state stops carrying image recipes for stopped Stacks: `desired_state_for` collects `run_image` references only from Stacks whose desired state is `running`. Today a stopped Stack's image still rides the heartbeat and the daemon builds it on first contact — with a placeholder digest that build *fails*, emitting error events on a box that was born misconfigured, which is precisely what the ruling forbids. With the gate, first boot deploys and builds nothing, `theozolith status` exits 0 (node healthy, Stack stopped-by-desire), and the flip to `running` is the single act that starts the build-and-run sequence. A `rebuild` of a stopped Stack's image waits for the flip by the same rule.

### The control-hosts update-ordering special case is deleted

`POST /api/v1/product/update` ordered fan-out so nodes hosting a `control` Stack updated last. With ADR-0035 no node can host control, the Config Repo validation refuses a Stack named `control`, and the ordering collapses to plain per-node fan-out; the Control Node still updates itself last through its own `os.execv` path (ADR-0015).

## Consequences

- **Positive**: one provisioning mechanism at every fleet size — the internal flow is a caller of the standard machinery, testable by asserting the argv it runs and by the existing `LiveControl` end-to-end rig; the local node's loopback dial address makes LAN renumbering a non-event for it; first boot is inert by construction.
- **Negative**: `--with-local-node` hard-requires the all-distributions bare-metal install (refusal with remediation otherwise); the node-side install steps exist twice (shell installer for remote boxes, Python for the local one) — held together by a unit-body drift test rather than a shared file, accepted because the shell installer must stay a curl-able standalone.
- **Neutral**: multi-node join-string provisioning is byte-for-byte unchanged (the temporary listener is invisible off-box — it binds loopback); quarantine, drain, and Stack mechanics are the multi-node ones unchanged.

## Alternatives rejected

- **Temporary in-process serve instead of starting the unit**: a second serve lifecycle that exists only during init — different supervisor, different port story, and a teardown/handoff seam where the real service replaces the temporary one; starting the real unit early is strictly more honest and is what the operator was about to do anyway.
- **Making the production bootstrap listener answer loopback callers with a loopback control URL**: puts a peer-dependent conditional inside the deliberately inert three-inert-values listener (ADR-0023/0026) to serve exactly one caller that init itself controls; a second instance with the right value is simpler and leaves the production surface untouched.
- **Calling `provisioning.provision()` in-process from control**: crosses the control→nodedaemon component boundary with an undeclared import and would *not* be the code path a human paste runs (which enters through the installed CLI); the child process is the same grammar, same entry point, same implementation.
- **Scaffolding with a real fetched base digest**: init would need network access and a registry round-trip on the critical setup path, and a digest fetched at init silently ages; the README's explicit pin step keeps the pin an operator act (ADR-0006's spirit).
- **Leaving image builds unconditional for stopped Stacks**: ships a guaranteed first-boot build failure with the placeholder digest, or forces the scaffold to omit `run_image` and grow a fourth finish-line step; gating on desired state matches the ruling's own words.
