#!/usr/bin/env bash
# Run the issue #31 gate on a Docker-capable host (e.g. the machine that runs
# the snow-maker dev containers). Needs a GNU/Linux host; run it however your
# docker socket access requires (e.g. `sudo ./run-spike.sh`).
#
# FRESH run (no spike-tailscale-state volume yet — gate items 1/4): the
# REUSABLE auth key (tskey-auth-...) is read from stdin — paste it at the
# hidden prompt, or pipe it in from a secret manager:
#
#   ./run-spike.sh                          # prompts, input hidden
#   pass show tailnet/spike | ./run-spike.sh
#
# The key is held ONLY in a mode-0600 temp file on a verified tmpfs
# (/dev/shm) for the duration of the run and removed on every exit path (the
# removal is printed as proof). It never enters argv, shell history, the
# build context, or any durable file. A file path may be given as $1 only if
# it already lives on a tmpfs; durable host files are refused — gate item 5
# forbids durable plaintext — as is any path inside the Docker build context.
#
# REUSE run (volume exists — gate item 3): no key is read and nothing secret
# is mounted; the retained-identity branch must work keyless.
#
# After you finish the SSH checks (press Enter), the script stops the
# container and sweeps every surface for the exact key value with grep -Ff,
# using the tmpfs key file as the PATTERN FILE — the value itself never
# enters argv or output: image history, container env + mount metadata
# (docker inspect), recorded process argv (docker top), the full container
# log, and every file on the retained state volume.
set -euo pipefail

cd "$(dirname "$0")"
CONTEXT_DIR=$(pwd -P)

IMAGE=ozolith-ts-spike
CONTAINER=ozolith-ts-spike
VOLUME=spike-tailscale-state

SPIKE_KEY_FILE=""
EVIDENCE_DIR=""
LOG_PID=""

cleanup() {
  status=$?
  trap - EXIT
  [[ -n "$LOG_PID" ]] && kill "$LOG_PID" 2>/dev/null || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  # Raw captures may only contain the key if a sweep FAILED — they live on
  # tmpfs and are removed either way. The volume and image are deliberately
  # retained (machine state, not secrets): checks 3/4 need them.
  [[ -n "$EVIDENCE_DIR" ]] && rm -rf "$EVIDENCE_DIR"
  if [[ -n "$SPIKE_KEY_FILE" ]]; then
    rm -f "$SPIKE_KEY_FILE"
    if [[ -e "$SPIKE_KEY_FILE" ]]; then
      echo "WARNING: temporary key file STILL EXISTS: $SPIKE_KEY_FILE" >&2
    else
      echo "==> temporary key file removed (proof: '! -e $SPIKE_KEY_FILE' holds)"
    fi
  fi
  exit "$status"
}
# No `exec docker run` anywhere: every path must return here so the traps
# can remove the temporary secret material.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

require_tmpfs() { # $1: a path whose filesystem must be RAM-backed
  local fstype
  fstype=$(stat -f -c %T "$1")
  case "$fstype" in
    tmpfs | ramfs) ;;
    *)
      echo "refusing: $1 is on '$fstype', not a tmpfs — a durable plaintext key violates gate item 5." >&2
      echo "          Pipe the key on stdin instead (see the header of this script)." >&2
      exit 1
      ;;
  esac
}

if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  MODE=reuse
else
  MODE=fresh
fi

