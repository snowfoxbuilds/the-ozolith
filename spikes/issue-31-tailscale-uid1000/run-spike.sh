#!/usr/bin/env bash
# Run the issue #31 gate on a Docker-capable host (e.g. the machine that runs
# the snow-maker dev containers).
#
# Usage: ./run-spike.sh /path/to/authkey-file
# The file holds one line: a REUSABLE tailscale auth key (tskey-auth-...).
# A plain reusable key is fine for the spike; the production key policy
# (tagged tag:flightdeck, ACL-bounded) is a separate concern.
set -euo pipefail

AUTHKEY_FILE=${1:?usage: run-spike.sh /path/to/authkey-file}
AUTHKEY_FILE=$(realpath "$AUTHKEY_FILE")
[[ -s "$AUTHKEY_FILE" ]] || { echo "auth key file is empty: $AUTHKEY_FILE" >&2; exit 1; }

cd "$(dirname "$0")"
docker build -t ozolith-ts-spike .
docker volume create spike-tailscale-state >/dev/null

# Deliberately absent: --cap-add, --device, --privileged. The container gets
# nothing beyond Docker's default unprivileged capability set — that absence
# IS the experiment. The key reaches the container only as a read-only file
# mount; only its PATH appears in env/argv (gate item 5).
exec docker run --rm --name ozolith-ts-spike \
  -e TS_AUTHKEY_FILE=/run/secrets/ts-authkey \
  -e SPIKE_HOSTNAME=spike \
  --mount "type=bind,source=${AUTHKEY_FILE},target=/run/secrets/ts-authkey,readonly" \
  -v spike-tailscale-state:/var/lib/tailscale \
  ozolith-ts-spike
