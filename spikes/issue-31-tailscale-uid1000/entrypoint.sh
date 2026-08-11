#!/bin/sh
# Issue #31 gate commands, run as uid-1000 with no capabilities. This is spike
# scaffolding, not the production flightdeck-start lifecycle — but it already
# follows the two rules the removed draft broke that matter for a correct
# verdict: the enrollment decision is made BEFORE tailscaled launches (no race
# on state-file creation), and every failure exits non-zero instead of looping
# or idling, so a failed gate is unmissable.
set -u

echo "==> running as uid $(id -u) ($(id -un)); expecting 1000/ozolith"

# Decide the branch before the daemon can touch the statedir.
if [ -f /var/lib/tailscale/tailscaled.state ]; then
  ENROLL=no
  echo "==> existing state found: reusing identity, no auth key (gate item 3)"
else
  ENROLL=yes
  echo "==> empty statedir: fresh enrollment via file-form auth key (gate item 1)"
fi

tailscaled \
  --tun=userspace-networking \
  --statedir=/var/lib/tailscale \
  --socket=/tmp/tailscaled.sock &
TAILSCALED_PID=$!

i=0
while [ ! -S /tmp/tailscaled.sock ]; do
  kill -0 "$TAILSCALED_PID" 2>/dev/null || {
    echo "FAIL: tailscaled exited before creating its socket" >&2; exit 1; }
  i=$((i + 1))
  [ "$i" -le 30 ] || { echo "FAIL: socket not ready after 30s" >&2; exit 1; }
  sleep 1
done

if [ "$ENROLL" = yes ]; then
  tailscale --socket=/tmp/tailscaled.sock up \
      --ssh \
      --hostname="${SPIKE_HOSTNAME:-spike}" \
      --auth-key="file:${TS_AUTHKEY_FILE:?TS_AUTHKEY_FILE not set}" \
    || { echo "FAIL: enrollment failed (gate item 1)" >&2; exit 1; }
else
  tailscale --socket=/tmp/tailscaled.sock up \
      --ssh \
      --hostname="${SPIKE_HOSTNAME:-spike}" \
    || { echo "FAIL: up with existing state failed (gate item 3)" >&2; exit 1; }
fi

echo "==> up; status:"
tailscale --socket=/tmp/tailscaled.sock status

echo "==> ready — from another tailnet machine: ssh ozolith@${SPIKE_HOSTNAME:-spike}"

# Stay alive for the SSH checks, but die visibly if the daemon dies.
while kill -0 "$TAILSCALED_PID" 2>/dev/null; do sleep 5; done
echo "FAIL: tailscaled exited post-enrollment" >&2
exit 1
