#!/usr/bin/env bash
# Run the issue #31 gate on a Docker-capable host (e.g. the machine that runs
# the snow-maker dev containers). Needs a GNU/Linux host with an ordinary
# root-daemon Docker; run it however your docker socket access requires —
# `sudo ./run-spike.sh` is the expected shape and is fully supported (see the
# key permission model below). Rootless Docker and userns-remap daemons are
# NOT supported: there the container's uid 1000 is not host uid 1000. Both are
# POSITIVELY detected (docker info SecurityOptions) and rejected before any
# key is read; the uid-1000 readability preflight below stays as a second,
# independent check of the actual mount.
#
# FRESH vs REUSE is decided from ACTUAL state, not the volume's mere
# existence: only a spike-tailscale-state volume holding a NON-EMPTY
# tailscaled.state is a reusable identity (keyless run — gate item 3). An
# absent volume, or a volume left behind by a failed first attempt (missing
# or empty state file), takes the fresh-enrollment path (gate items 1/4) —
# through the existing volume when there is one; nothing is ever deleted
# silently. Any docker/volume probe error fails closed before a key is read.
#
# FRESH run: the REUSABLE auth key (tskey-auth-...) is read from stdin —
# paste it at the hidden prompt, or pipe it in from a secret manager:
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
# tmpfs, and never from inside the Docker build context.
#
# After you finish the SSH checks (press Enter), the script stops the
# container and sweeps every surface for the exact key value with grep -Ff,
# using the key file as the PATTERN FILE — the value itself never enters
# argv or output. Every sweep is TRI-STATE and FAILS CLOSED: a grep hit is a
# failure, a clean miss is the only pass, and a scanner error — including a
# missing, unreadable, or empty evidence capture — is a failure too (an
# unscannable surface proves nothing). Every promised capture is itself
# mandatory: a failed `docker top`/`inspect`/`logs`/`history` fails the run.
# Surfaces: image history, container env + mount metadata (docker inspect),
# recorded process argv (docker top, live and pre-stop as separate files),
# the full container log, and every file on the retained state volume.
#
# Cleanup is provable and failure-aware: every exit path — success, failure,
# SIGINT, SIGTERM — attempts EVERY teardown step (log follower, ALL
# key-bearing containers, evidence directory, key directory) regardless of
# earlier failures, then verifies each container is gone and the key
# directory no longer exists. "All key-bearing containers" means every
# container that received the key bind mount: the main spike container AND
# the two scratch containers (the uid-1000 readability preflight and the
# state-volume scanner). The scratch runs use --rm, but --rm is convenience
# cleanup, not proof — an interruption or a daemon/client failure can leave
# the container behind — so each scratch container gets a per-run,
# collision-safe name that is recorded BEFORE its docker run is invoked, and
# cleanup queries, removes when present, and re-queries every recorded name
# (a normally auto-removed scratch container is proven clean by that query,
# never counted as an rm failure). Any step that fails, or any absence that
# cannot be proven, turns the exit status nonzero (an original failure
# status is preserved). If ANY key-bearing container's absence cannot be
# proven on a run that mounted a key, a prominent warning tells the operator
# to REVOKE the key immediately — a bind mount can keep the key readable
# inside a surviving container.
#
# Self-test: ./test-run-spike.sh exercises mode selection, daemon-mode
# rejection, the sweep tri-state, entrypoint failure propagation, and the
# cleanup paths with stubbed docker/tailscale binaries — no engine, tailnet,
# or key needed.
set -euo pipefail

IMAGE=ozolith-ts-spike
CONTAINER=ozolith-ts-spike
VOLUME=spike-tailscale-state

