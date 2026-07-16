# deploy

Running both drivers — the Worker and the Reviewer — on one box (M2). The drivers are
**plain host processes** that create one ephemeral run container per Run / review round
(ADR-0013); compose does not run the actors. No Control Node and no Node Daemon yet:
GitHub-only operation is the permanent degraded mode (ADR-0002), and daemon supervision
lands in M3.

Deployment footprint (the deletion test, NODE-SUBSTRATE.md): **docker + the TheOzolith
package + a `.env`** — nothing else.

## Setup

1. Bootstrap the target repo (labels + issue forms):

   ```sh
   GITHUB_TOKEN=... theozolith-bootstrap --repo owner/name
   ```

2. Build the run-container image (from the repo root):

   ```sh
   docker build -f worker/docker/Dockerfile.claude -t theozolith-run-claude:local .
   # optional: --build-arg KNOWLEDGE_SOURCE=... --build-arg KNOWLEDGE_PIN=...
   # optional: --build-arg OZOLITH_UID=$(id -u)   # match the driver user
   ```

3. Install the package on the box and configure:

   ```sh
   pip install ./knowledge ./worker
   cp deploy/.env.example .env    # fill in both PATs and the model API key
   ```

   Every variable honors the VAR_FILE convention: `<NAME>_FILE=/run/secrets/<name>`
   reads the value from a file instead. One `.env` serves both drivers: the
   role-prefixed variables (`WORKER_GITHUB_TOKEN`, `REVIEWER_GITHUB_TOKEN`,
   `WORKER_MODEL`, `REVIEWER_MODEL`, …) route to the right driver.

4. Run the drivers (don't run them as root):

   ```sh
   set -a; . ./.env; set +a
   theozolith-worker &      # or: theozolith-worker --once   (single dev pass)
   theozolith-reviewer &    # or: theozolith-reviewer --once
   ```

   `--once` performs a single poll(-claim-run) pass and exits — the daemon-less dev mode.
   Optional systemd units live in `deploy/systemd/` (a convenience only; daemon-grade
   supervision is M3).

## Job-dir ownership

Run containers write into the bind-mounted job directory (`THEOZOLITH_JOBS_DIR`). Keep
the files owned by the driver user either by building the image with
`--build-arg OZOLITH_UID=$(id -u)` or by setting
`THEOZOLITH_CONTAINER_USER=$(id -u):$(id -g)` in `.env`.

## Observing and attaching

- Live containers: `docker ps --filter label=theozolith.owner` — names are
  `ozolith-run-<run-id>` and `ozolith-review-<pr>-round-<n>`.
- Attach to any live agent session (input is permitted and lands in the transcript):

  ```sh
  docker exec -it ozolith-run-<run-id> tmux attach
  ```

  Detach with `C-b d`. No live run container = nothing to attach to.
- Evidence bundles (incl. full session transcripts): branch `theozolith/evidence`
  in the target repo, `runs/issue-<N>/`.

## Cleanup / deletion test

```sh
pkill -f theozolith-worker; pkill -f theozolith-reviewer   # stop the drivers
docker ps -aq --filter label=theozolith.owner | xargs -r docker rm -f
docker volume rm theozolith-cache                          # named cache volumes
pip uninstall theozolith-worker theozolith-knowledge
rm .env
```

After this the box is clean. A driver killed mid-Run leaves an orphaned run container
(identifiable by its `theozolith.*` labels) and a stale issue claim — manual cleanup in
M2; the Node Daemon's reaper and the zombie-claim janitor land in M3.
