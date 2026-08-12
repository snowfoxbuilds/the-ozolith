#!/bin/sh
# Issue #31 gate commands, run as uid-1000 with no capabilities. This is spike
# scaffolding, not the production flightdeck-start lifecycle — but it already
# follows the two rules the removed draft broke that matter for a correct
# verdict: the enrollment decision is made BEFORE tailscaled launches (no race
# on state-file creation), and every failure exits non-zero instead of looping
# or idling, so a failed gate is unmissable. Every required command below is
# individually mandatory: "==> ready" can only print after ALL of them passed.
set -u

# SPIKE_* path/timing knobs exist only so the stubbed self-test
# (test-run-spike.sh) can exercise this script outside a container; a real
# container run sets none of them and uses these defaults.
STATEDIR=${SPIKE_STATEDIR:-/var/lib/tailscale}
SOCKET=${SPIKE_SOCKET:-/tmp/tailscaled.sock}
READY_SECS=${SPIKE_READY_SECS:-30}
POLL_SECS=${SPIKE_POLL_SECS:-5}

echo "==> running as uid $(id -u) ($(id -un)); expecting 1000/ozolith"

# The exact binary under test goes into the evidence: the gate verdict is
# only valid for the release PR #35 pins (the Dockerfile installs precisely
# that sha256-verified archive). Mandatory: an unrunnable binary is a failed
# gate, not a missing log line.
echo "==> tailscale version (must match the release the production example pins):"
tailscale version \
  || { echo "FAIL: tailscale version failed — the binary under test is unusable" >&2; exit 1; }

# Decide the branch before the daemon can touch the statedir. Only a
# NON-EMPTY tailscaled.state means a reusable identity; a missing or empty
# file (what a failed earlier attempt leaves behind) means fresh enrollment.
# A corrupt non-empty file must fail VISIBLY in `up` below — state is never
# deleted or regenerated automatically.
if [ -s "$STATEDIR/tailscaled.state" ]; then
  ENROLL=no
  echo "==> existing state found: non-empty tailscaled.state — reusing identity, no auth key (gate item 3)"
else
  ENROLL=yes
  echo "==> empty statedir: no non-empty tailscaled.state — fresh enrollment via file-form auth key (gate item 1)"
fi

tailscaled \
  --tun=userspace-networking \
  --statedir="$STATEDIR" \
  --socket="$SOCKET" &
TAILSCALED_PID=$!

# Ready means BOTH: the socket exists AND the LocalAPI answers a request —
# `status --json` exits 0 for any backend state once the daemon serves, so it
# works pre-login. Bounded, and daemon death is detected immediately instead
# of being reported as a timeout.
i=0
until [ -S "$SOCKET" ] && tailscale --socket="$SOCKET" status --json >/dev/null 2>&1; do
  kill -0 "$TAILSCALED_PID" 2>/dev/null || {
    echo "FAIL: tailscaled exited before its LocalAPI became ready" >&2; exit 1; }
  i=$((i + 1))
  [ "$i" -le "$READY_SECS" ] || {
    echo "FAIL: LocalAPI not ready after ${READY_SECS}s" >&2; exit 1; }
  sleep 1
done

if [ "$ENROLL" = yes ]; then
  tailscale --socket="$SOCKET" up \
      --ssh \
      --hostname="${SPIKE_HOSTNAME:-spike}" \
      --auth-key="file:${TS_AUTHKEY_FILE:?TS_AUTHKEY_FILE not set}" \
    || { echo "FAIL: enrollment failed (gate item 1)" >&2; exit 1; }
else
  tailscale --socket="$SOCKET" up \
      --ssh \
      --hostname="${SPIKE_HOSTNAME:-spike}" \
    || { echo "FAIL: up with existing state failed (gate item 3)" >&2; exit 1; }
fi

echo "==> up; status:"
tailscale --socket="$SOCKET" status \
  || { echo "FAIL: post-up status failed — the daemon must answer after up" >&2; exit 1; }

echo "==> ready — from another tailnet machine: ssh ozolith@${SPIKE_HOSTNAME:-spike}"

# Stay alive for the SSH checks, but die visibly if the daemon dies.
while kill -0 "$TAILSCALED_PID" 2>/dev/null; do sleep "$POLL_SECS"; done
echo "FAIL: tailscaled exited post-enrollment" >&2
exit 1
