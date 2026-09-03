Status: ACCEPTED

Date: 2026-07-28

# ADR-0025: Join-string serialization, checksum, and error-text catalogue

## Context

ADR-0023 fixed the join string's semantics — version prefix, `{addr, ca_sha256, token, exp}` payload, integrity checksum, ~120 characters — and delegated the exact encoding. Both ends are independent implementations (control composes with its dependencies available; the node parses stdlib-only, no shared import across the component boundary), so the format must be trivial to mirror and a cross-component round-trip test must pin the two together. This is a delegated decision from the M7 brief (first-run setup, node provisioning, storage partition).

## Decision

### Encoding (version `ozjoin1`)

```
ozjoin1:<base64url, unpadded, of payload || checksum>

payload  = exp:u32be || ca_sha256:32 raw bytes || token:16 raw bytes || addr:utf-8 (rest)
checksum = first 4 bytes of SHA-256(b"ozjoin1" || payload)
```

- Fixed-width binary fields before the one variable field: no delimiters, no JSON, no length bytes. A typical `ip:port` addr yields ~105 characters total.
- `addr` is the **bootstrap listener** address (`host` or `host:port`); the HTTPS exchange port rides in the canonical control URL the listener serves, so both nonstandard ports round-trip with one addr field.
- `exp` is unix seconds as unsigned 32-bit big-endian (join tokens live an hour, not decades; the 2106 rollover outlives this format).
- The checksum keys on the version prefix, so a payload re-labelled under a future version never validates. It is integrity against copy-paste truncation, not authentication — the CA fingerprint and the token are the security.
- The composer is `theozolith_control.joinstring`; the parser is `theozolith_nodedaemon.provisioning`. A nodedaemon test composes with the control module and parses with the node module.

### Error-text catalogue (node side, fail closed and loud)

| Condition | Text (prefix) |
| --- | --- |
| wrong prefix, bad base64, truncation, checksum mismatch | `malformed join string: <detail>` |
| `ozjoinN` for N ≠ 1 | `unsupported join-string version 'ozjoinN' — this node distribution speaks ozjoin1; update it` |
| fetched CA hashes wrong | `CA fingerprint mismatch: possible MITM, or a stale join string after a CA rotation … Nothing was transmitted to the control channel.` |
| exchange answers 401 | `join token rejected (expired, consumed, or revoked) — nothing was persisted on this node …` |

Expiry is **not** checked node-side before the exchange: the server's clock is the authority (ADR-0023: expired/consumed/revoked reject cleanly *after* TLS), and the three cases are deliberately indistinguishable off-box. `--inspect` displays the expiry for debugging.

## Alternatives Considered

- **JSON payload**: doubles the length (the brief's ~120-char budget exists so the string survives terminals and chat clients intact) and invites schema drift between the two stdlib-only ends.
- **CRC32 checksum**: SHA-256 is already imported on both ends for the fingerprint; a second algorithm buys nothing.
- **Separate fields for both ports**: the control URL already carries the HTTPS port canonically; duplicating it creates a disagreement channel.
