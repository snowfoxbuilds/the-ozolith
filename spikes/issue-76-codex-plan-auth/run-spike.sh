#!/usr/bin/env bash
# Issue #76 spike driver: prove ChatGPT-plan auth works for headless
# `codex exec` in a Run-container posture, characterize auth.json rotation,
# and capture sanitized `--json` stream fixtures for the CodexAdapter
# parsers. See README.md for the full S1–S8 procedure and hygiene model.
#
# Hygiene (proportionate #31 ethos):
#   - ONE RUN AT A TIME: an exclusive non-blocking flock(2) on the /dev/shm
#     DIRECTORY INODE (fd 9, read-only) — no lock file is ever created.
#   - The credential lives in a named Docker volume (spike-codex-auth) and,
#     transiently, in this process's memory and the spike containers' env.
#     It NEVER appears in argv (docker -e is passed BARE, the value rides
#     the environment — exactly the production containers.py mechanism),
#     never in the build context, and never on non-tmpfs disk.
#   - EVERYTHING written to ./evidence/ passes the sanitizer first: every
#     string leaf of auth.json is scrubbed, plus a belt-and-braces pass over
#     long token-shaped substrings. Raw container output only ever exists in
#     a 0700 mktemp directory on /dev/shm, removed on exit.
#   - Foreign debris is never deleted: a pre-existing auth volume is reused
#     (S1 refuses to overwrite captured state without --force-login).
#
# Usage:
#   ./run-spike.sh build
#   ./run-spike.sh s1-login [--force-login]
#   ./run-spike.sh s2-headless [--label NAME] [--use-s1] [--update-live]
#                              [--skip-auth] [--config FILE] [-- CODEX_ARGS...]
#   ./run-spike.sh s3-rotation [RUNS=4] [INTERVAL_SECONDS=1800]
#   ./run-spike.sh s4-stale
#   ./run-spike.sh s5-fixtures
#   ./run-spike.sh s6-sandbox
#   ./run-spike.sh s7-config MODEL [EFFORT]
#   ./run-spike.sh s8-version
#   ./run-spike.sh auth-fields {s1|live}
#   ./run-spike.sh s3-summarize        # re-read evidence, report rotated fields

set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="ozolith-codex-spike:latest"
VOLUME="spike-codex-auth"
EVIDENCE_DIR="$SPIKE_DIR/evidence"
PROMPT_OK="Reply with exactly: OK"
# Base flags for every exec: JSONL stream, no git-repo requirement (the
# throwaway home/cwd is not a checkout). Sandbox is per-step.
BASE_ARGS=(--json --skip-git-repo-check)

die() { echo "run-spike: $*" >&2; exit 1; }

# --- single-run lock on the /dev/shm directory inode (no lock file) --------
[ -d /dev/shm ] || die "/dev/shm missing — need a tmpfs for staging"
exec 9< /dev/shm
flock -n 9 || die "another spike run holds the /dev/shm lock"

STAGING=""
cleanup() { [ -n "$STAGING" ] && rm -rf -- "$STAGING"; }
trap cleanup EXIT
STAGING="$(mktemp -d /dev/shm/ozolith-codex-spike.XXXXXX)"
chmod 0700 "$STAGING"

mkdir -p "$EVIDENCE_DIR"

# --- helpers ---------------------------------------------------------------

# Read one captured auth document from the volume into stdout (memory only —
# callers capture into a variable; never redirect this to non-tmpfs disk).
read_auth() { # s1|live
    docker run --rm -v "$VOLUME:/auth:ro" --entrypoint cat "$IMAGE" \
        "/auth/auth-$1.json" \
        || die "no captured auth-$1.json — run s1-login first"
}

# Sanitize a raw capture: scrub every auth.json string leaf plus any long
# token-shaped substring, then move into evidence/. Secrets ride into the
# sanitizer through the environment, never argv.
sanitize_to_evidence() { # raw-file evidence-name
    local raw="$1" name="$2"
    SANITIZE_SECRETS="${CODEX_AUTH_JSON:-}" python3 - "$raw" > "$EVIDENCE_DIR/$name" <<'PY'
import json, os, re, sys
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
secrets = []
doc = os.environ.get("SANITIZE_SECRETS", "")
if doc:
    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, str) and len(value) >= 8:
            secrets.append(value)
    try:
        walk(json.loads(doc))
    except ValueError:
        secrets.append(doc)
