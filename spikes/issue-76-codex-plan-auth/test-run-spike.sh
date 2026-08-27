#!/usr/bin/env bash
# Stubbed regression suite for the issue #76 spike harness (CI: spike-harness
# job). No Docker, no network, no credential: `docker` and `codex` are PATH
# stubs; the assertions pin the harness's hygiene contract:
#   - the credential value never appears in any docker argv (bare -e only)
#   - everything landing in evidence/ is sanitized (auth leaves + JWT shapes)
#   - refusal paths (missing S1 state, S1 overwrite, concurrent run) fail loud
#   - the entrypoint materializes a 0600 auth.json in a fresh CODEX_HOME and
#     propagates the exec's true exit code
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAILURES=0

say() { printf '%s\n' "$*"; }
fail() { say "FAIL: $*"; FAILURES=$((FAILURES + 1)); }
pass() { say "ok: $*"; }

SECRET_ACCESS="stub-access-token-value-0123456789abcdef0123456789abcdef"
SECRET_REFRESH="stub-refresh-token-value-fedcba9876543210fedcba9876543210"
AUTH_DOC='{"tokens":{"access_token":"'"$SECRET_ACCESS"'","refresh_token":"'"$SECRET_REFRESH"'","account_id":"acct-1"},"last_refresh":"2026-08-26T00:00:00Z"}'

# --- isolated copy of the harness + stubs ----------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT
cp "$HERE/run-spike.sh" "$HERE/entrypoint.sh" "$WORK/"
mkdir -p "$WORK/bin"
STUB_LOG="$WORK/docker-argv.log"
export STUB_LOG STUB_STATE="$WORK/state"
mkdir -p "$STUB_STATE"

cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
# Records argv; emulates just enough of the harness's docker calls.
printf '%s\n' "ARGV: $*" >> "$STUB_LOG"
args=("$@")
has() { local want="$1" a; for a in "${args[@]}"; do [ "$a" = "$want" ] && return 0; done; return 1; }
after_entrypoint() {
    local i
    for ((i = 0; i < ${#args[@]}; i++)); do
        if [ "${args[i]}" = "--entrypoint" ]; then echo "${args[i+1]}"; return; fi
    done
}
case "$1" in
    build) touch "$STUB_STATE/built"; exit 0 ;;
    run) ;;
    *) exit 0 ;;
esac
case "$(after_entrypoint)" in
    cat)
        name="${args[-1]}"
        case "$name" in
            */auth-live.json) [ -f "$STUB_STATE/live" ] && { cat "$STUB_STATE/live"; exit 0; } || exit 1 ;;
            */auth-s1.json)   [ -f "$STUB_STATE/s1" ]   && { cat "$STUB_STATE/s1";   exit 0; } || exit 1 ;;
        esac
        exit 1 ;;
    test)
        [ -f "$STUB_STATE/s1" ] && exit 0 || exit 1 ;;
    chown|bash|python3)
        exit 0 ;;
    codex)
        echo "codex-cli 0.150.0"; exit 0 ;;
    "")
        # the headless exec run: emit a stream that includes the secret (the
        # sanitizer must scrub it) plus a foreign JWT-shaped token
        echo "STREAM_BEGIN"
        echo "{\"type\":\"error\",\"message\":\"bearer $CODEX_AUTH_JSON\"}"
        echo "leaked-access: $(python3 -c 'import json,os;print(json.loads(os.environ["CODEX_AUTH_JSON"])["tokens"]["access_token"])' 2>/dev/null || true)"
        # A JWT-shaped token the auth doc does NOT contain, built at
        # runtime so no JWT-shaped literal ever sits in the repo (the
        # secret scan would rightly flag one).
        seg() { printf '%s' "$1" | base64 | tr -d '=\n'; }
        echo "foreign-jwt: $(seg '{"alg":"RS256","typ":"JWT"}').$(seg '{"sub":"spike-fixture"}').$(seg 'not-a-real-signature')"
        echo "STREAM_END"
        echo "AUTH_FIELD_SHA before tokens.access_token=aaaaaaaaaaaaaaaa"
        echo "AUTH_FIELD_SHA after tokens.access_token=bbbbbbbbbbbbbbbb"
        echo "CODEX_EXIT 0"
        exit 0 ;;
