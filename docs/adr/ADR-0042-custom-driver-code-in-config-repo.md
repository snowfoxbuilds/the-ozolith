Status: ACCEPTED

Date: 2026-08-08

Provenance: grilling 2026-08-06/08 (driver customization thread).

# ADR-0042: Custom driver code in the Config Repo, delivered as a hash-pinned config distribution

## Context

ADR-0020 made inheritance the code-level extension surface (custom worker types extend the
base Worker), but no delivery mechanism existed: nodes never pull source or build the product,
and process-kind Stacks ran only commands from the product distribution (ADR-0013). Custom
worker definitions are operator content — hosting them in a fork of the public product repo
would publish them, violating the ADR-0004 boundary (all personal content lives in the private
repo). That boundary outranks ADR-0007's "pure data, no machinery" charter.

## Decision

- Custom driver code lives in the private Config Repo under drivers/. Never secret values.
- On config change, the Control Node packages drivers/ into a hash-pinned config-distribution
  artifact, served over the same artifact-pull path as theozolith build output. The heartbeat
  channel carries only the drivers-hash reference (channel invariant intact in substance).
- The Node Daemon reconciles the drivers-hash like the product pin; off-hash nodes are
  dispatch-ineligible until convergence.
- Worker-type definitions name their driver: driver = "builtin:<name>" (product distribution)
  or "drivers/<name>" (config distribution). ADR-0013's process-Stack wording amends to
  "a native command from the product distribution or the deployment's config distribution".
- Defaults are referenced, never copied: no built-in driver file exists in the Config Repo.
  A fork is a deliberate copy from the product checkout at the pinned version, stamped with
  its ancestor product version in a header.
- Stable extension API: theozolith.worker.api exports the base Worker, built-in type classes,
  and job-directory/claim-protocol interfaces. Everything else is internal, no stability
  promise; api-module changes are release-note events.
- Version skew is advisory: the artifact records the product version it was built against;
  mismatch is reported in heartbeats (status/flags), never fail-closed. Real breakage crashes
  at driver start into existing supervision, theozolith.error, and quarantine (ADR-0016).

## Consequences

- Config Repo write access now equals code execution with driver credentials on nodes.
  drivers/ is git-native only: the web UI and any future config editor refuse to touch it.
- Amends ADR-0007 (charter: operator content, not just data), ADR-0013 (process command
  source), and the NODE-SUBSTRATE extension-surface doctrine (single enumerated code
  exception to "all config, no code hooks").
- Routine product updates can leave a custom driver on a stale API; this surfaces as an
  advisory stamp, then a visible crash — never a silent wrong behavior guarantee.

## Alternatives

- Product-checkout fork via theozolith build: zero new machinery, but publishes custom
  worker definitions (public repo). Rejected on ADR-0004.
- Private package pinned in config: second artifact pipeline for a single operator. Rejected.
- Code riding the command channel as payload: breaks the channel invariant in substance.
- Defaults mirrored into drivers/ with overwrite-on-update: shadowing ambiguity plus updater
  commits in operator git history. Rejected.
- Fail-closed on stamp mismatch: every product update bricks custom-driver Stacks. Rejected.