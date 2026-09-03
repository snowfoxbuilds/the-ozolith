Status: ACCEPTED
Date: 2026-09-03

Provenance: issue #115 was a feasibility spike — a live proof of concept that
a credential-free `gh` authenticates through driver-side auth injection over
`http_unix_socket`, with the design questions left open. The design here is
the ruling of the human grilling session of 2026-09-03 that followed it,
which settled every open question: a Unix socket in the job dir as the
transport, a read-only relay, every driver-bearing worker type, a dedicated
credential that is never the driver PAT, the Context Tree retired rather than
kept beside `gh`, and Run Contract `schema_version` 2. The issue body is the
spike's record, not the approved plan. A pre-merge review of the first draft
(#117) folded in five hardening gaps — audit integrity, audit bounds and
redaction, the credential's capability contract and preflight, redirect
semantics, and compatibility-version consistency — without changing any of
the rulings above.

# ADR-0057: GitHub Relay — `gh` as the agent's read surface, credential-free by driver-side auth injection

## Context

Run containers hold no GitHub credential (ADR-0013), so an agent's only view of
GitHub was the Context Tree: a driver-serialized snapshot of the issue, PR,
and dependency closure mounted into the job dir (#52, ADR-0053). That kept
every input deterministic and evidence-recorded, but it gave the agent a
frozen copy of one conversation and nothing else — no other issue, no upstream
repo, no check or workflow state, no live state — and it made the agent's
GitHub interaction a bespoke file layout rather than the tool every engineer
already uses. Issue #115 proved the alternative live: `gh` sets
`http_unix_socket` and sends plaintext HTTP over a Unix socket; a driver-owned
relay on that socket injects the `Authorization` header, re-originates TLS to
GitHub, and gates every request — a credential-free `gh` (empty config dir, a
sentinel `GH_TOKEN`) authenticated as the real account and a disallowed call
was refused with a 403 the client rendered on stderr. The spike also
established the design's one hard finding: nearly every high-level `gh`
command tunnels through the single `POST /graphql` endpoint, so "which
commands are allowed" is a GraphQL-body classification problem, not a
path-allowlist problem, and it must fail closed. Workers are not yet live, so
nothing here preserves compatibility with the Context Tree shape.

Two facts about the job directory and the credential shape the hardening
below. The whole job dir is bind-mounted and therefore agent-writable once
the container starts (the reason the driver already freezes a trusted input
snapshot before launch), so a security record placed there is the agent's to
rewrite. And a fine-grained token's grants are not introspectable through the
API, so the product can prove a token has the reads it needs but never that
it lacks writes it does not.

## Decision

1. **`gh` replaces the Context Tree as the agent's GitHub read surface.** The
   prompt carries the task statement; `gh` is the window onto everything else.
   Inline in the prompt: the rules; repository and issue number (plus PR number
   and branch on resume rounds); the issue body; on a Review Run the PR body
   exactly as the driver composed it (narrative plus Decisions Section — the
   artifact under judgment); on resume rounds the revised plan; the
   topologically ordered dependency issue numbers the driver already computes,
   and any Chained Base note. Comments, reviews, timeline, checks, other
   issues, other repositories: `gh`. The driver-computed git facts of a Review
   Run — `base.md`, `changed-files.md`, `signals.md` — stay as job-dir files:
   they are git facts, not GitHub reads. The Context Tree serializer and its
   `input/issue/`, `input/pr/` (GitHub-derived parts), and `input/deps/` trees
   are deleted outright, including the pre-launch evidence snapshot of them —
   GitHub is durable and the audit log below records what was read.
2. **Read-only. The Output Proposal stays the sole mutation surface.** The
   relay permits REST `GET`/`HEAD` and GraphQL `query` operations; every
   mutation — REST write verbs and any GraphQL document containing a
   non-query operation — is refused at the relay. ADR-0046 is unchanged: the
   agent gains eyes, not hands, and the driver still renders every published
   artifact from the validated proposal. A refusal carries a per-class message
   the client renders verbatim, pointing at the existing channel (a mutation:
   "Workers never write to GitHub. Everything you want published goes through
   your Output Proposal — run `format-output status`."). Permitting any
   mutation through the relay is a separate decision against ADR-0046, not an
   extension of this one.
3. **A dedicated read-only credential, never the driver PAT — with a stated
   capability contract and a boot preflight that proves it.** Every
   driver-bearing worker type declares a required secret slot,
   `GITHUB_READ_TOKEN` (`""` — every instantiating Stack must bind it; refused
   at config load when unbound or unreadable, per ADR-0047), delivered
   `_FILE`-style from tmpfs like `GITHUB_TOKEN` and read by the driver as
   `{ROLE}_GITHUB_READ_TOKEN` with the existing precedence convention. The
   slot name travels in Candidate Bundles like every other slot.
   - *Minimum permission matrix* (operator doctrine, documented with the slot
     in the example worker types; not product-enforced because it cannot be):
     a fine-grained token on GitHub.com with these repository permissions,
     all **read**, on every repository the worker may read — **Metadata**
     (every read), **Contents** (commits, trees, blobs, `gh pr diff`, the
     default-branch commit the preflight exercises), **Issues**, **Pull
     requests**, **Checks** (check runs and suites, `gh pr checks`), **Commit
     statuses**, and **Actions** (workflow runs and jobs — the CI surface the
     Reviewer is promised) — plus public-repository read, which fine-grained
     tokens carry implicitly. On GHES the endpoint set is identical under the
     host's `/api/v3` and `/api/graphql` prefixes; fine-grained tokens exist
     only on versions that ship them, so an operator without them binds a
     classic token with the smallest read scope the host offers — not
     read-only, an operator-accepted residual the relay's policy still bounds.
   - *What "CI logs" means under item 5*: check-run output and annotations,
     commit statuses, and workflow run and job listings with their step
     conclusions. Raw job-log and artifact downloads answer with a redirect to
     a signed URL on another origin and are therefore refused (item 5) —
     `gh run view --log` is outside the supported command matrix in v1, with
     the refusal saying so. Serving those origins is the explicit-allowlist
     decision item 5 names, never a silent widening.
   - *Boot preflight*: the driver's one-per-boot identity dry-run gains a
     relay-credential preflight, run through the relay's own upstream client
     so host pinning, prefixes, and redirect rules are exercised too:
     identity (`GET /user`); the Bound Workspace (`GET /repos/{owner}/{repo}`,
     which also yields the default branch); then one representative read per
     promised capability against that repository — `commits?per_page=1`
     (Contents; its first sha is the probe commit), `issues?state=all&per_page=1`,
     `pulls?state=all&per_page=1`, `commits/{sha}/check-runs?per_page=1`
     (Checks), `commits/{sha}/status` (Commit statuses), `actions/runs?per_page=1`
     (Actions), and a one-field GraphQL `query` for the repository id (the
     GraphQL endpoint through this token). An empty result set is a pass; a
     401 or 403 fails; a 404 on the Actions listing after the repository
     itself resolved records "Actions unavailable on this host" in the boot
     report rather than failing (a host or repository with Actions disabled
     has no CI-run surface to promise); any other 404 fails. A failure names
     the missing capability, the endpoint, and the host — never a credential
     byte — and no claim is accepted while it stands. A missing, unreadable,
     or expired token fails the same way (unreadable at config load; expired
     as a 401). The product never probes with a mutation. Honest limit: the
     preflight verifies the reads the product needs and cannot prove the
     token lacks unrelated write permissions — an accidentally write-capable
     token passes it — so the read-only credential remains an operator
     requirement and the relay's policy remains the enforcement backstop.
   - Fact recorded for operators: GitHub's primary rate limit is per user
     account and shared by every token that account owns, so permission
     separation comes from the scoped token while a separate rate budget
     needs a separate machine account — an operator optimization the product
     neither requires nor recommends; the per-Run caps below are the
     product's protection of the driver's budget.
4. **Classification is a stdlib GraphQL document lexer, fail-closed.** `worker/`
   stays dependency-free (ADR-0010): a hand-written lexer strips comments and
   string literals, walks the top-level definitions, and classifies each as
   `query`, `mutation`, `subscription`, or `fragment`; a document with more
   than one operation, any non-query operation, an unparseable body, or an
   unclassifiable definition is refused. The lexer is tested against a fixture
   corpus of every wire shape the pinned `gh` emits (issue and PR view, list,
   diff, checks; repo view; search; raw `gh api graphql`), plus multi-operation
   documents and comment/string evasion cases. REST is classified by method,
   and a small explicit path denylist refuses admin-class reads regardless of
   method — webhook, deploy-key, Actions secret and variable, collaborator, and
   invitation sub-resources of a repository, and `/orgs/*`, `/enterprises/*`,
   `/user/*` beyond `/user` itself — as defense in depth against a mis-scoped
   token.
5. **Host-pinned, credential-scoped, redirect-safe; no repository parsing.**
   The relay re-originates only to the Bound Workspace's GitHub host
   (`api.github.com`, or a GHES host's `/api/v3` and `/api/graphql`); the
   harness sets `GH_HOST` accordingly. Which repositories are readable is
   bounded by the credential, not by a second parser: cross-repository reads
   within the bound set are acceptable, public-repository reads are a feature.
   Redirects are part of the pin. The upstream client never follows a
   redirect on its own. On a 3xx the relay resolves `Location` against the
   request URL and follows only when the result has exactly the configured
   scheme (`https`), host (byte-equal after lowercasing, no trailing dot, no
   percent-encoding or user-info in the authority), port, and API prefix; the
   same method (a 303 turning into GET changes nothing — `GET` and `HEAD` are
   all the relay ever sends); at most 3 hops; and the per-request size and
   time budgets still hold. The credential is re-attached only to such a
   same-origin hop. Everything else — another host, `http`, another port,
   user-info, an unparseable or scheme-relative `Location`, a loop, a longer
   chain — fails closed: the client receives a 502-class refusal carrying the
   stable reason, and no header of the original request reaches the foreign
   origin. This deliberately excludes GitHub's own asset and archive origins
   (`objects.githubusercontent.com`, the Actions log and artifact stores,
   release assets, `codeload`): serving any of them later is a separate
   decision that names an explicit host allowlist and strips the credential
   before the redirected hop. Each redirect decision is one audit record —
   status, decision, reason code, and the `Location`'s scheme and host only,
   never its path or query, which may carry a signed token.
6. **Transport: a socket file inside the job dir; the relay is a per-Run driver
   child with per-Run budgets.** The socket lives at the job-dir root (never
   under `input/`) and reaches the container through the mount that already
   exists — no new mount, per-Run by construction. ADR-0013's channel clause
   is amended, not reopened: the job directory remains the only driver↔harness
   channel, and the socket file in it carries policy-filtered GitHub reads
   outward and never a credential, authority, or reporter inward. The relay
   runs as a driver child process per Run so a request flood or parser crash
   cannot take the Run's driver with it. Budgets, per Run (defaults): 2,000
   requests; 4 concurrent; 1 MiB request body and 16 MiB response body per
   request; 32 MiB of request bytes and 256 MiB of response bytes in
   aggregate; 30 s upstream timeout per request; 3 redirect hops; and the
   audit budget of item 8. Budgets are reserved atomically under the relay's
   lock before the upstream call — request bytes on receipt, response bytes
   as they stream, a stream that crosses its budget cut and answered with a
   502-class refusal, the bytes still counted — so concurrent requests near a
   limit cannot overshoot it. Refusals count as requests, so a refused flood
   exhausts the count rather than looping forever. Once any budget is
   exhausted every further request receives one stable message naming it
   ("GitHub Relay: <budget> exhausted for this Run; further `gh` calls are
   refused. Your prompt carries the task; `format-output status` shows your
   proposal."). The relay asks the upstream for identity encoding and hands
   the client plain bytes. Its lifetime is the agent phase only: created
   before launch, unlinked when the agent exits, so gate jobs — repo-declared
   commands running agent-authored code — never see GitHub. The harness
   bootstraps the client: it writes `http_unix_socket` into a `GH_CONFIG_DIR`
   outside the checkout (the key has no environment-variable form), and sets
   `GH_HOST` and a sentinel `GH_TOKEN` in the agent environment — `gh`
   refuses to run with no token at all, and the relay overwrites the header.
   The product base images pin a `gh` version; the relay and the client it is
   tested against ship together (a base bump moves every derived-image
   identity — accepted).
7. **Every worker type, one implementation.** Implementer, Reviewer, and the
   Initializer when built share one relay in the base driver and one policy;
   the only per-type variable is prompt wording. No in-container blocking
   exists or may be added: the agent owns its container, so a wrapper `gh` or
   a CLI permission rule is bypassable by construction; the relay is the
   single enforcement point and the refusal message is the feedback channel.
   Flight Decks are untouched — they hold their own credential (ADR-0019).
8. **Audit, not reproduction — in a sink the container cannot reach.** The
   audit log is the security-relevant record of what the credential was asked
   to do, so nothing the run container can write, replace, link, or name may
   be its source of truth.
   - *Sink*: the relay creates the authoritative `gh-audit.jsonl` in
     driver-owned storage outside every container mount — beside the trusted
     input snapshot the driver already keeps, in a per-Run directory the
     driver owns with mode 0700 — opened once with
     `O_CREAT|O_EXCL|O_NOFOLLOW|O_APPEND` and mode 0600 under a name unique to
     the relay instance (run id plus attempt), refusing any pre-existing
     entry; every write goes through that descriptor and the pathname is
     never reopened. The job dir never holds an audit file, and the evidence
     copy comes from the sink alone.
   - *Record shape, redacted by construction*: sequence number; timestamp;
     method; path, with the query string reduced to parameter names plus each
     value's byte length and sha256 (literal values only for the enumerated
     routing parameters `page`, `per_page`, `state`, `sort`, `direction`, and
     only when printable ASCII under 256 bytes); GraphQL operation type and
     name (capped at 128 bytes); variables as name, JSON type, byte length,
     and sha256 of the canonical encoding (literal values only for `owner`,
     `name`, `repo`, `number`, `first`, `last`, `states`, under the same
     printable cap); decision with a reason code from a closed enum, never
     client text; upstream status; request and response byte counts; and the
     redirect decisions of item 5. Never recorded: credentials, the sentinel,
     `Authorization` or any other header, request bodies, response bodies,
     upstream error text, refusal message text, repository content, or any
     bytes copied from the client beyond the fields above; every string is
     ASCII-escaped JSON, so invalid UTF-8 and control bytes cannot reach the
     file unescaped. A record is at most 4 KiB — one that would exceed it is
     replaced by a fixed-size truncation marker — and the file at most 16 MiB
     per Run: at that cap the relay writes one terminal
     `audit-budget-exhausted` record and refuses every further request
     without writing again, so a refusal can never grow the log.
   - *Failure*: a failed audit write (error, disk full, closed descriptor)
     fails closed — the relay stops serving, every further request receives
     the stable `audit-unavailable` refusal, a theozolith.error is emitted,
     and the Run's evidence records `gh_audit: failed`. The Run continues on
     the task in its prompt: no request can reach the credential unaudited.
   - *Publication*: after the agent phase ends and the relay has exited, the
     driver fsyncs the sink, writes `gh-audit.summary.json` beside it —
     record count, byte count, sha256, allowed and refused counts, aggregate
     request and response bytes, budgets hit, termination reason (`clean`,
     `killed`, `crashed`, `orphaned`), audit-failure flag — and publishes
     both into the evidence bundle from the sink. The summary detects
     collection errors (a truncated copy, a missed publish); it is not a
     tamper seal, since nothing untrusted ever reaches the sink. The sink is
     deleted only once the bundle is durably pushed: while a push fails it
     stays and the existing evidence retry carries it; a sink orphaned by a
     driver crash is published by the boot-time evidence sweep as
     `terminated: orphaned` and then removed. A `gh` call counter joins
     progress telemetry. Full input reproducibility is knowingly given up:
     requests are the record, and responses remain recoverable from GitHub.
9. **Run Contract surface (`schema_version` 2).** The job dir gains the socket,
   `worker`'s public API exports the relay as an entry point with an injectable
   upstream, and the bench driver runs the identical policy, never a copy. Two
   upstream modes exist: **live** (host plus a credential source, `_FILE`
   style, never argv) and **none** — the relay refuses every request with an
   explicit message ("This benchmark run has no GitHub upstream; the task is
   fully described in your prompt") and the bench records `gh_upstream: none`.
   A real scratch repository carrying the task's issue (and, in review mode,
   the branch and PR) is the full-fidelity shape; running with no repository
   available is a permitted, recorded benchmark-mode variable, and it stays
   meaningful because the prompt carries the complete task statement (item 1).
   A fixture upstream that serves the task from files is rejected: high-level
   `gh` commands are GraphQL, and answering them means emulating GitHub's
   schema. Both modes run the same audit sink, budgets, and redirect rules;
   the `none` upstream simply never receives a request.
10. **Lifecycle and failure semantics, in order.** Configure (the slot is
    declared, ADR-0047 refuses an unbound or unreadable binding at config
    load) → boot preflight (item 3; no claim before it passes) → per Run:
    create the trusted audit sink (item 8) → bind the socket at the job-dir
    root, unlinking any stale entry the driver itself left there → launch the
    agent → per request: strip the sentinel and every client `Authorization`,
    classify (items 2, 4), reserve budgets (item 6), send with the credential,
    apply the redirect rule (item 5), stream the answer, append the audit
    record → on agent exit: stop accepting, abort in-flight upstream requests
    (a client mid-request sees a closed connection), write the terminal
    record, unlink the socket, exit; the driver waits a bounded interval, then
    kills the relay and records `killed` → publish the sink (item 8) → run
    gates with no socket present → clean the job directory (the sink is in
    another tree and untouched) → retain or push evidence, deleting the sink
    only after a durable push. Failure cases: a relay crash or hang is
    detected as child exit — the driver unlinks the socket, records
    `crashed`, and does not restart the relay (the agent's further calls fail
    at connect; nothing unaudited can happen without the process; the Run
    continues on its prompt); a driver crash takes the relay with the cgroup
    (ADR-0013 kill-the-tree) and leaves the sink for the boot sweep; a
    container interruption ends the agent phase and the shutdown path above
    runs; parallel Runs on one node are isolated by construction — each has
    its own job dir, socket, relay child, and exclusively created sink; a
    local retry or completion retry runs a fresh relay instance with a fresh
    sink name (the attempt suffix) against the same job dir, the previous
    socket unlinked first; a replacement host or restored configuration
    passes through the boot preflight again like any boot; a Run whose job
    manifest carries a `schema_version` the CLI does not accept fails
    pre-work as an infra-class failure exactly as today, and a job dir still
    carrying Context Tree paths is neither read nor tolerated — they are
    unknown entries.
11. **Test obligations for the implementation PR.** With the lexer fixture
    corpus and the pinned-`gh` wire shapes of item 4, the implementation
    must land tests for: audit-path tampering — attempted unlink, replacement,
    truncation, symlink substitution, a forged pre-existing log at the sink
    path, concurrent writers, and a crash during final publication, each
    leaving the published record intact or the failure visible; redaction and
    budgets — oversized variables, many maximum-size requests, hostile query
    strings, invalid UTF-8 and control bytes, secret-shaped values,
    exhaustion of every budget with its exact message, concurrent requests at
    a limit, and byte-exact redaction output; the token preflight — each
    missing capability failing by name, empty result sets passing, expired
    and unreadable tokens, the Actions-unavailable case, and no credential
    byte in any message; redirects — same-host followed, cross-host,
    HTTP downgrade, changed port, user-info authority, loops, over-length
    chains, and proof that `Authorization` never reaches the redirected
    server; shutdown with active requests; socket and sink cleanup after
    every exit path; parallel-Run isolation; GitHub.com and GHES prefix and
    host handling; bench `live` and `none` parity through the exported entry
    point; no relay reachable during gate jobs; no credential in argv, the
    agent's environment, logs, errors, evidence, request echoes, or
    redirected requests; every refusal message byte-exact; and `schema_version`
    2 with the new prompt shape.

## Consequences

- **Positive**: agents read GitHub with the tool and idioms they already know,
  reaching beyond one frozen conversation; the write side is untouched, so the
  ADR-0046 guarantee — forbidden mutations unrepresentable — holds exactly as
  before; the credential in the relay cannot write even if a body evades
  classification; one prompt shape serves production and the bench; every
  read is audited in a record the agent cannot touch, bounded in size, and
  free of repository content; a redirect can never carry the credential off
  the pinned origin; a missing read capability surfaces at boot, not as a
  mid-Run 403; the Context Tree serializer, its evidence snapshot, and the
  navigation guide are deleted rather than maintained beside a second source.
- **Negative**: a Run's inputs are no longer fully reproducible from its
  evidence bundle; a second GitHub credential per worker type is operator
  work, and its permission matrix is longer than the first draft promised; the
  relay is new trusted code — an HTTP parser fed by the agent, a GraphQL lexer
  whose fixture corpus must move with every `gh` bump, and a redirect
  validator; the driver gains a per-Run storage lifecycle for the audit sink;
  raw CI log and artifact bytes are not served in v1; review mode's
  constructible-without-GitHub property now yields a recorded mode deviation
  rather than production fidelity; a bench run at full fidelity needs the
  bench side to provision real GitHub objects.
- **Neutral**: amends ADR-0013 (channel clause), ADR-0053 (Context Tree
  closure and Review Run parity clauses), and ADR-0054 with BENCH-CONTRACT.md
  (`schema_version` 2, relay entry point, upstream modes); ADR-0046 and
  ADR-0010 are explicitly unchanged; the glossary gains GitHub Relay and
  retires Context Tree; the ADR-0017 "context is read at checkout" clause now
  means the agent reads it live through the relay; the preflight proves
  presence of reads, not absence of writes — the only proof GitHub's API
  offers.

## Alternatives Considered

- **Keep the Context Tree beside `gh`**: rejected — two sources for the same
  conversation confuse the agent about which is canonical, and the snapshot's
  determinism benefit is worth less than the maintenance of a second surface;
  the prompt's inline task statement keeps the bench runnable without it.
- **A mutation allowlist through the relay** (e.g. comment on the claimed PR):
  rejected — it competes with the Output Proposal, collides with the driver's
  fixed verdict-publication order and the ban on parsing pipeline state from
  prose, and turns a structural guarantee into a parser that must stay
  correct.
- **The driver PAT in the relay**: rejected — the classifier would be the only
  thing between a prompt-injected session and a GitHub write, admin-class
  reads would be real leaks, and a looping session could exhaust the driver's
  own budget.
- **`graphql-core` as a runtime dependency**: rejected — ADR-0010's
  stdlib-only doctrine holds; the needed classification is small enough for a
  hand-written lexer, and the read-only credential bounds a misclassification
  to a read.
- **Repository scoping inside the relay**: rejected — a second parser over
  GraphQL variables and literals for a bound the credential already enforces.
- **A shim `gh` relaying argv through job files to a driver-run real `gh`**:
  rejected — it honors the letter of the file-only channel but places
  agent-controlled argv inside a credentialed process (`gh extension install`,
  `gh alias`, `--repo` steering) and reimplements gh's surface; the socket in
  the job dir keeps `gh` execution in the container.
- **TLS interception (`HTTPS_PROXY` plus a baked CA)**: rejected — gh's
  `http_unix_socket` makes the relay a plain reverse proxy with no trust-store
  changes.
- **In-container command blocking** (wrapper on PATH, CLI permission `deny`
  rules): rejected — bypassable by the process that owns the container, absent
  a codex twin, outside ADR-0055's declarative allowlist, and duplicative of
  the refusal message.
- **Archiving response bodies into evidence**: rejected — unbounded size for
  a triage case GitHub itself serves.
- **The audit log in the job dir, copied into evidence at exit**: rejected —
  the job dir is agent-writable, so the copy would authenticate whatever the
  agent left there; the same reason the driver already snapshots inputs
  before launch.
- **Recording GraphQL variable values and query strings verbatim**: rejected
  — 2,000 requests of 1 MiB each is gigabytes of attacker-chosen bytes in
  durable evidence, and the values carry repository content and search text;
  names, sizes, and digests answer every triage question the log exists for.
- **Following redirects transparently (the HTTP client's default)**: rejected
  — a permitted GET can answer with a redirect to any origin, and a client
  that follows it re-sends the credential there; GitHub's own asset stores
  are exactly such origins.
- **Restarting a crashed relay mid-Run**: rejected — it reintroduces state
  (budgets, sink) that a fresh instance cannot faithfully continue, and a
  crashed relay is a bug to fix, not a condition to paper over; the Run still
  has its task.
- **Anonymous public GitHub as the no-upstream bench mode**: rejected — the
  task repository 404s among partial answers and rate-limit noise; an explicit
  refusal teaches the agent the situation on its first call.
- **A fixture upstream serving the Context Tree for bench**: rejected —
  emulating GitHub's GraphQL schema for a moving `gh`.
- **Relay alive for the whole container**: rejected — gate steps run
  agent-authored code and need no GitHub.

## Relevant PRs

- #117 — records this decision and the amendments it makes to ADR-0013,
  ADR-0053, ADR-0054, the pipeline spec, the bench contract, and the
  glossary; its pre-merge review folded in the five hardening items named in
  the provenance.