if [[ "$MODE" == fresh ]]; then
  require_tmpfs /dev/shm
  SPIKE_KEY_FILE=$(mktemp /dev/shm/ozolith-ts-spike-key.XXXXXX)
  chmod 0600 "$SPIKE_KEY_FILE"
  if [[ $# -ge 1 ]]; then
    SRC=$(realpath -e "$1")
    case "$SRC" in
      "$CONTEXT_DIR"/*)
        echo "refusing: $SRC is inside the Docker build context ($CONTEXT_DIR) — a key there can end up in an image layer." >&2
        exit 1
        ;;
    esac
    require_tmpfs "$SRC"
    cat -- "$SRC" >"$SPIKE_KEY_FILE"
  elif [[ -t 0 ]]; then
    printf 'Paste the reusable auth key (input hidden): ' >&2
    IFS= read -rs SPIKE_KEY
    printf '\n' >&2
    printf '%s\n' "$SPIKE_KEY" >"$SPIKE_KEY_FILE" # printf is a builtin: no argv
    unset SPIKE_KEY
  else
    IFS= read -r SPIKE_KEY || true
    printf '%s\n' "$SPIKE_KEY" >"$SPIKE_KEY_FILE"
    unset SPIKE_KEY
  fi
  grep -q '[^[:space:]]' "$SPIKE_KEY_FILE" || {
    echo "no auth key provided" >&2
    exit 1
  }
elif [[ $# -ge 1 ]]; then
  echo "note: volume $VOLUME exists — reuse run; the key argument is ignored and not read" >&2
fi

echo "==> building the spike image (version- and sha256-pinned tailscale; see Dockerfile)"
docker build -t "$IMAGE" .

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
EVIDENCE_DIR=$(mktemp -d /dev/shm/ozolith-ts-evidence.XXXXXX)

# Deliberately absent: --cap-add, --device, --privileged. The container gets
# nothing beyond Docker's default unprivileged capability set — that absence
# IS the experiment. On a fresh run the key reaches the container only as a
# read-only mount of the tmpfs file; only its PATH appears in env/argv
# (gate item 5). On a reuse run nothing secret is mounted at all.
if [[ "$MODE" == fresh ]]; then
  docker volume create "$VOLUME" >/dev/null
  echo "==> fresh run: empty state volume — enrollment branch expected (gate items 1/4)"
  docker run -d --name "$CONTAINER" \
    -e TS_AUTHKEY_FILE=/run/secrets/ts-authkey \
    -e SPIKE_HOSTNAME=spike \
    --mount "type=bind,source=${SPIKE_KEY_FILE},target=/run/secrets/ts-authkey,readonly" \
    -v "$VOLUME":/var/lib/tailscale \
    "$IMAGE" >/dev/null
else
  echo "==> reuse run: retained state volume, keyless — identity branch expected (gate item 3)"
  docker run -d --name "$CONTAINER" \
    -e SPIKE_HOSTNAME=spike \
    -v "$VOLUME":/var/lib/tailscale \
    "$IMAGE" >/dev/null
fi

docker logs -f "$CONTAINER" 2>&1 | tee "$EVIDENCE_DIR/container.log" &
LOG_PID=$!

i=0
until grep -q '^==> ready' "$EVIDENCE_DIR/container.log" 2>/dev/null; do
  if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != true ]]; then
    sleep 1 # let the final log lines flush through tee
    echo "FAIL: container exited before reaching ready — the failure above is the finding." >&2
    echo "      Do NOT retry with --cap-add/--device: per #31 the privileged fallback is a human/ADR decision." >&2
    exit 1
  fi
  i=$((i + 1))
  [[ "$i" -le 120 ]] || {
    echo "FAIL: container not ready after 120s" >&2
    exit 1
  }
  sleep 1
done

# Argv/metadata snapshots while the daemon is live (docker top uses the
# host's ps — the transient `tailscale up` argv carried only the file: path,
# visible in the log above).
docker top "$CONTAINER" >"$EVIDENCE_DIR/top.txt"
docker inspect "$CONTAINER" >"$EVIDENCE_DIR/inspect.json"

echo
echo "==> container is up. From ANOTHER tailnet machine run the SSH checks:"
echo "      ssh ozolith@spike whoami              # expect: ozolith"
echo "      ssh ozolith@spike cat /spike-marker   # expect: spike-container-fs"
echo "==> when done, press Enter here to stop the container and run the key-absence sweep."
if [[ -r /dev/tty ]]; then
  IFS= read -r _ </dev/tty
else
  echo "==> no controlling tty: waiting 300s for the SSH checks, then sweeping"
  sleep 300
fi

docker top "$CONTAINER" >>"$EVIDENCE_DIR/top.txt" 2>/dev/null || true
docker stop -t 10 "$CONTAINER" >/dev/null
kill "$LOG_PID" 2>/dev/null || true
wait "$LOG_PID" 2>/dev/null || true
LOG_PID=""
docker logs "$CONTAINER" >"$EVIDENCE_DIR/container.log" 2>&1 # complete, incl. shutdown
docker history --no-trunc "$IMAGE" >"$EVIDENCE_DIR/history.txt"

SWEEP_RESULT="not run (reuse run: no secret entered this run — item-5 evidence comes from the fresh runs)"
if [[ "$MODE" == fresh ]]; then
  echo
  echo "==> gate item 5 sweep: exact key value, grep -Ff with the tmpfs key file as the"
  echo "    pattern file — the value itself never enters argv or output."
  sweep_hit=0
  scan() { # $1: label, $2: captured file
    if grep -qFf "$SPIKE_KEY_FILE" "$2"; then
      echo "    FAIL: key value found in $1" >&2
      sweep_hit=1
    else
      echo "    ok: key value absent from $1"
    fi
  }
  scan "image history (docker history --no-trunc)" "$EVIDENCE_DIR/history.txt"
  scan "container env + mount metadata (docker inspect; only the non-secret path appears)" "$EVIDENCE_DIR/inspect.json"
  scan "process argv snapshots (docker top, live + pre-stop)" "$EVIDENCE_DIR/top.txt"
  scan "full container log" "$EVIDENCE_DIR/container.log"
  # The state volume is scanned INSIDE a scratch container so the pattern
  # file stays a path everywhere; -l prints only matching FILENAMES.
  set +e
  hits=$(docker run --rm \
    --mount "type=bind,source=${SPIKE_KEY_FILE},target=/run/secrets/pattern,readonly" \
    -v "$VOLUME":/state:ro \
    --entrypoint /bin/sh "$IMAGE" \
    -c 'grep -rlFf /run/secrets/pattern /state' 2>&1)
  rc=$?
  set -e
  case $rc in
    0)
      echo "    FAIL: key value found on the state volume in:" >&2
      printf '%s\n' "$hits" | sed 's/^/      /' >&2
      sweep_hit=1
      ;;
    1) echo "    ok: key value absent from every file on the $VOLUME volume" ;;
    *)
      echo "    FAIL: state-volume scan errored (rc=$rc): $hits" >&2
      sweep_hit=1
      ;;
  esac
  if [[ "$sweep_hit" -ne 0 ]]; then
    SWEEP_RESULT="FAILED — a durable copy of the key exists; gate item 5 does NOT pass"
  else
    SWEEP_RESULT="passed on all five surfaces (history, inspect, argv, log, state volume)"
  fi
fi

echo
echo "==> sanitized evidence block — paste into issue #31:"
echo "--------------------------------------------------------------------------"
echo "harness: spikes/issue-31-tailscale-uid1000 @ $(git rev-parse --short HEAD 2>/dev/null || echo '<commit>')"
echo "run mode: $MODE"
grep '^==> running as uid' "$EVIDENCE_DIR/container.log" || true
sed -n '/^==> tailscale version/,/^==>/{/^==> tailscale version/d;/^==>/d;p;}' "$EVIDENCE_DIR/container.log" | sed 's/^/tailscale version: /'
grep -E '^==> (existing state found|empty statedir)' "$EVIDENCE_DIR/container.log" || true
echo "key-absence sweep: $SWEEP_RESULT"
echo "--------------------------------------------------------------------------"
if [[ "$MODE" == fresh ]] && [[ "${sweep_hit:-0}" -ne 0 ]]; then
  exit 1
fi
