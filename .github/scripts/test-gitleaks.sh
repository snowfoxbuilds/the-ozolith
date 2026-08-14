#!/usr/bin/env bash
# Regression tests for the secret-scan CI job (.github/workflows/ci.yml).
# Runs after the pinned gitleaks install, before the real history scan, and
# proves four properties of the scanner + .gitleaks.toml combination:
#
#   1. Token-shaped content in an ordinary file is detected (the rule-scoped
#      exemption has not neutered the generic-api-key rule, and github-pat
#      works).
#   2. The single known false positive — generic-api-key extracting exactly
#      `t.TwoKeyMap=void` from the vendored xterm.js at its exact path — is
#      exempted (the real vendored file is the fixture).
#   3. The exemption is not a file exemption: a fake GitHub PAT planted in
#      that same xterm.js file is still detected by the github-pat rule.
#   4. The production command's --diff-merges=first-parent catches a secret
#      introduced only by a merge conflict-resolution commit — content the
#      pinned version's plain --all never diffs.
#
# Canary values are assembled from fragments at runtime so no complete
# token shape ever sits in this repository's history (the real scan would
# flag it). Every scanner invocation uses --redact.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${REPO_ROOT}/.gitleaks.toml"
XTERM_REL="control/src/theozolith_control/web/static/xterm.js"
LOG_OPTS="--all --diff-merges=first-parent" # must match the production step

[ -f "${CONFIG}" ] || { echo "FAIL: missing ${CONFIG}" >&2; exit 1; }
[ -f "${REPO_ROOT}/${XTERM_REL}" ] || { echo "FAIL: missing ${XTERM_REL}" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# Hermetic git: ignore the runner's config, pin an identity.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
git_c() { git -C "$1" -c user.name=secret-scan-test -c user.email=ci@invalid "${@:2}"; }

# Canary fragments (see header). Concatenated only at runtime, into files
# under ${TMP} that the trap removes.
GENERIC_A='Zq7xJ2mK9w'
GENERIC_B='Rt4vNp8yLc'
PAT_HEAD='ghp_A1b2C3d4E5f6G7h8'
PAT_TAIL='I9j0K1l2M3n4O5p6Q7r8'

fail() { echo "FAIL: $*" >&2; exit 1; }

# run_scan <repo> <log-opts> <report-path> — prints the gitleaks exit code.
# Exit 1 specifically means "leaks found"; anything else nonzero is an error.
run_scan() {
  local rc=0
  gitleaks git "$1" --config "${CONFIG}" --log-opts="$2" \
    --redact --no-banner --report-format json --report-path "$3" || rc=$?
  echo "${rc}"
}

has_rule() { grep -q "\"RuleID\": \"$2\"" "$1"; }

echo "=== test 1: canaries in ordinary files are detected"
R1="${TMP}/ordinary"
git init -q -b main "${R1}"
printf 'apikey = "%s%s"\n' "${GENERIC_A}" "${GENERIC_B}" > "${R1}/config.py"
printf '%s%s\n' "${PAT_HEAD}" "${PAT_TAIL}" > "${R1}/notes.txt"
git_c "${R1}" add -A
git_c "${R1}" commit -qm "add configuration"
rc="$(run_scan "${R1}" "${LOG_OPTS}" "${TMP}/r1.json")"
[ "${rc}" -eq 1 ] || fail "test 1: expected exit 1 (leaks found), got ${rc}"
has_rule "${TMP}/r1.json" generic-api-key || fail "test 1: generic-api-key did not fire"
has_rule "${TMP}/r1.json" github-pat || fail "test 1: github-pat did not fire"

echo "=== test 2: the exact xterm.js false positive is exempted"
R2="${TMP}/vendored"
git init -q -b main "${R2}"
mkdir -p "${R2}/$(dirname "${XTERM_REL}")"
cp "${REPO_ROOT}/${XTERM_REL}" "${R2}/${XTERM_REL}"
git_c "${R2}" add -A
git_c "${R2}" commit -qm "vendor xterm.js"
rc="$(run_scan "${R2}" "${LOG_OPTS}" "${TMP}/r2.json")"
[ "${rc}" -eq 0 ] || fail "test 2: vendored xterm.js not clean (exit ${rc}) — exemption broken or file re-vendored"

echo "=== test 3: a real token shape in that same xterm.js is still detected"
printf '\nvar q="%s%s";\n' "${PAT_HEAD}" "${PAT_TAIL}" >> "${R2}/${XTERM_REL}"
git_c "${R2}" commit -aqm "plant PAT canary"
rc="$(run_scan "${R2}" "${LOG_OPTS}" "${TMP}/r3.json")"
[ "${rc}" -eq 1 ] || fail "test 3: expected exit 1 (leaks found), got ${rc}"
has_rule "${TMP}/r3.json" github-pat || fail "test 3: github-pat did not fire inside xterm.js"

echo "=== test 4: a secret introduced only by a merge resolution is detected"
R4="${TMP}/merge"
git init -q -b main "${R4}"
printf 'channel = alpha\n' > "${R4}/app.conf"
git_c "${R4}" add -A
git_c "${R4}" commit -qm "base"
git_c "${R4}" checkout -qb feature
printf 'channel = beta\n' > "${R4}/app.conf"
git_c "${R4}" commit -aqm "feature side"
git_c "${R4}" checkout -q main
printf 'channel = gamma\n' > "${R4}/app.conf"
git_c "${R4}" commit -aqm "main side"
git_c "${R4}" merge feature >/dev/null 2>&1 || true
git_c "${R4}" ls-files -u | grep -q . || fail "test 4: expected a merge conflict"
# Resolve the conflict by introducing the canary: it exists in the merge
# commit's tree only, never in either parent.
printf 'channel = delta\napikey = "%s%s"\n' "${GENERIC_A}" "${GENERIC_B}" > "${R4}/app.conf"
git_c "${R4}" add app.conf
git_c "${R4}" commit -qm "merge feature"
rc="$(run_scan "${R4}" "${LOG_OPTS}" "${TMP}/r4a.json")"
[ "${rc}" -eq 1 ] || fail "test 4: production log-opts missed the merge-only secret (exit ${rc})"
has_rule "${TMP}/r4a.json" generic-api-key || fail "test 4: generic-api-key did not fire on the merge diff"
# Document the gap the flag closes: at the pinned gitleaks version, plain
# --all diffs no merge commits and must miss the same secret.
rc="$(run_scan "${R4}" "--all" "${TMP}/r4b.json")"
[ "${rc}" -eq 0 ] || fail "test 4: plain --all unexpectedly exited ${rc}; pinned-version behavior changed"

echo "OK: all secret-scan regression tests passed"
