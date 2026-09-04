#!/usr/bin/env bash
# Re-capture the `gh` wire shapes behind this corpus (README.md, "Capture
# method"). Usage: GH=/path/to/the/pinned/gh regenerate.sh <out-dir>
# Every case writes <out-dir>/<case>/NN.http, one file per request in wire
# order; README.md's table says which capture becomes which fixture. The
# sidecars are hand-derived from ADR-0057 and are never generated.
set -euo pipefail

GH="${GH:?set GH to the pinned gh binary}"
OUT="${1:?usage: regenerate.sh <out-dir>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SOCK="$WORK/gh.sock"
CFG="$WORK/cfg"
HOME_DIR="$WORK/home"
mkdir -p "$CFG" "$HOME_DIR" "$WORK/cwd" "$OUT"
printf 'http_unix_socket: %s\n' "$SOCK" > "$CFG/config.yml"

capture() {
  local name="$1"; shift
  rm -rf "$OUT/$name"
  python3 "$HERE/capture_server.py" "$SOCK" "$OUT/$name" &
  local pid=$!
  for _ in $(seq 1 50); do [ -S "$SOCK" ] && break; sleep 0.1; done
  (
    cd "$WORK/cwd"
    env -i PATH=/usr/bin:/bin HOME="$HOME_DIR" TZ=UTC GH_CONFIG_DIR="$CFG" GH_TOKEN=sentinel \
      GH_HOST=github.com GH_NO_UPDATE_NOTIFIER=1 GH_PAGER=cat NO_COLOR=1 "$GH" "$@" \
      > /dev/null 2>&1 || true   # gh exits non-zero on the stub answers; the bytes are captured
  )
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  echo "$name: $(ls "$OUT/$name" | tr '\n' ' ')"
}

capture issue_view issue view 1 --repo OWNER/REPO
capture pr_view pr view 1 --repo OWNER/REPO
capture issue_list issue list --repo OWNER/REPO
capture pr_list pr list --repo OWNER/REPO
capture pr_diff pr diff 1 --repo OWNER/REPO
capture pr_checks pr checks 1 --repo OWNER/REPO
capture repo_view repo view OWNER/REPO
capture api_graphql api graphql \
  -f query='query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id } }' \
  -f owner=OWNER -f name=REPO
capture search_issues search issues --repo OWNER/REPO --state open --author octocat \
  --label "help wanted" --visibility public "multi word term"
capture search_prs search prs --repo OWNER/REPO --state open --author octocat \
  --label "help wanted" --visibility public "multi word term"
capture api_search_q api -X GET search/issues -f q='repo:OWNER/REPO is:open path/to thing:x'
capture api_ref_encoded api -X GET repos/OWNER/REPO/commits -f sha=feature/branch
capture api_ref_raw api 'repos/OWNER/REPO/commits?sha=feature/branch'
capture api_repeated api 'repos/OWNER/REPO/issues?labels=a&labels=b&state=open&labels=a'
capture api_double_encoded api 'repos/OWNER/REPO/issues?q=%2541'
capture api_plus api 'search/issues?q=a+b'
capture api_pct20 api 'search/issues?q=a%20b'
capture api_space_f api -X GET search/issues -f q='a b'
CAPTURE_LINK='<https://api.github.com/search/issues?q=repo%3AOWNER%2FREPO+is%3Aopen&per_page=2&page=2>; rel="next"' \
  capture api_paginate api --paginate 'search/issues?q=repo%3AOWNER%2FREPO+is%3Aopen&per_page=2'
