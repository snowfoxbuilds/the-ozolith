# configs-example — a starter Config Repo

A complete, minimal Config Repo (ADR-0006): copy it to your Control Node's
`configs/` and edit in place. It declares two pipeline workers (Implementer,
Reviewer) as process Stacks and one **Flight Deck** as an interactive container
Stack. Everything with a placeholder — image digests, the knowledge URL, secret
values — is yours to fill in.

This directory is the sanctioned private-side surface for deployment detail the
product never carries (the guardrail test enforces that boundary): network
transports, site-specific wiring, and the Flight Deck's baked start sequence
all live here, never in product code or images.

## Reaching a Flight Deck

Attach from the Control Node's **web terminal** (the Stack's `attach` argv in
`stacks/flightdeck.toml`), or SSH to the node's host and `docker exec` into the
container — two hops.

**One-hop remote access** (SSH/VSCode straight into the container as
`ozolith`, issue #20 §1) is not wired in this example yet: it is gated on the
issue #24 Step 0 privilege spike (userspace networking as uid-1000 with no
added capabilities), which needs a Docker-capable box to run. The split-out
work — including the settled enrollment-key policy (a reusable, tagged,
ACL-bounded key stored encrypted control-side, delivered read-only from tmpfs,
never durable plaintext) and the required container start lifecycle — is
tracked as **issue #31**.
