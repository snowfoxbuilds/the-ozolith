#!/usr/bin/env bash
# Shell-level regression tests for run-spike.sh and entrypoint.sh — no Docker
# engine, tailnet, or auth key needed (docker, tailscale, and tailscaled are
# stubbed onto PATH; the run lock is the REAL flock(2) on the real /dev/shm
# directory inode). Covers exactly what the gate's credibility rests on:
#
#   - ONE RUN AT A TIME: the flock run lock — taken on the /dev/shm directory
#     inode via fd 9, never through a lock file — is acquired before anything
#     else and excludes a second run entirely: no key intake, no docker
#     calls, no removal of the first run's containers. It is released on
#     success, ordinary failure, SIGINT, and SIGTERM; only the harness
#     process holds fd 9 (every spawned external child has it closed), so
#     SIGKILLing just the harness releases the lock PROMPTLY even while its
#     children survive, and the next run then refuses the leftover debris
#     instead. The legacy /dev/shm/ozolith-ts-spike.lock pathname is dead:
#     a symlink planted there is never opened, changed, or chmodded;
#   - the DEBRIS AUDIT fails closed BEFORE any key intake: stale containers
#     of every name generation (the legacy fixed main name and per-run
#     main/preflight/keyscan names), stale key directories, stale evidence
#     directories, and a failing audit query all refuse the run — and none
#     of the debris is removed automatically;
#   - fresh-vs-reuse is decided from ACTUAL state: absent volume -> fresh;
#     non-empty tailscaled.state -> keyless reuse; existing volume without
#     usable state -> fresh through the same volume (recovery, no deletion);
#     any docker/volume probe error fails closed BEFORE a key is read;
#   - rootless and userns-remap daemons are positively rejected before any
#     secret intake; the uid-1000 readability preflight fails closed;
#   - the entrypoint propagates every required-command failure (version,
#     LocalAPI readiness, up, post-up status) as a nonzero exit and never
#     prints ready after a failure; daemon death stays nonzero;
#   - every promised evidence capture (docker top live AND pre-stop, inspect,
#     logs, history) is independently required — no "|| true" swallowing;
#   - both sweeps are tri-state and FAIL CLOSED: grep rc 0 = secret hit
#     (fail), rc 1 = absent (the only pass), rc >= 2 / docker failure =
#     scanner error (fail); missing, unreadable, or empty evidence fails;
#     the state-volume scanner sweeps file CONTENTS, PATHNAMES, DIRECTORY
#     NAMES, and SYMLINK TARGETS through an ephemeral tar representation and
#     emits ONLY fixed clean/hit/error statuses — no filename, matched byte,
#     or scanner stderr ever surfaces (exercised against the real payload
#     for every naming surface, and against the stub for status plumbing);
#   - NO LOG-DERIVED OUTPUT BEFORE A CLEAN EXACT-KEY SCAN: the container log
#     is captured into tmpfs only (never streamed); the ready-time excerpt
#     and early-failure diagnostics print only after a clean miss of the
#     exact bytes shown; a hit, a scanner error, or an uncapturable log
#     withholds them with a value-free failure and the revoke-now warning;
#     a sweep failure withholds the sanitized evidence block and every log
#     line it would have quoted (a leak parked in an evidence-selected line
#     included);
#   - cleanup attempts EVERY step regardless of earlier failures and proves
#     the absence of EVERY container that received the key bind mount — the
#     main container plus the uid-1000 preflight and state-volume scanner
#     scratch containers, ALL tracked under per-run collision-safe names
#     recorded BEFORE their docker run (--rm is convenience, not proof), and
#     ONLY current-run names are ever removed. A normally auto-removed
#     scratch container counts as already clean, a lingering one is removed
#     with proof, per-container rm failures and unprovable queries turn the
#     run nonzero with the value-free revoke-now warning, later cleanup
#     steps still run after earlier failures, an original nonzero status is
#     preserved — exercised on normal exit, ordinary failure, and
#     SIGINT/SIGTERM parked inside each key-bearing scratch operation;
#   - the key value never appears in the harness's own output, in any argv
#     it passes to docker or tailscale, or in any container name.
#
# Run: ./test-run-spike.sh   (exits non-zero on any failure)
set -euo pipefail
cd "$(dirname "$0")"

TESTTMP=$(mktemp -d)
# Some tests plant real debris under /dev/shm (the paths the harness audits);
# track those separately so an aborted test run cannot leave them behind.
SHM_DEBRIS=()
trap 'rm -rf "$TESTTMP" ${SHM_DEBRIS[@]+"${SHM_DEBRIS[@]}"}' EXIT
STUB=$TESTTMP/bin
mkdir -p "$STUB"

# The harness locks the REAL /dev/shm directory inode — these tests exercise
# the real flock(2). flock(1) with a directory operand opens that directory
# itself, so the helpers below contend on exactly the inode the harness
# locks. LEGACY_LOCKFILE is the retired lock-file pathname: nothing may ever
# open, change, or chmod it (or anything planted at it) again.
LOCKDIR=/dev/shm
LEGACY_LOCKFILE=/dev/shm/ozolith-ts-spike.lock
command -v flock >/dev/null || { echo "FATAL: these tests need util-linux flock (the harness does too)" >&2; exit 1; }