for secret in sorted(secrets, key=len, reverse=True):
    raw = raw.replace(secret, "[REDACTED]")
# belt-and-braces: JWT-ish and long bearer-ish runs
raw = re.sub(r"eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})+", "[REDACTED-JWT]", raw)
raw = re.sub(r"\b[A-Za-z0-9_-]{40,}\b", "[REDACTED-TOKEN]", raw)
sys.stdout.write(raw)
PY
    echo "evidence: $EVIDENCE_DIR/$name"
}

# One headless exec through the entrypoint's prepare() sequence.
run_exec() { # label use_s1 update_live skip_auth config_file codex-args...
    local label="$1" use_s1="$2" update_live="$3" skip_auth="$4" config_file="$5"
    shift 5
    local -a docker_args=(run --rm -e CODEX_AUTH_JSON -e CODEX_CONFIG_TOML
                          -e UPDATE_LIVE -e SKIP_AUTH)
    export CODEX_AUTH_JSON="" CODEX_CONFIG_TOML="" UPDATE_LIVE="" SKIP_AUTH=""
    if [ "$skip_auth" = 1 ]; then
        SKIP_AUTH=1
    elif [ "$use_s1" = 1 ]; then
        CODEX_AUTH_JSON="$(read_auth s1)"
    else
        CODEX_AUTH_JSON="$(read_auth live)"
    fi
    [ -n "$config_file" ] && CODEX_CONFIG_TOML="$(cat "$config_file")"
    if [ "$update_live" = 1 ]; then
        UPDATE_LIVE=1
        docker_args+=(-v "$VOLUME:/auth")
    fi
    local raw="$STAGING/$label.raw" status=0
    docker "${docker_args[@]}" "$IMAGE" "$@" > "$raw" 2>&1 || status=$?
    sanitize_to_evidence "$raw" "$label.log"
    echo "run-spike: $label finished (docker exit $status; CODEX_EXIT is in the log)"
}

# --- steps -----------------------------------------------------------------

cmd_build() {
    docker build -t "$IMAGE" "$SPIKE_DIR"
}

cmd_s1_login() {
    local force=0
    [ "${1:-}" = "--force-login" ] && force=1
    if [ "$force" != 1 ] \
        && docker run --rm -v "$VOLUME:/auth:ro" --entrypoint test "$IMAGE" \
               -f /auth/auth-s1.json 2>/dev/null; then
        die "auth-s1.json already captured; pass --force-login to redo S1"
    fi
    # Named volumes are created root-owned; hand them to uid 1000 first.
    docker run --rm -u 0 -v "$VOLUME:/auth" --entrypoint chown "$IMAGE" \
        1000:1000 /auth
    # Interactive device-code login; CODEX_HOME lives on the volume only for
    # this login so the captured document never touches the host filesystem.
    docker run -it --rm -v "$VOLUME:/auth" --entrypoint bash "$IMAGE" -lc '
        set -eu
        export CODEX_HOME=/auth/login-home
        mkdir -p "$CODEX_HOME" && chmod 0700 "$CODEX_HOME"
        codex login --device-auth
        umask 077
        cp "$CODEX_HOME/auth.json" /auth/auth-s1.json
        cp "$CODEX_HOME/auth.json" /auth/auth-live.json
        echo "S1: captured auth-s1.json + auth-live.json"
    '
    cmd_auth_fields s1 | tee "$EVIDENCE_DIR/s1-auth-fields.log"
}

cmd_auth_fields() { # s1|live — field names + value hashes only (safe output)
    docker run --rm -v "$VOLUME:/auth:ro" --entrypoint python3 "$IMAGE" \
        -c '
import hashlib, json, sys
with open("/auth/auth-" + sys.argv[1] + ".json") as handle:
    doc = json.load(handle)
def walk(prefix, value):
    if isinstance(value, dict):
        for key, item in value.items():
            walk(prefix + "." + key if prefix else key, item)
    else:
        print("AUTH_FIELD_SHA", sys.argv[1],
              prefix + "=" + hashlib.sha256(repr(value).encode()).hexdigest()[:16])
walk("", doc)
' "$1"
}

