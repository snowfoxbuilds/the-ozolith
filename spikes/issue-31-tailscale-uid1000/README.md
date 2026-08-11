# Spike: uid-1000, capability-free tailscaled (issue #31 gate)

Runnable harness for the Step-0 gate of
[#31](https://github.com/snowfoxbuilds/the-ozolith/issues/31): verify that
`tailscaled` running as **unprivileged uid-1000 `ozolith`** — no TUN device,
no `NET_ADMIN`, no added capabilities, `--tun=userspace-networking` — still
delivers working Tailscale SSH one-hop access, a state-volume-stable
identity, and file-form auth-key hygiene.

This is spike scaffolding, not product code, and it must not grow into the
production `flightdeck-start` lifecycle (that lands via #31 with its own
seven-point lifecycle and executable tests). It lives in the repo so the
gate procedure is reviewed and versioned; the *results* are recorded as a
sanitized comment on #31. The dev/CI container cannot run it (no Docker
engine; seccomp denies namespace creation even to root), so it runs
out-of-band on a Docker-capable host that is on the tailnet.

## Prerequisites

- A Docker-capable host on your tailnet.
- A second tailnet machine to ssh from.
- A **reusable** auth key (`tskey-auth-...`) in a file, one line. A plain
  reusable key is fine for the spike; the production key policy (tagged
  `tag:flightdeck`, ACL-bounded) is a separate concern.
- Tailnet ACLs allowing SSH to the spike node as user `ozolith` with
  `action: accept` (`check` breaks headless one-hop).

## Run

```sh
./run-spike.sh /path/to/authkey-file
```

`run-spike.sh` builds the image and starts the container with **no**
`--cap-add`, **no** `--device`, **no** `--privileged` — that absence is the
experiment. The key reaches the container only as a read-only file mount.

If the gate fails, capture the exact error — the failure mode is the
finding. Do **not** retry with `--cap-add`/`--device`: per #31, the
privileged fallback is a human/ADR decision, not a spike workaround.

## The five checks (record each, sanitized, in #31)

### 1. Enrollment via file-form key, uid-1000, no caps

The container log shows `uid 1000`, then `up; status:` with the node
listed. No sudo exists in the image; docker run adds no capabilities.

### 2. One-hop SSH lands as ozolith in the container fs

From another tailnet machine:

```sh
ssh ozolith@spike whoami              # expect: ozolith
ssh ozolith@spike cat /spike-marker   # expect: spike-container-fs
```

### 3. Identity survives container/image replacement

Note the node's 100.x address (`tailscale status` output in the log). Stop
the container (Ctrl-C), optionally `docker build` again, re-run
`run-spike.sh`. Expect the log's "existing state found" branch, the same
100.x address, no new machine in the admin console, and ssh still landing.

### 4. State-volume loss permits deliberate re-enrollment

```sh
docker volume rm spike-tailscale-state
./run-spike.sh /path/to/authkey-file
```

Expect the fresh-enrollment branch to succeed with the same reusable key
and a new machine to appear; prune the stale one in the admin console
(that prune is part of the documented operator procedure).

### 5. No secret value outside the mounted file

```sh
docker history ozolith-ts-spike | grep -i tskey             # expect: nothing
docker inspect ozolith-ts-spike --format '{{.Config.Env}}'  # path only, no key
docker exec ozolith-ts-spike sh -c 'pgrep -a tailscale'     # file:... form only
```

Also confirm the container log printed no key value.

## Caveat worth recording

With `--tun=userspace-networking`, *inbound* Tailscale SSH works but
*outbound* dials from inside the container to the tailnet need the SOCKS5/
HTTP proxy. The Flight Deck use case is inbound-only, so this is fine —
but note it in #31 so nobody later assumes outbound works.

## Cleanup

```sh
docker rm -f ozolith-ts-spike 2>/dev/null
docker volume rm spike-tailscale-state
docker rmi ozolith-ts-spike
```

Then delete the spike machine from the tailscale admin console and, if you
minted a spike-only auth key, revoke it.