# --- the docker stub -------------------------------------------------------
# Stateful when DOCKER_STUB_COUNTER_DIR is set: created containers are
# recorded in $DOCKER_STUB_COUNTER_DIR/containers (docker run -d records the
# main container; the --rm scratch runs record only when a *_LINGER knob
# simulates a container surviving its own --rm), docker rm unrecords, and
# docker ps answers name-filter queries from the record — so the harness's
# query -> remove -> re-query proof loop runs against realistic state.
# Env knobs:
#   DOCKER_STUB_INFO_RC          docker info rc (daemon unreachable)
#   DOCKER_STUB_SECOPTS          docker info SecurityOptions JSON
#   DOCKER_STUB_VOLUME_EXISTS    volume inspect: 1 = exists
#   DOCKER_STUB_VOLINSPECT_ERR   volume inspect: daemon-level error (not "no such volume")
#   DOCKER_STUB_STATE_RC         state probe rc: 0 non-empty state, 1 empty/missing, >1 error
#   DOCKER_STUB_BUILD_RC / DOCKER_STUB_BUILD_SLEEP
#   DOCKER_STUB_PREFLIGHT_RC / DOCKER_STUB_PREFLIGHT_SLEEP
#   DOCKER_STUB_PREFLIGHT_MARKER  file touched when the preflight run starts
#   DOCKER_STUB_PREFLIGHT_LINGER  1 = the preflight scratch container survives its --rm
#   DOCKER_STUB_VOLSCAN_RC       state-volume scanner rc (0 hit, 1 clean, else error)
#   DOCKER_STUB_VOLSCAN_OUT      raw bytes the scanner tries to emit on BOTH
#                                stdout and stderr — must never surface
#   DOCKER_STUB_VOLSCAN_SLEEP    seconds the scanner run stays parked
#   DOCKER_STUB_VOLSCAN_MARKER    file touched when the scanner run starts
#   DOCKER_STUB_VOLSCAN_LINGER    1 = the scanner scratch container survives its --rm
#   DOCKER_STUB_TOP1_RC / DOCKER_STUB_TOP2_RC   first (live) / second (pre-stop) docker top
#   DOCKER_STUB_RM_RC            docker rm rc (every rm call)
#   DOCKER_STUB_RM_FAIL_MATCH    docker rm fails (rc 1) for names containing this substring
#   DOCKER_STUB_RM_KEEP          1 = docker rm reports success but the container survives
#   DOCKER_STUB_PS_RC            exact-name queries (name=^...$; the absence proofs) fail
#   DOCKER_STUB_PS_PREFIX_RC     prefix-name queries (the debris audit) fail
#   DOCKER_STUB_RUNNING          docker inspect .State.Running answer (default true)
#   DOCKER_STUB_NO_READY         1 = the container log omits the "==> ready" line
#   DOCKER_STUB_LOGS_RC          docker logs rc (the log cannot be captured)
#   DOCKER_STUB_LEAK             injected into every container-log emission
#   DOCKER_STUB_LEAK_LATE        injected only into NON-follow docker logs output —
#                                simulates a leak arriving after the ready-time snapshot
#   DOCKER_STUB_COUNTER_DIR      per-run scratch for stub state (top counter, container records)
#   DOCKER_STUB_CALLS            file logging every stubbed docker argv
cat >"$STUB/docker" <<'STUBEOF'
#!/bin/bash
cmd=${1:-}; shift || true
[ -n "${DOCKER_STUB_CALLS:-}" ] && printf 'docker %s %s\n' "$cmd" "$*" >>"$DOCKER_STUB_CALLS"
REC=""
[ -n "${DOCKER_STUB_COUNTER_DIR:-}" ] && REC="$DOCKER_STUB_COUNTER_DIR/containers"
rec_add() { [ -n "$REC" ] && printf '%s\n' "$1" >>"$REC"; }
rec_has() { [ -n "$REC" ] && [ -f "$REC" ] && grep -qxF -- "$1" "$REC"; }
rec_del() {
  rec_has "$1" || return 0
  grep -vxF -- "$1" "$REC" >"$REC.tmp" || true
  mv "$REC.tmp" "$REC"
}
case "$cmd" in
  info)
    if [ "${DOCKER_STUB_INFO_RC:-0}" != 0 ]; then
      echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock." >&2
      exit "${DOCKER_STUB_INFO_RC}"
    fi
    printf '%s\n' "${DOCKER_STUB_SECOPTS:-[\"name=seccomp,profile=builtin\",\"name=cgroupns\"]}"
    exit 0 ;;
  volume)
    case "${1:-}" in
      inspect)
        if [ -n "${DOCKER_STUB_VOLINSPECT_ERR:-}" ]; then
          echo "error during connect: stub daemon error" >&2
          exit 1
        fi
        [ "${DOCKER_STUB_VOLUME_EXISTS:-0}" = 1 ] && exit 0
        echo "Error response from daemon: get spike-tailscale-state: no such volume" >&2
        exit 1 ;;
      create) exit 0 ;;
    esac ;;
  build) sleep "${DOCKER_STUB_BUILD_SLEEP:-0}"; exit "${DOCKER_STUB_BUILD_RC:-0}" ;;
  rm)
    name=""
    for a in "$@"; do case "$a" in -*) ;; *) name=$a ;; esac; done
    [ "${DOCKER_STUB_RM_RC:-0}" != 0 ] && exit "${DOCKER_STUB_RM_RC}"
    case "$name" in *"${DOCKER_STUB_RM_FAIL_MATCH:-/never/}"*) exit 1 ;; esac
    [ "${DOCKER_STUB_RM_KEEP:-0}" = 1 ] && exit 0
    if rec_has "$name"; then rec_del "$name"; exit 0; fi
    [ -z "$REC" ] && exit 0 # stateless mode: legacy always-succeeds behavior
    echo "Error: No such container: $name" >&2
    exit 1 ;;
  ps)
    pats=(); exact=0
    for a in "$@"; do
      case "$a" in
        name=^*)
          pats+=("${a#name=^}")
          case "$a" in *\$) exact=1 ;; esac ;;
      esac
    done
    if [ "$exact" = 1 ]; then
      [ "${DOCKER_STUB_PS_RC:-0}" != 0 ] && exit "${DOCKER_STUB_PS_RC}"
    else
      [ "${DOCKER_STUB_PS_PREFIX_RC:-0}" != 0 ] && exit "${DOCKER_STUB_PS_PREFIX_RC}"
    fi
    { [ -n "$REC" ] && [ -f "$REC" ]; } || exit 0
    while IFS= read -r n; do
      for p in "${pats[@]}"; do
        case "$p" in
          *\$) [ "$n" = "${p%\$}" ] && { echo "id-$n"; break; } ;;
          *) case "$n" in "$p"*) echo "id-$n"; break ;; esac ;;
        esac
      done
    done <"$REC"
    exit 0 ;;
  run)
    name=""; prev=""
    for a in "$@"; do [ "$prev" = "--name" ] && name=$a; prev=$a; done
    case "$*" in
      *"tailscaled.state"*) exit "${DOCKER_STUB_STATE_RC:-1}" ;;
      *"tar -cf"*)
        [ -n "${DOCKER_STUB_VOLSCAN_MARKER:-}" ] && : >"$DOCKER_STUB_VOLSCAN_MARKER"
        sleep "${DOCKER_STUB_VOLSCAN_SLEEP:-0}"
        [ "${DOCKER_STUB_VOLSCAN_LINGER:-0}" = 1 ] && [ -n "$name" ] && rec_add "$name"
        if [ -n "${DOCKER_STUB_VOLSCAN_OUT:-}" ]; then
          printf '%s\n' "$DOCKER_STUB_VOLSCAN_OUT"
          printf '%s\n' "$DOCKER_STUB_VOLSCAN_OUT" >&2
        fi
        exit "${DOCKER_STUB_VOLSCAN_RC:-1}" ;;
      *"id -u"*)
        [ -n "${DOCKER_STUB_PREFLIGHT_MARKER:-}" ] && : >"$DOCKER_STUB_PREFLIGHT_MARKER"
        sleep "${DOCKER_STUB_PREFLIGHT_SLEEP:-0}"
        [ "${DOCKER_STUB_PREFLIGHT_LINGER:-0}" = 1 ] && [ -n "$name" ] && rec_add "$name"
        exit "${DOCKER_STUB_PREFLIGHT_RC:-0}" ;;
      *)
        [ -n "$name" ] && rec_add "$name"
        echo stub-container-id; exit 0 ;;
    esac ;;
  logs)
    if [ "${DOCKER_STUB_LOGS_RC:-0}" != 0 ]; then
      echo "stub: docker logs failed" >&2
      exit "${DOCKER_STUB_LOGS_RC}"
    fi
    follow=0
    for a in "$@"; do [ "$a" = "-f" ] && follow=1; done
    if [ "${DOCKER_STUB_STATE_RC:-1}" = 0 ]; then
      branch="==> existing state found: non-empty tailscaled.state — reusing identity, no auth key (gate item 3)"
    else
      branch="==> empty statedir: no non-empty tailscaled.state — fresh enrollment via file-form auth key (gate item 1)"
    fi
    cat <<EOF
==> running as uid 1000 (ozolith); expecting 1000/ozolith
==> tailscale version (must match the release the production example pins):
1.102.2
  go version: stub
$branch
==> up; status:
100.64.0.9 spike ozolith@ linux -
${DOCKER_STUB_LEAK:-}
EOF
    [ "${DOCKER_STUB_NO_READY:-0}" = 1 ] || echo "==> ready — from another tailnet machine: ssh ozolith@spike"
    if [ "$follow" = 0 ] && [ -n "${DOCKER_STUB_LEAK_LATE:-}" ]; then
      printf '%s\n' "$DOCKER_STUB_LEAK_LATE"
    fi
    exit 0 ;;
  inspect)
    case "$*" in
      *State.Running*) echo "${DOCKER_STUB_RUNNING:-true}" ;;
      *) echo '[{"Config":{"Env":["TS_AUTHKEY_FILE=/run/secrets/ts-authkey"]},"Mounts":[{"Destination":"/run/secrets/ts-authkey"}]}]' ;;
    esac
    exit 0 ;;
  top)
    n=1
    if [ -n "${DOCKER_STUB_COUNTER_DIR:-}" ]; then
      [ -f "$DOCKER_STUB_COUNTER_DIR/top.n" ] && n=$(( $(cat "$DOCKER_STUB_COUNTER_DIR/top.n") + 1 ))
      echo "$n" >"$DOCKER_STUB_COUNTER_DIR/top.n"
    fi
    if [ "$n" -eq 1 ]; then rc=${DOCKER_STUB_TOP1_RC:-0}; else rc=${DOCKER_STUB_TOP2_RC:-0}; fi
    [ "$rc" != 0 ] && exit "$rc"
    printf 'PID CMD\n1 /bin/sh /usr/local/bin/spike-entrypoint\n2 tailscaled --tun=userspace-networking\n'
    exit 0 ;;
  stop) exit 0 ;;
  history) printf 'IMAGE CREATED BY\nabc RUN curl -fsSL tailscale_1.102.2_amd64.tgz\n'; exit 0 ;;
esac
exit 0
STUBEOF
chmod +x "$STUB/docker"

# --- tailscale/tailscaled stubs for the entrypoint tests ---------------------
# Env knobs: TS_STUB_VERSION_RC, TS_STUB_STATUSJSON_RC, TS_STUB_UP_RC,
# TS_STUB_STATUS_RC, TS_STUB_ARGV_LOG (argv record), TSD_STUB_NO_SOCKET,
# TSD_STUB_LIFETIME (seconds the fake daemon stays alive).
TSSTUB=$TESTTMP/tsbin
mkdir -p "$TSSTUB"
cat >"$TSSTUB/tailscale" <<'STUBEOF'
#!/bin/bash
[ -n "${TS_STUB_ARGV_LOG:-}" ] && printf 'tailscale %s\n' "$*" >>"$TS_STUB_ARGV_LOG"
case "$*" in
  version*)
    [ "${TS_STUB_VERSION_RC:-0}" != 0 ] && exit "${TS_STUB_VERSION_RC}"
    echo "1.102.2"; echo "  go version: stub"; exit 0 ;;
  *"status --json"*) exit "${TS_STUB_STATUSJSON_RC:-0}" ;;
  *" up --ssh"*) exit "${TS_STUB_UP_RC:-0}" ;;
  *status*)
    [ "${TS_STUB_STATUS_RC:-0}" != 0 ] && exit "${TS_STUB_STATUS_RC}"
    echo "100.64.0.9 spike ozolith@ linux -"; exit 0 ;;