# The two key-bearing scratch operations (uid-1000 readability preflight,
# state-volume scanner) run under per-run, collision-safe names: a fixed
# name could collide with — or silently clear — the debris of another run,
# while a per-run name can only ever denote THIS run's container. The names
# carry no secret (prefix + pid + $RANDOM). Each tracking variable below is
# assigned BEFORE the corresponding docker run is invoked, so a partially
# created container or a client-side docker failure is still visible to
# cleanup; "" means that operation was never attempted.
SCRATCH_PREFLIGHT_PREFIX=ozolith-ts-spike-preflight-
SCRATCH_SCAN_PREFIX=ozolith-ts-spike-keyscan-
SCRATCH_RUN_TAG="$$-${RANDOM}"
PREFLIGHT_CONTAINER=""
SCAN_CONTAINER=""

SPIKE_KEY_DIR=""
SPIKE_KEY_FILE=""
EVIDENCE_DIR=""
LOG_PID=""
CONTAINER_STARTED=0
MODE=""
MODE_DETAIL=""
VOLUME_EXISTS=0
sweep_hit=0

# prove_container_gone <name> <label> — one container's provable teardown:
# query whether it exists, remove it when present (or when the query itself
# failed and existence cannot be ruled out), then query again and PROVE the
# absence. A container that is already gone — the normal outcome for the
# --rm scratch runs — counts as clean, not as an rm failure. Returns:
#   0  absence proven, no step failed
#   1  a step failed (rm, or the pre-removal query), but absence WAS proven
#   2  absence UNPROVEN (still listed, or the proving query failed)
prove_container_gone() {
  local name=$1 label=$2 listed step_failed=0
  if listed=$(docker ps -aq --filter "name=^${name}\$" 2>/dev/null); then
    if [[ -z "$listed" ]]; then
      echo "==> $label already absent (proof: 'docker ps -a' does not list $name)"
      return 0
    fi
    if ! docker rm -f "$name" >/dev/null 2>&1; then
      echo "CLEANUP FAIL: docker rm -f $name ($label) failed" >&2
      step_failed=1
    fi
  else
    # The existence query failed: removal is still attempted (best effort,
    # its rc deliberately ignored — without the query there is no telling a
    # real rm failure from 'no such container'), and the re-query below is
    # the arbiter of proof.
    echo "CLEANUP FAIL: could not query docker for the $label ($name) before removal" >&2
    step_failed=1
    docker rm -f "$name" >/dev/null 2>&1
  fi
  if listed=$(docker ps -aq --filter "name=^${name}\$" 2>/dev/null); then
    if [[ -n "$listed" ]]; then
      echo "CLEANUP FAIL: $label $name STILL EXISTS after docker rm -f" >&2
      return 2
    fi
    echo "==> $label removed (proof: 'docker ps -a' no longer lists $name)"
    return "$step_failed"
  fi
  echo "CLEANUP FAIL: could not query docker for the $label ($name) — removal is UNPROVEN" >&2
  return 2
}

