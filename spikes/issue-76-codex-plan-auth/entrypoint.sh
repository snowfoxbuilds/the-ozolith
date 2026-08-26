#!/bin/sh
# In-container step runner for the issue #76 codex plan-auth spike.
#
# Materializes the delivered credential into a FRESH throwaway CODEX_HOME —
# exactly the sequence CodexAdapter.prepare() will perform in a Run
# container — then executes one `codex exec` and reports, on stdout, only
# what the host harness may keep as evidence:
#
#   AUTH_FIELD_SHA before|after <field>=<sha256-of-value>   (names+hashes, never values)
#   CODEX_EXIT <n>                                          (the exec's true exit code)
#
# plus the raw `codex exec` stream between STREAM_BEGIN/STREAM_END markers.
# The HOST sanitizer scrubs the stream before anything lands on disk; this
# script itself never prints a byte of auth.json.
#
# Environment contract:
#   CODEX_AUTH_JSON   the full auth.json document (required unless SKIP_AUTH=1)
#   CODEX_CONFIG_TOML optional config.toml content to place in CODEX_HOME (S7)
#   UPDATE_LIVE=1     copy the post-run auth.json to /auth/auth-live.json
#                     (the rotation-accumulates arm of S3; /auth must be mounted)
#   SKIP_AUTH=1       run without materializing a credential (auth-failure fixture)
#
# Arguments: passed to `codex exec` verbatim after the built-in ones.

set -eu

home="$(mktemp -d /tmp/codex-home.XXXXXX)"
chmod 0700 "$home"
CODEX_HOME="$home"
export CODEX_HOME

auth_field_shas() {
    # names + sha256(value) only; tolerate absent file (SKIP_AUTH runs)
    label="$1"
    file="$CODEX_HOME/auth.json"
    [ -f "$file" ] || return 0
    python3 - "$label" "$file" <<'PY'
import hashlib, json, sys
label, path = sys.argv[1], sys.argv[2]
def walk(prefix, value):
    if isinstance(value, dict):
        for key, item in value.items():
            walk(f"{prefix}.{key}" if prefix else key, item)
    else:
        digest = hashlib.sha256(repr(value).encode()).hexdigest()[:16]
        print(f"AUTH_FIELD_SHA {label} {prefix}={digest}")
with open(path) as handle:
    walk("", json.load(handle))
PY
}

if [ "${SKIP_AUTH:-}" != "1" ]; then
    if [ -z "${CODEX_AUTH_JSON:-}" ]; then
        echo "spike-entrypoint: CODEX_AUTH_JSON is required (or SKIP_AUTH=1)" >&2
        exit 2
    fi
    # The prepare() sequence: 0600 leaf, written before the CLI ever runs.
    umask 077
    printf '%s' "$CODEX_AUTH_JSON" > "$CODEX_HOME/auth.json"
fi

if [ -n "${CODEX_CONFIG_TOML:-}" ]; then
    printf '%s' "$CODEX_CONFIG_TOML" > "$CODEX_HOME/config.toml"
fi

auth_field_shas before

echo "STREAM_BEGIN"
set +e
codex exec "$@" < /dev/null
exec_status=$?
set -e
echo "STREAM_END"
echo "CODEX_EXIT $exec_status"

auth_field_shas after

if [ "${UPDATE_LIVE:-}" = "1" ] && [ -f "$CODEX_HOME/auth.json" ]; then
    if [ -d /auth ]; then
        cp "$CODEX_HOME/auth.json" /auth/auth-live.json.tmp
        chmod 0600 /auth/auth-live.json.tmp
        mv /auth/auth-live.json.tmp /auth/auth-live.json
        echo "LIVE_UPDATED"
    else
        echo "spike-entrypoint: UPDATE_LIVE=1 but /auth is not mounted" >&2
        exit 2
    fi
fi

exit "$exec_status"
