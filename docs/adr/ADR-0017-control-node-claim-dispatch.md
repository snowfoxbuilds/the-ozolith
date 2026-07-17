Status: ACCEPTED

Date: 2026-07-17

# ADR-0017: Control Node claim dispatch — claim-write-through

## Context

M3 made the Control Node operationally load-bearing (secrets distribution, infrastructure commands, the zombie-claim janitor) while ADR-0002 still framed it as advisory in issue coordination. Per-driver GitHub polling duplicated discovery across the fleet, the assign-and-verify race dance existed only because claiming had no arbiter, and the advisory pre-filter left a window where GitHub showed a plan_ready issue that was effectively spoken for. Grilling 2026-07-17 resolved the contradiction.

## Decision

Claims dispatch through the Control Node, write-through to GitHub:

- Workers and the Reviewer request work from the Control Node instead of polling GitHub. One dispatch path for both actors; the Reviewer side is discovery-only (no claim label exists on PRs).
- The Control Node is the single writer of claim creation: it selects the issue, writes the claim to GitHub itself (assigns the Worker's GitHub login, adds in_progress), then returns the issue in the same response. Grants are serialized internally; assign-and-verify is deleted.
- GitHub remains the sole source of coordination truth. The Control Node reconciles to GitHub, never the reverse; hand-edited labels stay meaningful; a lost Control Node database rebuilds from GitHub.
- Claim release has three owners: the driver releases on every classified ending (completion, failure, empty PR); the Control Node releases claims it wrote that never activated (no claimed event within the activation window, ~60 seconds — a lost response or a driver death before pickup is otherwise invisible to every reaper, since a never-activated Run emits zero events); the janitor handles death after activation, past the zombie grace period.
- Availability: Control Node down = in-flight Runs finish and publish (drivers hold their own PATs for all non-claim GitHub writes); new claims and new review rounds pause. Richer fallback behavior is a backlog item.
- Dev mode: --once requires a reachable Control Node; running theozolith-control serve locally is the daemon-less path. No second claim path exists.
- Prerequisites: the Control Node's GitHub token graduates from optional (janitor-only) to required, and driver registration carries each driver's GitHub login.
## Consequences

- **Positive**: claim races are structurally impossible (single serialized writer); GitHub is never stale about ownership; one GitHub poller instead of N (rate-limit headroom); the Control Node's live-claim picture becomes first-party (it granted every claim it watches), tightening zombie detection.
- **Negative**: the Control Node is a single point of failure for pipeline throughput (accepted; fallback is backlog); it requires a write-scoped GitHub token and knowledge of driver identities; ADR-0002's "the pipeline ships PRs without it" narrows to "in-flight Runs ship without it".
- **Neutral**: supersedes ADR-0002 in part — GitHub-owns-coordination survives; the advisory claim path is retired. Residual ADR-0002 invariant: the Control Node never originates coordination beyond claim creation (it cannot approve, revise, or advance an issue); janitorial liveness corrections are the enumerated exception.
## Alternatives Considered

- **Advisory pre-filter (status quo, ADR-0002)**: rejected — the M3 reality already made the Control Node load-bearing, and the advisory posture bought availability the operator no longer wants at the cost of racy, duplicated discovery.
- **Control arbitrates, driver writes the claim (lease model)**: rejected — requires new lease/TTL logic, and GitHub is stale between grant and the driver's write; write-through removes both the staleness window and the lease machinery.
- **Control Node database as coordination authority (GitHub as rendering)**: rejected — breaks hand-edited labels and human workflows until a full control UI exists; makes the Control Node database load-bearing for cluster state; inverts rather than solves the two-sources-of-truth problem.
