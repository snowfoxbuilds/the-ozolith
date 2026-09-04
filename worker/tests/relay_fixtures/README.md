# GitHub Relay fixture corpus

Byte-exact wire shapes the pinned `gh` emits, plus hand-authored adversarial,
negative, and hostile inputs, each with a sidecar naming the outcome the relay
must produce. `test_relay_classify.py`, `test_relay_ingress.py`, and
`test_relay_audit.py` drive the corpus; the governing contract is
`docs/adr/ADR-0057-github-relay.md` (decision items 4, 6, 8, and 11).

## Pinned client

- `GH_VERSION`: `2.100.0`
- `GH_SHA256`: `e4d4bb4498e8d007abe545b6568926793ace1b6447da598294a610018cb164be`

The checksum is the sha256 of the release tarball
`gh_2.100.0_linux_amd64.tar.gz` from
`https://github.com/cli/cli/releases/download/v2.100.0/`, verified against the
release's `gh_2.100.0_checksums.txt`. Both run images
(`worker/docker/Dockerfile.claude`, `worker/docker/Dockerfile.codex`) install
exactly this tarball, and `test_deploy.py` holds their `GH_VERSION` equal to
the value above: the relay and the client it is tested against ship
together. Bumping `gh` means regenerating every `gh-*` fixture below from the
new binary and re-deriving its sidecars by hand.

## Capture method

Every `gh-*` fixture is what `gh` put on the wire — no byte was edited. A
throwaway Unix-socket capture server (`capture_server.py`, stdlib only)
records each request byte-exact and answers just enough JSON for `gh` to keep
going; `gh` is pointed at it through the pinned binary's `http_unix_socket`
config key with a sentinel `GH_TOKEN`, under a scrubbed environment so the
recorded headers carry nothing from the capturing host:

```sh
tar -xzf gh_2.100.0_linux_amd64.tar.gz          # after `sha256sum -c`
GH="$PWD/gh_2.100.0_linux_amd64/bin/gh" bash worker/tests/relay_fixtures/regenerate.sh /tmp/gh-captures
```

`regenerate.sh` runs, for each case, exactly:

```sh
printf 'http_unix_socket: %s\n' "$SOCK" > "$CFG/config.yml"
env -i PATH=/usr/bin:/bin HOME="$HOME_DIR" TZ=UTC GH_CONFIG_DIR="$CFG" GH_TOKEN=sentinel \
    GH_HOST=github.com GH_NO_UPDATE_NOTIFIER=1 GH_PAGER=cat NO_COLOR=1 "$GH" <args>
```

with the capture server listening on `$SOCK` and writing `<out>/<case>/NN.http`
(one file per request, in wire order). The pagination case additionally has
the server answer the first `/search/` request with
`Link: <https://api.github.com/search/issues?q=repo%3AOWNER%2FREPO+is%3Aopen&per_page=2&page=2>; rel="next"`
so `gh api --paginate` emits its follow-up request. The captures map onto the
fixtures as follows (`NN` is the request's position in the case):

| Case (`gh` arguments) | Capture | Fixture |
| --- | --- | --- |
| `issue view 1 --repo OWNER/REPO` | 01 | `graphql/gh-issue-view.json` |
| `pr view 1 --repo OWNER/REPO` | 01, 02 | `graphql/gh-pr-view.json`, `graphql/gh-pr-view-project-items.json` |
| `issue list --repo OWNER/REPO` | 01 | `graphql/gh-issue-list.json` |
| `pr list --repo OWNER/REPO` | 01 | `graphql/gh-pr-list.json` |
| `pr diff 1 --repo OWNER/REPO` | 01, 02 | `graphql/gh-pr-diff.json`, `rest/gh-pr-diff.http` |
| `pr checks 1 --repo OWNER/REPO` | 01–04 | `graphql/gh-pr-checks-lookup.json`, `graphql/gh-pr-checks-introspect-workflow-run.json`, `graphql/gh-pr-checks-introspect-pull-request.json`, `graphql/gh-pr-checks.json` |
| `repo view OWNER/REPO` | 01 | `graphql/gh-repo-view.json` |
| `api graphql -f query='query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id } }' -f owner=OWNER -f name=REPO` | 01 | `graphql/gh-api-graphql.json`, `rest/gh-api-graphql.http` |
| `search issues --repo OWNER/REPO --state open --author octocat --label "help wanted" --visibility public "multi word term"` | 01, 02 | `graphql/gh-search-introspect-search-type.json`, `rest/gh-search-issues.http` |
| `search prs --repo OWNER/REPO --state open --author octocat --label "help wanted" --visibility public "multi word term"` | 02 | `rest/gh-search-prs.http` |
| `api -X GET search/issues -f q='repo:OWNER/REPO is:open path/to thing:x'` | 01 | `rest/gh-api-search-q.http` |
| `api -X GET repos/OWNER/REPO/commits -f sha=feature/branch` | 01 | `rest/gh-api-ref-encoded.http` |
| `api 'repos/OWNER/REPO/commits?sha=feature/branch'` | 01 | `rest/gh-api-ref-raw.http` |
| `api 'repos/OWNER/REPO/issues?labels=a&labels=b&state=open&labels=a'` | 01 | `rest/gh-api-repeated-params.http` |
| `api 'repos/OWNER/REPO/issues?q=%2541'` | 01 | `rest/gh-api-double-encoded.http` |
| `api 'search/issues?q=a+b'` | 01 | `rest/gh-api-plus.http` |
| `api 'search/issues?q=a%20b'` | 01 | `rest/gh-api-pct20.http` |
| `api -X GET search/issues -f q='a b'` | 01 | `rest/gh-api-space-f.http` |
| `api --paginate 'search/issues?q=repo%3AOWNER%2FREPO+is%3Aopen&per_page=2'` | 01, 02 | `rest/gh-api-paginate-1.http`, `rest/gh-api-paginate-2.http` |

The GraphQL fixtures are the request bodies alone; the REST fixtures are the
complete request bytes, request line and headers included, so the header
allowlist is exercised against what `gh` really sends.

## Layout

- `graphql/<name>.json` — one `POST /graphql` body, byte-exact.
  `<name>.expected.json` gives the classification: `parsed`,
  `operation_type`, `operation_name`, `refusal` (a reason code or `null`),
  and the variable names in body order. `gh-*` files are captures; `adv-*`
  files are hand-authored adversarial documents (multi-operation, keywords
  hidden in comments and string literals, anonymous queries, mutations,
  subscriptions, empty and garbage bodies, duplicate keys, bad encodings, a
  present `variables` member that is not an object — `null` included).
- `rest/<name>.http` — a complete captured request. `<name>.expected.json`
  gives the method and the byte-exact canonical upstream request-target the
  relay reconstructs.
- `rest/<name>.target` — one raw request-target, byte-exact, hand-authored.
  `<name>.expected.json` gives either `upstream_target` (the canonical
  request-target) or `refusal` as `{stage, reason, status}`. `pos-*` cases
  canonicalize; `neg-query-*`, `neg-path-*`, and `neg-form-*` refuse at the
  named stage.
- `ingress/<name>.http` — hostile request bytes (smuggling shapes, folding,
  control bytes, oversize and unknown tokens, non-origin-form targets,
  fragments, `HTTP/1.0`, the HTTP/2 preface, malformed framing, bodies on
  `GET`). `<name>.expected.json` gives `{stage, reason, status}`; `stage` is
  `null` once the request-target validated (a header, framing, or body
  refusal).
- `capture_server.py`, `regenerate.sh` — the capture tooling above.
