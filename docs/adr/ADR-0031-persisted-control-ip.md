Status: ACCEPTED

Date: 2026-07-28

# ADR-0031: The persisted control IP and init address confirmation

## Context

The node channel is IP-only by ruling (ADR-0023, as amended: the node channel is IP-only, the hostname origin is browser-only): the join exchange answers the IP-based control URL, nodes persist and dial it, and no DNS or rediscovery machinery exists. That makes the control IP load-bearing deployment state — it feeds every join-string mint (CLI, API, dashboard), the bootstrap listener's `/control-url`, and the server-certificate SAN. Detecting it at mint time is exactly the bug being fixed: inside the reference compose container, `detect_host_ip()` returns the Docker bridge IP, poisoning every printed address. Two decisions were delegated: the persisted IP's home, and how init avoids silently shipping a wrong address.

## Decision

### Home: a read-only `[control]` field in control.toml

```toml
[control]
public_origin = "https://<slug>.theozolith.internal"   # what browsers dial
control_ip = "192.168.1.20"                            # what nodes dial
```

- Written by `theozolith-control init` (and `recover --ip` on an address change), validated as an IP literal, committed with the fixed author identity (`theozolith: control ip <ip>`), rendered read-only in the settings form, refused by the settings write path — the exact `public_origin` precedent under the ADR-0029 fixed schema.
- It must survive backup/recovery (ADR-0024), and `configs/` is precisely the durable, restored half; the origin already proved the pattern.
- Every mint surface reads it; **no mint path calls `detect_host_ip()`** — this also closes the SAN-drift seam: the address in every join string is the address in the certificate SAN, so the exchange TLS always verifies.
- The operator CLI's URL fallback prefers `https://<control_ip>[:port]` over the slug origin: the machine channel keeps zero DNS dependency end to end.

### Init address confirmation: refuse-and-require `--ip` inside a container

- `init` with no `--ip` **refuses to run inside a container** (`/.dockerenv` or `/run/.containerenv` present) with an error naming the fix: `docker compose run --rm control init --ip <LAN-IP>`. The documented compose flow passes `--ip` explicitly.
- Outside a container, auto-detection remains the default and the chosen address is printed prominently in the handoff (`re-run with --ip` if wrong) — a bare-metal box's outbound address is almost always right, and a confirmation prompt on every init would punish the common case to guard the containerized one, which the refusal already guards deterministically.
- `recover` never auto-detects: it uses the restored `control_ip` unless `--ip` overrides it, persisting the override and warning that a changed IP costs one join-string re-paste per node — and that those nodes will **not** appear in the unregistered view (their heartbeats go to the dead address and never arrive).

## Alternatives Considered

- **A flat file under `secrets/`**: the IP is not secret, and `secrets/` is the never-in-git sibling — putting routable-address configuration there muddies the partition's durability-class legibility (ADR-0024).
- **A `[settings]` tier-2 key**: would make the IP dashboard-editable, silently desynchronizing it from the certificate SAN; re-pointing the node channel is a deliberate CLI act (`recover --ip`), like re-pointing the origin.
- **Confirmation prompt on every init**: interactive friction on the common bare-metal case; the container case — the only one that reliably detects wrong — is caught deterministically by the refusal.
- **Detect-and-warn inside containers**: a warning above a correct-looking handoff is exactly how a bridge IP ships to a LAN; refusal is the only output that cannot be pasted onward.

## Relevant PRs

- #9 — review (findings 1–2) that produced the same-day "Node channel addressing" ruling (ADR-0023, as amended) this ADR's delegated decisions implement.
