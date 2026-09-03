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
the rulings above. A second review of the same PR carried one further human
ruling — V1 supports GitHub.com only, a configured GitHub Enterprise Server
host fails closed before dispatch, no classic-token fallback exists, and GHES
support waits for a future explicit decision — and corrected five defects in
the first hardening: audit records are now written ahead of credential use,
GraphQL `POST` redirects are refused, responses are budget-gated before any
byte reaches the agent, every retry gets a fresh Run directory, and the
credential's defense-in-depth claim is stated conditionally.

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

Three facts shape the hardening below. The whole job dir is bind-mounted and
therefore agent-writable once the container starts (the reason the driver
already freezes a trusted input snapshot before launch), so a security record
placed there is the agent's to rewrite. A fine-grained token's grants are not
introspectable through the API, so the product can prove a token has the
reads it needs but never that it lacks writes it does not. And every Run —
first attempt, local retry, completion retry — already has its own `run_id`,
its own job directory keyed by that id, and its own evidence bundle; the relay
inherits that shape rather than inventing an attempt counter beside it.

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
   relay permits REST `GET`/`HEAD` and single-operation GraphQL `query`
   documents — which `gh` sends as `POST /graphql` and the relay classifies by
   body, never by method alone; every mutation — REST write verbs and any
   GraphQL document containing a non-query operation — is refused at the
   relay. ADR-0046 is unchanged: the agent gains eyes, not hands, and the
   driver still renders every published artifact from the validated proposal.
   A refusal carries a per-class message the client renders verbatim, pointing
   at the existing channel (a mutation: "Workers never write to GitHub.
   Everything you want published goes through your Output Proposal — run
   `format-output status`."). Permitting any mutation through the relay is a
   separate decision against ADR-0046, not an extension of this one.
3. **A dedicated fine-grained GitHub.com credential, never the driver PAT —
   with a stated capability contract, a host contract, and a boot preflight
   that proves what can be proved.** Every driver-bearing worker type declares
   a required secret slot, `GITHUB_READ_TOKEN` (`""` — every instantiating
   Stack must bind it; refused at config load when unbound or unreadable, per
   ADR-0047), delivered `_FILE`-style from tmpfs like `GITHUB_TOKEN` and read
   by the driver as `{ROLE}_GITHUB_READ_TOKEN` with the existing precedence
   convention. The slot name travels in Candidate Bundles like every other
   slot.
   - *Supported host — GitHub.com only (amended 2026-09-03, #117)*: V1 accepts
     exactly one Bound Workspace host for driver-bearing Stacks, `github.com`,
     whose canonical API origin is `https://api.github.com` with the existing
     GitHub.com REST paths at its root and GraphQL at `/graphql`. Any other
     effective host — a GitHub Enterprise Server instance in every spelling
     the ADR-0056 canonicalizer recognizes as a host identity, or anything
     else — is refused at config load and again, unconditionally, at driver
     boot, before any claim or Review Run is accepted, with one message:
     "GitHub Enterprise Server is unsupported in V1: the GitHub Relay requires
     a future relay-host contract." No credential preflight, relay child,
     socket, or agent container is started for an unsupported host. The
     upstream client is written against a single host adapter (origin, REST
     root, GraphQL path) and V1 ships exactly one; a second adapter is a
     future decision with its own ADR, and nothing here anticipates its
     behavior. ADR-0056's host canonicalizer is unchanged: it still names a
     GHES host as an identity, and this ruling refuses that identity for
     dispatch.
   - *Token form — fine-grained only, no classic-token fallback*: the slot
     must hold a fine-grained personal access token. The driver recognizes the
     fine-grained format by its `github_pat_` prefix at boot and refuses any
     other form — classic (`ghp_`), OAuth, App installation, or unrecognized —
     naming the form and the slot, never a byte of the value. The format check
     proves shape, not permissions; the preflight below proves reads; nothing
     proves the absence of writes.
   - *Minimum permission matrix* (operator doctrine, documented with the slot
     in the example worker types; not product-enforced because it cannot be):
     a fine-grained token on GitHub.com with these repository permissions,
     all **read**, on every repository the worker may read — **Metadata**
     (every read), **Contents** (commits, trees, blobs, `gh pr diff`, the
     default-branch commit the preflight exercises), **Issues**, **Pull
     requests**, **Checks** (check runs and suites, `gh pr checks`), **Commit
     statuses**, and **Actions** (workflow runs and jobs — the CI surface the
     Reviewer is promised) — plus public-repository read, which fine-grained
     tokens carry implicitly.
   - *What "CI logs" means under item 5*: check-run output and annotations,
     commit statuses, and workflow run and job listings with their step
     conclusions. Raw job-log and artifact downloads answer with a redirect to
     a signed URL on another origin and are therefore refused (item 5) —
     `gh run view --log` is outside the supported command matrix in v1, with
     the refusal saying so. Serving those origins is the explicit-allowlist
     decision item 5 names, never a silent widening.
   - *Boot preflight*: once the host and token-form checks pass, the driver's
     one-per-boot identity dry-run gains a relay-credential preflight, run
     through the relay's own upstream client so host pinning, the API paths,
     the response gate, and the redirect rules are exercised too: identity
     (`GET /user`); the Bound Workspace (`GET /repos/{owner}/{repo}`, which
     also yields the default branch); then one representative read per
     promised capability against that repository — `commits?per_page=1`
     (Contents; its first sha is the probe commit), `issues?state=all&per_page=1`,
     `pulls?state=all&per_page=1`, `commits/{sha}/check-runs?per_page=1`
     (Checks), `commits/{sha}/status` (Commit statuses), `actions/runs?per_page=1`
     (Actions), and a one-field GraphQL `query` for the repository id (the
     GraphQL endpoint through this token). An empty result set is a pass; a
     401 or 403 fails; a 404 on the Actions listing after the repository
     itself resolved records "Actions unavailable for this repository" in the
     boot report rather than failing (a repository with Actions disabled has
     no CI-run surface to promise); any other 404 fails. A failure names the
     missing capability, the endpoint, and the host — never a credential
     byte — and no claim is accepted while it stands. A missing, unreadable,
     or expired token fails the same way (unreadable at config load; expired
     as a 401). The product never probes with a mutation.
   - *The credential guarantee, stated honestly (amended 2026-09-03, #117)*:
     operators must bind a GitHub.com fine-grained token carrying only the
     read permissions above. The preflight proves the required reads and
     cannot prove the absence of unrelated write grants — an accidentally
     write-capable token passes it. When the token is provisioned as
     required, it is defense in depth: a request that evades classification
     still reaches GitHub with a credential that cannot write. When an
     operator supplies a broader token, the relay's classifier and denylist
     are the write barrier, and the product claims nothing stronger — no
     message, doc, or evidence field states that the credential cannot write.
     The driver PAT is never used by the relay under any condition: not as a
     fallback, not for the preflight, not when the slot is unbound.
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
   token. Classification applies to every URL the relay is about to send with
   the credential, a redirect target as much as the original request (item 5).
5. **Host-pinned, credential-scoped, redirect-safe; no repository parsing.**
   The relay re-originates only to `https://api.github.com` (item 3); the
   harness sets `GH_HOST=github.com`. Which repositories are readable is
   bounded by the credential, not by a second parser: cross-repository reads
   within the bound set are acceptable, public-repository reads are a feature.
   Redirects are part of the pin, and their handling is decided per request
   class (amended 2026-09-03, #117). The upstream client never follows a
   redirect on its own.
   - *GraphQL `POST /graphql`*: any 3xx answer is refused outright — the relay
     never follows it, never rewrites the request to `GET`, and never replays
     the body. The client receives the 502-class refusal below.
   - *REST `GET` and `HEAD`*: a 3xx may be followed only when the effective
     method stays identical. A status whose standard semantics change the
     method (a `303` on anything but `GET`, and any status the relay does not
     recognize as method-preserving) is refused; `301`, `302`, `307`, and
     `308` on `GET`/`HEAD` are candidates, and the relay re-sends the same
     method with no body.
   - *Every followed hop is a new policy decision*, never inherited from the
     approval of the original endpoint: resolve `Location` against the
     request URL, then require exactly the configured scheme (`https`), host
     (`api.github.com`, byte-equal after lowercasing, no trailing dot, no
     percent-encoding or user-info in the authority), port, and the GitHub.com
     API root; re-run the REST admin-read denylist on the resolved path;
     reapply the method policy; and reapply the redirect-hop (at most 3),
     upstream-time, request-byte, response-byte, and audit budgets. Only a
     hop that passes every check is sent, and only then is the credential
     attached to it. No client-supplied `Authorization` or other sensitive
     header ever accompanies any hop.
   - Everything else — another host, `http`, another port, user-info, an
     unparseable or scheme-relative `Location`, a denylisted path, a loop, a
     longer chain, a budget already spent — fails closed: the client receives
     a 502-class refusal carrying the stable reason, and no header of the
     original request reaches the refused target. This deliberately excludes
     GitHub's own asset, archive, raw-log, and artifact origins
     (`objects.githubusercontent.com`, the Actions log and artifact stores,
     release assets, `codeload`): serving any of them later is a separate
     decision that names an explicit host allowlist and strips the credential
     before the redirected hop.
   - A redirect answer is evaluated before any of its body is delivered
     (item 6), and each redirect decision is one audit record — status,
     decision, reason code, and the `Location`'s scheme and host only, never
     its path or query, which may carry a signed token.
6. **Transport: a socket file inside the job dir; the relay is a per-Run driver
   child with per-Run budgets and a pre-delivery response gate.** The socket
   lives at the job-dir root (never under `input/`) and reaches the container
   through the mount that already exists — no new mount, per-Run by
   construction. ADR-0013's channel clause is amended, not reopened: the job
   directory remains the only driver↔harness channel, and the socket file in
   it carries policy-filtered GitHub reads outward and never a credential,
   authority, or reporter inward. The relay runs as a driver child process per
   Run so a request flood or parser crash cannot take the Run's driver with
   it.
   - *Budgets, per Run (defaults)*: 2,000 requests; 4 concurrent; 1 MiB
     request body and 16 MiB response body per request; 32 MiB of request
     bytes and 256 MiB of response bytes in aggregate; 30 s upstream timeout
     per request; 3 redirect hops; and the audit budget of item 8. Budgets
     are reserved atomically under the relay's lock before the intent record
     of item 8 is written — request bytes on receipt, the full per-request
     response allowance against the aggregate before the upstream call — so
     concurrent requests near a limit cannot overshoot it; unused response
     allowance is returned when the actual count is known. Refusals count as
     requests, so a refused flood exhausts the count rather than looping
     forever. Once any budget is exhausted every further request receives one
     stable message naming it ("GitHub Relay: <budget> exhausted for this Run;
     further `gh` calls are refused. Your prompt carries the task;
     `format-output status` shows your proposal.").
   - *Pre-delivery response gate (amended 2026-09-03, #117)*: no downstream
     status, header, or body byte is sent until the entire upstream response
     has been read and has passed the per-request and remaining aggregate
     response-byte limits. The relay reads the upstream body counting actual
     bytes, into bounded memory or a spool file in driver-owned temporary
     storage outside every container mount; the worst-case exposure is
     bounded by construction at concurrency × per-request response limit
     (64 MiB with the defaults) per Run. Crossing either limit aborts the
     upstream read, discards the buffer or spool, answers the client with the
     stable 502-class budget refusal before any upstream byte has reached it,
     and appends the completion record (item 8). A response that passes is
     delivered whole: the validated status, the allowed headers, and the
     complete body. Response content is never persisted into evidence, logs,
     or the audit sink, and every spool file is deleted on success, refusal,
     timeout, relay exit, driver interruption, and by the boot sweep for any
     Run the driver did not clean. A redirect answer passes through the same
     gate before item 5 decides it, so no redirect body is ever delivered.
     The relay asks the upstream for identity encoding and hands the client
     plain bytes.
   - *Lifetime and bootstrap*: the relay lives for the agent phase only —
     created before launch, unlinked when the agent exits, so gate jobs —
     repo-declared commands running agent-authored code — never see GitHub.
     The harness bootstraps the client: it writes `http_unix_socket` into a
     `GH_CONFIG_DIR` outside the checkout (the key has no environment-variable
     form), and sets `GH_HOST` and a sentinel `GH_TOKEN` in the agent
     environment — `gh` refuses to run with no token at all, and the relay
     overwrites the header. The product base images pin a `gh` version; the
     relay and the client it is tested against ship together (a base bump
     moves every derived-image identity — accepted).
7. **Every worker type, one implementation.** Implementer, Reviewer, and the
   Initializer when built share one relay in the base driver and one policy;
   the only per-type variable is prompt wording. No in-container blocking
   exists or may be added: the agent owns its container, so a wrapper `gh` or
   a CLI permission rule is bypassable by construction; the relay is the
   single enforcement point and the refusal message is the feedback channel.
   Flight Decks are untouched — they hold their own credential (ADR-0019).
8. **Audit, not reproduction — write-ahead, in a sink the container cannot
   reach.** The audit log is the security-relevant record of what the
   credential was asked to do, so nothing the run container can write,
   replace, link, or name may be its source of truth, and no credentialed
   request may exist without a record written before it.
   - *Sink*: the relay creates the authoritative `gh-audit.jsonl` in
     driver-owned storage outside every container mount — beside the trusted
     input snapshot the driver already keeps, in the per-Run directory keyed
     by `run_id` that the driver owns with mode 0700 — opened once with
     `O_CREAT|O_EXCL|O_NOFOLLOW|O_APPEND` and mode 0600 under a name unique to
     the Run (the `run_id`), refusing any pre-existing entry; every write goes
     through that descriptor and the pathname is never reopened. The job dir
     never holds an audit file, and the evidence copy comes from the sink
     alone.
   - *Write-ahead protocol (amended 2026-09-03, #117)*: a request is handled
     in a fixed order. First the relay strips the sentinel and every client
     `Authorization`, classifies (items 2, 4, 5), and reserves every
     applicable budget (item 6) — including audit capacity for the intent
     record, the completion record, and the terminal record that item 8's cap
     guarantees. Then it appends an **intent record** to the sink with a
     single write followed by `fdatasync`. Only that durable write authorizes
     credential use: the upstream connection is opened and the credential
     attached only after it returns, and if it fails no connection is
     attempted and no credential is touched — the client receives the
     `audit-unavailable` refusal. A request refused during classification or
     reservation writes one intent record carrying `decision: refused` and
     its reason, and never proceeds. After the upstream operation finishes —
     delivered, refused at the response gate, redirected and refused, timed
     out, or aborted — the relay appends a **completion record** correlated
     to the intent by sequence number, carrying the upstream status, the
     actual request and response byte counts, every redirect decision of item
     5, and the terminal reason. If the completion append fails, the intent
     record already proves the credential use; the relay enters the
     audit-unavailable state and forwards nothing further. Capacity is
     reserved up front so the relay can never finish an upstream call and
     then lack room for its completion record: the last reservation the file
     cap admits still fits both records and the terminal record.
   - *Record shape, redacted by construction*: sequence number; record kind
     (`intent`, `completion`, `terminal`); timestamp; method; path, with the
     query string reduced to parameter names plus each value's byte length
     and sha256 (literal values only for the enumerated routing parameters
     `page`, `per_page`, `state`, `sort`, `direction`, and only when printable
     ASCII under 256 bytes); GraphQL operation type and name (capped at 128
     bytes); variables as name, JSON type, byte length, and sha256 of the
     canonical encoding (literal values only for `owner`, `name`, `repo`,
     `number`, `first`, `last`, `states`, under the same printable cap);
     decision with a reason code from a closed enum, never client text; the
     budgets reserved (intent) or the upstream status, byte counts, redirect
     decisions, and terminal reason (completion). Never recorded:
     credentials, the sentinel, `Authorization` or any other header, request
     bodies, response bodies, upstream error text, refusal message text,
     repository content, or any bytes copied from the client beyond the
     fields above; every string is ASCII-escaped JSON, so invalid UTF-8 and
     control bytes cannot reach the file unescaped. A record is at most 4 KiB
     — one that would exceed it is replaced by a fixed-size truncation marker
     — and the file at most 16 MiB per Run: when a reservation would cross
     that cap the relay writes one terminal `audit-budget-exhausted` record
     and refuses every further request without writing again, so a refusal
     can never grow the log.
   - *Audit unavailable*: the state a failed audit write (error, disk full,
     closed descriptor, short write) puts the relay in. The process stays up
     and keeps answering the socket; it forwards nothing, attaches the
     credential to nothing, and returns the fixed `audit-unavailable` refusal
     without writing again. A theozolith.error is emitted and the Run's
     evidence records `gh_audit: failed`. The Run continues on the task in
     its prompt.
   - *Publication*: after the agent phase ends and the relay has exited, the
     driver fsyncs the sink, writes `gh-audit.summary.json` beside it —
     record count, byte count, sha256, request counts by outcome (`complete`:
     intent with its completion; `refused`: refused before any upstream I/O;
     `incomplete`: intent whose completion the relay could not write and
     accounted for in its terminal record or its audit-failure transition;
     `indeterminate`: intent with neither a completion nor any terminal
     accounting — the relay died between intent and completion, so whether
     the upstream connection was opened is unknown), aggregate request and
     response bytes, budgets hit, termination reason (`clean`, `killed`,
     `crashed`, `orphaned`), audit-failure flag — and publishes both into the
     evidence bundle from the sink. An `indeterminate` request is therefore
     always distinguishable from one that never passed authorization: the
     latter has a `refused` intent or no record at all. The summary detects
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
   upstream modes exist: **live** (a GitHub.com credential source, `_FILE`
   style, never argv — the host is fixed by item 3) and **none** — the relay
   refuses every request with an explicit message ("This benchmark run has no
   GitHub upstream; the task is fully described in your prompt") and the
   bench records `gh_upstream: none`. A real scratch repository carrying the
   task's issue (and, in review mode, the branch and PR) is the
   full-fidelity shape; running with no repository available is a permitted,
   recorded benchmark-mode variable, and it stays meaningful because the
   prompt carries the complete task statement (item 1). A fixture upstream
   that serves the task from files is rejected: high-level `gh` commands are
   GraphQL, and answering them means emulating GitHub's schema. Both modes
   run the same write-ahead audit sink, budgets, response gate, and redirect
   rules; the `none` upstream simply never receives a request, so every
   record it produces is a `refused` intent.
10. **Lifecycle and failure semantics, in order.** Configure (the slot is
    declared; ADR-0047 refuses an unbound or unreadable binding, and item 3
    refuses an unsupported host, at config load) → driver boot: host check,
    token-form check, preflight (item 3; no claim or Review Run before all
    three pass) → per Run, in a job directory and a driver-owned trusted
    directory both keyed by this Run's `run_id`: create the audit sink (item
    8) → bind the socket at the job-dir root — the job directory is freshly
    created for this `run_id`, so an entry already at the socket path is a
    loud pre-work infra-class failure, never unlinked blindly → launch the
    agent → per request: strip, classify, reserve budgets and audit capacity,
    append and sync the intent record, open the upstream connection with the
    credential, apply the redirect rule per hop (item 5), read the whole
    response through the gate (item 6), deliver or refuse, append the
    completion record (item 8) → on agent exit: stop accepting, abort
    in-flight upstream requests (a client mid-request sees a closed
    connection; each aborted request gets a completion record with reason
    `aborted`, or is listed in the terminal record when that append fails),
    write the terminal record, delete any spool, unlink the socket, exit; the
    driver waits a bounded interval, then kills the relay and records
    `killed` → publish the sink (item 8) → run gates with no socket present →
    clean the job directory and any spool (the sink is in another tree and
    untouched) → retain or push evidence, deleting the sink only after a
    durable push.
    - *Retries (amended 2026-09-03, #117)*: every retry is a new Run with a
      new `run_id`, and therefore a fresh job directory, socket, relay child,
      trusted directory, audit sink, and evidence bundle; nothing reopens or
      reuses the previous Run's socket, job directory, or sink. A local retry
      carries no filesystem state at all — fresh clone, fresh container. A
      completion retry carries only what the existing trusted carryover
      mechanism already enumerates — the authorized working tree and the
      pending Output Proposal — transferred by the driver into the new Run;
      no socket, spool, audit record, or budget state travels with them. The
      previous Run's sink stays attached to that Run's evidence bundle.
    - *Crash points*: a relay crash or hang is detected as child exit — the
      driver unlinks the socket, deletes the spool, records `crashed`, and
      does not restart the relay (the agent's further calls fail at connect;
      the Run continues on its prompt); a crash after an intent record and
      before the upstream connection, during the upstream request, or after
      completion but before the completion record all leave the same
      evidence — an intent without a completion — which the summary reports
      as `indeterminate`, and the credential is treated as possibly used. A
      driver crash takes the relay with the cgroup (ADR-0013 kill-the-tree)
      and leaves the sink and any spool for the boot sweep, which publishes
      the sink as `orphaned` and deletes the spool; stale-socket cleanup is
      that same sweep's orphan recovery for the dead Run's own job directory,
      never a normal retry mechanism. A container interruption ends the agent
      phase and the shutdown path above runs. Parallel Runs on one node are
      isolated by construction — each has its own `run_id`, job dir, socket,
      relay child, spool, and exclusively created sink. A replacement host or
      restored configuration passes through the boot checks again like any
      boot. A Run whose job manifest carries a `schema_version` the CLI does
      not accept fails pre-work as an infra-class failure exactly as today,
      and a job dir still carrying Context Tree paths is neither read nor
      tolerated — they are unknown entries.
11. **Test obligations for the implementation PR.** With the lexer fixture
    corpus and the pinned-`gh` wire shapes of item 4, the implementation
    must land tests for:
    - host and token contract — a GHES host in every canonicalizer spelling
      refused at config load and at driver boot before any preflight, relay,
      socket, container, claim, or Review Run, with the V1 message; no
      classic-token fallback (a `ghp_`, OAuth, App, and unrecognized token
      form each refused by name; the driver PAT never read by the relay under
      any failure); the preflight — each missing capability failing by name,
      empty result sets passing, expired and unreadable tokens, the
      Actions-unavailable case, an accidentally broad token passing with no
      message overclaiming, and no credential byte in any message;
    - write-ahead audit — no credentialed request without a durable intent
      record (an injected intent-write failure proves no upstream connection
      is opened); intent and completion records correlating exactly; a
      completion-write failure leaving the intent and freezing the relay in
      audit-unavailable while the process keeps refusing; the reservation
      that guarantees room for completion and terminal records at the file
      cap; crashes injected after intent, during upstream, and after
      completion each leaving an `indeterminate` request diagnosable in the
      published summary; audit-path tampering — attempted unlink,
      replacement, truncation, symlink substitution, a forged pre-existing
      log at the sink path, concurrent writers, and a crash during final
      publication — each leaving the published record intact or the failure
      visible;
    - redaction and budgets — oversized variables, many maximum-size
      requests, hostile query strings, invalid UTF-8 and control bytes,
      secret-shaped values, exhaustion of every budget with its exact
      message, concurrent requests at a limit, and byte-exact redaction
      output;
    - redirects — GraphQL `POST` answered with 301, 302, 303, 307, and 308,
      each refused, with proof that no redirected GraphQL request is sent;
      `GET` and `HEAD` method preservation across every followed status; a
      permitted endpoint redirecting to a denylisted path, refused; full
      revalidation on every hop; same-host followed; cross-host, HTTP
      downgrade, changed port, user-info authority, loops, over-length chains
      refused; and proof that `Authorization` never reaches any refused
      target;
    - the response gate — a response exactly at and one byte over the
      per-request limit, the aggregate limit reached across requests,
      concurrent requests near the aggregate limit, an upstream timeout
      mid-body, an interrupted spool, spool disk exhaustion, and proof that
      no downstream byte precedes budget validation and that every spool is
      deleted on every path including the boot sweep;
    - lifecycle — shutdown with active requests; socket and sink cleanup
      after every exit path; a pre-existing socket-path entry in a fresh job
      directory failing loud; local and completion retries each getting a
      fresh job directory, socket, relay, and sink, with completion carryover
      containing only the enumerated state; parallel-Run isolation; driver
      restart and orphan sweep; evidence-push failure retaining the sink;
      bench `live` and `none` parity through the exported entry point; no
      relay reachable during gate jobs;
    - and, throughout, no credential in argv, the agent's environment, logs,
      errors, evidence, audit metadata, request echoes, redirect targets, or
      refused requests; every refusal and diagnostic message byte-exact and
      never claiming the token cannot write; and `schema_version` 2 with the
      new prompt shape.

## Consequences

- **Positive**: agents read GitHub with the tool and idioms they already know,
  reaching beyond one frozen conversation; the write side is untouched, so the
  ADR-0046 guarantee — forbidden mutations unrepresentable — holds exactly as
  before; with the credential provisioned as item 3 requires, a body that
  evades classification still meets a credential that cannot write — a
  guarantee the operator supplies and the product verifies only in part; one
  prompt shape serves production and the bench; every credentialed request is
  preceded by a durable record the agent cannot touch, bounded in size, and
  free of repository content; a redirect can never carry the credential off
  `api.github.com`, and a GraphQL request is never redirected at all; no
  partial upstream body ever reaches the agent, so a budget refusal is always
  a clean 502; a missing read capability, an unsupported host, or a wrong
  token form surfaces at boot, not as a mid-Run 403; the Context Tree
  serializer, its evidence snapshot, and the navigation guide are deleted
  rather than maintained beside a second source.
- **Negative**: a Run's inputs are no longer fully reproducible from its
  evidence bundle; a second GitHub credential per worker type is operator
  work, and its permission matrix is longer than the first draft promised; an
  operator who supplies a broader token silently downgrades the relay's
  classifier from defense in depth to the sole write barrier, and the product
  cannot detect it; GitHub Enterprise Server operators cannot run workers in
  V1; the relay is new trusted code — an HTTP parser fed by the agent, a
  GraphQL lexer whose fixture corpus must move with every `gh` bump, a
  redirect validator, and a response gate holding up to 64 MiB per Run in
  memory or spool; every request costs two audit records and one `fdatasync`
  before its upstream call; the driver gains a per-Run storage lifecycle for
  the sink and the spool; raw CI log and artifact bytes are not served in v1;
  review mode's constructible-without-GitHub property now yields a recorded
  mode deviation rather than production fidelity; a bench run at full
  fidelity needs the bench side to provision real GitHub objects.
- **Neutral**: amends ADR-0013 (channel clause), ADR-0053 (Context Tree
  closure and Review Run parity clauses), and ADR-0054 with BENCH-CONTRACT.md
  (`schema_version` 2, relay entry point, upstream modes); ADR-0046,
  ADR-0010, and ADR-0056's host canonicalizer are explicitly unchanged; the
  per-Run `run_id`, job directory, and evidence bundle contract is reused,
  not extended; the glossary gains GitHub Relay and retires Context Tree; the
  ADR-0017 "context is read at checkout" clause now means the agent reads it
  live through the relay; the preflight proves presence of reads, not absence
  of writes — the only proof GitHub's API offers.

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
- **GHES in V1 through a classic token with the smallest read scope**:
  rejected (2026-09-03, #117) — a classic scope is never read-only, so the
  relay's classifier would be the write barrier by design rather than by
  operator mistake, and a second host adapter with its own path prefixes and
  token forms is a contract of its own; V1 refuses the host and a future ADR
  decides GHES on its merits.
- **`graphql-core` as a runtime dependency**: rejected — ADR-0010's
  stdlib-only doctrine holds; the needed classification is small enough for a
  hand-written lexer, and a correctly provisioned read-only credential bounds
  a misclassification to a read.
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
- **One audit record per request, appended after the upstream call**:
  rejected (2026-09-03, #117) — a failed append after the call leaves a
  credentialed request with no record at all; writing the intent first makes
  the record the authorization, so the worst outcome is a recorded request
  with an unknown result.
- **Recording GraphQL variable values and query strings verbatim**: rejected
  — 2,000 requests of 1 MiB each is gigabytes of attacker-chosen bytes in
  durable evidence, and the values carry repository content and search text;
  names, sizes, and digests answer every triage question the log exists for.
- **Following redirects transparently (the HTTP client's default)**: rejected
  — a permitted GET can answer with a redirect to any origin, and a client
  that follows it re-sends the credential there; GitHub's own asset stores
  are exactly such origins.
- **Following a GraphQL `POST` redirect, or replaying it as `GET`**: rejected
  (2026-09-03, #117) — replaying the body re-sends an agent-chosen document
  to a target the original classification never saw, and converting to `GET`
  changes the operation's meaning; GitHub's GraphQL endpoint does not
  redirect in normal operation, so a 3xx there is an anomaly to refuse.
- **Streaming the upstream body to the agent and cutting it at the budget**:
  rejected (2026-09-03, #117) — once a status line or body byte is on the
  socket the answer cannot become a 502, so the budget would be advisory; a
  bounded buffer or driver-owned spool costs at most 64 MiB per Run and makes
  the refusal real.
- **Reusing the job directory and socket across retries with an attempt
  suffix**: rejected (2026-09-03, #117) — it contradicts the per-Run
  `run_id`, job-directory, and evidence contract every other component
  already follows, and reopening a directory the previous container could
  write would reintroduce the agent-writable-path problem the sink exists to
  avoid.
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
  glossary; its first review folded in the five hardening items named in the
  provenance, and its second review carried the GitHub.com-only ruling and
  the write-ahead audit, GraphQL-redirect, response-gate, per-Run retry, and
  credential-guarantee corrections.
