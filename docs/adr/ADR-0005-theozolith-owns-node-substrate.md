Status: ACCEPTED

Date: 2026-07-13

# ADR-0005: TheOzolith owns the node substrate; the private deployment extends it

## Context

TheOzolith (coding Workers) and the private Home Server project both need the same substrate: nodes that heartbeat to a central dashboard, receive desired-state commands, and run docker stacks. ADR-0004 originally kept two separate control services on the Pi, duplicating that substrate.

## Decision

TheOzolith ships the node substrate as part of the product: the monitoring dashboard, the heartbeat/command mechanism (heartbeat responses carry infrastructure commands: drain, recycle, update), and docker stack lifecycle via a per-box Node Agent. The private deployment becomes a downstream consumer: private stacks (NAS, media, cron jobs) are privately defined workloads run by the Node Agent, and overlays (Tailscale, WoL/power handling) stay private-side.

Substrate admission rule: a feature enters the product only if an external adopter of a coding-worker fleet would want it. Everything else is implemented private-side against extension points (custom stacks, overlays, out-of-band scripts).

Infrastructure command authority sits with the Control Node; issue coordination stays on GitHub. ADR-0002 is unchanged.

## Consequences

- **Positive**: one control service and one dashboard instead of two; the legacy Home Server node agent is retired in favor of the product's Node Agent; the substrate gets dogfooding from a second real consumer; the open-source story improves because the fleet layer is part of the product.
- **Negative**: the product's surface grows beyond the pipeline (fleet agent, command protocol) with more to maintain and document; constant pressure for private needs to leak into public interfaces, held back only by the admission rule; the private deployment must migrate its existing setup scripts and agent onto the Node Agent.
- **Neutral**: ADR-0004's private-to-public dependency rule is unchanged and strengthened; ADR-0004's two-separate-control-services consequence is superseded.
## Alternatives Considered

- **Two separate control services (ADR-0004 original)**: rejected — duplicated heartbeat/dashboard code once both systems exist.
- **Extract the substrate into a third shared repo**: rejected — packaging overhead with no independent consumer; the product needs the substrate anyway and is its natural home.
- **The private deployment as a full extension of the product (all workloads in-product)**: rejected — non-coding workloads (NAS, media, power) would warp the public product's scope.
