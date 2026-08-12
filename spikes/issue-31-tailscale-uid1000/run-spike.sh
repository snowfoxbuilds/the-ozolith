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
# ONE RUN AT A TIME: an exclusive non-blocking flock(2) is taken on the
# stable /dev/shm DIRECTORY INODE — opened read-only on fd 9 — BEFORE any
# image build, volume probe, or secret intake, and held until cleanup
# completes. No lock FILE is ever created, opened, chmodded, or removed:
# the legacy /dev/shm/ozolith-ts-spike.lock pathname is dead, and anything
# planted there (a symlink included) is never touched. A second invocation
# fails immediately — before reading anything secret — instead of consuming
# the first run's containers or tmpfs directories. The lock is kernel-owned
# and dies WITH THIS PROCESS: every external command spawned after
# acquisition runs with fd 9 closed (see ext below), so a SIGKILLed run
# releases the lock the moment it dies even while children it started are
# still alive — and what protects the NEXT run from that run's leftovers is
# the debris audit, which (also before any secret intake) refuses to
# proceed while any prior-generation container name (the legacy fixed name
# or any per-run main/preflight/keyscan name), stale key directory, or
# stale evidence directory exists. Debris is NEVER removed automatically —
# a stale container or directory may still hold a PRIOR auth key, and
# deleting objects this run did not create is an operator decision.
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
# CAPTURED CONTENT NEVER REACHES OUTPUT UNSCANNED: the container log is
# followed into the tmpfs evidence directory ONLY — it is never streamed or
# tee'd through the terminal. On a fresh run, no raw or log-derived byte is
# printed until an exact-key scan of those same bytes has returned a clean
# miss: the ready-time excerpt is a scanned snapshot, early-failure
# diagnostics are captured and scanned before display, and the sanitized
# evidence block (which quotes log lines) prints only after the full sweep
# below passed on every surface. A hit — or a scan that cannot be completed —
# withholds the content, prints a value-free failure, and demands immediate
# key revocation.
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
# the full container log, and the retained state volume — whose file
# CONTENTS, PATHNAMES, DIRECTORY NAMES, and SYMLINK TARGETS are all swept
# through an ephemeral tar representation built inside the scratch scanner,
# which emits no raw data: its stdout/stderr are discarded unread and only
# a fixed clean/hit/error status comes back.
#
# Cleanup is provable, failure-aware, and OWNERSHIP-SAFE: every exit path —
# success, failure, SIGINT, SIGTERM — attempts EVERY teardown step (log
# follower, ALL key-bearing containers, evidence directory, key directory)
# regardless of earlier failures, then verifies each container is gone and
# the key directory no longer exists. "All key-bearing containers" means
# every container that received the key bind mount: the main spike container
# AND the two scratch containers (the uid-1000 readability preflight and the
# state-volume scanner). ALL THREE run under per-run, collision-safe names —
# there is no fixed container name anywhere — recorded BEFORE their docker
# run is invoked, and cleanup queries, removes, and re-queries ONLY those
# recorded names, so it can never touch a container this run did not create
# (a normally auto-removed scratch container is proven clean by that query,
# never counted as an rm failure). Any step that fails, or any absence that
# cannot be proven, turns the exit status nonzero (an original failure
# status is preserved). If ANY key-bearing container's absence cannot be
# proven on a run that mounted a key, a prominent warning tells the operator
# to REVOKE the key immediately — a bind mount can keep the key readable
# inside a surviving container.
#
# Self-test: ./test-run-spike.sh exercises the run-lock lifetime (contention,
# release on every exit path, SIGKILL-of-only-the-harness releasing the lock
# while children survive, the dead legacy lock pathname), mode selection,
# daemon-mode rejection, the sweep tri-state (against the real scanner
# payload for every naming surface), entrypoint failure propagation, and the
# cleanup paths with stubbed docker/tailscale binaries — no engine, tailnet,
# or key needed.
set -euo pipefail

IMAGE=ozolith-ts-spike
VOLUME=spike-tailscale-state

