Status: ACCEPTED

Date: 2026-08-01

Provenance: chat working session 2026-08-01, prompted by the first bare-metal deployment report (portless origin vs. 8443 bind — connection refused with serve running). Amends ADR-0022 (the slug public origin is retired) and ADR-0023 (first-run handoff, CA trust demoted to optional, root-mediated bare-metal setup); consumes ADR-0031 (the persisted control IP becomes the browser address too). ADR-0027's login rate limit is unchanged and now load-bearing.

# ADR-0034: IP-based browser origin, optional CA trust, root-mediated bare-metal setup

## Context

The first bare-metal deployment hit the seam between the portless slug origin and serve's unprivileged 8443 bind: the CLI and every mint surface dial external 443, the reference compose flow bridges it with a `443:8443` mapping, and nothing warns when no mapping exists. Meanwhile the two irreducibly manual per-device steps ADR-0023 accepted — a DNS record and CA trust — are the worst first-run friction in the product, and the DNS step is not even *possible* on iOS without router-level DNS.

ADR-0022/0023 rejected an IP-literal origin on three grounds. Two have aged out: ADR-0031 made a static IP / DHCP reservation a hard prerequisite (the DHCP objection is moot), and the slug's 128 bits were always declared defense-in-depth — the password check and rate limit "must stand alone" (ADR-0023). The third ground — CA trust is still needed for warning-free HTTPS — is true but answerable the way TrueNAS answers it: ship the warning, make trust the optional upgrade.

## Decision

### The browser origin is the control IP

- The canonical browser origin is `https://<control_ip>[:<control_port>]` — the same persisted address nodes dial (ADR-0031). `control_port` is a new read-only `[control]` field beside `control_ip`, default 443; `init --port` sets a nonstandard one for shared boxes. An ABSENT field means 443; a present-but-malformed value **fails closed** (`ControlTomlError` at load/serve/recover), never a silent redirect of the fleet to 443 — the hand-edit-typo posture of the fixed schema (ADR-0029) extends to the address.
- The slug origin, `origin-init`, `--base-domain`, the `theozolith.internal` namespace, and the per-device DNS step are **retired**. `public_origin` leaves `control.toml`; `THEOZOLITH_PUBLIC_ORIGIN` is removed, not deprecated.
- `BrowserGuard` is unchanged in shape: exact-match enforcement of exactly one Host and one Origin, now derived from `control_ip` + `control_port`. The `__Host-` session cookie works as-is on an HTTPS IP-literal origin.
- The dashboard becomes discoverable by LAN port scan. Accepted: discovery was never the boundary — the admin password and the ADR-0027 rate limit are the front line, exactly as those ADRs required them to be.

### TrueNAS-model TLS: the warning ships, CA trust is the upgrade

- First browser contact shows the self-signed-CA interstitial; the operator clicks through and logs in. This is the default, documented path — no DNS record, no certificate install, nothing to do per device.
- Trusting the per-deployment CA (downloaded from the bootstrap listener, as today) remains the opt-in green-lock upgrade, and the public-domain certificate escape hatch (ADR-0023) still deletes the warning wholesale.
- **This host never sets HSTS** — previously an accident, now an invariant: HSTS hard-blocks click-through on certificate errors.
- Accepted consequence, stated plainly: a browser that clicked through has no MITM protection on that session — a LAN attacker who can spoof the control IP could present their own certificate and harvest the password or dashboard-entered secrets. Operators who want browser-channel integrity trust the CA (one step, no DNS). The **machine channel is untouched**: nodes pin the CA fingerprint from the join string before transmitting anything (ADR-0023), so provisioning, heartbeats, and secret distribution never depended on device trust and still don't.

### Root-mediated bare-metal setup with a capability-granting unit