esac
exit 0
STUB
chmod 0755 "$WORK/bin/docker"
export PATH="$WORK/bin:$PATH"

run_harness() { (cd "$WORK" && ./run-spike.sh "$@"); }

# --- 1. s2 happy path: bare -e, sanitized evidence -------------------------
printf '%s' "$AUTH_DOC" > "$STUB_STATE/live"
: > "$STUB_LOG"
if run_harness s2-headless --label t-happy > /dev/null 2>&1; then
    pass "s2-headless runs against stubbed docker"
else
    fail "s2-headless should succeed with live auth present"
fi
if grep -q -- "-e CODEX_AUTH_JSON " "$STUB_LOG" \
    && ! grep -q "CODEX_AUTH_JSON=" "$STUB_LOG"; then
    pass "credential env flag is passed bare (no value in argv)"
else
    fail "expected bare -e CODEX_AUTH_JSON in docker argv"
fi
if grep -qF "$SECRET_ACCESS" "$STUB_LOG" || grep -qF "$SECRET_REFRESH" "$STUB_LOG"; then
    fail "secret value leaked into docker argv"
else
    pass "no secret value in any docker argv"
fi
EV="$WORK/evidence/t-happy.log"
if [ -f "$EV" ]; then
    pass "evidence file written"
    if grep -qF "$SECRET_ACCESS" "$EV" || grep -qF "$SECRET_REFRESH" "$EV"; then
        fail "secret value survived sanitization"
    else
        pass "auth leaves scrubbed from evidence"
    fi
    grep -q "REDACTED-JWT" "$EV" \
        && pass "foreign JWT-shaped token scrubbed" \
        || fail "JWT belt-and-braces scrub missing"
    grep -q "^CODEX_EXIT 0" "$EV" \
        && pass "CODEX_EXIT preserved in evidence" \
        || fail "CODEX_EXIT missing from evidence"
else
    fail "evidence file not written"
fi

# --- 2. s2 refusal without S1 state ----------------------------------------
rm -f "$STUB_STATE/live"
if out="$(run_harness s2-headless --label t-refuse 2>&1)"; then
    fail "s2-headless should fail without captured auth"
else
    grep -q "s1-login" <<<"$out" \
        && pass "missing-auth refusal names s1-login" \
        || fail "missing-auth refusal message unhelpful: $out"
fi
printf '%s' "$AUTH_DOC" > "$STUB_STATE/live"

# --- 3. s1 overwrite refusal ------------------------------------------------
printf '%s' "$AUTH_DOC" > "$STUB_STATE/s1"
if out="$(run_harness s1-login 2>&1)"; then
    fail "s1-login should refuse to overwrite captured state"
else
    grep -q -- "--force-login" <<<"$out" \
        && pass "s1 overwrite refusal names --force-login" \
        || fail "s1 refusal message unhelpful: $out"
fi

# --- 4. s3 summarizer reports rotated fields --------------------------------
mkdir -p "$WORK/evidence"
cat > "$WORK/evidence/s3-rot-1.log" <<'EOF'
AUTH_FIELD_SHA before tokens.access_token=1111111111111111
AUTH_FIELD_SHA before tokens.refresh_token=2222222222222222
AUTH_FIELD_SHA after tokens.access_token=3333333333333333
AUTH_FIELD_SHA after tokens.refresh_token=2222222222222222
EOF
cat > "$WORK/evidence/s3-rot-2.log" <<'EOF'
AUTH_FIELD_SHA before tokens.access_token=3333333333333333
AUTH_FIELD_SHA before tokens.refresh_token=2222222222222222
AUTH_FIELD_SHA after tokens.access_token=4444444444444444
AUTH_FIELD_SHA after tokens.refresh_token=2222222222222222
EOF
if out="$(run_harness s3-summarize 2>&1)"; then
    grep -q "tokens.access_token" <<<"$out" \
        && ! grep -q "tokens.refresh_token:" <<<"$out" \
        && pass "s3 summarizer reports only rotated fields" \
        || fail "s3 summary wrong: $out"