# One run at a time: an exclusive flock(2) is taken non-blocking on the
# STABLE INODE of this directory itself, opened read-only on fd 9, before
# any image build, volume probe, or secret intake, and held until cleanup
# completes. Locking the directory inode means no lock file is ever
# created, opened, or chmodded: there is no pathname to precheck, no mode
# to share between root and non-root invocations, and nothing a planted
# file or symlink at the legacy /dev/shm/ozolith-ts-spike.lock path can
# redirect — that path is never touched again. The lock is kernel-owned, so
# an aborted run can never leave it stuck — on SIGKILL the OS releases it,
# and the debris audit is what protects the next run.
RUN_LOCK_DIR=/dev/shm

# No child process may inherit the run-lock fd: any external process that
# can outlive this run (a docker client, the log follower, the parked
# SSH-wait sleep) would otherwise keep the lock held after this process
# died, making the lock lie about whether a run is alive. EVERY external
# command spawned after the lock is acquired is therefore routed through
# this single helper, which closes fd 9 for the child (a no-op before
# acquisition and under the sourced self-test); the ONLY external process
# allowed to inherit fd 9 is the `flock -n 9` call that acquires the lock
# on it. One caveat the log follower depends on: backgrounding a wrapper
# call forks a shell that WAITS on the external child, and that fork
# inherits the caller's fds before the helper ever runs — so a backgrounded
# call site must additionally close fd 9 on the background job itself
# (`... 9>&- &`).
ext() { "$@" 9>&-; }
docker() { ext command docker "$@"; }

# EVERY container this harness creates gets a per-run, collision-safe name:
# <prefix><pid>-<RANDOM>. A fixed name could collide with — or tempt the
# script into silently deleting — the debris of another run, while a per-run
# name can only ever denote THIS run's container; cleanup only ever removes
# names recorded for this run. CONTAINER_NAME_PREFIX is shared by all of
# them AND by the fixed name older harness revisions gave the main container
# — the debris audit matches on it, so one query catches every generation of
# leftover. The names carry no secret. Each tracking variable below is
# assigned BEFORE the corresponding docker run is invoked, so a partially
# created container or a client-side docker failure is still visible to
# cleanup; "" means that operation was never attempted.
CONTAINER_NAME_PREFIX=ozolith-ts-spike
MAIN_PREFIX=${CONTAINER_NAME_PREFIX}-main-
SCRATCH_PREFLIGHT_PREFIX=${CONTAINER_NAME_PREFIX}-preflight-
SCRATCH_SCAN_PREFIX=${CONTAINER_NAME_PREFIX}-keyscan-
RUN_TAG="$$-${RANDOM}"
CONTAINER=""
PREFLIGHT_CONTAINER=""
SCAN_CONTAINER=""

