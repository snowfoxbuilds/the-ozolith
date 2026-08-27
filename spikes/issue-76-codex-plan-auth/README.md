# Issue #76 spike: Codex plan-auth viability + headless exec fixtures

Observation procedure for the Codex Reviewer's credential design (ADR-0052
§5). **Pre-merge execution was intentionally waived** (operator ruling):
production rollout is the validation environment, and these checks are
post-deployment observations — run them (or record the equivalent from
production Run evidence) so the answers land durably on issue #76. Three
questions:

1. **Does ChatGPT-plan (subscription) auth work for headless `codex exec` in
   a Run-container posture?** (S2 — if not, plan auth is unsupported
   headless and ADR-0052 §5's API-key question opens)
2. **How does `auth.json` behave under use** — which fields rotate, does the
   refresh token itself rotate, does a stale stored copy recover? (S3/S4 —
   picks credential design (a) Fernet-secret-per-Run vs (b) persistent auth
   volume)
3. **What exactly does the pinned CLI's `--json` stream look like** — model
   announcement, usage, error shapes, exit codes, sandbox-flag behavior?
   (S5–S8 — the fixtures the CodexAdapter parsers are written against)

Pinned CLI: `@openai/codex@0.150.0`. The evidence is only valid for the
pinned version; bump the Dockerfile deliberately and re-run.

## Hygiene model (proportionate #31 ethos)

- `auth.json` is a credential. It lives in the named Docker volume
  `spike-codex-auth` and, transiently, in the harness process's memory and
  spike containers' environment. It never appears in argv (`docker run -e
  CODEX_AUTH_JSON` is passed BARE — the value rides the environment, the
  same mechanism production `containers.py` uses), never in the build
  context, never on non-tmpfs host disk.
- Everything written to `evidence/` is sanitized first: every string leaf of
  the auth document is scrubbed, plus a belt-and-braces pass over JWT-shaped
  and long token-shaped substrings. Raw output only ever exists in a 0700
  mktemp directory on `/dev/shm`, removed on exit.
- One run at a time: a non-blocking `flock` on the `/dev/shm` directory
  inode (no lock file is ever created).
- Nothing foreign is deleted. S1 refuses to overwrite captured auth state
  without `--force-login`. When the spike is done, revoke the credential
  with `codex logout` (or from the ChatGPT account's connected-devices page)
  and `docker volume rm spike-codex-auth`.

## Procedure

Needs a Docker-capable Linux host (root-daemon Docker; the dev container
blocks the needed namespaces — run this on the host, like #31).

```sh
./run-spike.sh build
./run-spike.sh s1-login          # interactive device-code login (one time)
./run-spike.sh s2-headless       # headless exec with materialized auth (the credential-design question)
./run-spike.sh s3-rotation 4 1800   # 4 runs, 30 min apart; prints rotated fields
./run-spike.sh s4-stale          # original S1 copy still authenticates?
./run-spike.sh s5-fixtures       # success / auth-fail / bad-model / tool-call streams
./run-spike.sh s6-sandbox        # sandbox flag matrix
./run-spike.sh s7-config MODEL [EFFORT]   # config.toml binding per candidate model
./run-spike.sh s8-version
```

Re-run `s4-stale` again after a multi-day gap before calling S4 passed.
`s3-summarize` recomputes the rotation report from existing evidence.

## Evidence checklist (comment on #76)

- [ ] S1: auth.json field names (from `s1-auth-fields.log` — names+hashes only)
- [ ] S2: `CODEX_EXIT`, prompt round-trip, no interactive prompt (`s2-headless.log`)
- [ ] S3: rotated-field report; whether the CLI wrote auth.json mid-run
- [ ] S4: stale-copy verdict (immediately after rotation AND after a multi-day gap)
- [ ] S5: four sanitized stream fixtures; where the model is announced (if
      anywhere); usage location; exit codes per outcome
- [ ] S6: which sandbox flags a Run container needs
- [ ] S7: config binding verdict per candidate (model, effort) pair
- [ ] S8: `codex --version` output shape
- [ ] Decision: design (a) confirmed, or the design-(b) amendment filed (ADR-0052 §5)

Copy the relevant `evidence/*.log` files into the comment — they are
sanitized by construction, but read them before posting anyway.

## Files

- `Dockerfile` — spike image: run-image posture (uid-1000 `ozolith`,
  python:3.13-slim), pinned codex CLI, no theozolith packages.
- `entrypoint.sh` — in-container step: materializes `CODEX_AUTH_JSON` into a
  fresh 0700 `CODEX_HOME` as a 0600 `auth.json` (exactly the
  `CodexAdapter.prepare()` sequence), runs one `codex exec`, reports
  names+hashes and the true exit code.
- `run-spike.sh` — host driver (steps above).
- `test-run-spike.sh` — stubbed regression suite (no Docker, no network, no
  credential); wired into the CI `spike-harness` job.