cleanup() {
  status=$?
  trap - EXIT
  # Cleanup must be provable: errexit is OFF in here so EVERY step below is
  # attempted no matter what failed earlier; each failure is recorded, and any
  # failure — or any absence that cannot be proven — turns the final exit
  # nonzero. A nominally successful run does not get to report success past a
  # teardown it cannot prove; an original nonzero status is preserved.
  set +e
  local failed=0

  # 1) the log-follow process
  if [[ -n "$LOG_PID" ]]; then
    kill "$LOG_PID" 2>/dev/null
    local i
    for i in 1 2 3 4 5; do
      kill -0 "$LOG_PID" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$LOG_PID" 2>/dev/null; then
      kill -9 "$LOG_PID" 2>/dev/null
      sleep 0.2
    fi
    if kill -0 "$LOG_PID" 2>/dev/null; then
      echo "CLEANUP FAIL: log-follow process (pid $LOG_PID) could not be terminated" >&2
      failed=1
    fi
    wait "$LOG_PID" 2>/dev/null
  fi

  # 2) EVERY container that received the key bind mount: the main container
  # and the two key-bearing scratch containers (uid-1000 preflight, state-
  # volume scanner). Each one is queried, removed when present, and re-
  # queried until its absence is PROVEN — --rm on the scratch runs is
  # convenience cleanup, not proof, and an unproven removal of a container
  # that bind-mounts the key is an emergency, not a shrug: the mount pins
  # the inode, so the key can stay readable inside a surviving container
  # even after the host copy below is deleted. Every container is processed
  # regardless of earlier failures.
  local prc unproven=0
  if [[ "$CONTAINER_STARTED" -eq 1 ]]; then
    prove_container_gone "$CONTAINER" "container"
    prc=$?
    [[ "$prc" -ne 0 ]] && failed=1
    [[ "$prc" -eq 2 ]] && unproven=1
  fi
  if [[ -n "$PREFLIGHT_CONTAINER" ]]; then
    prove_container_gone "$PREFLIGHT_CONTAINER" "preflight container"
    prc=$?
    [[ "$prc" -ne 0 ]] && failed=1
    [[ "$prc" -eq 2 ]] && unproven=1
  fi
  if [[ -n "$SCAN_CONTAINER" ]]; then
    prove_container_gone "$SCAN_CONTAINER" "state-scanner container"
    prc=$?
    [[ "$prc" -ne 0 ]] && failed=1
    [[ "$prc" -eq 2 ]] && unproven=1
  fi
  if [[ "$unproven" -eq 1 && -n "$SPIKE_KEY_DIR" ]]; then
    echo "!!! ------------------------------------------------------------------ !!!" >&2
    echo "!!! WARNING: this run bind-mounted the auth key and at least one        !!!" >&2
    echo "!!! container that received that mount has UNPROVEN removal. The key    !!!" >&2
    echo "!!! value may still be readable inside a surviving container even       !!!" >&2
    echo "!!! after the host copy is deleted. REVOKE THE AUTH KEY IMMEDIATELY:    !!!" >&2
    echo "!!! tailscale admin console -> Settings -> Keys -> revoke.              !!!" >&2
    echo "!!! ------------------------------------------------------------------ !!!" >&2
  fi

  # 3) evidence directory (tmpfs; raw captures could hold the key only if a
  # sweep FAILED — removed either way, and the removal must be proven)
  if [[ -n "$EVIDENCE_DIR" ]]; then
    rm -rf "$EVIDENCE_DIR" 2>/dev/null
    if [[ -e "$EVIDENCE_DIR" ]]; then
      echo "CLEANUP FAIL: evidence directory STILL EXISTS: $EVIDENCE_DIR — remove it by hand" >&2
      failed=1
    fi
  fi

  # 4) key directory. The volume and image are deliberately retained
  # (machine state, not secrets): checks 3/4 need them.
  if [[ -n "$SPIKE_KEY_DIR" ]]; then
    rm -rf "$SPIKE_KEY_DIR" 2>/dev/null
    if [[ -e "$SPIKE_KEY_DIR" ]]; then
      echo "CLEANUP FAIL: temporary key directory STILL EXISTS: $SPIKE_KEY_DIR — remove it by hand and REVOKE the key" >&2
      failed=1
    else
      echo "==> temporary key directory removed (proof: '! -e $SPIKE_KEY_DIR' holds)"
    fi
  fi

  if [[ "$failed" -ne 0 ]]; then
    echo "CLEANUP FAILED: teardown could not be completed or proven — treat this run as FAILED." >&2
    [[ "$status" -eq 0 ]] && status=1
  fi
  exit "$status"
}

