Status: ACCEPTED

Date: 2026-07-28

# ADR-0026: Bootstrap listener default port and process arrangement

## Context

ADR-0023 fixed the listener's shape — dedicated plaintext port, GET-only, closed route table of three inert values, never mounted on the HTTPS app — and delegated the default port and its systemd/socket arrangement (M7 brief).

## Decision

- **Default port 6965** ("OZOL" on a phone keypad — memorable, unprivileged, and colliding with nothing registered that matters on a trusted LAN). It is a tier-2 setting (`bootstrap_port` in `control.toml`); a nonstandard value rides the join-string addr.
- **Routes**: `/ca.pem` (the CA certificate, `application/x-pem-file`), `/origin`, `/control-url` (both `text/plain`). Everything else 404s; non-GET/HEAD methods answer 405. No auth, no state, no cookies, no logging.
- **Same process as `serve`, own socket, own thread** (`theozolith_control.bootstrap.BootstrapServer`, a stdlib `ThreadingHTTPServer`): no second systemd unit, no socket activation, no separate container. The Control Node is one service to run, restart, and reason about; the listener serves three static values and needs none of the app stack. It starts exactly when a CA exists to serve and dies with the process. The reference compose publishes `6965:6965` beside `443:8443`.

## Alternatives Considered

- **A separate systemd unit / socket activation**: a second lifecycle to keep in sync with the CA material for a server whose entire job is three `Content-Length`ed byte strings.
- **Mounting the routes on the HTTPS app with a port check**: exactly what ADR-0023 forbids — the fail-closed origin posture of ADR-0022 must not gain a plaintext carve-out.
- **A privileged port (80)**: needs root or capabilities for zero operator benefit; the join string carries the port anyway.
