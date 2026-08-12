#!/usr/bin/env bash
# Run the issue #31 gate on a Docker-capable host (e.g. the machine that runs
# the snow-maker dev containers). Needs a GNU/Linux host with an ordinary
# root-daemon Docker; run it however your docker socket access requires —
# `sudo ./run-spike.sh` is the expected shape and is fully supported (see the
# key permission model below). Rootless Docker and userns-remap daemons are
# NOT supported: there the container's uid 1000 is not host uid 1000, and the
# readability preflight fails closed rather than guessing.
#
# FRESH run (no spike-tailscale-state volume yet — gate items 1/4): the
# REUSABLE auth key (tskey-auth-...) is read from stdin — paste it at the
# hidden prompt, or pipe it in from a secret manager:
#
#   sudo ./run-spike.sh                     # prompts, input hidden
#   pass show tailnet/spike | sudo ./run-spike.sh
#
# Key permission model (cross-UID delivery): the key lives in a mode-0700
# directory on a VERIFIED tmpfs (/dev/shm) — the directory is the barrier
# against other host users — with a single leaf file the deliberately
# selected container can read as uid 1000:
#   - invoked as root (sudo):   leaf chown 1000:1000, mode 0400
#   - invoked as uid 1000:      leaf mode 0400 (same uid as the container)
#   - invoked as any other uid: leaf mode 0444 — readable only through the
#     0700 directory, which nothing but this script (and root) can traverse
# The SAME leaf mount serves the enrollment container and the state-volume
# scanner. A preflight container proves the mounted key is readable as uid
# 1000 (without printing a byte of it) before enrollment is attempted. The
# value never enters argv, shell history, environment values, or the build
# context; a path argument is accepted only if the file already lives on a
# tmpfs, and never from inside the Docker build context. Every exit path —
# success, failure, SIGINT, SIGTERM — removes the ENTIRE key directory and
# prints the removal as proof.
#
# REUSE run (volume exists — gate item 3): no key is read and nothing secret
# is mounted; the retained-identity branch must work keyless.
#
# After you finish the SSH checks (press Enter), the script stops the
# container and sweeps every surface for the exact key value with grep -Ff,
# using the key file as the PATTERN FILE — the value itself never enters
# argv or output. Every sweep is TRI-STATE and FAILS CLOSED: a grep hit is a
# failure, a clean miss is the only pass, and a scanner error — including a
# missing, unreadable, or empty evidence capture — is a failure too (an
# unscannable surface proves nothing). Surfaces: image history, container
# env + mount metadata (docker inspect), recorded process argv (docker top),
# the full container log, and every file on the retained state volume.
#
# Self-test: ./test-run-spike.sh exercises the sweep tri-state and the
# cleanup paths with a stubbed docker — no engine, tailnet, or key needed.
set -euo pipefail

IMAGE=ozolith-ts-spike
CONTAINER=ozolith-ts-spike
VOLUME=spike-tailscale-state

SPIKE_KEY_DIR=""
SPIKE_KEY_FILE=""
EVIDENCE_DIR=""
LOG_PID=""
sweep_hit=0

cleanup() {
  status=$?
  trap - EXIT
  [[ -n "$LOG_PID" ]] && kill "$LOG_PID" 2>/dev/null || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  # Raw captures may only contain the key if a sweep FAILED — they live on
  # tmpfs and are removed either way. The volume and image are deliberately
  # retained (machine state, not secrets): checks 3/4 need them.
  [[ -n "$EVIDENCE_DIR" ]] && rm -rf "$EVIDENCE_DIR"
  if [[ -n "$SPIKE_KEY_DIR" ]]; then
    rm -rf "$SPIKE_KEY_DIR"
    if [[ -e "$SPIKE_KEY_DIR" ]]; then
      echo "WARNING: temporary key directory STILL EXISTS: $SPIKE_KEY_DIR" >&2
    else
      echo "==> temporary key directory removed (proof: '! -e $SPIKE_KEY_DIR' holds)"
    fi
  fi
  exit "$status"
}

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