require_supported_docker() {
  # Daemon-mode enforcement happens BEFORE any secret intake: on rootless and
  # userns-remap daemons the container's uid 1000 is not host uid 1000, so
  # cross-UID key delivery cannot mean what the gate needs it to mean. This
  # is POSITIVE detection from the daemon's own SecurityOptions; the uid-1000
  # readability preflight later is a second, independent check of the actual
  # mount — not the daemon-mode detector.
  command -v docker >/dev/null 2>&1 || {
    echo "FAIL: docker not found on PATH — this harness needs an ordinary root-daemon Docker." >&2
    exit 1
  }
  local secopts
  if ! secopts=$(docker info --format '{{json .SecurityOptions}}' 2>&1); then
    echo "FAIL: cannot query the Docker daemon (docker info failed) — refusing to read a key:" >&2
    printf '%s\n' "$secopts" | sed 's/^/      /' >&2
    exit 1
  fi
  if grep -q 'name=rootless' <<<"$secopts"; then
    echo "FAIL: rootless Docker daemon detected (SecurityOptions reports name=rootless) — unsupported." >&2
    echo "      Under rootless Docker the container's uid 1000 is not host uid 1000, so the gate's" >&2
    echo "      cross-UID key delivery cannot be verified. Run on an ordinary root-daemon host." >&2
    exit 1
  fi
  if grep -q 'name=userns' <<<"$secopts"; then
    echo "FAIL: userns-remap Docker daemon detected (SecurityOptions reports name=userns) — unsupported." >&2
    echo "      With userns-remap the container's uid 1000 maps to a shifted host uid, so the gate's" >&2
    echo "      cross-UID key delivery cannot be verified. Run on an ordinary root-daemon host." >&2
    exit 1
  fi
  echo "==> docker daemon: ordinary root daemon (no rootless / userns-remap security option)"
}

reject_stale_scratch_containers() {
  # Scratch names are per-run and collision-safe, so anything matching the
  # scratch prefixes can only be debris from a run this script could not
  # clean up (e.g. kill -9 mid-preflight) or from a concurrently live run —
  # and the preflight/scanner containers are exactly the ones that bind-
  # mount an auth key, so a stale one may still hold a PRIOR key readable.
  # Refuse to proceed, before any secret intake. Nothing is removed here:
  # deleting containers this run did not create is an operator decision,
  # and an unrelated container that merely matches the prefix must never be
  # silently destroyed.
  local stale
  if ! stale=$(docker ps -aq \
    --filter "name=^${SCRATCH_PREFLIGHT_PREFIX}" \
    --filter "name=^${SCRATCH_SCAN_PREFIX}" 2>&1); then
    echo "FAIL: cannot check for stale key-bearing scratch containers (docker ps failed):" >&2
    printf '%s\n' "$stale" | sed 's/^/      /' >&2
    exit 1
  fi
  if [[ -n "$stale" ]]; then
    echo "FAIL: stale key-bearing scratch container(s) exist (name prefix ${SCRATCH_PREFLIGHT_PREFIX}* / ${SCRATCH_SCAN_PREFIX}*):" >&2
    printf '%s\n' "$stale" | sed 's/^/      /' >&2
    echo "      Such containers bind-mounted an auth key when they were created; a prior run was" >&2
    echo "      interrupted before it could prove their removal (or another run is live right now)." >&2
    echo "      Inspect and remove them (docker rm -f <id>), REVOKE the key that run used, then" >&2
    echo "      re-run. Refusing to continue — this check runs before any key intake." >&2
    exit 1
  fi
}

