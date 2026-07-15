# deploy

Compose files and `.env.example` covering the full configuration surface of a TheOzolith
deployment. See `docs/specs/NODE-SUBSTRATE.md` (deployment boundary: with the private config
repo gone, TheOzolith must run anywhere with docker compose plus a `.env`).

## M2: Worker + Reviewer on one box

```sh
theozolith-bootstrap --repo <owner/name>   # once, against the target repo
cp .env.example .env                       # fill in the two PATs + API key
docker compose up --build -d
```

`docker-compose.yml` runs one Claude Worker and the Reviewer as two long-lived containers,
each with its own machine-user PAT (no self-grading by construction, ADR-0008). No Control
Node exists yet: GitHub-only operation is the permanent degraded mode (ADR-0002), so this
is a complete deployment, not scaffolding.

Secrets arrive via `.env` for now, but every variable already honors the `<NAME>_FILE`
convention — point `WORKER_GITHUB_TOKEN_FILE` (etc.) at a mounted file and drop the plain
variable when a secret store lands (M3+).

The agent process runs inside tmux in each container; attach to a live session with
`docker exec -it <container> tmux attach` (the M4 dashboard terminal uses the same path).
Workers recycle themselves by exiting after N Runs; `restart: unless-stopped` brings them
back fresh.
