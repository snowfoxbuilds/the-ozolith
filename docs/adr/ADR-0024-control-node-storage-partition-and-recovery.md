Status: ACCEPTED

Date: 2026-07-27

Provenance: grilling session 2026-07-27; authored directly in Notion (ADR-0001). Amends ADR-0015 (single data dir, single SQLite database), ADR-0016 (cache-never-archive made true by construction), ADR-0022 (public-origin storage location); refines ADR-0023's storage references; leaves ADR-0006's never-contains-secret-values rule intact.

# ADR-0024: Control Node storage partition and recovery

## Context

ADR-0016 declared the control database "a cache, never the archive," and ADR-0018 relied on that rule to keep the terminal audit log out of SQLite — yet the same file housed the encrypted secret store (ADR-0015) and gained per-node tokens (ADR-0023): deleting the "deletable" database destroyed every secret and every node enrollment. Separately, no Control Node recovery story existed, and ADR-0023's deletion of `.env` reopened where durable state lives. Grilling 2026-07-27 resolved all three together.

## Decision

### Directory partition by durability class

`~/.theozolith/` is the data dir, partitioned; the monolithic flat data dir dissolves.

```javascript
~/.theozolith/
├── configs/   # git repo (GitHub-synced): stacks/, images/, product.toml,
│              # control.toml — including the public origin as a read-only field
├── secrets/   # never in any git tree: master.key, CA keypair, server TLS
│              # material, admin password hash, store.db
├── cache/     # cache.db — deletable by construction; never backed up
└── logs/      # terminal-audit.log; systemd journal remains diagnostic depth
```

### SQLite split: store.db and cache.db

- `store.db`: encrypted secret ciphertext (ADR-0015 mechanics unchanged — master.key + Fernet, single-transaction rotation) and per-node tokens (ADR-0023).
- `cache.db`: node/stack state, event cache (~10 GB budget), janitor findings, browser sessions, outstanding join tokens.
- Deleting `cache.db` is always safe and is the documented recovery move: state rebuilds within one heartbeat round, operators re-login, unredeemed join strings are re-minted. ADR-0016's cache-never-archive rule becomes true by construction instead of true with unstated exceptions.
### Public origin location

The persisted public origin moves from the flat `public-origin` file (ADR-0022) into `control.toml` as a tier-1 field: written by init, rendered read-only in the dashboard settings form, `THEOZOLITH_PUBLIC_ORIGIN` override unchanged. Origin semantics, fail-closed parsing, and exact-Host/Origin enforcement are untouched — only the storage location changes. The origin is deployment customization, not machine state; restoring the Config Repo must restore it.

### Backup doctrine: local copy, never GitHub

- Backup is a local copy of `~/.theozolith/` to another device, excluding `cache/` (and optionally `logs/`). One folder, one copy command.
- Secret material never leaves trusted devices. GitHub holds only declarative configuration and is explicitly not a full backup: a repo clone looks like a deployment but cannot resurrect one.
- `secrets/` is a sibling of `configs/`, never a git-ignored child: `git clean -x` in the working tree — a routine command under git-native config editing — would delete master.key, the CA keypair, and every per-node token, and a fresh clone would look complete while silently missing the load-bearing directory.
- Stale backups degrade gracefully: nodes enrolled after the copy re-provision with one join-string paste each; secrets entered after the copy are re-entered. Operator guidance is one line: re-copy after enrolling nodes or adding secrets. The dashboard's unregistered-nodes view (ADR-0023) lists exactly the nodes needing re-provisioning — their now-unknown heartbeats keep arriving and are surfaced, never registered.
### Recovery: one folder, one command, zero node touches

1. Install the TheOzolith package on the replacement box; restore `~/.theozolith/` from the backup copy.
2. `theozolith-control recover` validates completeness loudly (names exactly what is missing), re-mints the server certificate from the restored CA — the new box's IP lands in the SAN (ADR-0023) — and starts serving.
3. The operator updates the private-side DNS or hosts entry to the new address.
Nodes pin the CA, not the server certificate, and hold non-expiring per-node tokens restored with `store.db`: they reconnect on their capped backoff, untouched. Sessions and events died with `cache.db` — a re-login and one heartbeat round recover them.

## Consequences

- **Positive**: cache-never-archive is true by construction; the durability class of every byte is legible from its path; backup is one sentence (copy `~/.theozolith/` minus `cache/`); recovery is one command and touches no node; no secret material ever leaves trusted devices.
- **Negative**: GitHub is not a disaster-recovery path — losing the Control Node without a recent local copy means re-init, fleet re-provisioning, and secret re-entry (accepted, priced against never shipping secret ciphertext off trusted devices); backup cadence is the operator's responsibility.
- **Neutral**: `terminal-audit.log` relocates to `logs/` (ADR-0018's path reference is superseded; its rejection of SQLite audit storage stands); node-side storage is unchanged.
## Alternatives Considered

- **Sealed recovery bundle committed to the Config Repo** (`recovery.enc` under a printed recovery key): rejected — concentrates the whole deployment into one off-box ciphertext; a leaked recovery key plus GitHub access exposes everything.
- **store.db inside the Config Repo**: rejected — violates never-contains-secret-values; git is the wrong tool for a mutable binary database; dissolves the desired-state vs runtime-state boundary that keeps nodes from reading the repo.
- **Git-ignored secrets/ inside configs/**: rejected — `git clean -x` deletes it, and a clone from GitHub looks complete while missing it.
- **One database with documented exception tables**: rejected — preserves the exact ambiguity that produced the contradiction.
- **Secrets as flat encrypted files**: rejected — loses ADR-0015's single-transaction master-key rotation.
