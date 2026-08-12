#!/usr/bin/env bash
# Shell-level regression tests for run-spike.sh — no Docker engine, tailnet,
# or auth key needed (docker is stubbed onto PATH). Covers exactly what the
# gate's credibility rests on:
#
#   - both sweeps are tri-state and FAIL CLOSED: grep rc 0 = secret hit
#     (fail), rc 1 = absent (the only pass), rc >= 2 / docker failure =
#     scanner error (fail); missing, unreadable, or empty evidence fails;
#   - cleanup removes the entire key directory — with printed proof — on
#     normal exit, on failure, and on SIGINT/SIGTERM interruption;
#   - reuse runs stay keyless end-to-end;
#   - the key value never appears in the harness's own output.
#
# Run: ./test-run-spike.sh   (exits non-zero on any failure)
set -euo pipefail
cd "$(dirname "$0")"

TESTTMP=$(mktemp -d)
trap 'rm -rf "$TESTTMP"' EXIT
STUB=$TESTTMP/bin
mkdir -p "$STUB"

# --- the docker stub -------------------------------------------------------
# Env knobs: DOCKER_STUB_VOLUME_EXISTS (volume inspect rc), DOCKER_STUB_BUILD_RC,
# DOCKER_STUB_BUILD_SLEEP, DOCKER_STUB_PREFLIGHT_RC, DOCKER_STUB_VOLSCAN_RC,
# DOCKER_STUB_VOLSCAN_OUT, DOCKER_STUB_LEAK (injected into the container log).
cat >"$STUB/docker" <<'STUBEOF'
#!/bin/bash
cmd=${1:-}; shift || true
case "$cmd" in
  volume)
    case "${1:-}" in
      inspect) [ "${DOCKER_STUB_VOLUME_EXISTS:-0}" = 1 ] && exit 0 || exit 1 ;;
      create) exit 0 ;;
    esac ;;
  build) sleep "${DOCKER_STUB_BUILD_SLEEP:-0}"; exit "${DOCKER_STUB_BUILD_RC:-0}" ;;
  rm) exit 0 ;;
  run)
    case "$*" in
      *"grep -rlFf"*)
        [ -n "${DOCKER_STUB_VOLSCAN_OUT:-}" ] && printf '%s\n' "$DOCKER_STUB_VOLSCAN_OUT"
        exit "${DOCKER_STUB_VOLSCAN_RC:-1}" ;;
      *"id -u"*) exit "${DOCKER_STUB_PREFLIGHT_RC:-0}" ;;
      *) echo stub-container-id; exit 0 ;;
    esac ;;
  logs)
    cat <<EOF
==> running as uid 1000 (ozolith); expecting 1000/ozolith
==> tailscale version (must match the release the production example pins):
1.102.2
  go version: stub
==> empty statedir: fresh enrollment via file-form auth key (gate item 1)
==> up; status:
100.64.0.9 spike ozolith@ linux -
==> ready — from another tailnet machine: ssh ozolith@spike
${DOCKER_STUB_LEAK:-}
EOF
    exit 0 ;;
  inspect)
    case "$*" in
      *State.Running*) echo true ;;
      *) echo '[{"Config":{"Env":["TS_AUTHKEY_FILE=/run/secrets/ts-authkey"]},"Mounts":[{"Destination":"/run/secrets/ts-authkey"}]}]' ;;
    esac
    exit 0 ;;
  top) printf 'PID CMD\n1 /bin/sh /usr/local/bin/spike-entrypoint\n2 tailscaled --tun=userspace-networking\n'; exit 0 ;;
  stop) exit 0 ;;
  history) printf 'IMAGE CREATED BY\nabc RUN curl -fsSL tailscale_1.102.2_amd64.tgz\n'; exit 0 ;;
esac
exit 0
STUBEOF
chmod +x "$STUB/docker"

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

# --- scan_state_volume: tri-state, fail closed -------------------------------

echo "== scan_state_volume tri-state"
run_scan_vol() { # $1: scanner rc, $2: scanner stdout
  (
    source ./run-spike.sh
    SPIKE_KEY_FILE=$PATTERN
    export PATH="$STUB:$PATH" DOCKER_STUB_VOLSCAN_RC="$1" DOCKER_STUB_VOLSCAN_OUT="${2:-}"
    scan_state_volume 2>&1
    echo "sweep_hit=$sweep_hit"
  )
}

out=$(run_scan_vol 0 "/state/tailscaled.state")
grep -q 'FAIL: key value found on the state volume' <<<"$out" \
  && grep -q '/state/tailscaled.state' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "volume grep rc 0 (hit) fails, filenames only" || fail "volume grep rc 0 (hit) fails, filenames only"

out=$(run_scan_vol 1)
grep -q 'ok: key value absent from every file' <<<"$out" && grep -q 'sweep_hit=0' <<<"$out" \
  && pass "volume grep rc 1 (absent) passes" || fail "volume grep rc 1 (absent) passes"

out=$(run_scan_vol 2 "grep: /state: some error")
grep -q 'state-volume scanner error (rc=2)' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "volume grep rc 2 fails closed" || fail "volume grep rc 2 fails closed"

