Status: ACCEPTED

Date: 2026-07-13

# ADR-0004: Private depends on public — the product is self-contained, the private deployment hosts it

## Context

The pipeline shares hardware and monitoring patterns with the private Home Server project (Pi control node, node roles, Tailscale). The product (now TheOzolith; ADR-0007) may be open-sourced or sold later, so it must not entangle with private infrastructure.

## Decision

Dependency arrow points private to public only. TheOzolith ships the complete product: Worker images, the Control Node service, the Claim Protocol, compose files, and a documented .env config surface. The private deployment consumes pinned versioned product images via one private Config Repo (ADR-0006; originally thin roles.d handlers — archived in Historical Context). The Worker-to-Control-Node protocol is HTTP with bearer-token auth at a configured URL (TLS mandatory as of the 2026-07-13 secrets amendment; see ADR-0006); Tailscale is a deployment detail applied host-side or as a private compose overlay, never baked into product images. The private and product control services remain separate services on the same box (amended 2026-07-13, ADR-0005 — see Amendments).

## Consequences

- **Positive**: open-sourcing requires no disentangling; deployments are reproducible and rollbackable via pinned tags; the product is testable standalone (deletion test: the product runs anywhere with compose plus a .env); endpoint auth does not depend on network perimeter.
- **Negative**: two control services and dashboards on the Pi (superseded by ADR-0005); updates require an explicit version bump in the private Config Repo.
- **Neutral**: Tailscale remains the transport in the personal deployment.

## Alternatives Considered

- **Extend the homeserver control service with pipeline endpoints**: rejected — couples the public product to private infrastructure.
- **Shared control-plane library in a third repo**: rejected — premature abstraction for one consumer.
- **Single repo holding both public product and private configs**: rejected — cannot split public and private cleanly. (TheOzolith is a public-only monorepo; ADR-0007.)

## Amendments

- **2026-07-13 (ADR-0005)**: the private and product control services no longer remain separate services on the same box — TheOzolith's product owns the node substrate and the private deployment extends it instead.