SPIKE_KEY_DIR=""
SPIKE_KEY_FILE=""
EVIDENCE_DIR=""
LOG_PID=""
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
      ext sleep 0.2
    done
    if kill -0 "$LOG_PID" 2>/dev/null; then
      kill -9 "$LOG_PID" 2>/dev/null
      ext sleep 0.2
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
  # regardless of earlier failures — and ONLY these per-run recorded names
  # are ever removed: nothing this run did not create is touched.
  local prc unproven=0
  if [[ -n "$CONTAINER" ]]; then
    prove_container_gone "$CONTAINER" "main container"
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
    ext rm -rf "$EVIDENCE_DIR" 2>/dev/null
    if [[ -e "$EVIDENCE_DIR" ]]; then
      echo "CLEANUP FAIL: evidence directory STILL EXISTS: $EVIDENCE_DIR — remove it by hand" >&2
      failed=1
    fi
  fi

  # 4) key directory. The volume and image are deliberately retained
  # (machine state, not secrets): checks 3/4 need them.
  if [[ -n "$SPIKE_KEY_DIR" ]]; then
    ext rm -rf "$SPIKE_KEY_DIR" 2>/dev/null
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
  # type -P, not command -v: the docker() wrapper above would satisfy the
  # latter even with no docker binary anywhere on PATH.
  type -P docker >/dev/null 2>&1 || {
    echo "FAIL: docker not found on PATH — this harness needs an ordinary root-daemon Docker." >&2
    exit 1
  }
  local secopts
  if ! secopts=$(docker info --format '{{json .SecurityOptions}}' 2>&1); then
    echo "FAIL: cannot query the Docker daemon (docker info failed) — refusing to read a key:" >&2
    printf '%s\n' "$secopts" | ext sed 's/^/      /' >&2
    exit 1
  fi
  if ext grep -q 'name=rootless' <<<"$secopts"; then
    echo "FAIL: rootless Docker daemon detected (SecurityOptions reports name=rootless) — unsupported." >&2
    echo "      Under rootless Docker the container's uid 1000 is not host uid 1000, so the gate's" >&2
    echo "      cross-UID key delivery cannot be verified. Run on an ordinary root-daemon host." >&2
    exit 1
  fi
  if ext grep -q 'name=userns' <<<"$secopts"; then
    echo "FAIL: userns-remap Docker daemon detected (SecurityOptions reports name=userns) — unsupported." >&2
    echo "      With userns-remap the container's uid 1000 maps to a shifted host uid, so the gate's" >&2
    echo "      cross-UID key delivery cannot be verified. Run on an ordinary root-daemon host." >&2
    exit 1
  fi
  echo "==> docker daemon: ordinary root daemon (no rootless / userns-remap security option)"
}

acquire_run_lock() {
  # Mutual exclusion FIRST — before the daemon is even queried: two
  # interleaved runs could each pass the debris audit and then mistake the
  # other's containers or tmpfs directories for their own debris (or their
  # own property). Non-blocking: a held lock means a live concurrent run,
  # and queueing silently behind it would start a surprise run later.
  #
  # The lock target is the /dev/shm DIRECTORY INODE itself, opened
  # read-only on fd 9 — nothing is created, chmodded, or unlinked, so there
  # is no pathname a concurrent or malicious actor can pre-plant, and no
  # lstat-precheck/open race to defend. The fd stays open until this
  # process exits, i.e. through cleanup; ext() closes it in every external
  # child, so the harness process is the ONLY thing pinning the lock and
  # the kernel drops it the instant this process dies — SIGKILL included,
  # surviving children included.
  command -v flock >/dev/null 2>&1 || {
    echo "FAIL: flock not found (util-linux) — refusing to run without mutual exclusion:" >&2
    echo "      a concurrent run could consume this run's containers or key material." >&2
    exit 1
  }
  if ! exec 9<"$RUN_LOCK_DIR"; then
    echo "FAIL: cannot open the run-lock directory $RUN_LOCK_DIR — refusing to run without" >&2
    echo "      mutual exclusion." >&2
    exit 1
  fi
  if ! flock -n 9; then
    echo "FAIL: another run of this harness holds the run lock (flock(2) on the" >&2
    echo "      $RUN_LOCK_DIR directory inode)." >&2
    echo "      Exactly one run may exist at a time — a second run could consume the first" >&2
    echo "      run's containers or secret-bearing tmpfs directories. Wait for it to finish" >&2
    echo "      (or stop it), then re-run. Refusing before any key intake." >&2
    exit 1
  fi
  echo "==> run lock acquired (flock on the $RUN_LOCK_DIR directory inode, fd 9) — held"
  echo "    until cleanup completes; a SIGKILLed run releases it via the kernel the moment"
  echo "    it dies, and the debris audit below protects the next run"
}

