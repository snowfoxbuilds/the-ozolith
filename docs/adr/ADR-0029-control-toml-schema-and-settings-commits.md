Status: ACCEPTED

Date: 2026-07-28

Provenance: delegated decision from the M7 brief; implements ADR-0023's configuration-surface contract (control.toml, tier 2) and ADR-0024's public-origin relocation.

# ADR-0029: control.toml schema layout and the settings-form commit convention

## Context

`control.toml` in the Config Repo is the durable home for tier-2 tunables; every setting ships a default so the file is optional; the dashboard edits it via a fixed-schema commit (the `product.toml` pin-bump precedent); the public origin lives in it as a read-only field. Schema layout and commit convention were delegated.

## Decision

### Schema: two tables, two write paths

```toml
[control]
public_origin = "https://<slug>.theozolith.internal"   # read-only; written by init/origin-init

[settings]                       # tier 2 — only keys off their shipped default appear
heartbeat_seconds = 60
zombie_grace_seconds = 600
janitor_sweep_seconds = 60
activation_window_seconds = 60
tail_budget_bytes = 10737418240
terminal_session_cap = 8
session_days = 30
offpin_beats = 3
stop_grace_seconds = 30
bootstrap_port = 6965
installer_url = "https://github.com/…/install-nodedaemon.sh"
```

- The registry of keys, types, defaults, labels, and `THEOZOLITH_*` override names is one table in `theozolith_control.controltoml` — the loader, the settings form, and the env-override validation all read it, so a key exists everywhere or nowhere.
- **Unknown keys fail closed** at load: a typo in a hand-edited file surfaces as an error, never as a silently ignored setting.
- Writes regenerate the whole file from the fixed schema and emit only non-default `[settings]` keys — the file stays minimal, remains optional, and free-form content has nowhere to live (the post-V1 config editor stays out of scope, ADR-0006).
- `heartbeat_seconds` and `stop_grace_seconds` ride desired state to nodes; a node-local env override off the shipped default wins on that node (expert hatch preserved).

### Commit convention

- Author identity: `theozolith <theozolith@invalid>` — the same fixed identity `theozolith update`/`build` already use for pin bumps; machine commits are attributable as machine commits.
- Message format: `theozolith: settings: <key> = <value>` (one key per commit — the form saves one field at a time); origin writes use `theozolith: public origin <origin>`.
- The commit touches only `control.toml`; a no-op write (value unchanged) produces no commit. In folder mode (no `.git`) the file write itself is the record, matching `product.write_pin`.
- The settings form refuses `public_origin` outright: re-pointing a deployment is `origin-init --force`, a deliberate CLI act with DNS/TLS consequences, never a form field.

## Alternatives rejected

- **Ignore-unknown-keys loading**: the classic misconfiguration trap — a misspelled tunable "works" at the default forever.
- **Preserving hand-authored comments/layout on write**: a TOML round-tripper dependency (or a fragile homegrown one) to protect prose inside a machine-managed file; commentary belongs in the Config Repo's own docs.
- **One commit batching many keys**: the form has no batch UI, and per-key commits make `git log control.toml` the settings audit trail.