esac
exit 0
STUBEOF
cat >"$TSSTUB/tailscaled" <<'STUBEOF'
#!/bin/bash
[ -n "${TS_STUB_ARGV_LOG:-}" ] && printf 'tailscaled %s\n' "$*" >>"$TS_STUB_ARGV_LOG"
sock=""
for a in "$@"; do case "$a" in --socket=*) sock=${a#--socket=} ;; esac; done
if [ "${TSD_STUB_NO_SOCKET:-0}" != 1 ] && [ -n "$sock" ]; then
  rm -f "$sock"
  python3 -c 'import socket,sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$sock"
fi
sleep "${TSD_STUB_LIFETIME:-2}"
exit 0
STUBEOF
chmod +x "$TSSTUB/tailscale" "$TSSTUB/tailscaled"
command -v python3 >/dev/null || { echo "FATAL: these tests need python3 (unix-socket stub)" >&2; exit 1; }

fails=0
pass() { echo "  ok: $1"; }
fail() {
  echo "  FAIL: $1" >&2
  fails=1
}
expect_grep() { # $1: description, $2: file, $3: fixed string expected present
  grep -qF -- "$3" "$2" && pass "$1" || fail "$1"
}
expect_not_grep() { # $1: description, $2: file, $3: fixed string expected absent
  grep -qF -- "$3" "$2" && fail "$1" || pass "$1"
}
expect_nonzero() { # $1: description, $2: rc
  [[ "$2" -ne 0 ]] && pass "$1" || fail "$1 (rc=0)"
}

PATTERN=$TESTTMP/pattern
printf 'tskey-auth-UNITSECRET\n' >"$PATTERN"

# --- scan_file: tri-state, fail closed --------------------------------------

echo "== scan_file tri-state"
run_scan_file() { # $1: evidence path; prints scan output plus sweep_hit=N
  (
    source ./run-spike.sh
    SPIKE_KEY_FILE=$PATTERN
    scan_file "unit surface" "$1" 2>&1
    echo "sweep_hit=$sweep_hit"
  )
}

printf 'padding tskey-auth-UNITSECRET padding\n' >"$TESTTMP/ev-hit"
out=$(run_scan_file "$TESTTMP/ev-hit")
grep -q 'FAIL: key value found' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "grep rc 0 (hit) fails the sweep" || fail "grep rc 0 (hit) fails the sweep"

printf 'nothing secret here\n' >"$TESTTMP/ev-clean"
out=$(run_scan_file "$TESTTMP/ev-clean")
grep -q 'ok: key value absent' <<<"$out" && grep -q 'sweep_hit=0' <<<"$out" \
  && pass "grep rc 1 (absent) is the only pass" || fail "grep rc 1 (absent) is the only pass"

out=$(run_scan_file "$TESTTMP/ev-missing")
grep -q 'missing, unreadable, or empty' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "missing evidence fails closed" || fail "missing evidence fails closed"

: >"$TESTTMP/ev-empty"
out=$(run_scan_file "$TESTTMP/ev-empty")
grep -q 'missing, unreadable, or empty' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "empty evidence fails closed" || fail "empty evidence fails closed"

if [[ "$EUID" -ne 0 ]]; then # root reads through chmod 000
  printf 'x\n' >"$TESTTMP/ev-unreadable"
  chmod 000 "$TESTTMP/ev-unreadable"
  out=$(run_scan_file "$TESTTMP/ev-unreadable")
  grep -q 'missing, unreadable, or empty' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
    && pass "unreadable evidence fails closed" || fail "unreadable evidence fails closed"
  chmod 600 "$TESTTMP/ev-unreadable"
fi

mkdir "$TESTTMP/ev-dir" # passes the -r/-s precheck, then grep errors with rc 2
out=$(run_scan_file "$TESTTMP/ev-dir")
grep -q 'scanner error (grep rc=2)' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "grep rc 2 (scanner error) fails closed" || fail "grep rc 2 (scanner error) fails closed"

# --- state-volume scanner payload: every naming surface, statuses only ------
# The payload (STATE_SCAN_SCRIPT) is exactly what /bin/sh runs inside the
# scratch container; here it runs on the host against planted directories,
# so the tar-representation semantics — contents, pathnames, directory
# names, symlink targets — are tested for real, not through a stub. In every
# case the payload must emit NOTHING (the verdict is the exit status alone),
# and in particular never the key.

echo "== state-volume scanner payload: naming surfaces, fixed statuses, silence"
PAYDIR=$TESTTMP/payload
mkdir -p "$PAYDIR"
run_payload() { # $1: pattern file, $2: dir to scan — runs the container-side payload verbatim
  (
    source ./run-spike.sh
    sh -c "$STATE_SCAN_SCRIPT" scan "$1" "$2" "$PAYDIR/scan.tar"
  )
}
pay_case() { # $1: description, $2: pattern file, $3: dir, $4: expected rc
  rm -f "$PAYDIR/scan.tar"
  local rc=0 out
  out=$(run_payload "$2" "$3" 2>&1) || rc=$?
  [[ "$rc" -eq "$4" ]] && pass "$1 (rc=$4)" || fail "$1 (want rc=$4, got rc=$rc)"
  [[ -z "$out" ]] && pass "$1: payload emits nothing" || fail "$1: payload emits nothing"
  grep -qF -- 'tskey-auth-UNITSECRET' <<<"$out" \
    && fail "$1: output is key-free" || pass "$1: output is key-free"
}

d=$PAYDIR/content-hit
mkdir -p "$d"
printf 'padding tskey-auth-UNITSECRET padding\n' >"$d/tailscaled.state"
pay_case "content hit (key inside a file)" "$PATTERN" "$d" 0

d=$PAYDIR/filename-hit
mkdir -p "$d"
: >"$d/leak-tskey-auth-UNITSECRET-leak" # zero bytes: the NAME is the only carrier
pay_case "filename-only hit (empty file, key in the name)" "$PATTERN" "$d" 0

d=$PAYDIR/dirname-hit
mkdir -p "$d/dir-tskey-auth-UNITSECRET-dir" # empty dir: its NAME is the only carrier
pay_case "directory-name hit (empty directory)" "$PATTERN" "$d" 0

d=$PAYDIR/symlink-hit
mkdir -p "$d"
ln -s "dangling-tskey-auth-UNITSECRET-target" "$d/link" # the TARGET STRING is the only carrier
pay_case "symlink-target hit (dangling target string)" "$PATTERN" "$d" 0

d=$PAYDIR/clean
mkdir -p "$d/sub"
printf 'nothing secret here\n' >"$d/sub/tailscaled.state"
ln -s innocent-target "$d/link"
pay_case "clean volume (contents, names, dirs, targets all innocent)" "$PATTERN" "$d" 1

pay_case "traversal error (nonexistent directory) fails closed" "$PATTERN" "$PAYDIR/does-not-exist" 3

if [[ "$EUID" -ne 0 ]]; then # root traverses through chmod 000
  d=$PAYDIR/unreadable
  mkdir -p "$d/blocked"
  : >"$d/blocked/f"
  chmod 000 "$d/blocked"
  pay_case "unreadable subdirectory (archive error) fails closed" "$PATTERN" "$d" 3
  chmod 700 "$d/blocked"

  cp "$PATTERN" "$PAYDIR/pattern-blocked"
  chmod 000 "$PAYDIR/pattern-blocked"
  pay_case "grep failure (unreadable pattern file) fails closed" "$PAYDIR/pattern-blocked" "$PAYDIR/clean" 3
  chmod 600 "$PAYDIR/pattern-blocked"
fi

# --- scan_state_volume: tri-state, fail closed, raw output discarded ---------

echo "== scan_state_volume: status plumbing (stubbed docker)"
run_scan_vol() { # $1: scanner rc, $2: raw bytes the scanner tries to emit
  (
    source ./run-spike.sh
    SPIKE_KEY_FILE=$PATTERN
    export PATH="$STUB:$PATH" DOCKER_STUB_VOLSCAN_RC="$1" DOCKER_STUB_VOLSCAN_OUT="${2:-}"
    scan_state_volume 2>&1
    echo "sweep_hit=$sweep_hit"
  )
}

out=$(run_scan_vol 0 "/state/dir-tskey-auth-UNITSECRET/hit")
grep -q 'FAIL: key value found on the state volume' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "scanner rc 0 (hit) fails the sweep" || fail "scanner rc 0 (hit) fails the sweep"
grep -qF -- 'tskey-auth-UNITSECRET' <<<"$out" \
  && fail "a hit prints no raw scanner data (a matching pathname can BE the key)" \
  || pass "a hit prints no raw scanner data (a matching pathname can BE the key)"

out=$(run_scan_vol 1)
grep -q 'ok: key value absent from the' <<<"$out" && grep -q 'sweep_hit=0' <<<"$out" \
  && pass "scanner rc 1 (clean miss) is the only pass" || fail "scanner rc 1 (clean miss) is the only pass"

out=$(run_scan_vol 3 "tar: ./f-tskey-auth-UNITSECRET: Cannot open: Permission denied")
grep -q 'state-volume scanner error (rc=3)' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "payload archive/grep error (rc=3) fails closed" || fail "payload archive/grep error (rc=3) fails closed"
grep -qF -- 'tskey-auth-UNITSECRET' <<<"$out" \
  && fail "scanner stderr is discarded (a traversal error can name a key-bearing path)" \
  || pass "scanner stderr is discarded (a traversal error can name a key-bearing path)"

out=$(run_scan_vol 125 "docker: daemon error naming tskey-auth-UNITSECRET somehow")
grep -q 'state-volume scanner error (rc=125)' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "docker-level failure (rc=125) fails closed" || fail "docker-level failure (rc=125) fails closed"
grep -qF -- 'tskey-auth-UNITSECRET' <<<"$out" \
  && fail "docker-level stderr is discarded too" || pass "docker-level stderr is discarded too"

# --- cleanup: failure-aware, order-preserving, status-preserving -------------

echo "== cleanup unit behavior"
run_cleanup() { # $1: simulated exit status, $2: evidence dir or "", $3: key dir or ""
  (
    source ./run-spike.sh
    set +e
    EVIDENCE_DIR=$2
    SPIKE_KEY_DIR=$3
    LOG_PID=""
    CONTAINER=""
    (exit "$1")
    cleanup
  ) 2>&1
}

ev=$(mktemp -d "$TESTTMP/ev.XXXXXX")
key=$(mktemp -d "$TESTTMP/key.XXXXXX")
rc=0
out=$(run_cleanup 7 "$ev" "$key") || rc=$?
[[ "$rc" -eq 7 ]] && pass "original nonzero status (7) is preserved" || fail "original nonzero status (7) is preserved (got $rc)"
grep -qF "temporary key directory removed (proof" <<<"$out" \
  && pass "key-dir removal proof printed" || fail "key-dir removal proof printed"
[[ ! -e "$ev" && ! -e "$key" ]] && pass "evidence and key dirs really removed" || fail "evidence and key dirs really removed"

rc=0
out=$(
  (
    source ./run-spike.sh
    set +e
    sleep 30 &
    LOG_PID=$!
    echo "logpid=$LOG_PID"
    EVIDENCE_DIR=""
    SPIKE_KEY_DIR=""
    CONTAINER=""
    (exit 0)
    cleanup
  ) 2>&1
) || rc=$?
lp=$(sed -n 's/^logpid=//p' <<<"$out")
[[ "$rc" -eq 0 ]] && pass "clean teardown with a live log process exits 0" || fail "clean teardown with a live log process exits 0 (got $rc)"
if [[ -n "$lp" ]] && kill -0 "$lp" 2>/dev/null; then
  fail "log-follow process terminated by cleanup"
  kill -9 "$lp" 2>/dev/null || true
else
  pass "log-follow process terminated by cleanup"
fi

if [[ "$EUID" -ne 0 ]]; then # root removes through chmod 000
  echo "== cleanup: evidence-dir removal failure (later steps still run)"
  ev=$(mktemp -d "$TESTTMP/ev.XXXXXX")
  mkdir "$ev/poison" && : >"$ev/poison/f" && chmod 000 "$ev/poison"
  key=$(mktemp -d "$TESTTMP/key.XXXXXX")
  rc=0
  out=$(run_cleanup 0 "$ev" "$key") || rc=$?
  expect_nonzero "evidence removal failure turns a success nonzero" "$rc"
  grep -qF "evidence directory STILL EXISTS" <<<"$out" \
    && pass "evidence-dir failure is recorded" || fail "evidence-dir failure is recorded"
  grep -qF "temporary key directory removed (proof" <<<"$out" \
    && pass "later key-dir step still ran after the failure" || fail "later key-dir step still ran after the failure"
  [[ ! -e "$key" ]] && pass "key dir removed despite the earlier failure" || fail "key dir removed despite the earlier failure"
  chmod 700 "$ev/poison" && rm -rf "$ev"

  echo "== cleanup: key-dir removal failure"
  ev=$(mktemp -d "$TESTTMP/ev.XXXXXX")
  key=$(mktemp -d "$TESTTMP/key.XXXXXX")
  mkdir "$key/poison" && : >"$key/poison/f" && chmod 000 "$key/poison"
  rc=0
  out=$(run_cleanup 0 "$ev" "$key") || rc=$?
  expect_nonzero "key-dir removal failure turns a success nonzero" "$rc"
  grep -qF "temporary key directory STILL EXISTS" <<<"$out" \
    && pass "unremoved key dir is loudly reported" || fail "unremoved key dir is loudly reported"
  [[ ! -e "$ev" ]] && pass "earlier evidence-dir step still ran" || fail "earlier evidence-dir step still ran"
  chmod 700 "$key/poison" && rm -rf "$key"
fi

# --- entrypoint: branch selection + failure propagation ----------------------

echo "== entrypoint: state-based branch selection and mandatory commands"
EPDIR=$TESTTMP/ep
EPSTATE=$EPDIR/state
EPSOCK=$EPDIR/ts.sock
mkdir -p "$EPSTATE"
printf 'tskey-auth-EPSECRET\n' >"$EPDIR/authkey"

run_ep() { # $1: out-file, then env overrides as NAME=VALUE...
  local out=$1
  shift
  rm -f "$EPSOCK" "$EPDIR/argv.log"
  local rc=0
  env PATH="$TSSTUB:$PATH" \
    SPIKE_STATEDIR="$EPSTATE" SPIKE_SOCKET="$EPSOCK" \
    SPIKE_READY_SECS=3 SPIKE_POLL_SECS=1 \
    TS_AUTHKEY_FILE="$EPDIR/authkey" TS_STUB_ARGV_LOG="$EPDIR/argv.log" \
    "$@" sh ./entrypoint.sh >"$out" 2>&1 || rc=$?
  return "$rc"
}

rm -f "$EPSTATE/tailscaled.state"
rc=0
run_ep "$TESTTMP/ep-fresh" TSD_STUB_LIFETIME=1 || rc=$?
expect_grep "missing state file takes the fresh branch" "$TESTTMP/ep-fresh" "empty statedir"
expect_grep "ready printed once every required command passed" "$TESTTMP/ep-fresh" "==> ready"
expect_nonzero "post-enrollment daemon death exits nonzero" "$rc"
expect_grep "post-enrollment daemon death is reported" "$TESTTMP/ep-fresh" "tailscaled exited post-enrollment"
expect_grep "fresh up passes the key by PATH (file: form)" "$EPDIR/argv.log" "--auth-key=file:$EPDIR/authkey"
expect_not_grep "the key value never enters argv" "$EPDIR/argv.log" "tskey-auth-EPSECRET"

printf 'FAKE-NODE-STATE\n' >"$EPSTATE/tailscaled.state"
rc=0
run_ep "$TESTTMP/ep-reuse" TSD_STUB_LIFETIME=1 || rc=$?
expect_grep "non-empty state takes the reuse branch" "$TESTTMP/ep-reuse" "existing state found"
expect_grep "reuse still reaches ready" "$TESTTMP/ep-reuse" "==> ready"
expect_not_grep "reuse up is keyless (no --auth-key in argv)" "$EPDIR/argv.log" "--auth-key"

: >"$EPSTATE/tailscaled.state" # zero bytes: what a failed first attempt leaves
rc=0
run_ep "$TESTTMP/ep-empty" TSD_STUB_LIFETIME=1 || rc=$?
expect_grep "EMPTY state file takes the fresh branch" "$TESTTMP/ep-empty" "empty statedir"
expect_grep "empty-state run enrolls with the file-form key" "$EPDIR/argv.log" "--auth-key=file:"

rm -f "$EPSTATE/tailscaled.state"
rc=0
run_ep "$TESTTMP/ep-badver" TS_STUB_VERSION_RC=1 TSD_STUB_LIFETIME=1 || rc=$?
expect_nonzero "tailscale version failure exits nonzero" "$rc"
expect_grep "version failure is reported" "$TESTTMP/ep-badver" "FAIL: tailscale version failed"
expect_not_grep "no ready after a version failure" "$TESTTMP/ep-badver" "==> ready"

rc=0
run_ep "$TESTTMP/ep-earlydeath" TSD_STUB_NO_SOCKET=1 TSD_STUB_LIFETIME=0.2 || rc=$?
expect_nonzero "daemon death before ready exits nonzero" "$rc"
expect_grep "pre-ready death is distinguished from a timeout" "$TESTTMP/ep-earlydeath" "exited before its LocalAPI"
expect_not_grep "no ready after pre-ready death" "$TESTTMP/ep-earlydeath" "==> ready"

rc=0
run_ep "$TESTTMP/ep-noapi" TS_STUB_STATUSJSON_RC=1 TSD_STUB_LIFETIME=8 || rc=$?
expect_nonzero "socket without a responding LocalAPI is not ready" "$rc"
expect_grep "bounded LocalAPI wait times out" "$TESTTMP/ep-noapi" "LocalAPI not ready after 3s"
expect_not_grep "no ready on LocalAPI timeout" "$TESTTMP/ep-noapi" "==> ready"

rc=0
run_ep "$TESTTMP/ep-upfail" TS_STUB_UP_RC=1 TSD_STUB_LIFETIME=3 || rc=$?
expect_nonzero "fresh up failure exits nonzero" "$rc"
expect_grep "fresh up failure is reported" "$TESTTMP/ep-upfail" "FAIL: enrollment failed (gate item 1)"
expect_not_grep "no ready after an up failure" "$TESTTMP/ep-upfail" "==> ready"

printf 'FAKE-NODE-STATE\n' >"$EPSTATE/tailscaled.state"
rc=0
run_ep "$TESTTMP/ep-reupfail" TS_STUB_UP_RC=1 TSD_STUB_LIFETIME=3 || rc=$?
expect_nonzero "reuse up failure (e.g. corrupt state) exits nonzero" "$rc"
expect_grep "reuse up failure is reported visibly" "$TESTTMP/ep-reupfail" "FAIL: up with existing state failed (gate item 3)"
[[ -s "$EPSTATE/tailscaled.state" ]] && pass "corrupt state is never deleted" || fail "corrupt state is never deleted"

rm -f "$EPSTATE/tailscaled.state"
rc=0
run_ep "$TESTTMP/ep-statusfail" TS_STUB_STATUS_RC=1 TSD_STUB_LIFETIME=3 || rc=$?
expect_nonzero "post-up status failure exits nonzero" "$rc"
expect_grep "post-up status failure is reported" "$TESTTMP/ep-statusfail" "FAIL: post-up status failed"
expect_not_grep "no ready after a status failure" "$TESTTMP/ep-statusfail" "==> ready"

# --- end-to-end: mode selection, captures, cleanup, keylessness ---------------

key_dirs() { compgen -G '/dev/shm/ozolith-ts-spike-key.*' || true; }
assert_no_new_key_dirs() { # $1: description, $2: dirs before
  [[ "$(key_dirs)" == "$2" ]] && pass "$1" || fail "$1"
}
assert_lock_free() { # $1: description — the run lock must not be held
  flock -n "$LOCKDIR" true 2>/dev/null && pass "$1" || fail "$1"
}
lock_released_within() { # $1: seconds — poll until the run lock is free
  local i
  for ((i = 0; i < $1 * 10; i++)); do
    flock -n "$LOCKDIR" true 2>/dev/null && return 0
    sleep 0.1
  done
  return 1
}
evidence_dirs() { compgen -G '/dev/shm/ozolith-ts-evidence.*' || true; }
run_e2e() { # $1: out-file, then env overrides as NAME=VALUE...; stdin = key
  local out=$1
  shift
  local cdir
  cdir=$(mktemp -d "$TESTTMP/stubstate.XXXXXX")
  local rc=0
  env PATH="$STUB:$PATH" SPIKE_NONINTERACTIVE=1 SPIKE_SSH_WAIT_SECS=1 \
    DOCKER_STUB_COUNTER_DIR="$cdir" "$@" \
    ./run-spike.sh >"$out" 2>&1 || rc=$?
  return "$rc"
}

echo "== end-to-end: normal exit (absent volume -> fresh enrollment, sweep passes)"
KEY="tskey-auth-E2E-$$-${RANDOM}"
before=$(key_dirs)
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-normal" DOCKER_STUB_CALLS="$TESTTMP/calls-normal" || rc=$?
[[ "$rc" -eq 0 ]] && pass "exits 0" || fail "exits 0 (got $rc)"
expect_grep "the run lock is taken first" "$TESTTMP/out-normal" "run lock acquired"
expect_grep "the debris audit ran clean" "$TESTTMP/out-normal" "debris audit clean"
expect_grep "daemon mode positively verified" "$TESTTMP/out-normal" "ordinary root daemon"
expect_grep "absent volume selects the fresh path" "$TESTTMP/out-normal" "FRESH run"
expect_grep "uid-1000 readability preflight ran" "$TESTTMP/out-normal" "readable as uid 1000"
expect_grep "the ready-time excerpt is scan-gated" "$TESTTMP/out-normal" "scanned for the exact key value first — clean miss"
expect_grep "the scanned-clean log content is then shown" "$TESTTMP/out-normal" "100.64.0.9"
expect_grep "sweep passed on all six surfaces" "$TESTTMP/out-normal" "key-absence sweep: passed on all six surfaces"
expect_grep "evidence records the exact version" "$TESTTMP/out-normal" "tailscale version: 1.102.2"
expect_grep "main container runs under a per-run name" "$TESTTMP/calls-normal" "--name ozolith-ts-spike-main-"
expect_grep "main-container removal proven under the per-run name" "$TESTTMP/out-normal" "main container removed (proof"
expect_grep "auto-removed preflight scratch counts as already clean" "$TESTTMP/out-normal" "preflight container already absent (proof"
expect_grep "auto-removed scanner scratch counts as already clean" "$TESTTMP/out-normal" "state-scanner container already absent (proof"
expect_grep "preflight scratch run is named for tracking" "$TESTTMP/calls-normal" "--name ozolith-ts-spike-preflight-"
expect_grep "scanner scratch run is named for tracking" "$TESTTMP/calls-normal" "--name ozolith-ts-spike-keyscan-"
expect_grep "cleanup proof printed" "$TESTTMP/out-normal" "temporary key directory removed (proof"
expect_grep "the fresh volume is created" "$TESTTMP/calls-normal" "volume create"
if grep -E 'docker rm -f ozolith-ts-spike($| )' "$TESTTMP/calls-normal" | grep -q .; then
  fail "the legacy fixed name is never removed (no unconditional docker rm)"
else
  pass "the legacy fixed name is never removed (no unconditional docker rm)"
fi
if grep -E '^docker rm ' "$TESTTMP/calls-normal" \
  | grep -Ev ' ozolith-ts-spike-(main|preflight|keyscan)-[0-9]+-[0-9]+$' | grep -q .; then
  fail "every docker rm targets a current-run per-run name only"
else
  pass "every docker rm targets a current-run per-run name only"
fi
expect_not_grep "key value never printed by the harness" "$TESTTMP/out-normal" "$KEY"
expect_not_grep "key value never enters any docker argv" "$TESTTMP/calls-normal" "$KEY"
assert_no_new_key_dirs "key directory gone after normal exit" "$before"
assert_lock_free "the run lock is released after a successful run"

echo "== end-to-end: a key already in the log is caught at the FIRST display gate"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-leak" DOCKER_STUB_LEAK="$KEY" || rc=$?
expect_nonzero "exits non-zero on a hit" "$rc"
expect_grep "the hit is caught before anything log-derived prints" "$TESTTMP/out-leak" "ALREADY contains the exact auth-key value"
expect_grep "immediate revocation is demanded" "$TESTTMP/out-leak" "REVOKE THE AUTH KEY IMMEDIATELY"
expect_not_grep "the key value itself never prints" "$TESTTMP/out-leak" "$KEY"
expect_not_grep "no log content is displayed at all" "$TESTTMP/out-leak" "100.64.0.9"
expect_not_grep "no evidence block on a withheld log" "$TESTTMP/out-leak" "sanitized evidence block"
expect_grep "cleanup proof still printed" "$TESTTMP/out-leak" "temporary key directory removed (proof"
assert_no_new_key_dirs "key directory gone after ready-time hit" "$before"
assert_lock_free "the run lock is released after a ready-time hit"

echo "== end-to-end: a LATE leak in an evidence-selected line is withheld with the block"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-leaklate" \
  DOCKER_STUB_LEAK_LATE="==> running as uid 1000 leaked $KEY" || rc=$?
expect_nonzero "exits non-zero on a late hit" "$rc"
expect_grep "the ready-time snapshot was still clean" "$TESTTMP/out-leaklate" "scanned for the exact key value first — clean miss"
expect_grep "the sweep names the hit surface" "$TESTTMP/out-leaklate" "FAIL: key value found in full container log"
expect_grep "the sweep failure withholds the block" "$TESTTMP/out-leaklate" "key-absence sweep FAILED"
expect_grep "immediate revocation is demanded" "$TESTTMP/out-leaklate" "REVOKE THE AUTH KEY IMMEDIATELY"
expect_not_grep "the evidence block is withheld" "$TESTTMP/out-leaklate" "sanitized evidence block"
expect_not_grep "the selected uid line is withheld too" "$TESTTMP/out-leaklate" "leaked"
expect_not_grep "the key value itself never prints" "$TESTTMP/out-leaklate" "$KEY"
assert_no_new_key_dirs "key directory gone after late-leak failure" "$before"

echo "== end-to-end: early container failure — diagnostics only after a clean scan"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-earlyclean" DOCKER_STUB_NO_READY=1 \
  DOCKER_STUB_RUNNING=false || rc=$?
expect_nonzero "early exit fails the run" "$rc"
expect_grep "the early failure is named" "$TESTTMP/out-earlyclean" "container exited before reaching ready"
expect_grep "the partial log is scanned before display" "$TESTTMP/out-earlyclean" "scanned for the exact key value first — clean miss"
expect_grep "the clean partial log IS displayed" "$TESTTMP/out-earlyclean" "100.64.0.9"
expect_grep "cleanup proof after early failure" "$TESTTMP/out-earlyclean" "temporary key directory removed (proof"

echo "== end-to-end: early failure with a leaking log is withheld, value-free"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-earlyleak" DOCKER_STUB_NO_READY=1 \
  DOCKER_STUB_RUNNING=false DOCKER_STUB_LEAK="$KEY" || rc=$?
expect_nonzero "early exit with a leak fails the run" "$rc"
expect_grep "the log is withheld on a hit" "$TESTTMP/out-earlyleak" "WITHHELD: it contains the exact auth-key value"
expect_grep "immediate revocation is demanded" "$TESTTMP/out-earlyleak" "REVOKE THE AUTH KEY IMMEDIATELY"
expect_not_grep "the key value itself never prints" "$TESTTMP/out-earlyleak" "$KEY"
expect_not_grep "no log content is displayed" "$TESTTMP/out-earlyleak" "100.64.0.9"

echo "== end-to-end: early failure whose log cannot be captured is withheld, value-free"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-earlynolog" DOCKER_STUB_NO_READY=1 \
  DOCKER_STUB_RUNNING=false DOCKER_STUB_LOGS_RC=1 || rc=$?
expect_nonzero "an uncapturable early-failure log fails the run" "$rc"
expect_grep "the missing capture is named" "$TESTTMP/out-earlynolog" "could not be captured"
expect_grep "immediate revocation is demanded" "$TESTTMP/out-earlynolog" "REVOKE THE AUTH KEY IMMEDIATELY"
expect_not_grep "no log content is displayed" "$TESTTMP/out-earlynolog" "100.64.0.9"
assert_no_new_key_dirs "key directory gone after the early-failure trio" "$before"

echo "== end-to-end: valid retained state -> keyless reuse"
rc=0
run_e2e "$TESTTMP/out-reuse" DOCKER_STUB_VOLUME_EXISTS=1 DOCKER_STUB_STATE_RC=0 \
  DOCKER_STUB_CALLS="$TESTTMP/calls-reuse" </dev/null || rc=$?
[[ "$rc" -eq 0 ]] && pass "exits 0" || fail "exits 0 (got $rc)"
expect_grep "non-empty state selects the reuse path" "$TESTTMP/out-reuse" "REUSE run"
expect_grep "the container log shows the existing-state branch" "$TESTTMP/out-reuse" "existing state found"
expect_grep "sweep deliberately not run keyless" "$TESTTMP/out-reuse" "key-absence sweep: not run (reuse run"
expect_not_grep "no key material was created" "$TESTTMP/out-reuse" "temporary key directory"
expect_not_grep "the existing volume is not re-created" "$TESTTMP/calls-reuse" "volume create"
assert_no_new_key_dirs "no key directory appeared at all" "$before"

echo "== end-to-end: existing volume WITHOUT usable state -> fresh enrollment (recovery)"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-recover" DOCKER_STUB_VOLUME_EXISTS=1 \
  DOCKER_STUB_STATE_RC=1 DOCKER_STUB_CALLS="$TESTTMP/calls-recover" || rc=$?
[[ "$rc" -eq 0 ]] && pass "exits 0" || fail "exits 0 (got $rc)"
expect_grep "empty-state volume recovers via fresh enrollment" "$TESTTMP/out-recover" "recovery from a failed attempt"
expect_grep "recovery run sweeps like any fresh run" "$TESTTMP/out-recover" "key-absence sweep: passed on all six surfaces"
expect_not_grep "the existing volume is NOT deleted or re-created" "$TESTTMP/calls-recover" "volume create"
expect_not_grep "no volume rm either" "$TESTTMP/calls-recover" "volume rm"
assert_no_new_key_dirs "key directory gone after recovery run" "$before"

echo "== end-to-end: docker/volume probe errors fail closed BEFORE any key intake"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-volerr" DOCKER_STUB_VOLINSPECT_ERR=1 || rc=$?
expect_nonzero "volume-inspect error exits non-zero" "$rc"
expect_grep "the ambiguity is named" "$TESTTMP/out-volerr" "cannot tell fresh from reuse"
expect_not_grep "no key was read on a probe error" "$TESTTMP/out-volerr" "temporary key directory"
assert_no_new_key_dirs "no key directory on a volume-inspect error" "$before"

rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-staterr" DOCKER_STUB_VOLUME_EXISTS=1 DOCKER_STUB_STATE_RC=125 || rc=$?
expect_nonzero "state-probe error exits non-zero" "$rc"
expect_grep "the probe failure is named" "$TESTTMP/out-staterr" "cannot tell fresh from reuse"
expect_not_grep "no key was read on a state-probe error" "$TESTTMP/out-staterr" "temporary key directory"
assert_no_new_key_dirs "no key directory on a state-probe error" "$before"

echo "== end-to-end: unsupported daemon modes are rejected before secret intake"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-rootless" \
  DOCKER_STUB_SECOPTS='["name=seccomp,profile=builtin","name=rootless"]' || rc=$?
expect_nonzero "rootless daemon is rejected" "$rc"
expect_grep "rootless rejection is explicit" "$TESTTMP/out-rootless" "rootless Docker daemon detected"
expect_not_grep "no key was read under rootless" "$TESTTMP/out-rootless" "temporary key directory"
assert_no_new_key_dirs "no key directory under rootless" "$before"

rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-userns" \
  DOCKER_STUB_SECOPTS='["name=seccomp,profile=builtin","name=userns"]' || rc=$?
expect_nonzero "userns-remap daemon is rejected" "$rc"
expect_grep "userns rejection is explicit" "$TESTTMP/out-userns" "userns-remap Docker daemon detected"
assert_no_new_key_dirs "no key directory under userns-remap" "$before"

rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-noinfo" DOCKER_STUB_INFO_RC=1 || rc=$?
expect_nonzero "unreachable daemon is rejected" "$rc"
expect_grep "docker info failure is named" "$TESTTMP/out-noinfo" "cannot query the Docker daemon"
assert_no_new_key_dirs "no key directory when the daemon is unreachable" "$before"

echo "== end-to-end: key readability preflight failure"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-preflight" DOCKER_STUB_PREFLIGHT_RC=1 || rc=$?
expect_nonzero "preflight failure exits non-zero" "$rc"
expect_grep "preflight failure is reported" "$TESTTMP/out-preflight" "FAIL: preflight"
expect_grep "cleanup proof printed after preflight failure" "$TESTTMP/out-preflight" "temporary key directory removed (proof"
assert_no_new_key_dirs "key directory gone after preflight failure" "$before"

echo "== end-to-end: preflight docker run failure with a LINGERING scratch container"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-preflinger" DOCKER_STUB_PREFLIGHT_RC=125 \
  DOCKER_STUB_PREFLIGHT_LINGER=1 DOCKER_STUB_CALLS="$TESTTMP/calls-preflinger" || rc=$?
expect_nonzero "preflight docker failure exits non-zero" "$rc"
expect_grep "the lingering preflight scratch is removed WITH proof" "$TESTTMP/out-preflinger" "preflight container removed (proof"
expect_grep "key-dir proof still printed" "$TESTTMP/out-preflinger" "temporary key directory removed (proof"
expect_not_grep "no key value in output" "$TESTTMP/out-preflinger" "$KEY"
expect_not_grep "no key value in any docker argv (incl. scratch names)" "$TESTTMP/calls-preflinger" "$KEY"
assert_no_new_key_dirs "key directory gone after lingering preflight" "$before"

echo "== end-to-end: scanner docker run failure with a LINGERING scratch container"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-scanlinger" DOCKER_STUB_VOLSCAN_RC=125 \
  DOCKER_STUB_VOLSCAN_LINGER=1 || rc=$?
expect_nonzero "scanner docker failure exits non-zero (sweep fails closed)" "$rc"
expect_grep "the sweep records the scanner error" "$TESTTMP/out-scanlinger" "state-volume scanner error (rc=125)"
expect_grep "the lingering scanner scratch is removed WITH proof" "$TESTTMP/out-scanlinger" "state-scanner container removed (proof"
expect_grep "the main container was still cleaned too" "$TESTTMP/out-scanlinger" "==> main container removed (proof"
expect_grep "key-dir proof still printed" "$TESTTMP/out-scanlinger" "temporary key directory removed (proof"
assert_no_new_key_dirs "key directory gone after lingering scanner" "$before"

echo "== end-to-end: docker rm failure on the preflight scratch container"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-prefrmfail" DOCKER_STUB_PREFLIGHT_LINGER=1 \
  DOCKER_STUB_RM_FAIL_MATCH=preflight || rc=$?
expect_nonzero "preflight-scratch rm failure turns a passing run non-zero" "$rc"
expect_grep "the sweep itself had passed" "$TESTTMP/out-prefrmfail" "key-absence sweep: passed"
expect_grep "the preflight rm failure is recorded" "$TESTTMP/out-prefrmfail" "CLEANUP FAIL: docker rm -f ozolith-ts-spike-preflight-"
expect_grep "the surviving preflight scratch is loud" "$TESTTMP/out-prefrmfail" "STILL EXISTS after docker rm -f"
expect_grep "unproven scratch removal demands revocation" "$TESTTMP/out-prefrmfail" "REVOKE THE AUTH KEY IMMEDIATELY"
expect_grep "the scanner scratch step still ran after the failure" "$TESTTMP/out-prefrmfail" "state-scanner container already absent (proof"
expect_grep "the key-dir step still ran after the failure" "$TESTTMP/out-prefrmfail" "temporary key directory removed (proof"
expect_not_grep "the revoke warning stays value-free" "$TESTTMP/out-prefrmfail" "$KEY"
assert_no_new_key_dirs "key directory gone despite preflight rm failure" "$before"

echo "== end-to-end: docker rm failure on the scanner scratch container"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-scanrmfail" DOCKER_STUB_VOLSCAN_LINGER=1 \
  DOCKER_STUB_RM_FAIL_MATCH=keyscan || rc=$?
expect_nonzero "scanner-scratch rm failure turns a passing run non-zero" "$rc"
expect_grep "the scanner rm failure is recorded" "$TESTTMP/out-scanrmfail" "CLEANUP FAIL: docker rm -f ozolith-ts-spike-keyscan-"
expect_grep "the surviving scanner scratch is loud" "$TESTTMP/out-scanrmfail" "STILL EXISTS after docker rm -f"
expect_grep "unproven scratch removal demands revocation" "$TESTTMP/out-scanrmfail" "REVOKE THE AUTH KEY IMMEDIATELY"
expect_grep "the earlier main-container step had run" "$TESTTMP/out-scanrmfail" "==> main container removed (proof"
expect_grep "the key-dir step still ran after the failure" "$TESTTMP/out-scanrmfail" "temporary key directory removed (proof"
assert_no_new_key_dirs "key directory gone despite scanner rm failure" "$before"

echo "== end-to-end: stale containers of EVERY name generation are rejected before key intake"
for stalename in ozolith-ts-spike ozolith-ts-spike-main-9999-42 \
  ozolith-ts-spike-preflight-9999-42 ozolith-ts-spike-keyscan-9999-42; do
  staledir=$(mktemp -d "$TESTTMP/stubstate.XXXXXX")
  printf '%s\n' "$stalename" >"$staledir/containers"
  rc=0
  printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-stale" DOCKER_STUB_COUNTER_DIR="$staledir" \
    DOCKER_STUB_CALLS="$TESTTMP/calls-stale" || rc=$?
  expect_nonzero "a stale $stalename container refuses the run" "$rc"
  expect_grep "the stale debris is named ($stalename)" "$TESTTMP/out-stale" "stale spike container"
  expect_grep "the operator is told to revoke the prior key ($stalename)" "$TESTTMP/out-stale" "REVOKE the key that run used"
  expect_not_grep "refusal happens before any key intake ($stalename)" "$TESTTMP/out-stale" "temporary key directory"
  expect_not_grep "the stale container is NOT auto-removed ($stalename)" "$TESTTMP/calls-stale" "docker rm"
  grep -qxF "$stalename" "$staledir/containers" \
    && pass "the debris still exists afterwards ($stalename)" \
    || fail "the debris still exists afterwards ($stalename)"
  rm -f "$TESTTMP/calls-stale"
done
assert_no_new_key_dirs "no key directory on stale-container refusal" "$before"

echo "== end-to-end: a failing debris-audit query fails closed before key intake"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-staleq" DOCKER_STUB_PS_PREFIX_RC=1 || rc=$?
expect_nonzero "a failing debris query fails closed" "$rc"
expect_grep "the audit-query failure is named" "$TESTTMP/out-staleq" "cannot audit for stale spike containers"
expect_not_grep "no key intake on an audit-query failure" "$TESTTMP/out-staleq" "temporary key directory"
assert_no_new_key_dirs "no key directory on an audit-query failure" "$before"

echo "== end-to-end: stale tmpfs key/evidence directories are rejected before key intake"
for pattern in /dev/shm/ozolith-ts-spike-key.XXXXXX /dev/shm/ozolith-ts-evidence.XXXXXX; do
  fakedir=$(mktemp -d "$pattern")
  SHM_DEBRIS+=("$fakedir")
  rc=0
  printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-shmdebris" \
    DOCKER_STUB_CALLS="$TESTTMP/calls-shmdebris" || rc=$?
  expect_nonzero "stale $pattern debris refuses the run" "$rc"
  expect_grep "the tmpfs debris is named ($pattern)" "$TESTTMP/out-shmdebris" "stale spike tmpfs debris"
  expect_grep "the operator is told to revoke the prior key ($pattern)" "$TESTTMP/out-shmdebris" "REVOKE the key that run used"
  expect_not_grep "refusal happens before any key intake ($pattern)" "$TESTTMP/out-shmdebris" "temporary key directory"
  expect_not_grep "no container is ever run or removed ($pattern)" "$TESTTMP/calls-shmdebris" "docker rm"
  [[ -d "$fakedir" ]] && pass "the tmpfs debris was NOT auto-removed ($pattern)" \
    || fail "the tmpfs debris was NOT auto-removed ($pattern)"
  rm -rf "$fakedir" "$TESTTMP/calls-shmdebris"
done

echo "== end-to-end: every promised docker top capture is required"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-top1" DOCKER_STUB_TOP1_RC=1 || rc=$?
expect_nonzero "first (live) docker top failure fails the run" "$rc"
expect_grep "the live capture is named" "$TESTTMP/out-top1" "promised evidence capture failed (process argv, live"
expect_grep "cleanup proof printed after live-top failure" "$TESTTMP/out-top1" "temporary key directory removed (proof"
assert_no_new_key_dirs "key directory gone after live-top failure" "$before"

rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-top2" DOCKER_STUB_TOP2_RC=1 || rc=$?
expect_nonzero "second (pre-stop) docker top failure fails the run" "$rc"
expect_grep "the pre-stop capture is named" "$TESTTMP/out-top2" "promised evidence capture failed (process argv, pre-stop"
assert_no_new_key_dirs "key directory gone after pre-stop-top failure" "$before"

echo "== end-to-end: cleanup failures surface (docker rm / absence proof)"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-rmfail" DOCKER_STUB_RM_RC=1 || rc=$?
expect_nonzero "docker rm failure turns a passing run non-zero" "$rc"
expect_grep "the sweep itself had passed" "$TESTTMP/out-rmfail" "key-absence sweep: passed"
expect_grep "the rm failure is recorded" "$TESTTMP/out-rmfail" "CLEANUP FAIL: docker rm -f"
expect_grep "the overall cleanup verdict is FAILED" "$TESTTMP/out-rmfail" "CLEANUP FAILED"
assert_no_new_key_dirs "key directory gone despite rm failure" "$before"

rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-psfail" DOCKER_STUB_PS_RC=1 || rc=$?
expect_nonzero "unprovable container absence turns the run non-zero" "$rc"
expect_grep "the unproven removal is named" "$TESTTMP/out-psfail" "removal is UNPROVEN"
expect_grep "preflight scratch absence is UNPROVEN too" "$TESTTMP/out-psfail" "could not query docker for the preflight container"
expect_grep "scanner scratch absence is UNPROVEN too" "$TESTTMP/out-psfail" "could not query docker for the state-scanner container"
expect_grep "the operator is told to revoke the key" "$TESTTMP/out-psfail" "REVOKE THE AUTH KEY IMMEDIATELY"
expect_grep "later cleanup steps still ran" "$TESTTMP/out-psfail" "temporary key directory removed (proof"
expect_not_grep "the revoke warning stays value-free" "$TESTTMP/out-psfail" "$KEY"
assert_no_new_key_dirs "key directory gone despite unproven container removal" "$before"

rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-pslinger" DOCKER_STUB_RM_KEEP=1 || rc=$?
expect_nonzero "a lingering container turns the run non-zero" "$rc"
expect_grep "the lingering container is named" "$TESTTMP/out-pslinger" "STILL EXISTS after docker rm -f"
expect_grep "the operator is told to revoke the key" "$TESTTMP/out-pslinger" "REVOKE THE AUTH KEY IMMEDIATELY"
assert_no_new_key_dirs "key directory gone despite lingering container" "$before"

echo "== end-to-end: ordinary failure before any key intake (docker build fails)"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-fail" DOCKER_STUB_BUILD_RC=1 || rc=$?
expect_nonzero "exits non-zero" "$rc"
expect_not_grep "no key was read before the build" "$TESTTMP/out-fail" "temporary key directory"
assert_no_new_key_dirs "no key directory after a pre-key failure" "$before"
assert_lock_free "the run lock is released after an ordinary failure"

for sig in INT TERM; do
  want=$([[ "$sig" == INT ]] && echo 130 || echo 143)
  for phase in preflight keyscan; do
    echo "== end-to-end: cleanup on SIG$sig during the $phase key-bearing scratch operation"
    # --default-signal: background jobs of a non-interactive shell get SIGINT
    # ignored, and a signal ignored at shell entry cannot be trapped — reset
    # it so the harness sees the interrupt exactly like an operator's Ctrl-C.
    # The marker file parks the kill INSIDE the key-bearing scratch operation
    # (the stub touches it when that docker run starts, then sleeps), so the
    # trap must prove scratch-container absence with real key material on
    # disk.
    cdir=$(mktemp -d "$TESTTMP/stubstate.XXXXXX")
    marker=$TESTTMP/marker.$sig.$phase
    rm -f "$marker"
    if [[ "$phase" == preflight ]]; then
      knobs=(DOCKER_STUB_PREFLIGHT_SLEEP=4 "DOCKER_STUB_PREFLIGHT_MARKER=$marker")
    else
      knobs=(DOCKER_STUB_VOLSCAN_SLEEP=4 "DOCKER_STUB_VOLSCAN_MARKER=$marker")
    fi
    out=$TESTTMP/out-sig-$sig-$phase
    printf '%s\n' "$KEY" | env --default-signal=SIGINT PATH="$STUB:$PATH" \
      SPIKE_NONINTERACTIVE=1 SPIKE_SSH_WAIT_SECS=1 DOCKER_STUB_COUNTER_DIR="$cdir" \
      "${knobs[@]}" ./run-spike.sh >"$out" 2>&1 &
    pid=$!
    for _ in $(seq 1 100); do [[ -e "$marker" ]] && break; sleep 0.1; done
    [[ -e "$marker" ]] && pass "run parked inside the $phase scratch operation" \
      || fail "run parked inside the $phase scratch operation"
    kill "-$sig" "$pid"
    rc=0
    wait "$pid" || rc=$?
    [[ "$rc" -eq "$want" ]] && pass "exits $want on SIG$sig ($phase)" || fail "exits $want on SIG$sig ($phase) (got $rc)"
    expect_grep "preflight scratch absence proven on SIG$sig ($phase)" "$out" "preflight container already absent (proof"
    if [[ "$phase" == keyscan ]]; then
      expect_grep "scanner scratch absence proven on SIG$sig" "$out" "state-scanner container already absent (proof"
      expect_grep "main-container removal proven on SIG$sig" "$out" "==> main container removed (proof"
    fi
    expect_grep "cleanup proof printed on SIG$sig ($phase)" "$out" "temporary key directory removed (proof"
    expect_not_grep "no key value in SIG$sig ($phase) output" "$out" "$KEY"
    assert_no_new_key_dirs "key directory gone after SIG$sig ($phase)" "$before"
    assert_lock_free "the run lock is released after SIG$sig ($phase)"
  done
done

echo "== end-to-end: a live run's lock excludes a second run entirely"
cdir=$(mktemp -d "$TESTTMP/stubstate.XXXXXX")
marker=$TESTTMP/marker.lock
rm -f "$marker"
printf '%s\n' "$KEY" | env PATH="$STUB:$PATH" SPIKE_NONINTERACTIVE=1 SPIKE_SSH_WAIT_SECS=1 \
  DOCKER_STUB_COUNTER_DIR="$cdir" DOCKER_STUB_PREFLIGHT_SLEEP=4 \
  DOCKER_STUB_PREFLIGHT_MARKER="$marker" ./run-spike.sh >"$TESTTMP/out-lock1" 2>&1 &
lockpid=$!
for _ in $(seq 1 100); do [[ -e "$marker" ]] && break; sleep 0.1; done
[[ -e "$marker" ]] && pass "run 1 parked inside its preflight, lock held" \
  || fail "run 1 parked inside its preflight, lock held"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-lock2" DOCKER_STUB_CALLS="$TESTTMP/calls-lock2" || rc=$?
expect_nonzero "a second run is refused while the first is live" "$rc"
expect_grep "the refusal names the held lock" "$TESTTMP/out-lock2" "another run of this harness holds the run lock"
expect_not_grep "the second run never reached key intake" "$TESTTMP/out-lock2" "temporary key directory"
if [[ -s "$TESTTMP/calls-lock2" ]]; then
  fail "the second run made no docker calls at all (no run, no rm of run 1's containers)"
else
  pass "the second run made no docker calls at all (no run, no rm of run 1's containers)"
fi
rc=0
wait "$lockpid" || rc=$?
[[ "$rc" -eq 0 ]] && pass "run 1 completed normally after the refused second run" \
  || fail "run 1 completed normally after the refused second run (got $rc)"
expect_grep "run 1's sweep still passed" "$TESTTMP/out-lock1" "key-absence sweep: passed"
assert_lock_free "the run lock is released after run 1 finished"
assert_no_new_key_dirs "no key-directory debris after the concurrency test" "$before"

echo "== end-to-end: SIGKILL releases the OS lock; the NEXT run refuses the debris"
cdir=$(mktemp -d "$TESTTMP/stubstate.XXXXXX")
marker=$TESTTMP/marker.sigkill
rm -f "$marker"
printf '%s\n' "$KEY" | env PATH="$STUB:$PATH" SPIKE_NONINTERACTIVE=1 SPIKE_SSH_WAIT_SECS=1 \
  DOCKER_STUB_COUNTER_DIR="$cdir" DOCKER_STUB_PREFLIGHT_SLEEP=4 \
  DOCKER_STUB_PREFLIGHT_MARKER="$marker" ./run-spike.sh >"$TESTTMP/out-sigkill" 2>&1 &
killpid=$!
for _ in $(seq 1 100); do [[ -e "$marker" ]] && break; sleep 0.1; done
[[ -e "$marker" ]] && pass "run parked inside its preflight with real key material on disk" \
  || fail "run parked inside its preflight with real key material on disk"
kill -9 "$killpid"
rc=0
wait "$killpid" || rc=$?
[[ "$rc" -eq 137 ]] && pass "SIGKILL leaves exit 137 (no trap could run)" \
  || fail "SIGKILL leaves exit 137 (no trap could run) (got $rc)"
lock_released_within 10 && pass "the kernel released the lock after SIGKILL" \
  || fail "the kernel released the lock after SIGKILL"
[[ "$(key_dirs)" != "$before" ]] && pass "SIGKILL left key-directory debris behind (cleanup never ran)" \
  || fail "SIGKILL left key-directory debris behind (cleanup never ran)"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-postkill" || rc=$?
expect_nonzero "the next run refuses the SIGKILL debris" "$rc"
expect_grep "the stale key directory is named" "$TESTTMP/out-postkill" "stale spike tmpfs debris"
expect_grep "the operator is told to revoke the prior key" "$TESTTMP/out-postkill" "REVOKE the key that run used"
expect_not_grep "no key intake with debris present" "$TESTTMP/out-postkill" "temporary key directory removed"
[[ "$(key_dirs)" != "$before" ]] && pass "the debris was NOT auto-removed by the next run" \
  || fail "the debris was NOT auto-removed by the next run"
for d in $(key_dirs); do
  printf '%s\n' "$before" | grep -qxF -- "$d" || rm -rf "$d"
done
assert_no_new_key_dirs "SIGKILL debris cleaned up by the test itself" "$before"

echo "== end-to-end: SIGKILL during the SSH wait — the lock dies with the harness, not its children"
# Park the harness in the ordinary non-docker SSH-wait sleep, then SIGKILL
# ONLY the harness. Because every external child is spawned with fd 9
# closed, the surviving sleep must (a) not hold fd 9 and (b) not pin the
# lock: the kernel must release it the moment the harness dies, and the
# very next run must get straight through lock acquisition (stopping only
# at the debris audit, which is the designed protection for this case).
ev_before=$(evidence_dirs)
cdir=$(mktemp -d "$TESTTMP/stubstate.XXXXXX")
printf '%s\n' "$KEY" | env PATH="$STUB:$PATH" SPIKE_NONINTERACTIVE=1 SPIKE_SSH_WAIT_SECS=31 \
  DOCKER_STUB_COUNTER_DIR="$cdir" ./run-spike.sh >"$TESTTMP/out-killwait" 2>&1 &
waitpid=$!
sleeppid=""
for _ in $(seq 1 200); do
  sleeppid=$(pgrep -P "$waitpid" -f '^sleep 31$' || true)
  [[ -n "$sleeppid" ]] && break
  sleep 0.1
done
[[ -n "$sleeppid" ]] && pass "run parked in the ordinary (non-docker) SSH-wait sleep" \
  || fail "run parked in the ordinary (non-docker) SSH-wait sleep"
if [[ -n "$sleeppid" ]]; then
  [[ "$(readlink "/proc/$waitpid/fd/9" 2>/dev/null)" == /dev/shm ]] \
    && pass "the harness itself holds fd 9 open on /dev/shm" \
    || fail "the harness itself holds fd 9 open on /dev/shm"
  flock -n "$LOCKDIR" true 2>/dev/null \
    && fail "the lock is held while the harness is parked" \
    || pass "the lock is held while the harness is parked"
  [[ ! -e "/proc/$sleeppid/fd/9" ]] \
    && pass "the surviving child does NOT have fd 9" \
    || fail "the surviving child does NOT have fd 9"
fi
kill -9 "$waitpid"
rc=0
wait "$waitpid" || rc=$?
[[ "$rc" -eq 137 ]] && pass "SIGKILL leaves exit 137 (no trap could run)" \
  || fail "SIGKILL leaves exit 137 (no trap could run) (got $rc)"
if [[ -n "$sleeppid" ]] && kill -0 "$sleeppid" 2>/dev/null; then
  pass "the sleep child is still alive after the harness died"
else
  fail "the sleep child is still alive after the harness died"
fi
lock_released_within 3 && pass "the lock was released promptly despite the surviving child" \
  || fail "the lock was released promptly despite the surviving child"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-postkillwait" || rc=$?
expect_nonzero "the next run is stopped by the debris audit, not the lock" "$rc"
expect_grep "the next run acquired the lock promptly" "$TESTTMP/out-postkillwait" "run lock acquired"
expect_grep "the stale tmpfs debris is what refused it" "$TESTTMP/out-postkillwait" "stale spike tmpfs debris"
expect_not_grep "no key value in the killed run's output" "$TESTTMP/out-killwait" "$KEY"
[[ -n "$sleeppid" ]] && kill -9 "$sleeppid" 2>/dev/null
wait 2>/dev/null || true
for d in $(key_dirs); do
  printf '%s\n' "$before" | grep -qxF -- "$d" || rm -rf "$d"
done
for d in $(evidence_dirs); do
  printf '%s\n' "$ev_before" | grep -qxF -- "$d" || rm -rf "$d"
done
assert_no_new_key_dirs "SSH-wait SIGKILL debris cleaned up by the test itself" "$before"

echo "== the legacy lock path is dead: a planted symlink is never opened, changed, or chmodded"
# Older harness revisions opened (O_APPEND|O_CREAT) and chmodded a lock FILE
# at $LEGACY_LOCKFILE. The directory-inode lock must never touch that
# pathname again: plant a symlink there to a read-only sentinel and run a
# full normal run through it. The sentinel is mode 0404 (no write bit even
# for the owner), so any attempt to open it for writing through the symlink
# would fail the run — rc 0 alone proves no such open happened (under a
# non-root test run); the stat comparison proves no change and no chmod
# either way; and the old lstat precheck would have refused the symlink
# outright, so a PASSING run also proves that precheck is gone.
sentinel=$TESTTMP/lock-sentinel
printf 'sentinel-do-not-touch\n' >"$sentinel"
chmod 0404 "$sentinel"
# A host that ever ran an old harness revision still has the retired lock
# file (empty by design — the old header documented it as carrying no data
# and never being deleted). Clear it so the symlink can be planted.
rm -f "$LEGACY_LOCKFILE"
ln -s "$sentinel" "$LEGACY_LOCKFILE"
SHM_DEBRIS+=("$LEGACY_LOCKFILE")
sent_before=$(stat -c '%a %u %g %s %Y %Z' "$sentinel")
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-legacylock" || rc=$?
[[ "$rc" -eq 0 ]] && pass "a planted legacy-lock symlink does not affect the run at all" \
  || fail "a planted legacy-lock symlink does not affect the run at all (rc=$rc)"
expect_grep "the run still locks the directory inode" "$TESTTMP/out-legacylock" "run lock acquired"
expect_grep "the run's sweep still passed" "$TESTTMP/out-legacylock" "key-absence sweep: passed"
[[ -L "$LEGACY_LOCKFILE" && "$(readlink "$LEGACY_LOCKFILE")" == "$sentinel" ]] \
  && pass "the planted symlink itself is untouched" || fail "the planted symlink itself is untouched"
[[ "$(stat -c '%a %u %g %s %Y %Z' "$sentinel")" == "$sent_before" ]] \
  && pass "the sentinel target is not changed or chmodded (mode/size/mtime/ctime identical)" \
  || fail "the sentinel target is not changed or chmodded (mode/size/mtime/ctime identical)"
grep -qxF 'sentinel-do-not-touch' "$sentinel" \
  && pass "sentinel content intact" || fail "sentinel content intact"
expect_not_grep "the harness never even names the legacy lock path" "$TESTTMP/out-legacylock" "$LEGACY_LOCKFILE"
rm -f "$LEGACY_LOCKFILE"
chmod 600 "$sentinel"
assert_no_new_key_dirs "no key-directory debris after the legacy-lock test" "$before"
assert_lock_free "the run lock is released after the legacy-lock test"

echo
if [[ "$fails" -ne 0 ]]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: all checks passed"