probe_mode() {
  # Fresh-vs-reuse from ACTUAL state, not the volume's mere existence: only a
  # non-empty tailscaled.state is a reusable identity. A volume left behind
  # by a failed first attempt (missing or empty state file) is recovered
  # through fresh enrollment WITHOUT deleting anything. Every probe error
  # fails closed — and all of this runs before any key is read.
  local out rc
  out=$(docker volume inspect "$VOLUME" 2>&1) && rc=0 || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    if grep -qi 'no such volume' <<<"$out"; then
      MODE=fresh
      VOLUME_EXISTS=0
      MODE_DETAIL="fresh (no $VOLUME volume yet)"
      echo "==> no state volume yet ($VOLUME): FRESH run — enrollment (gate items 1/4)"
      return 0
    fi
    echo "FAIL: cannot tell fresh from reuse — docker volume inspect failed:" >&2
    printf '%s\n' "$out" | sed 's/^/      /' >&2
    exit 1
  fi
  VOLUME_EXISTS=1
  # The volume exists: check for a non-empty tailscaled.state from inside a
  # scratch container — the same uid-1000 view the real run will have.
  out=$(docker run --rm -v "$VOLUME":/state:ro --entrypoint /bin/sh "$IMAGE" \
    -c '[ -s /state/tailscaled.state ]' 2>&1) && rc=0 || rc=$?
  case "$rc" in
    0)
      MODE=reuse
      MODE_DETAIL="reuse (non-empty tailscaled.state on $VOLUME)"
      echo "==> volume $VOLUME holds a non-empty tailscaled.state: REUSE run — keyless identity (gate item 3)"
      ;;
    1)
      MODE=fresh
      MODE_DETAIL="fresh (existing $VOLUME volume without usable state — recovery)"
      echo "==> volume $VOLUME exists but holds no usable tailscaled.state: FRESH run through the"
      echo "    EXISTING volume (recovery from a failed attempt; nothing is deleted)"
      ;;
    *)
      echo "FAIL: cannot tell fresh from reuse — the state probe failed (rc=$rc):" >&2
      printf '%s\n' "$out" | sed 's/^/      /' >&2
      exit 1
      ;;
  esac
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
  # This is a second, independent check of the delivery path; daemon mode was
  # already positively verified before the key was read.
  #
  # This scratch container receives the key bind mount, so it is tracked:
  # the per-run name is recorded BEFORE docker run so that a partial
  # creation or a client-side failure is still cleanup-visible, and cleanup
  # later PROVES its absence — --rm alone is convenience, not proof.
  PREFLIGHT_CONTAINER="${SCRATCH_PREFLIGHT_PREFIX}${SCRATCH_RUN_TAG}"
  if docker run --rm --name "$PREFLIGHT_CONTAINER" \
    --mount "type=bind,source=${SPIKE_KEY_FILE},target=/run/secrets/ts-authkey,readonly" \
    --entrypoint /bin/sh "$IMAGE" \
    -c '[ "$(id -u)" = 1000 ] && [ -r /run/secrets/ts-authkey ] && [ -s /run/secrets/ts-authkey ]'; then
    echo "==> preflight: key file is readable as uid 1000 inside the container"
  else
    echo "FAIL: preflight — the mounted key is not readable as uid 1000 in the container." >&2
    echo "      The daemon mode already passed the SecurityOptions check, so this is a problem" >&2
    echo "      with the leaf or its mount (permissions, /dev/shm options). Fix the delivery" >&2
    echo "      path; do not weaken the container's privilege model to work around it." >&2
    exit 1
  fi
}