- Bare-metal setup is `sudo theozolith init`: it creates a dedicated system user, lays the ADR-0024 partition down at a fixed system path (`/var/lib/theozolith-control`, mirroring the nodes' `/var/lib/theozolith`), and installs and enables `theozolith-control.service` — `User=` the service user, `AmbientCapabilities=CAP_NET_BIND_SERVICE`, `ExecStart=… serve --port 443`. The service binds 443 directly as an unprivileged process; no root serve, no setcap on a shared interpreter, nothing to keep alive in a terminal.
- The installer is one idempotent implementation shared by `init` and root-mediated `recover`: ensure the user, repair partition ownership, (re)write the unit with the persisted external port, daemon-reload, enable. Recovery therefore restores the *service*, not just the data — its "systemctl start" instruction is truthful by construction — and **never mints a new CA** (the ADR-0024 invariant stands: same-IP recovery touches no node; a changed IP costs one join-string re-paste per node).
- **The root installer only mutates its own leaf** (revision, PR #12 round 2): the recursive ownership handover runs against exactly `/var/lib/theozolith-control` — an exact, symlink-free match, validated before any mutation (root `init` refuses before writing any state). `THEOZOLITH_DATA_DIR` is honored everywhere else, but a root `chown -R` of an environment-controlled path is how a typo (`/`, `/var`, a symlink into the host) transfers the machine to the service user; operators needing another location run unprivileged or via compose. The constant path doubles as a unit-syntax-safe `Environment=` value by construction.
- The `ExecStart` path is checked against an **installation policy** before the unit is written — deliberately conservative, not a precise access computation as the service user: the path must use unit-syntax-safe characters (no spaces, quotes, `%` specifiers, backslashes, or control characters — the unit's directives are unquoted, and rejection is simpler to get right than systemd's quoting rules), be world-readable+executable, and sit under world-traversable ancestors. Group-accessible layouts that would in fact work are rejected by policy; a plain world-readable system install is the supported shape. The point is timing: a sudo invocation happily resolves an executable inside `/root` or a 0700 home venv, and persisting that would fail only at first boot. Setup fails with remediation ("install at a plain system path, e.g. a venv under /opt") instead of deferring the failure.
- serve's *default bind* stays 8443: hand-run dev and the compose flow (`443:8443` mapping) keep working unchanged; the unit passes `--port 443` explicitly. This was the least-churn option and the bind-vs-external distinction already exists.
- The mismatch fails loud: outside a container, serve compares its bind against the persisted `control_port` and warns prominently on mismatch — the exact silent failure that prompted this ADR.
- On the Control Node, admin subcommands (`build`, `update`, `join-token`, `secret`, …) read the admin token from the system data dir and are run with `sudo`. Remote/unprivileged use is unchanged: `CONTROL_NODE_URL` + `THEOZOLITH_ADMIN_TOKEN` + `THEOZOLITH_TLS_CA`.
- Migration for existing deployments needs **no CA rotation and no node re-paste**: the server certificate has carried `control_ip` in its SAN since ADR-0031, so IP browsing verifies immediately; the slug origin config is dropped on update, and stale hosts entries or trusted CAs are harmless leftovers. A pre-ADR home-dir install relocates (move the partition, chown, install the unit) without re-init.

## Alternatives rejected

- **Keep the slug origin alongside the IP origin**: once the IP works with zero setup, the slug is a dead entropy layer nobody provisions DNS for; carrying both doubles the accepted-origin set, the session-table shape, and the handoff text for a step no one performs.
- **Flip serve's default bind to 443**: breaks hand-run dev (bind: permission denied) and forces compose churn, for no user-visible gain over the unit passing `--port 443`.
- **Root serve / privilege-drop in app code**: the process holds the master key, CA key, and secret store; hand-rolling nginx's setuid machinery in Python is the wrong place to spend risk.
- **`setcap` on the interpreter**: a venv's `python` symlinks the system binary — the capability would bless every Python script on the box, or require a copied-interpreter venv that breaks on upgrades.
- **HSTS for the dashboard**: hard-blocks the click-through path that is now the default UX.
- **A public-domain certificate as the default**: an external dependency (domain, ACME, egress) in a product whose deletion test is "docker + package + init output"; it stays the documented opt-in.