make_key_dir() {
  # 0700 directory on verified tmpfs: the barrier against other host users.
  require_tmpfs /dev/shm
  SPIKE_KEY_DIR=$(mktemp -d /dev/shm/ozolith-ts-spike-key.XXXXXX)
  chmod 0700 "$SPIKE_KEY_DIR"
  SPIKE_KEY_FILE="$SPIKE_KEY_DIR/authkey"
  : >"$SPIKE_KEY_FILE"
  chmod 0600 "$SPIKE_KEY_FILE" # tight while the value is being written
}

finalize_key_perms() {
  # Make the leaf readable by the container's uid 1000 — and by nothing else
  # that the 0700 directory does not already admit. Ordinary root-daemon
  # Docker bind-mounts the leaf INODE, so inside the container only the
  # leaf's own owner/mode decide access; host-side, the directory decides.
  if [[ "$EUID" -eq 0 ]]; then
    chown 1000:1000 "$SPIKE_KEY_FILE"
    chmod 0400 "$SPIKE_KEY_FILE"
  elif [[ "$EUID" -eq 1000 ]]; then
    chmod 0400 "$SPIKE_KEY_FILE"
  else
    chmod 0444 "$SPIKE_KEY_FILE" # the 0700 directory is the host-side barrier
  fi
}

