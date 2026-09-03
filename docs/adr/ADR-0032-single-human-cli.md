Status: ACCEPTED

Date: 2026-07-30

# ADR-0032: One human CLI — `theozolith-control` folds into `theozolith`

## Context

ADR-0023 settled two human entry points on the Control Node: `theozolith` (fleet operator: `update`, `build`, `test`, `join-token`) and `theozolith-control` (service admin for the box: `init`, `serve`, `recover`, …). In practice the split confuses more than it protects: `theozolith-control` reads like the machine actors it sits beside (`theozolith-worker`, `theozolith-harness`, `theozolith-nodedaemon`) while being the *most* human-facing surface in the product, and the operator must remember which of two commands owns which verb when the two subcommand sets are disjoint anyway. The original danger-profile rationale (keep `rotate-key` far from `update`) is guard-flag territory — `init` already requires `--force` to re-run, passwords prompt — not entry-point territory.

## Decision

- **`theozolith` is the single human CLI.** All seventeen subcommands live under it: the service-admin half (`init`, `serve`, `recover`, `set-password`, `origin-init`, `tls-init`, `secret`, `command`, `unquarantine`, `status`, `flags`, `janitor`, `rotate-key`) and the fleet-operator half (`update`, `build`, `test`, `join-token`). No names collide; exit-code contracts are preserved (`ProductError` → 1, `ConfigError` → 2).
- **The merged parser lives in `cli.py`; `product.py` contributes via `register()`.** `product` must stay stdlib-only at module import for the `build.py` bootstrap (ADR-0030, unchanged); `cli` already imports `product`, so the dependency direction is free. `product.main` remains as a lazy delegate to the merged CLI, so `python -m theozolith_control.product` keeps working.
- **`theozolith-control` survives one release as a deprecated alias** — a second console script pointing at the same main — so existing compose entrypoints, shell history, and scripts keep working across the update. The reference container's `ENTRYPOINT` switches to `theozolith`.
- **Component names keep `theozolith-control`.** The pip distribution and the docker image names (`theozolith-control:local`, `ghcr.io/...`) identify the component, not the command, and do not change.
- **The naming rule going forward**: `theozolith` is what a human types on the Control Node; `theozolith-<component>` names a machine surface (`worker`, `reviewer`, `harness`, `validate-verdict`, `nodedaemon` — plus `nodedaemon provision`, the one node-side paste, which is machine-composed by `join-token create`) or a separable standalone component (`knowledge`, whose laptop-only install must not drag in the cluster manager, ADR-0007).

## Alternatives Considered

- **Keep the two-CLI split (status quo)**: the protection it buys is illusory (guards live on the subcommands), and the cost is real — a human surface named like a machine actor, and two top-level commands to remember for one box.
- **Namespaced `theozolith control <cmd>`**: an extra token on every daily command to resolve a collision that does not exist; the flat namespace is smaller than git's.
- **Fold `theozolith-knowledge sync` too**: `theozolith` ships in the control package; a laptop-only knowledge user would have to install the cluster manager and its dependency stack to sync skills — exactly what ADR-0007's separability rule forbids. Deferred, not decided against forever.
- **A new umbrella package dispatching to per-component CLIs**: real machinery (entry-point discovery, another installable) for what is, after this fold, a two-command product surface.
- **Renaming the distribution/image to match**: churn across compose files, CI, and node update lists with zero operator-facing gain; component identity and command identity are different names for different things.