cmd_s2_headless() {
    local label="s2-headless" use_s1=0 update_live=0 skip_auth=0 config_file=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --label) label="$2"; shift 2 ;;
            --use-s1) use_s1=1; shift ;;
            --update-live) update_live=1; shift ;;
            --skip-auth) skip_auth=1; shift ;;
            --config) config_file="$2"; shift 2 ;;
            --) shift; break ;;
            *) die "unknown s2 option $1" ;;
        esac
    done
    if [ $# -eq 0 ]; then
        set -- "$PROMPT_OK" "${BASE_ARGS[@]}" -s read-only
    fi
    run_exec "$label" "$use_s1" "$update_live" "$skip_auth" "$config_file" "$@"
}

cmd_s3_rotation() {
    local runs="${1:-4}" interval="${2:-1800}" i
    for i in $(seq 1 "$runs"); do
        cmd_s2_headless --label "s3-rot-$i" --update-live
        [ "$i" -lt "$runs" ] && sleep "$interval"
    done
    cmd_s3_summarize
}

cmd_s3_summarize() {
    # Which auth.json fields changed hash across the captured runs? Safe by
    # construction: only names+hashes ever reach evidence.
    grep -h "^AUTH_FIELD_SHA" "$EVIDENCE_DIR"/s3-rot-*.log 2>/dev/null \
        | python3 -c '
import sys
seen, rotated = {}, set()
for line in sys.stdin:
    parts = line.split()
    if len(parts) != 3:
        continue
    field, _, digest = parts[2].partition("=")
    if field in seen and seen[field] != digest:
        rotated.add(field)
    seen[field] = digest
print("S3 rotated fields:", ", ".join(sorted(rotated)) or "(none)")
' | tee "$EVIDENCE_DIR/s3-summary.log"
}

cmd_s4_stale() {
    cmd_s2_headless --label s4-stale --use-s1
}

cmd_s5_fixtures() {
    cmd_s2_headless --label s5-success
    cmd_s2_headless --label s5-authfail --skip-auth
    printf 'model = "definitely-not-a-model"\n' > "$STAGING/badmodel.toml"
    cmd_s2_headless --label s5-badmodel --config "$STAGING/badmodel.toml"
    cmd_s2_headless --label s5-toolcall -- \
        "Run the shell command \`echo spike-ok\` and report its exact output." \
        "${BASE_ARGS[@]}" -s read-only
}

cmd_s6_sandbox() {
    cmd_s2_headless --label s6-none -- "$PROMPT_OK" "${BASE_ARGS[@]}"
    cmd_s2_headless --label s6-read-only -- "$PROMPT_OK" "${BASE_ARGS[@]}" -s read-only
    cmd_s2_headless --label s6-workspace-write -- "$PROMPT_OK" "${BASE_ARGS[@]}" -s workspace-write
    cmd_s2_headless --label s6-danger-full-access -- "$PROMPT_OK" "${BASE_ARGS[@]}" -s danger-full-access
    cmd_s2_headless --label s6-bypass -- "$PROMPT_OK" "${BASE_ARGS[@]}" \
        --dangerously-bypass-approvals-and-sandbox
}

cmd_s7_config() {
    local model="${1:?s7-config needs MODEL}" effort="${2:-}"
    {
        printf 'model = "%s"\n' "$model"
        [ -n "$effort" ] && printf 'model_reasoning_effort = "%s"\n' "$effort"
    } > "$STAGING/s7.toml"
    cmd_s2_headless --label "s7-${model}${effort:+-$effort}" --config "$STAGING/s7.toml"
}

cmd_s8_version() {
    docker run --rm --entrypoint codex "$IMAGE" --version \
        | tee "$EVIDENCE_DIR/s8-version.log"
}

# --- dispatch --------------------------------------------------------------

command="${1:-}"
[ $# -gt 0 ] && shift
case "$command" in
    build) cmd_build "$@" ;;
    s1-login) cmd_s1_login "$@" ;;
    s2-headless) cmd_s2_headless "$@" ;;
    s3-rotation) cmd_s3_rotation "$@" ;;
    s3-summarize) cmd_s3_summarize "$@" ;;
    s4-stale) cmd_s4_stale "$@" ;;
    s5-fixtures) cmd_s5_fixtures "$@" ;;
    s6-sandbox) cmd_s6_sandbox "$@" ;;
    s7-config) cmd_s7_config "$@" ;;
    s8-version) cmd_s8_version "$@" ;;
    auth-fields) cmd_auth_fields "${1:?auth-fields needs s1|live}" ;;
    *) die "unknown or missing command (see header usage)" ;;
esac