else
    fail "s3-summarize errored: $out"
fi

# --- 5. concurrent-run lock -------------------------------------------------
if out="$(
    exec 8< /dev/shm
    if flock -n 8; then
        run_harness s8-version 2>&1
    else
        echo "SKIP-LOCK-HELD-ELSEWHERE"
    fi
)"; then
    grep -q "SKIP-LOCK-HELD-ELSEWHERE" <<<"$out" \
        && pass "lock test skipped (outer lock unavailable)" \
        || fail "second run should fail while the lock is held"
else
    grep -q "another spike run" <<<"$out" \
        && pass "concurrent run refused via /dev/shm flock" \
        || fail "unexpected lock failure output: $out"
fi

# --- 6. entrypoint: 0600 leaf in a fresh CODEX_HOME, exit propagation ------
cat > "$WORK/bin/codex" <<'STUB'
#!/usr/bin/env bash
echo "HOME_SEEN $CODEX_HOME"
stat -c 'AUTH_MODE %a' "$CODEX_HOME/auth.json" 2>/dev/null || echo "AUTH_MODE absent"
exit "${STUB_CODEX_EXIT:-0}"
STUB
chmod 0755 "$WORK/bin/codex"
ep_out="$WORK/ep.out"
if CODEX_AUTH_JSON="$AUTH_DOC" STUB_CODEX_EXIT=7 sh "$WORK/entrypoint.sh" exec-args > "$ep_out" 2>&1; then
    fail "entrypoint should propagate the exec exit code (7)"
else
    status=$?
    [ "$status" -eq 7 ] \
        && pass "entrypoint propagates exec exit code" \
        || fail "entrypoint exit was $status, wanted 7"
fi
grep -q "AUTH_MODE 600" "$ep_out" \
    && pass "entrypoint materializes auth.json mode 0600" \
    || fail "auth.json mode wrong or absent: $(grep AUTH_MODE "$ep_out" || true)"
grep -q "^CODEX_EXIT 7" "$ep_out" \
    && pass "entrypoint reports CODEX_EXIT" \
    || fail "CODEX_EXIT line missing"
home_dir="$(sed -n 's/^HOME_SEEN //p' "$ep_out" | head -1)"
case "$home_dir" in
    /tmp/codex-home.*) rm -rf -- "$home_dir"; pass "fresh throwaway CODEX_HOME used" ;;
    *) fail "unexpected CODEX_HOME: $home_dir" ;;
esac
if CODEX_AUTH_JSON="" sh "$WORK/entrypoint.sh" exec-args > "$ep_out" 2>&1; then
    fail "entrypoint should refuse a missing credential"
else
    grep -q "CODEX_AUTH_JSON is required" "$ep_out" \
        && pass "entrypoint refuses a missing credential" \
        || fail "missing-credential message wrong"
fi
if CODEX_AUTH_JSON="" SKIP_AUTH=1 STUB_CODEX_EXIT=0 sh "$WORK/entrypoint.sh" exec-args > "$ep_out" 2>&1; then
    grep -q "AUTH_MODE absent" "$ep_out" \
        && pass "SKIP_AUTH runs with no auth.json (auth-failure fixture arm)" \
        || fail "SKIP_AUTH unexpectedly materialized auth.json"
    home_dir="$(sed -n 's/^HOME_SEEN //p' "$ep_out" | head -1)"
    case "$home_dir" in /tmp/codex-home.*) rm -rf -- "$home_dir" ;; esac
else
    fail "SKIP_AUTH arm should succeed"
fi

# ---------------------------------------------------------------------------
if [ "$FAILURES" -gt 0 ]; then
    say "$FAILURES failure(s)"
    exit 1
fi
say "all spike-harness regression checks passed"