out=$(run_scan_vol 125 "docker: daemon error")
grep -q 'state-volume scanner error (rc=125)' <<<"$out" && grep -q 'sweep_hit=1' <<<"$out" \
  && pass "docker-level failure (rc=125) fails closed" || fail "docker-level failure (rc=125) fails closed"

# --- end-to-end: cleanup + keylessness ---------------------------------------

key_dirs() { compgen -G '/dev/shm/ozolith-ts-spike-key.*' || true; }
assert_no_new_key_dirs() { # $1: description, $2: dirs before
  [[ "$(key_dirs)" == "$2" ]] && pass "$1" || fail "$1"
}
run_e2e() { # $1: out-file, then env overrides as NAME=VALUE...; stdin = key
  local out=$1
  shift
  local rc=0
  env PATH="$STUB:$PATH" SPIKE_NONINTERACTIVE=1 SPIKE_SSH_WAIT_SECS=1 "$@" \
    ./run-spike.sh >"$out" 2>&1 || rc=$?
  return "$rc"
}

echo "== end-to-end: normal exit (fresh, sweep passes)"
KEY="tskey-auth-E2E-$$-${RANDOM}"
before=$(key_dirs)
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-normal" || rc=$?
[[ "$rc" -eq 0 ]] && pass "exits 0" || fail "exits 0 (got $rc)"
expect_grep "uid-1000 readability preflight ran" "$TESTTMP/out-normal" "readable as uid 1000"
expect_grep "sweep passed on all five surfaces" "$TESTTMP/out-normal" "key-absence sweep: passed on all five surfaces"
expect_grep "evidence records the exact version" "$TESTTMP/out-normal" "tailscale version: 1.102.2"
expect_grep "cleanup proof printed" "$TESTTMP/out-normal" "temporary key directory removed (proof"
expect_not_grep "key value never printed by the harness" "$TESTTMP/out-normal" "$KEY"
assert_no_new_key_dirs "key directory gone after normal exit" "$before"

echo "== end-to-end: a leaked key is caught and fails the run"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-leak" DOCKER_STUB_LEAK="$KEY" || rc=$?
[[ "$rc" -ne 0 ]] && pass "exits non-zero on a hit" || fail "exits non-zero on a hit"
expect_grep "the hit surface is named" "$TESTTMP/out-leak" "FAIL: key value found in full container log"
expect_grep "sweep reported FAILED" "$TESTTMP/out-leak" "key-absence sweep: FAILED"
assert_no_new_key_dirs "key directory gone after sweep failure" "$before"

echo "== end-to-end: cleanup on failure (docker build fails)"
rc=0
printf '%s\n' "$KEY" | run_e2e "$TESTTMP/out-fail" DOCKER_STUB_BUILD_RC=1 || rc=$?
[[ "$rc" -ne 0 ]] && pass "exits non-zero" || fail "exits non-zero"
expect_grep "cleanup proof printed on failure" "$TESTTMP/out-fail" "temporary key directory removed (proof"
assert_no_new_key_dirs "key directory gone after failure" "$before"

for sig in INT TERM; do
  echo "== end-to-end: cleanup on SIG$sig"
  rc=0
  # --default-signal: background jobs of a non-interactive shell get SIGINT
  # ignored, and a signal ignored at shell entry cannot be trapped — reset it
  # so the harness sees the interrupt exactly like an operator's Ctrl-C.
  printf '%s\n' "$KEY" | env --default-signal=SIGINT PATH="$STUB:$PATH" SPIKE_NONINTERACTIVE=1 SPIKE_SSH_WAIT_SECS=1 \
    DOCKER_STUB_BUILD_SLEEP=3 ./run-spike.sh >"$TESTTMP/out-sig" 2>&1 &
  pid=$!
  sleep 1
  kill "-$sig" "$pid"
  wait "$pid" || rc=$?
  want=$([[ "$sig" == INT ]] && echo 130 || echo 143)
  [[ "$rc" -eq "$want" ]] && pass "exits $want on SIG$sig" || fail "exits $want on SIG$sig (got $rc)"
  expect_grep "cleanup proof printed on SIG$sig" "$TESTTMP/out-sig" "temporary key directory removed (proof"
  assert_no_new_key_dirs "key directory gone after SIG$sig" "$before"
done

echo "== end-to-end: reuse run stays keyless"
rc=0
run_e2e "$TESTTMP/out-reuse" DOCKER_STUB_VOLUME_EXISTS=1 </dev/null || rc=$?
[[ "$rc" -eq 0 ]] && pass "exits 0" || fail "exits 0 (got $rc)"
expect_grep "took the reuse branch" "$TESTTMP/out-reuse" "reuse run: retained state volume, keyless"
expect_grep "sweep deliberately not run keyless" "$TESTTMP/out-reuse" "key-absence sweep: not run (reuse run"
expect_not_grep "no key material was created" "$TESTTMP/out-reuse" "temporary key directory"
assert_no_new_key_dirs "no key directory appeared at all" "$before"

echo
if [[ "$fails" -ne 0 ]]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: all checks passed"