audit_stale_debris() {
  # Runs with the lock held and BEFORE any secret intake. Anything matching
  # the harness's container-name family or its tmpfs directory patterns can
  # only be (a) debris of a run that died too hard to clean up after itself
  # (SIGKILL, host crash — the lock excludes a live concurrent run) or (b)
  # an unrelated object that happens to collide. Either way this run must
  # not touch it: main/preflight/keyscan containers bind-mounted a PRIOR
  # auth key (a surviving container keeps that key readable through the
  # pinned mount), stale key/evidence directories may hold raw secret
  # material, and deleting objects this run did not create is an operator
  # decision. Fail closed with value-free guidance; nothing is removed
  # automatically, and no failure here can name a key value.
  local stale
  if ! stale=$(docker ps -aq --filter "name=^${CONTAINER_NAME_PREFIX}" 2>&1); then
    echo "FAIL: cannot audit for stale spike containers (docker ps failed):" >&2
    printf '%s\n' "$stale" | ext sed 's/^/      /' >&2
    echo "      Refusing to continue — this audit runs before any key intake." >&2
    exit 1
  fi
  if [[ -n "$stale" ]]; then
    echo "FAIL: stale spike container(s) exist — any name starting '${CONTAINER_NAME_PREFIX}':" >&2
    echo "      the legacy fixed main name '${CONTAINER_NAME_PREFIX}' or a per-run ${MAIN_PREFIX}*," >&2
    echo "      ${SCRATCH_PREFLIGHT_PREFIX}*, or ${SCRATCH_SCAN_PREFIX}* name:" >&2
    printf '%s\n' "$stale" | ext sed 's/^/      /' >&2
    echo "      A prior run was interrupted before it could prove their removal. Fresh-run main," >&2
    echo "      preflight, and keyscan containers bind-mounted an auth key when created, so a" >&2
    echo "      surviving one may still hold a PRIOR key readable. Nothing is removed" >&2
    echo "      automatically — inspect and remove them by hand (docker rm -f <id>)," >&2
    echo "      REVOKE the key that run used, then re-run. Refusing before any key intake." >&2
    exit 1
  fi
  if [[ ! -d /dev/shm || ! -r /dev/shm || ! -x /dev/shm ]]; then
    echo "FAIL: cannot audit /dev/shm for stale key/evidence directories — refusing before any key intake." >&2
    exit 1
  fi
  local d found=""
  for d in /dev/shm/ozolith-ts-spike-key.* /dev/shm/ozolith-ts-evidence.*; do
    if [[ -e "$d" || -L "$d" ]]; then
      found="$found$d"$'\n'
    fi
  done
  if [[ -n "$found" ]]; then
    echo "FAIL: stale spike tmpfs debris exists:" >&2
    printf '%s' "$found" | ext sed 's/^/      /' >&2
    echo "      A key directory may hold a PRIOR auth key in plaintext; an evidence directory" >&2
    echo "      may hold raw, unswept captures of one. A prior run died before its provable" >&2
    echo "      cleanup could remove them. Nothing is removed automatically — inspect and" >&2
    echo "      remove them by hand (rm -rf <dir>), REVOKE the key that run used, then re-run." >&2
    echo "      Refusing before any key intake." >&2
    exit 1
  fi
  echo "==> debris audit clean: no stale spike containers, key directories, or evidence directories"
}

warn_revoke() { # $1: value-free reason — the key must be treated as burned
  {
    echo "!!! ------------------------------------------------------------------ !!!"
    echo "!!! WARNING: $1"
    echo "!!! REVOKE THE AUTH KEY IMMEDIATELY:"
    echo "!!! tailscale admin console -> Settings -> Keys -> revoke."
    echo "!!! ------------------------------------------------------------------ !!!"
  } >&2
}

scan_for_display() { # $1: captured file. rc 0: proven clean miss (safe to
  # display); rc 1: the key value IS in the file; rc 2: cannot prove either
  # way (missing/empty pattern or capture, or a grep error) — as unsafe as a
  # hit, because an unscannable capture proves nothing.
  [[ -r "$SPIKE_KEY_FILE" && -s "$SPIKE_KEY_FILE" ]] || return 2
  [[ -r "$1" && -s "$1" ]] || return 2
  local rc=0
  ext grep -qFf "$SPIKE_KEY_FILE" "$1" || rc=$?
  case $rc in
    1) return 0 ;;
    0) return 1 ;;
    *) return 2 ;;
  esac
}