read_key() { # fills $SPIKE_KEY_FILE; the value never touches argv
  if [[ $# -ge 1 ]]; then
    local src
    src=$(realpath -e "$1")
    case "$src" in
      "$CONTEXT_DIR"/*)
        echo "refusing: $src is inside the Docker build context ($CONTEXT_DIR) — a key there can end up in an image layer." >&2
        exit 1
        ;;
    esac
    require_tmpfs "$src"
    cat -- "$src" >"$SPIKE_KEY_FILE"
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
}

preflight_key_readable() {
  # Prove the mounted leaf is readable as uid 1000 IN the container — the
  # exact mount enrollment will use — without printing a byte of the value.
  if docker run --rm \
    --mount "type=bind,source=${SPIKE_KEY_FILE},target=/run/secrets/ts-authkey,readonly" \
    --entrypoint /bin/sh "$IMAGE" \
    -c '[ "$(id -u)" = 1000 ] && [ -r /run/secrets/ts-authkey ] && [ -s /run/secrets/ts-authkey ]'; then
    echo "==> preflight: key file is readable as uid 1000 inside the container"
  else
    echo "FAIL: preflight — the mounted key is not readable as uid 1000 in the container." >&2
    echo "      Rootless Docker and userns-remap daemons are unsupported for the gate (the" >&2
    echo "      container's uid 1000 is not host uid 1000 there); run on an ordinary" >&2
    echo "      root-daemon Docker host." >&2
    exit 1
  fi
}

scan_file() { # $1: label, $2: captured evidence file — tri-state, fail closed
  if [[ ! -r "$2" || ! -s "$2" ]]; then
    echo "    FAIL: evidence for $1 is missing, unreadable, or empty ($2) — cannot prove key absence" >&2
    sweep_hit=1
    return 0
  fi
  local rc=0
  grep -qFf "$SPIKE_KEY_FILE" "$2" || rc=$?
  case $rc in
    0)
      echo "    FAIL: key value found in $1" >&2
      sweep_hit=1
      ;;
    1) echo "    ok: key value absent from $1" ;;
    *)
      echo "    FAIL: scanner error (grep rc=$rc) on $1 — cannot prove key absence" >&2
      sweep_hit=1
      ;;
  esac
}

scan_state_volume() {
  # Scanned INSIDE a scratch container (as uid 1000, like everything else)
  # with the SAME leaf mount enrollment used, so the pattern stays a path
  # everywhere; -l prints only matching FILENAMES. Tri-state: grep rc 0 is a
  # hit, 1 is the only pass, anything else (grep error, docker failure) is a
  # scanner failure and fails the gate.
  local rc=0 hits
  set +e
  hits=$(docker run --rm \
    --mount "type=bind,source=${SPIKE_KEY_FILE},target=/run/secrets/ts-authkey,readonly" \
    -v "$VOLUME":/state:ro \
    --entrypoint /bin/sh "$IMAGE" \
    -c 'grep -rlFf /run/secrets/ts-authkey /state' 2>&1)
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
      echo "    FAIL: state-volume scanner error (rc=$rc): $hits" >&2
      sweep_hit=1
      ;;
  esac
}

main() {
  cd "$(dirname "${BASH_SOURCE[0]}")"
  CONTEXT_DIR=$(pwd -P)

  local mode
  if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
    mode=reuse
  else
    mode=fresh
  fi

  if [[ "$mode" == fresh ]]; then
    make_key_dir
    read_key "$@"
    finalize_key_perms
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
  # read-only mount of the tmpfs leaf; only its PATH appears in env/argv
  # (gate item 5). On a reuse run nothing secret is mounted at all.
  if [[ "$mode" == fresh ]]; then
    preflight_key_readable
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

  local i=0
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
  # SPIKE_NONINTERACTIVE / SPIKE_SSH_WAIT_SECS exist for the stubbed
  # self-test (test-run-spike.sh); an operator run just presses Enter.
  if [[ "${SPIKE_NONINTERACTIVE:-0}" != 1 ]] && { : </dev/tty; } 2>/dev/null; then
    IFS= read -r _ </dev/tty
  else
    echo "==> no interactive terminal: waiting ${SPIKE_SSH_WAIT_SECS:-300}s for the SSH checks, then sweeping"
    sleep "${SPIKE_SSH_WAIT_SECS:-300}"
  fi

  docker top "$CONTAINER" >>"$EVIDENCE_DIR/top.txt" 2>/dev/null || true
  docker stop -t 10 "$CONTAINER" >/dev/null
  kill "$LOG_PID" 2>/dev/null || true
  wait "$LOG_PID" 2>/dev/null || true
  LOG_PID=""
  docker logs "$CONTAINER" >"$EVIDENCE_DIR/container.log" 2>&1 # complete, incl. shutdown
  docker history --no-trunc "$IMAGE" >"$EVIDENCE_DIR/history.txt"

  local sweep_result="not run (reuse run: no secret entered this run — item-5 evidence comes from the fresh runs)"
  if [[ "$mode" == fresh ]]; then
    echo
    echo "==> gate item 5 sweep: exact key value, grep -Ff with the tmpfs key file as the"
    echo "    pattern file — the value itself never enters argv or output. Tri-state and"
    echo "    fail-closed: a hit fails, a scanner error fails, only a clean miss passes."
    if [[ ! -r "$SPIKE_KEY_FILE" || ! -s "$SPIKE_KEY_FILE" ]]; then
      echo "    FAIL: the key pattern file is missing, unreadable, or empty — cannot sweep" >&2
      sweep_hit=1
    else
      scan_file "image history (docker history --no-trunc)" "$EVIDENCE_DIR/history.txt"
      scan_file "container env + mount metadata (docker inspect; only the non-secret path appears)" "$EVIDENCE_DIR/inspect.json"
      scan_file "process argv snapshots (docker top, live + pre-stop)" "$EVIDENCE_DIR/top.txt"
      scan_file "full container log" "$EVIDENCE_DIR/container.log"
      scan_state_volume
    fi
    if [[ "$sweep_hit" -ne 0 ]]; then
      sweep_result="FAILED — a key hit or an unscannable surface; gate item 5 does NOT pass"
    else
      sweep_result="passed on all five surfaces (history, inspect, argv, log, state volume)"
    fi
  fi

  echo
  echo "==> sanitized evidence block — paste into issue #31:"
  echo "--------------------------------------------------------------------------"
  echo "harness: spikes/issue-31-tailscale-uid1000 @ $(git rev-parse --short HEAD 2>/dev/null || echo '<commit>')"
  echo "run mode: $mode"
  grep '^==> running as uid' "$EVIDENCE_DIR/container.log" || true
  sed -n '/^==> tailscale version/,/^==>/{/^==> tailscale version/d;/^==>/d;p;}' "$EVIDENCE_DIR/container.log" | sed 's/^/tailscale version: /'
  grep -E '^==> (existing state found|empty statedir)' "$EVIDENCE_DIR/container.log" || true
  echo "key-absence sweep: $sweep_result"
  echo "--------------------------------------------------------------------------"
  if [[ "$mode" == fresh && "$sweep_hit" -ne 0 ]]; then
    exit 1
  fi
}

# Guard so test-run-spike.sh can `source` the functions above without
# executing a run; traps are installed only for a real execution, so every
# real exit path — success, failure, SIGINT, SIGTERM — funnels into cleanup.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  main "$@"
fi