capture_evidence() { # $1: label, $2: dest file, $3...: command — fail closed
  local label=$1 dest=$2
  shift 2
  # stderr goes into the capture too (a complete surface). On failure NOTHING
  # captured is printed — the file could hold exactly what must never reach
  # the terminal; cleanup removes the tmpfs evidence directory either way.
  if ! "$@" >"$dest" 2>&1; then
    echo "FAIL: promised evidence capture failed ($label: $*)." >&2
    echo "      An unscannable surface proves nothing — the gate cannot pass without it." >&2
    exit 1
  fi
  if [[ ! -r "$dest" || ! -s "$dest" ]]; then
    echo "FAIL: evidence capture ($label) left a missing, unreadable, or empty file: $dest" >&2
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
  #
  # Like the preflight, this scratch container receives the key bind mount,
  # so its per-run name is recorded BEFORE docker run and cleanup PROVES
  # its absence — --rm alone is convenience, not proof.
  local rc=0 hits
  SCAN_CONTAINER="${SCRATCH_SCAN_PREFIX}${SCRATCH_RUN_TAG}"
  set +e
  hits=$(docker run --rm --name "$SCAN_CONTAINER" \
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

  # Docker availability, daemon mode, and the absence of stale key-bearing
  # scratch containers are all validated BEFORE any key intake.
  require_supported_docker
  reject_stale_scratch_containers

  echo "==> building the spike image (version- and sha256-pinned tailscale; see Dockerfile)"
  docker build -t "$IMAGE" .

  probe_mode

  if [[ "$MODE" == fresh ]]; then
    make_key_dir
    read_key "$@"
    finalize_key_perms
    preflight_key_readable
  elif [[ $# -ge 1 ]]; then
    echo "note: reuse run — the key argument is ignored and not read" >&2
  fi

  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  EVIDENCE_DIR=$(mktemp -d /dev/shm/ozolith-ts-evidence.XXXXXX)

  # Deliberately absent: --cap-add, --device, --privileged. The container gets
  # nothing beyond Docker's default unprivileged capability set — that absence
  # IS the experiment. On a fresh run the key reaches the container only as a
  # read-only mount of the tmpfs leaf; only its PATH appears in env/argv
  # (gate item 5). On a reuse run nothing secret is mounted at all.
  CONTAINER_STARTED=1
  if [[ "$MODE" == fresh ]]; then
    if [[ "$VOLUME_EXISTS" -eq 0 ]]; then
      docker volume create "$VOLUME" >/dev/null
    fi
    echo "==> fresh run: enrollment branch expected (gate items 1/4)"
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
  # visible in the log above). Every promised capture is REQUIRED: a capture
  # failure is a run failure, never a silently skipped surface.
  capture_evidence "process argv, live (docker top)" "$EVIDENCE_DIR/top-live.txt" docker top "$CONTAINER"
  capture_evidence "container env + mount metadata (docker inspect)" "$EVIDENCE_DIR/inspect.json" docker inspect "$CONTAINER"

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

  capture_evidence "process argv, pre-stop (docker top)" "$EVIDENCE_DIR/top-prestop.txt" docker top "$CONTAINER"
  docker stop -t 10 "$CONTAINER" >/dev/null
  kill "$LOG_PID" 2>/dev/null || true
  wait "$LOG_PID" 2>/dev/null || true
  LOG_PID=""
  capture_evidence "full container log" "$EVIDENCE_DIR/container.log" docker logs "$CONTAINER"
  capture_evidence "image history (docker history --no-trunc)" "$EVIDENCE_DIR/history.txt" docker history --no-trunc "$IMAGE"

  local sweep_result="not run (reuse run: no secret entered this run — item-5 evidence comes from the fresh runs)"
  if [[ "$MODE" == fresh ]]; then
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
      scan_file "process argv snapshot (docker top, live)" "$EVIDENCE_DIR/top-live.txt"
      scan_file "process argv snapshot (docker top, pre-stop)" "$EVIDENCE_DIR/top-prestop.txt"
      scan_file "full container log" "$EVIDENCE_DIR/container.log"
      scan_state_volume
    fi
    if [[ "$sweep_hit" -ne 0 ]]; then
      sweep_result="FAILED — a key hit or an unscannable surface; gate item 5 does NOT pass"
    else
      sweep_result="passed on all six surfaces (history, inspect, argv live, argv pre-stop, log, state volume)"
    fi
  fi

  echo
  echo "==> sanitized evidence block — paste into issue #31:"
  echo "--------------------------------------------------------------------------"
  echo "harness: spikes/issue-31-tailscale-uid1000 @ $(git rev-parse --short HEAD 2>/dev/null || echo '<commit>')"
  echo "run mode: $MODE_DETAIL"
  grep '^==> running as uid' "$EVIDENCE_DIR/container.log" || true
  sed -n '/^==> tailscale version/,/^==>/{/^==> tailscale version/d;/^==>/d;p;}' "$EVIDENCE_DIR/container.log" | sed 's/^/tailscale version: /'
  grep -E '^==> (existing state found|empty statedir)' "$EVIDENCE_DIR/container.log" || true
  echo "key-absence sweep: $sweep_result"
  echo "--------------------------------------------------------------------------"
  if [[ "$MODE" == fresh && "$sweep_hit" -ne 0 ]]; then
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