fail_before_ready() { # $1: value-free reason. Never returns.
  # The container failed before ready and the operator needs diagnostics —
  # but scan-before-show: the log is captured into tmpfs, tri-state scanned
  # for the exact key value, and DISPLAYED ONLY after a clean miss. On a hit
  # or a scanner error the log is withheld, the failure stays value-free,
  # and the key is treated as burned. A reuse run is keyless (nothing secret
  # entered this run and no pattern file exists), so its log is shown as-is.
  local dlog="$EVIDENCE_DIR/container-death.log" verdict=0
  echo "FAIL: $1" >&2
  if ! docker logs "$CONTAINER" >"$dlog" 2>&1; then
    echo "      The container log could not be captured, so no diagnostics can be shown." >&2
    if [[ "$MODE" == fresh ]]; then
      warn_revoke "the failed container's log could not be captured — key absence is UNPROVEN."
    fi
    exit 1
  fi
  if [[ "$MODE" == fresh ]]; then
    scan_for_display "$dlog" || verdict=$?
    if [[ "$verdict" -eq 1 ]]; then
      echo "      The captured log is WITHHELD: it contains the exact auth-key value." >&2
      warn_revoke "the failed container's log CONTAINS the auth-key value."
      exit 1
    elif [[ "$verdict" -ne 0 ]]; then
      echo "      The captured log is WITHHELD: it could not be proven key-free (scanner" >&2
      echo "      error, or an empty/unreadable capture)." >&2
      warn_revoke "the failed container's log could not be proven key-free."
      exit 1
    fi
    echo "      Partial container log (scanned for the exact key value first — clean miss):" >&2
  else
    echo "      Partial container log (keyless reuse run — nothing secret entered this run):" >&2
  fi
  ext sed 's/^/      | /' "$dlog" >&2
  echo "      Do NOT retry with --cap-add/--device: per #31 the privileged fallback is a human/ADR decision." >&2
  exit 1
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
    if ext grep -qi 'no such volume' <<<"$out"; then
      MODE=fresh
      VOLUME_EXISTS=0
      MODE_DETAIL="fresh (no $VOLUME volume yet)"
      echo "==> no state volume yet ($VOLUME): FRESH run — enrollment (gate items 1/4)"
      return 0
    fi
    echo "FAIL: cannot tell fresh from reuse — docker volume inspect failed:" >&2
    printf '%s\n' "$out" | ext sed 's/^/      /' >&2
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
      printf '%s\n' "$out" | ext sed 's/^/      /' >&2
      exit 1
      ;;
  esac
}

require_tmpfs() { # $1: a path whose filesystem must be RAM-backed
  local fstype
  fstype=$(ext stat -f -c %T "$1")
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
  SPIKE_KEY_DIR=$(ext mktemp -d /dev/shm/ozolith-ts-spike-key.XXXXXX)
  ext chmod 0700 "$SPIKE_KEY_DIR"
  SPIKE_KEY_FILE="$SPIKE_KEY_DIR/authkey"
  : >"$SPIKE_KEY_FILE"
  ext chmod 0600 "$SPIKE_KEY_FILE" # tight while the value is being written
}

finalize_key_perms() {
  # Make the leaf readable by the container's uid 1000 — and by nothing else
  # that the 0700 directory does not already admit. Ordinary root-daemon
  # Docker bind-mounts the leaf INODE, so inside the container only the
  # leaf's own owner/mode decide access; host-side, the directory decides.
  if [[ "$EUID" -eq 0 ]]; then
    ext chown 1000:1000 "$SPIKE_KEY_FILE"
    ext chmod 0400 "$SPIKE_KEY_FILE"
  elif [[ "$EUID" -eq 1000 ]]; then
    ext chmod 0400 "$SPIKE_KEY_FILE"
  else
    ext chmod 0444 "$SPIKE_KEY_FILE" # the 0700 directory is the host-side barrier
  fi
}

