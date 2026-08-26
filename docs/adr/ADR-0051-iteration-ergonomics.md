Status: ACCEPTED

Date: 2026-08-26

Provenance: operator ruling (2026-08-26) — the from-source iteration loop was three manual rituals (hand-push the base image, re-pin the product in the Config Repo, run two build commands). Amends ADR-0030/0041 (the bootstrap shim publishes), ADR-0048 (ingest preserves an undeclared product pin); consumes ADR-0049 (the `:main` base stays private behind the managed `registry:<host>` credential). ADR-0023's "`theozolith build` cannot be the first command — it presupposes an installed CLI" is unaffected: the shim still installs first.

# ADR-0051: Iteration ergonomics — CI-published base, preserved product pin, one-step build

## Context

Three frictions sat on the path from "edit the source" to "the fleet runs it", each a hand ritual that the machinery around it had already made unnecessary.

**The base image was hand-pushed.** Every worker type and the Flight Deck derive from one base, `theozolith-run-claude` (worker/docker/Dockerfile.claude), which `COPY`s `worker/` and `knowledge/` — so nearly every product change invalidates the published image. CI proves the image still builds on every push (the `run-image` job, buildx + a GHA layer cache) and then discards it; publishing was a human running `docker build` + `docker push` with a personal PAT, and the config-side machinery that consumes a published tag (tag-only `base` refs, ingest tag→digest resolution per ADR-0048, the `registry:<host>` pull credential per ADR-0049) sat waiting for pushes that only happened when someone remembered.

**The product pin fought the update flow.** `theozolith build`/`theozolith update` write the product pin into the pinned build's `product.toml` (ADR-0024/0048), but ingest copies the human Config Repo's `product.toml` verbatim into staging and commits the whole staging tree — so the next `theozolith config ingest` reverted the pin to whatever stale value the Config Repo carried, and every product update obligated a manual pin bump in config-src. Worse, a Config Repo with **no** `product.toml` — including the one `theozolith init` scaffolds — silently *deleted* the pin from the pinned build; at the next serve start `ensure_pin` re-resolved it to the latest published release, a silent fleet retarget nobody asked for.

**The build was two commands doing the work twice.** `sudo python3 build.py` builds the four wheels and installs them on this box (ADR-0041); `sudo theozolith build` builds the same four wheels again from scratch, uploads them, and pins the version. ADR-0030 rejected chaining the two because "the bootstrap box may be the Control Node being born" — a real objection to *unconditional* chaining, not to a chain that knows when the Control Node does not exist yet.

## Decision

### 1. CI publishes the base image on merges to main

A dedicated `publish-run-image` job in `.github/workflows/ci.yml` — `needs: run-image`, gated to `push` events on `refs/heads/main`, with job-level `permissions: packages: write` and a GITHUB_TOKEN registry login — pushes `ghcr.io/snowfoxbuilds/theozolith-run-claude:main` plus an immutable `ghcr.io/snowfoxbuilds/theozolith-run-claude:sha-<sha>` whenever `worker/`, `knowledge/`, or the workflow itself changed. The package stays private.

- **A separate job, not a conditional push on `run-image`**: `permissions` is job-scoped, and the check job runs on every PR and branch push — `packages: write` must never ride it. `run-image`'s contract is unchanged: building is the whole check, nothing is pushed.
- **Path relevance is decided inside the job**, by diffing against `github.event.before`; the workflow's `on: push` stays unfiltered because the secret-scan job must see every ref (its comment is a standing invariant). An unresolvable diff base (force-push, first push) publishes rather than guesses — an extra push is harmless, a silently skipped one is not.
- **The publish rebuild is warm, not duplicated**: it reads the `run-image` GHA cache scope (`cache-from` only; the check job already writes `mode=max`, and a second writer on one scope buys nothing).
- **No build args**: since ADR-0048, knowledge is compiled at ingest and baked into *derived* images on nodes — the published base carries none (`KNOWLEDGE_SOURCE`/`KNOWLEDGE_PIN` remain a dev provision), and `OZOLITH_UID` keeps its default, matching the hand-pushed image.
- **The doctrine is unchanged.** Worker types reference the tag (`base = "…:main"`); each `theozolith config ingest` resolves the tag to the then-current digest into `pins.toml` (ADR-0048), authenticated by the managed `registry:ghcr.io` credential (ADR-0049). A fleet moves bases only when the operator ingests; between ingests it is digest-pinned. The `:sha-<sha>` tags exist for hand digest-pinning and rollback.
- **Operational prerequisite (one-time)**: the existing GHCR package was created by a hand push under a personal PAT; the repo's Actions token can push to it only after the package's settings grant this repo write access (Package settings → Manage Actions access). Until then the job fails loudly at the push step, whose comment names this remediation.

### 2. Ingest preserves an undeclared product pin (amends ADR-0048)

A source Config Repo that carries `product.toml` still wins — declarative release pinning is unchanged, divergence from a pin the update flow wrote is still surfaced in the report. A source that carries **no** `product.toml` no longer deletes the pin: ingest copies the pinned build's current `product.toml` forward into staging, and the report says so in both the real and dry-run paths. Absent in both trees stays absent.