read_key() { # fills $SPIKE_KEY_FILE; the value never touches argv
  if [[ $# -ge 1 ]]; then
    local src
    src=$(ext realpath -e "$1")
    case "$src" in
      "$CONTEXT_DIR"/*)
        echo "refusing: $src is inside the Docker build context ($CONTEXT_DIR) — a key there can end up in an image layer." >&2
        exit 1
        ;;
    esac
    require_tmpfs "$src"
    ext cat -- "$src" >"$SPIKE_KEY_FILE"
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
  ext grep -q '[^[:space:]]' "$SPIKE_KEY_FILE" || {
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
  PREFLIGHT_CONTAINER="${SCRATCH_PREFLIGHT_PREFIX}${RUN_TAG}"
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
  ext grep -qFf "$SPIKE_KEY_FILE" "$2" || rc=$?
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

# Container-side state-volume scanner payload, run by /bin/sh inside the
# scratch scanner ($1: exact-key pattern file, $2: directory to scan, $3:
# scratch path for the ephemeral tar representation — inside the container's
# own filesystem, destroyed with it). One tar stream carries EVERY surface
# of the volume: file contents in its data blocks; pathnames, directory
# names, and symlink targets in its headers — so a single fixed-string grep
# over the archive sweeps them all, including names a content-only
# `grep -r` would never read (and would PRINT on a hit). The payload emits
# nothing — its stdout/stderr are discarded here and again by the caller —
# and the verdict travels as the exit status alone:
#   0 = the key value is present somewhere on the volume (hit)
#   1 = clean miss (the only pass)
#   3 = the archive could not be built, or grep itself failed (scanner error)
STATE_SCAN_SCRIPT='
tar -cf "$3" -C "$2" . 2>/dev/null || exit 3
grep -aqFf "$1" "$3" 2>/dev/null
rc=$?
[ "$rc" -le 1 ] && exit "$rc"
exit 3
'

scan_state_volume() {
  # Scanned INSIDE a scratch container (as uid 1000, like everything else)
  # with the SAME leaf mount enrollment used, so the pattern stays a path
  # everywhere. The payload (STATE_SCAN_SCRIPT above) archives the volume
  # into an ephemeral in-container tar file and greps that representation,
  # so file CONTENTS, PATHNAMES, DIRECTORY NAMES, and SYMLINK TARGETS are
  # all swept in one pass — and NO scanner byte ever reaches the terminal:
  # stdout and stderr are discarded unread (a matching filename or a
  # traversal error message could itself carry the key), the verdict is the
  # exit status, and the only output is one of the fixed messages below.
  # Tri-state, fail closed: rc 0 is a hit, rc 1 is the only pass, anything
  # else (tar error, grep error, docker failure) is a scanner failure and
  # fails the gate.
  #
  # Like the preflight, this scratch container receives the key bind mount,
  # so its per-run name is recorded BEFORE docker run and cleanup PROVES
  # its absence — --rm alone is convenience, not proof.
  # The client's output is captured into a variable — NOT discarded with a
  # call-site redirection, which would still be in scope if a deferred
  # signal trap ran cleanup mid-command and would swallow cleanup's own
  # output — and then thrown away unread.
  local rc=0 scan_noise
  SCAN_CONTAINER="${SCRATCH_SCAN_PREFIX}${RUN_TAG}"
  set +e
  scan_noise=$(docker run --rm --name "$SCAN_CONTAINER" \
    --mount "type=bind,source=${SPIKE_KEY_FILE},target=/run/secrets/ts-authkey,readonly" \
    -v "$VOLUME":/state:ro \
    --entrypoint /bin/sh "$IMAGE" \
    -c "$STATE_SCAN_SCRIPT" scan /run/secrets/ts-authkey /state /tmp/state.tar 2>&1)
  rc=$?
  set -e
  unset -v scan_noise # deliberately never printed, whatever happened
  case $rc in
    0)
      echo "    FAIL: key value found on the state volume — in a file's content, pathname," >&2
      echo "      directory name, or symlink target; the matching surface is withheld" >&2
      sweep_hit=1
      ;;
    1) echo "    ok: key value absent from the $VOLUME volume (contents, pathnames, directory names, symlink targets)" ;;
    *)
      echo "    FAIL: state-volume scanner error (rc=$rc) — cannot prove key absence; scanner output is never shown" >&2
      sweep_hit=1
      ;;
  esac
}

main() {
  cd "$(dirname "${BASH_SOURCE[0]}")"
  CONTEXT_DIR=$(pwd -P)

  # Ordering is the contract: run lock -> daemon checks -> debris audit ->
  # image build -> state probe -> key intake. Nothing secret is read until
  # this run is provably the only run and the field is provably free of any
  # prior run's debris.
  acquire_run_lock
  require_supported_docker
  audit_stale_debris

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

  EVIDENCE_DIR=$(ext mktemp -d /dev/shm/ozolith-ts-evidence.XXXXXX)

  # Deliberately absent: --cap-add, --device, --privileged. The container gets
  # nothing beyond Docker's default unprivileged capability set — that absence
  # IS the experiment. On a fresh run the key reaches the container only as a
  # read-only mount of the tmpfs leaf; only its PATH appears in env/argv
  # (gate item 5). On a reuse run nothing secret is mounted at all.
  #
  # The main container's per-run name is recorded BEFORE docker run, like the
  # scratch containers': the debris audit proved no name in this family
  # exists and the lock excludes a concurrent creator, so no pre-removal of
  # anything is needed — or allowed.
  CONTAINER="${MAIN_PREFIX}${RUN_TAG}"
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

  # CAPTURE-ONLY log follow: the raw stream goes into the tmpfs evidence
  # directory and nowhere else — never through tee or the terminal. No
  # log-derived byte is printed before an exact-key scan of that same
  # content returns a clean miss. The 9>&- on the background job matters:
  # backgrounding the wrapper call forks a shell that waits on the docker
  # client, and that fork inherits fd 9 BEFORE ext() can strip it from the
  # exec'd client — closing it on the job itself keeps a wedged follower
  # (and its waiting shell) from pinning the run lock past this process.
  docker logs -f "$CONTAINER" >"$EVIDENCE_DIR/container.log" 2>&1 9>&- &
  LOG_PID=$!

  local i=0
  until ext grep -q '^==> ready' "$EVIDENCE_DIR/container.log" 2>/dev/null; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != true ]]; then
      fail_before_ready "container exited before reaching ready — its captured log holds the finding."
    fi
    i=$((i + 1))
    [[ "$i" -le 120 ]] || fail_before_ready "container not ready after 120s."
    ext sleep 1
  done

  # Argv/metadata snapshots while the daemon is live (docker top uses the
  # host's ps — the transient `tailscale up` argv carried only the file: path,
  # visible in the captured log). Every promised capture is REQUIRED: a
  # capture failure is a run failure, never a silently skipped surface.
  capture_evidence "process argv, live (docker top)" "$EVIDENCE_DIR/top-live.txt" docker top "$CONTAINER"
  capture_evidence "container env + mount metadata (docker inspect)" "$EVIDENCE_DIR/inspect.json" docker inspect "$CONTAINER"

  # First display gate. The operator needs the log (tailscale status, the
  # node's 100.x address) for the SSH-check phase, but on a fresh run
  # nothing log-derived may print unscanned: snapshot the follower's capture
  # so the scanned bytes and the displayed bytes are the SAME bytes, scan
  # the snapshot, and display only on a clean miss. A hit here means the key
  # already escaped into the log — stop immediately (the sooner revoked the
  # better) instead of proceeding to the SSH checks. The final sweep later
  # re-scans the FULL log after stop. A reuse run is keyless: no pattern
  # exists and nothing secret entered this run.
  ext cp "$EVIDENCE_DIR/container.log" "$EVIDENCE_DIR/container-ready.log"
  if [[ "$MODE" == fresh ]]; then
    local ready_verdict=0
    scan_for_display "$EVIDENCE_DIR/container-ready.log" || ready_verdict=$?
    if [[ "$ready_verdict" -eq 1 ]]; then
      echo "FAIL: the running container's log ALREADY contains the exact auth-key value —" >&2
      echo "      the log is withheld and the run stops now." >&2
      warn_revoke "the container log contains the auth-key value."
      exit 1
    elif [[ "$ready_verdict" -ne 0 ]]; then
      echo "FAIL: the running container's log could not be proven key-free (scanner error," >&2
      echo "      or an empty/unreadable capture) — the log is withheld and the run stops now." >&2
      warn_revoke "the container log could not be proven key-free."
      exit 1
    fi
    echo "==> container log so far (scanned for the exact key value first — clean miss):"
  else
    echo "==> container log so far (keyless reuse run — nothing secret entered this run):"
  fi
  ext sed 's/^/    | /' "$EVIDENCE_DIR/container-ready.log"

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
    ext sleep "${SPIKE_SSH_WAIT_SECS:-300}"
  fi

  capture_evidence "process argv, pre-stop (docker top)" "$EVIDENCE_DIR/top-prestop.txt" docker top "$CONTAINER"
  docker stop -t 10 "$CONTAINER" >/dev/null
  kill "$LOG_PID" 2>/dev/null || true
  wait "$LOG_PID" 2>/dev/null || true
  LOG_PID=""
  capture_evidence "full container log" "$EVIDENCE_DIR/container.log" docker logs "$CONTAINER"
  capture_evidence "image history (docker history --no-trunc)" "$EVIDENCE_DIR/history.txt" docker history --no-trunc "$IMAGE"
  # The evidence on #31 must pin the exact image the runs used, so the image
  # id is a promised (mandatory) capture like the rest — an id cannot carry
  # the key, so it is not a sweep surface, just a required record.
  capture_evidence "image id (docker inspect)" "$EVIDENCE_DIR/image-id.txt" docker inspect -f '{{.Id}}' "$IMAGE"

  local sweep_result="not run (reuse run: no secret entered this run — item-5 evidence comes from the fresh runs)"
  if [[ "$MODE" == fresh ]]; then
    echo
    echo "==> gate item 5 sweep: exact key value, grep -Ff with the tmpfs key file as the"
    echo "    pattern file — the value itself never enters argv or output. The state volume"
    echo "    is swept through a tar representation (contents, pathnames, directory names,"
    echo "    symlink targets; statuses only, no scanner output). Tri-state and fail-closed:"
    echo "    a hit fails, a scanner error fails, only a clean miss passes."
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
      # The evidence block below quotes container-log lines; with any swept
      # surface holding the key (or unprovable), NO log-derived byte may be
      # re-emitted — not even the lines the block would have selected. Fail
      # value-free and treat the key as burned.
      echo "FAIL: key-absence sweep FAILED — a surface held the key value or could not be" >&2
      echo "      proven clean. Gate item 5 does NOT pass; the evidence summary block and" >&2
      echo "      every log-derived line are WITHHELD." >&2
      warn_revoke "a swept surface held the key value or could not be proven clean."
      exit 1
    fi
    sweep_result="passed on all six surfaces (history, inspect, argv live, argv pre-stop, log, state volume)"
  fi

  # Reached only when the run is keyless (reuse) or every surface — the full
  # container log included — scanned as a proven clean miss, so quoting log
  # lines here cannot re-emit a key.
  echo
  echo "==> sanitized evidence block — paste into issue #31:"
  echo "--------------------------------------------------------------------------"
  echo "harness: spikes/issue-31-tailscale-uid1000 @ $(ext git rev-parse --short HEAD 2>/dev/null || echo '<commit>')"
  echo "image: $IMAGE ($(<"$EVIDENCE_DIR/image-id.txt"))"
  echo "run mode: $MODE_DETAIL"
  ext grep '^==> running as uid' "$EVIDENCE_DIR/container.log" || true
  ext sed -n '/^==> tailscale version/,/^==>/{/^==> tailscale version/d;/^==>/d;p;}' "$EVIDENCE_DIR/container.log" | ext sed 's/^/tailscale version: /'
  ext grep -E '^==> (existing state found|empty statedir)' "$EVIDENCE_DIR/container.log" || true
  echo "key-absence sweep: $sweep_result"
  echo "--------------------------------------------------------------------------"
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