- **This is the default, not a flag.** The preserving behavior is the only non-destructive reading of an absent declaration: "the Config Repo does not manage the product pin" (the update flow owns it), never "unpin the fleet". A flag would make the safe behavior opt-in and leave the scaffolded-repo deletion hole open.
- The starter Config Repo (`deploy/configs-example/`) ships **without** `product.toml`, teaching the ownership mode where `theozolith build`/`theozolith update` own the pin; its README documents both modes.

### 3. The bootstrap shim publishes (amends ADR-0030/0041)

After a successful install (and entry-point link publication in managed mode), `build.py` invokes the just-installed venv CLI as a subprocess — `<venv>/bin/theozolith build --source <checkout> --dist <checkout>/dist --if-initialized` — so `sudo python3 build.py` is the complete edit-to-fleet step on a Control Node box. `--no-publish` opts out.

- **`theozolith build --dist DIR`** uploads pre-built wheels instead of rebuilding: the wheel set is selected from DIR by the checkout's `source_version()` (the clean-tree check still applies first), and a missing or ambiguous component wheel at that exact version is a loud refusal — a persistent `dist/` accumulates prior SHAs' wheels, and version selection is the validation. This removes the duplicate multi-minute build ADR-0030's rejected alternative would have added.
- **`theozolith build --if-initialized`** turns exactly the two uninitialized-box shapes — no resolvable Control Node URL, no admin token, i.e. `statuscli.resolve_target`'s `TargetError` — into a printed skip with exit 0. This resolves ADR-0030's bootstrap objection where it belongs: target resolution has one implementation (flags beat env beat init artifacts, ADR-0039), and the shim never duplicates that policy by stat-ing token files or pattern-matching output; its whole contract with the publish is the subprocess exit code. Any failure *after* resolution — health check, upload, pin — stays loud: an initialized box that fails to publish fails the bootstrap command.
- **Subprocess, never in-process.** The shim imports `build_distribution` from the checkout (ADR-0030's stdlib-only boundary, unchanged, identity-test intact); the publish runs the *installed* CLI so checkout modules and installed modules never mix in one interpreter.
- The unmanaged `--venv` path runs the same publish through its own venv's CLI: an unconfigured dev box resolves no target and skips; an env-configured one publishes, which is the intent.

## Consequences

- **Positive**: base freshness is "merge to main, then re-ingest"; the default scaffold flow can no longer un-pin the fleet (and the `ensure_pin` latest-release retarget that followed is gone with it); a Control Node box goes from edited checkout to converging fleet with one command and one wheel build. The update loop that used to be four rituals is `git pull` (or merge) → `sudo python3 build.py` → `theozolith config ingest` when configs moved.
- **Negative**: two small new CLI flags (`--dist`, `--if-initialized`) and one shim flag (`--no-publish`) to hold; the shim gains a subprocess seam (pinned by the shim tests); a `:main` re-ingest moves the base digest whenever main moved — deliberate, that is what re-ingest means, but operators who want a frozen base must digest-pin or use a `:sha-<sha>` tag.
- **Neutral**: the published base is single-arch (amd64), matching the hand-pushed status quo — a multi-arch fleet adds QEMU + `platforms:` to the publish job when the need is real. `theozolith build` without flags behaves exactly as before (fresh build, loud failure on an uninitialized box). The init scaffold still writes a digest-placeholder base ref that a human resolves; pointing it at `:main` is a candidate follow-up, not part of this ruling.

## Alternatives rejected

- **Conditional push on the existing `run-image` job**: job-level `packages: write` would ride every PR and branch-push run of the check job; a job that holds a write credential should not be the one that runs on untrusted-adjacent events.
- **`paths:` filter on the workflow's `on: push`**: breaks the secret-scan invariant (every ref, every push — its header comment forbids narrowing).
- **Publish unconditionally on every main push**: noise pushes on doc-only merges; the in-job diff check is three lines.
- **An ingest flag for pin preservation** (`--keep-product-pin`): makes the non-destructive behavior opt-in, keeps the scaffolded-repo deletion hole, and adds a per-invocation ritual — the opposite of the goal. The declared-file-wins rule already covers operators who want the Config Repo to own the pin.
- **The shim stat-ing the admin-token file to decide skip-vs-publish**: duplicates `resolve_target`'s resolution policy, mis-skips boxes configured via `CONTROL_NODE_URL`/`THEOZOLITH_ADMIN_TOKEN`, and rots when the policy moves. The `TargetError`-to-skip conversion lives behind `--if-initialized` in the CLI, next to the one resolver.
- **build.py importing the CLI in-process for the publish**: mixes checkout `theozolith_control.*` with installed `theozolith_worker` in one interpreter — exactly the hazard the install-the-wheels path exists to avoid (ADR-0030).
- **`theozolith build` pip-upgrading the local box as the merge direction** (instead of the shim publishing): upgrading the venv the running process lives in is the messier seam, and it inverts ADR-0041 — the shim owns this box's environment; the CLI owns the fleet.
