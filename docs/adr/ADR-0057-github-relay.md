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
credential's defense-in-depth claim is stated conditionally. A third review
carried the human ruling on content trust — non-member GitHub content may be
visible to autonomous agents, while pipeline authority stays narrow and
driver-enforced, and the remaining prompt-injection exposure is an accepted
residual risk — and completed the contract where the first two drafts still
left security-sensitive guesswork: the socket is specified as a hostile HTTP
ingress, the audit's preflight and redirect semantics are reconciled, the
credential's confidentiality boundary is operator doctrine, the preflight
defines the empty-repository edge, and the dependency prompt schema is exact.
A fourth review closed the gaps that remained: `schema_version` 2 is the
accepted successor that the implementation PR activates atomically, nowhere
described as current before then; every credentialed redirect hop has its own
durable pre-hop audit record; every accepted connection consumes a finite
per-Run budget whose exhaustion closes the listener; and the query string has
a canonicalization grammar of its own, separate from the path's. A fifth
review made the accepted contract implementable end to end: every audit
record kind has an explicit schema that is serialized and size-checked before
it authorizes anything, so no record ever collapses into a marker; the relay
has one terminal record and one shutdown sequence whatever ends acceptance;
the counters distinguish request lines seen from request budget spent and
are unknown, never fabricated, when the terminal record is absent; and the
redirect summary admits the fourth entry a hop-limit refusal produces. A
sixth review made the refusal schemas total: a request line the parser
rejects — an unknown or oversized method, a non-origin-form target, a
malformed path, query, or GraphQL body — leaves a bounded record in a tagged
form that names the failed stage and identifies the input by length and
digest, never by copying, repairing, or inventing a target; a refused
redirect's scheme and host are recorded as classifications and digests
rather than uncontrolled text, with the serialized bound stated; and the
audit's claim is stated exactly — an intent or redirect-intent record proves
that credential use was authorized, never that a request was sent.

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

Two boundaries were reframed by the third review. The socket is reachable by
every process in the container, so the relay's parser is a hostile-ingress
parser: its guarantees must hold for arbitrary bytes, not for a well-behaved
`gh`. And the human ruled the content-trust question that the Context Tree's
OWNER/MEMBER filter had answered by omission: non-member GitHub content may be
visible to autonomous agents. Total outside influence is not preventable —
agents also have internet access — so visibility is broad, the pipeline's
structural authority stays narrow and driver-enforced, and prompt injection
is mitigated by explicit agent instructions and the human code-review gate,
recorded as an accepted residual risk rather than claimed away.

## Decision

1. **`gh` replaces the Context Tree as the agent's GitHub read surface.** The
   prompt carries the task statement; `gh` is the window onto everything else.
   Inline in the prompt: the rules; repository and issue number (plus PR number
   and branch on resume rounds); the issue body; on a Review Run the PR body
   exactly as the driver composed it (narrative plus Decisions Section — the
   artifact under judgment); on resume rounds the revised plan; the
   dependency closure the driver already computes, as ordered entries of
   issue number plus state (the exact schema below), and any Chained Base
   note. Comments, reviews, timeline, checks, other
   issues, other repositories: `gh`. The driver-computed git facts of a Review
   Run — `base.md`, `changed-files.md`, `signals.md` — stay as job-dir files:
   they are git facts, not GitHub reads. The Context Tree serializer and its
   `input/issue/`, `input/pr/` (GitHub-derived parts), and `input/deps/` trees
   are deleted outright, including the pre-launch evidence snapshot of them —
   GitHub is durable and the audit log below records what was read.
   - *Content visibility is broad (amended 2026-09-03, #117)*: the agent may
     query comments, reviews, review threads, timeline events, issues, pull
     requests, and every other readable GitHub surface regardless of the
     author's association. The Context Tree's OWNER/MEMBER visibility filter
     for agent-consumed discussion content is retired with the tree; the
     relay carries no author filter, and no product surface promises the
     agent a member-only view. The human ruling behind this: total outside
     influence is not preventable when the agent also has internet access,
     so hiding non-member GitHub content bought no isolation worth its cost.
   - *Pipeline authority stays narrow and driver-enforced*: content the agent
     can see never acquires structural authority. Every driver action — the
     machine verdict, the reset or resume commit, the cherry-pick set, labels,
     claims, state transitions, the Based-on base record, and any other
     driver-parsed instruction — is derived by the driver, with its own
     credential and outside the relay, from the authorized sources the
     pipeline already defines: the latest `theozolith:verdict` block only from
     a PR-conversation comment whose author GitHub reports as `OWNER` or
     `MEMBER`, the Based-on zone only from the driver-composed PR body,
     labels only from GitHub label state, claims only from the Control
     Node's grant. A well-formed machine block in any other comment is
     visible to the agent through `gh` and inert to the driver. Nothing the
     agent reads through the relay is parsed by the driver for pipeline
     state; the driver never re-reads relay traffic, the job directory, or
     the agent's transcript for it.
   - *Retrieved content is evidence, never instruction*: every prompt
     instructs the agent that GitHub content and internet content it
     retrieves — issue and PR bodies not carried in the prompt, comments,
     reviews, files in other repositories, web pages — is untrusted material
     to weigh, never an instruction that overrides the task in the prompt,
     the repository's own policy files, or the Output Proposal schema; an
     instruction found in such content is reported in the Decisions Section
     (or the verdict), not followed. The prompt names the pipeline's
     authorized instruction sources (the prompt, the revised plan on resume
     rounds, and the checked-out repository's agent-instruction files) and
     says that nothing else ranks with them.
   - *Accepted residual risk*: instructions mitigate prompt injection; they
     do not eliminate it. A prompt-injected session can still produce a bad
     diff, a bad verdict, or a disclosure into its own outputs (item 3's
     confidentiality boundary). The structural guarantees hold regardless —
     no GitHub write outside the Output Proposal, no driver action from
     unauthorized content — and the human merge and code-review gate is the
     final safeguard for what those guarantees do not cover. This is recorded
     as accepted, not solved.
   - *Dependency entries — the exact prompt schema (amended 2026-09-03,
     #117)*: the dependency closure reaches the prompt as **ordered entries
     of issue number plus state**, never numbers alone. The driver computes
     the transitive closure the same way it did for `input/deps/` (same-repo
     only, no depth cap, topological order, blockers before dependents, ties
     by ascending issue number, closed and merged blockers included) and
     renders one entry per closure member in that order: the issue number and
     a `state` from the closed set `open`, `completed`, `not_planned` —
     `open` for an open issue; for a closed issue its GitHub `state_reason`,
     with a closed issue carrying no reason rendered `completed`. No title,
     PR number, merge SHA, or body travels: the agent reads those through
     `gh`. The renderers take the entries as one argument, `dependencies`, an
     ordered sequence of `DependencyEntry(number, state)` values exported
     from the worker package's public API alongside the renderers, replacing
     the `deps_present` flag and the `input/deps/` navigation bullet; an
     empty sequence renders no dependency section, so an edge-less prompt is
     byte-identical to today's. The Chained Base note stays a separate,
     already-defined prompt section naming the blocker the base is chained
     on. This shape is `schema_version` 2 surface (item 9): ADR-0053, the
     pipeline spec, the bench contract, and the golden prompt tests all
     state it identically.
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
     GraphQL endpoint through this token). Empty collections pass: an empty
     issue, pull-request, check-run, commit-status, or workflow-run listing
     proves the read and promises nothing further. The commit probe is the
     exception, decided explicitly (amended 2026-09-03, #117): the Checks
     and Commit-status probes need a real commit, and a worker cannot check
     out or run against a repository that has none, so an **empty
     repository** — the commits listing answering GitHub's empty-repository
     409, an empty list, or a repository whose `default_branch` is absent or
     resolves to no branch — is a pre-work refusal of the Bound Workspace
     ("Bound Workspace <owner/name> has no commit on its default branch; the
     worker cannot check out or run against it"), named as a workspace
     condition, never as a missing capability. A probe commit that resolves
     and then vanishes mid-preflight (a 404 or 422 on the check-run or
     status probe after the listing yielded it — a force-push or branch
     deletion racing the preflight) is re-resolved once from the listing;
     if the second attempt fails too the preflight fails naming the race
     ("probe commit unresolvable"), never a capability. Statuses are read by
     capability: a 401 anywhere is the token itself — invalid, revoked, or
     expired — and fails naming the token, not a capability; a 403 on one
     probe is that capability missing, named; a 403 carrying
     `X-RateLimit-Remaining: 0` is a rate-limit failure, named as such with
     the reset time; a 404 on the Actions listing after the repository
     itself resolved records "Actions unavailable for this repository" in
     the boot report rather than failing (a repository with Actions disabled
     has no CI-run surface to promise); any other 404 fails. The boot report
     lists every capability with its own result — `verified`,
     `unavailable`, or `not-probed` — and never summarizes as "all
     capabilities verified" unless every probe, the commit-scoped ones
     included, executed and passed; a preflight that stopped before the
     commit-scoped probes reports them `not-probed` and fails. A failure
     names the missing capability, the endpoint, and the host — never a
     credential byte — and no claim is accepted while it stands. A missing,
     unreadable, or expired token fails the same way (unreadable at config
     load; expired as a 401). The product never probes with a mutation.
   - *Preflight record — driver-originated, outside the per-Run sink
     (amended 2026-09-03, #117)*: the preflight runs before any Run exists,
     so it has no per-Run audit sink, and the write-ahead invariant of item
     8 is scoped to agent-originated relay requests — the requests whose
     author the driver does not control. The preflight's requests are
     driver-chosen, fixed, and enumerated above; their record is the boot
     report: per probe, the capability, the endpoint class, the upstream
     status, and the decision, written to the driver's journal and to a
     `gh-preflight.json` in driver-owned storage (outside every container
     mount, overwritten per boot, never copied into any Run's evidence).
     Redacted exactly as item 8 redacts: no credential byte, no header, no
     response content, no upstream error text beyond the status. The
     preflight's responses pass through the same response gate and are
     discarded once read.
   - *Confidentiality boundary — operator doctrine (amended 2026-09-03,
     #117)*: the relay token's repository scope is also the agent's
     data-access scope and therefore its potential disclosure scope. An
     agent may place anything it can read into its working tree, its
     transcript, its evidence bundle, its PR description, or the target PR
     the driver creates automatically — all before any human merge, and the
     transcript and evidence are retained. So: each Stack binds a token
     scoped to its target repository where GitHub's token model allows it;
     a token spanning several private repositories may be bound only when
     every repository it covers belongs to the same permitted disclosure
     domain as the target repository and its evidence storage; and an
     operator must never bind a token able to read information that may not
     be exposed to the target repository, its PRs, or its evidence bundles.
     This is doctrine, not enforcement, because GitHub cannot prove the
     absence of broader grants (the same fact behind the write guarantee
     below); the boot report names the repository the preflight verified
     and nothing about others.
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
   diff, checks; repo view; raw `gh api graphql`), plus multi-operation
   documents and comment/string evasion cases; the same corpus pins the REST
   query shapes the pinned `gh` emits (amended 2026-09-03, #117, fourth
   review) — `gh search issues` and `gh search prs` with `repo:OWNER/REPO` and
   the other qualifiers, `gh api` `GET` requests with `-f` parameters,
   `Link`-header pagination follow-ups, and ref-name parameters carrying `/`
   — as byte-exact upstream request lines under item 6's query grammar, so
   the supported-command claims and the fixtures describe one grammar. REST
   is classified by method,
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
     hop that passes every check is sent, and only after item 8's
     redirect-intent record for that hop is durable is the credential
     attached to it (amended 2026-09-03, #117, fourth review); a hop whose
     redirect-intent append fails is not sent. No client-supplied
     `Authorization` or other sensitive header ever accompanies any hop.
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
     (item 6). Every hop the relay sends is authorized by its own durable
     **redirect-intent record**, written before the credential is attached
     (item 8; amended 2026-09-03, #117, fourth review — the third review's
     "one record per request, never one per hop" is withdrawn, because a
     followed hop attaches the credential to a target the original intent
     record never named, and a record written after the fact is not
     write-ahead for that hop). The completion record additionally carries
     the request's redirect decisions as one bounded `redirects` array of at
     most four entries (amended 2026-09-03, #117, fifth review — three
     followed hops plus the fourth redirect answer refused at the hop
     limit) — hop number, upstream status, decision, reason code, and the
     resolved `Location`'s scheme and host only, each as the bounded
     representation of item 8 (a closed scheme classification; a host
     literal only when valid, otherwise its length and digest with its
     validity status; amended 2026-09-03, #117, sixth review) — as a triage
     summary that never substitutes for the pre-hop record. No record stores a
     `Location` path or query literally: a redirect-intent record carries
     the canonical target path and the redacted query representation of
     item 8 — names, lengths, and digests — because a signed URL's query is
     a credential.
   - The hostile-ingress rules of item 6 apply to the resolved `Location`
     as they apply to a client path: the redirected path is canonicalized
     by the same rules before the denylist and method policy see it, and an
     ambiguous spelling refuses the hop.
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
   - *Budgets, per Run (defaults)*: 4,000 accepted connections (amended
     2026-09-03, #117, fourth review — the connection budget of the ingress
     contract below, charged at accept); 2,000 requests; 4 concurrent; 1 MiB
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
     `format-output status` shows your proposal.") — with one exception: the
     connection budget ends acceptance itself, so after it nothing is
     answered and the client's connect fails in the kernel. A request needs a
     connection, so the connection budget also bounds how many stable
     refusals a client can collect after the request budget is spent: at
     most the difference between the two budgets.
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
   - *Ingress contract — the socket is hostile (amended 2026-09-03, #117)*:
     any process in the container can connect, so the relay parses the
     socket as untrusted input and none of its guarantees depend on the
     client being `gh`. The parser is stdlib-only (ADR-0010) and fail-closed:
     everything below refuses with a 4xx-class answer carrying a stable
     reason code, writes a `refused` intent record (item 8) for every
     request line seen — in whichever tagged target form the input admits:
     the full form, the digest form, or the invalid form that names the
     failed stage without inventing a target (amended 2026-09-03, #117,
     sixth review) — and not at all under the audit-budget-exhausted or
     audit-unavailable state — and counts against the request budget. A
     request line is *seen* when its line end arrives within the
     request-line limit; whether it then validates is a separate question,
     and a connection whose bytes reach the limit before a line end has no
     seen request line — it is refused, closed, and counted in
     `no_request`.
     - *Connections*: one request per connection. The relay answers every
       request with `Connection: close`, closes after the response, and
       never reads a second request on a connection — bytes after the
       declared end of the body are discarded, never parsed, so pipelining
       and request smuggling have no second request to reach. At most 8
       connections may be open per Run (4 in flight under the concurrency
       budget, 4 waiting); a further connection is accepted and closed with
       the busy refusal without reading it. Inbound reads are bounded in
       time: 10 s to receive the complete request head, 30 s to receive the
       complete body, and no idle allowance — a connection that sends
       nothing for 10 s, or sends the head and stops, is closed. Connection-
       level rejections (the cap, a timeout before a request line, a first
       line reaching the request-line limit before its end) write no
       per-event record — they are counted in the connection counters below,
       which are bounded by construction.
     - *Connection budget (amended 2026-09-03, #117, fourth review)*: every
       accepted connection is charged one unit, under the relay's lock and
       before any byte is read from it, against the finite per-Run
       connection budget of item 6 (4,000 by default). An empty connection
       the peer closes at once, a partial request line, a head or body read
       timeout, a connection closed with the busy refusal above the open
       cap, and a connection that carries a request all cost exactly one
       unit, and nothing is refunded. The accept that spends the last unit
       is served; before it would accept again the relay closes the
       listening socket and unlinks the socket path, so every later connect
       fails in the kernel — an attempt after the unlink finds no socket,
       and a connection still queued in the listen backlog is reset when
       the listener closes — at no cost to the relay: there is no
       accept-and-refuse loop after exhaustion, bounded or otherwise. The
       relay then runs the one shutdown sequence of item 10 (amended
       2026-09-03, #117, fifth review): the connections already accepted
       (at most eight) finish under their own timeouts, their completion
       records are written where the audit state permits, the single
       terminal record is written with reason `connection-budget-exhausted`
       into the room reserved at sink creation — unless the relay is
       audit-unavailable, in which case no terminal record is written and
       the listener still closes — any spool is deleted, the exit report of
       item 8 names `connection-budget-exhausted`, and the relay exits with
       status zero; the driver records the termination reason `exhausted`
       from that report, never from the terminal record and never as
       `crashed`, and the Run continues on its prompt as after any relay
       exit. Whether the exit was healthy is read from three separate
       facts of the summary — the termination reason, the terminal
       record's state, and the audit-failure report — never from the
       termination reason alone. The counters are `accepted` (bounded by
       the connection budget), `busy_refused`, `no_request` (no request
       line seen: closed by the peer or timed out before a complete request
       line, or the request-line limit reached before a line end — the
       empty, partial, and over-long cases; amended 2026-09-03, #117,
       sixth review), `requests_seen` (request lines seen, bounded by
       `accepted`, since a request line parsed after the request budget is
       spent is still seen and still answered), and `requests_charged`
       (request lines charged against the 2,000-request budget, bounded by
       that budget — `min(requests_seen, 2000)` while the accounting is
       intact, and the terminal record states both so the summary can
       check it), plus the `connection_budget_exhausted`,
       `request_budget_exhausted`, and `audit_budget_exhausted` flags; each
       is an exact integer bounded by the connection budget, so no counter
       can saturate or wrap, and all of them live in the terminal record
       alone (item 8) — the summary copies them from a present, well-formed
       terminal record and reports them unknown otherwise. The request
       budget is unchanged: a request line parsed after it is spent still
       receives the stable refusal, and the connection budget bounds how
       many such refusals exist. The eight-open, four-in-flight, and
       one-request-per-connection rules stand as stated.
     - *Request line*: a seen request line is split at its first two `SP`
       bytes into a method token, a request-target, and a version — a part
       the line does not delimit is empty — and validated in a fixed order,
       the first failure naming the refusal's stage (amended 2026-09-03,
       #117, sixth review): `HTTP/1.1` exactly; any other version refuses
       (`505`). The request-target must be origin-form — a path beginning
       with `/`, optionally followed by `?` and a query; absolute-form,
       authority-form, and asterisk-form refuse, as does any fragment. The
       method token is `GET`, `HEAD`, or `POST` (the last only with the
       canonical path `/graphql`); `CONNECT`, `OPTIONS`, `TRACE`, every other
       token — a 5,000-byte token in a line under the limit included — and
       any upgrade attempt refuse. The request line is at most 8 KiB, the
       path at most 4 KiB, the query at most 4 KiB. These are admission
       limits on the parser, not a promise that every admitted target is
       recordable (amended 2026-09-03, #117, fifth review): item 8
       serializes the intent record for an admitted target and refuses the
       request as `audit-unrepresentable` when that record would not fit
       the record cap, so a target can pass every host, method, path,
       query, and denylist check here and still be refused there. The
       limits are not narrowed to close that gap — they bound the ingress,
       the record cap bounds the sink, and the pinned `gh` approaches
       neither. Validation here gates the authorization record, not the
       refusal record (sixth review): a request must pass every check
       before item 8 serializes the full-form record that can authorize
       it, while a request that fails any check is recorded in the form
       its input admits — the full or digest form once the target
       validated, the invalid form when it did not — under the reason of
       the check that refused it, so the record never claims a target the
       parser did not validate.
     - *Headers*: at most 64 fields and 16 KiB in total, no field over 8
       KiB; names are RFC 9110 tokens matched case-insensitively, values
       printable ASCII with tab, and any CR, LF, NUL, control byte, or
       non-ASCII byte anywhere in the head refuses. Obsolete line folding
       refuses. Framing is exact: a body is admitted only with a single
       `Content-Length` (decimal digits only, no sign, no duplicate or
       comma-joined value, at most the request-body budget) or a single
       `Transfer-Encoding: chunked` (the bare token, no other coding, no
       chunk extensions, no trailers, sizes as bare hexadecimal, the decoded
       total under the same budget); both headers present, either header
       repeated or conflicting, any other transfer coding, `Expect`,
       `Upgrade`, `Connection` naming an upgrade, `TE`, or `Trailer`
       refuses. A body on `GET` or `HEAD` refuses. A declared body that does
       not arrive in full within the read limit refuses.
     - *Path canonicalization, before classification*: the raw path is
       percent-decoded once and normalized before item 4's denylist, item
       5's method policy, and the `/graphql` match see it, and the
       canonical form is what item 8 records. Fail closed on every ambiguous
       spelling: a percent-decoded byte that is `/`, `\`, `%`, `?`, `#`,
       NUL, a control byte, or a non-ASCII byte; a literal backslash; an
       empty segment (`//`) or trailing slash; a `.` or `..` segment; a
       malformed or partial escape; a second layer of encoding; any
       character outside the unreserved set, `/`, and the sub-delimiters
       GitHub's API paths use. Nothing is resolved or collapsed — a path
       needing normalization refuses rather than being fixed. The path's
       rules apply to the path alone: the query has its own grammar below.
     - *Query canonicalization, separate from the path (amended 2026-09-03,
       #117, fourth review)*: the request-target is split at its first `?`
       before any decoding — everything before it is the path under the
       rules above, everything after it is the query, and the query is never
       re-split after decoding. The query is a sequence of `name=value`
       pairs separated by `&`, kept in wire order with every duplicate name
       preserved (`state=open&state=closed` stays two pairs in that order);
       a pair is split at its first `=` (later `=` bytes are value data), a
       pair with no `=` is a bare name, and an empty pair (a leading,
       trailing, or doubled `&`), an empty name, a bare `?` with nothing
       after it, or more than 32 pairs refuses. Raw query bytes are limited
       to the RFC 3986 query character set — unreserved, sub-delimiters,
       `:`, `@`, `/`, `?`, and `%` — so a raw space, quote, `<`, `>`,
       backslash, `^`, backquote, brace, pipe, control byte, or non-ASCII
       byte refuses before decoding. Each name and value is then decoded
       exactly once with the query-component semantics GitHub's API applies
       to its query strings: `%XX` with two hexadecimal digits yields that
       byte; `+` yields a space — the form convention the pinned `gh` uses
       when it encodes a search, so `q=repo:OWNER/REPO is:open` travels as
       `q=repo%3AOWNER%2FREPO+is%3Aopen` and `%20` spells the same space; a
       `%` not followed by two hexadecimal digits (`%`, `%2`, `%G1`, a `%`
       ending the value) refuses as a malformed escape; and a decoded NUL or
       other control byte (0x00–0x1F, 0x7F) refuses. Every other decoded
       byte is data, the reserved characters included: `:`, `/`, `@`, `?`,
       `+`, `=`, `&`, `#`, and `%` decoded from an escape carry no syntax in
       the decoded value — `sha=feature/branch` and `sha=feature%2Fbranch`
       decode to the same bytes, and `%2541` decodes to the literal data
       `%41` and is never decoded again, so an intentionally double-encoded
       input reaches GitHub as the literal percent data the client sent.
       Non-ASCII data travels only as escapes (`q=caf%C3%A9`); the decoded
       bytes are not validated as UTF-8, and GitHub receives what the client
       encoded. The decoded pairs are re-encoded canonically for the
       upstream request: every name and value byte outside the unreserved
       set (`ALPHA`, `DIGIT`, `-`, `.`, `_`, `~`) becomes `%XX` in uppercase
       hexadecimal — space `%20`, `+` `%2B`, `:` `%3A`, `/` `%2F` — and the
       pairs are rejoined with `=` and `&` in wire order, a bare name staying
       bare. The canonical query contains no bare `+` and no bare reserved
       byte, so it reads identically under RFC 3986 and form decoding, and
       GitHub decodes it to exactly the bytes the relay validated. Decoded
       query names and values take no part in policy: item 4's REST
       classification and admin-read denylist, item 5's method policy, and
       the `/graphql` match see the canonical path alone, and no query is
       ever consulted to route, allow, or refuse a request — a denylisted
       path is refused whatever its query, and a permitted path is permitted
       with any well-formed query. Item 8 records a query as its canonical
       names in wire order with each decoded value's byte length and digest
       (literal values only for the enumerated routing parameters) — the one
       representation an intent record and a redirect-intent record share.
     - *Upstream reconstruction*: the upstream request is built from the
       validated method, the canonical path, the re-encoded query, the
       allowlisted headers below, the injected `Authorization`, and the
       buffered body of exactly the declared length; no raw client byte is
       ever copied onto the upstream connection. Framing is the relay's:
       `Content-Length` recomputed from the buffered body, `Host` set to
       `api.github.com`, `Accept-Encoding: identity`, and a fixed
       `User-Agent` naming the relay and its version.
     - *Client-header allowlist*: forwarded, each at most once and at most
       1 KiB: `Accept`, `Content-Type` (on `POST` only, and only
       `application/json` with an optional charset), `X-GitHub-Api-Version`,
       `GraphQL-Features`, `If-None-Match`, `If-Modified-Since`. Every other
       header is stripped and, where the upstream needs one, rebuilt by the
       relay: `Host`, `Authorization`, the sentinel and any other credential
       header, `Proxy-Authorization` and every `Proxy-*`, `Connection` and
       every header it names, `Keep-Alive`, `Transfer-Encoding`,
       `Content-Length`, `TE`, `Trailer`, `Upgrade`, `Expect`, `Forwarded`,
       every `X-Forwarded-*`, `Via`, `Cookie`, `Accept-Encoding`,
       `User-Agent`, `Origin`, `Referer`, `Range`, and any header not in the
       allowlist.
     - *Response-header allowlist*: delivered after the gate, from the final
       upstream response only: `Content-Type`, `Date`, `ETag`,
       `Last-Modified`, `Cache-Control`, `Vary`, `Link`, `Retry-After`,
       every `X-RateLimit-*`, `X-GitHub-Request-Id`, `X-GitHub-Media-Type`,
       `X-GitHub-Api-Version-Selected`. Framing is recomputed after
       buffering — `Content-Length` from the delivered body (omitted on
       `HEAD`), `Connection: close`, never chunked. Everything else is
       stripped: `Location` (redirects are decided, never delivered),
       `Set-Cookie`, `Transfer-Encoding`, `Content-Encoding`, `Connection`,
       `Keep-Alive`, `Server`, `Alt-Svc`, `Strict-Transport-Security`,
       `Content-Security-Policy`, `X-OAuth-Scopes` and
       `X-Accepted-OAuth-Scopes` (they describe the credential), and any
       header not in the allowlist.
     - *Content encoding*: the relay requests identity and refuses any
       upstream response carrying a non-identity `Content-Encoding` (`gzip`,
       `deflate`, `br`, or anything else) with the 502-class refusal; it
       never decodes. The gate therefore counts exactly the bytes the agent
       would receive, and the 64 MiB worst case of item 6 is a bound on real
       bytes, not on compressed ones.
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
   replace, link, or name may be its source of truth, and no
   **agent-originated** relay request may reach the credential without a
   record written before it (scoped 2026-09-03, #117: the driver's own
   boot preflight is recorded by item 3's preflight record, outside any
   per-Run sink — the invariant guards the requests whose author the driver
   does not control).
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
     record, up to three redirect-intent records, and the completion record
     (the terminal record's capacity is reserved once per Run when the sink
     is created; fourth review). Then it serializes the **intent record**
     in full and measures it (amended 2026-09-03, #117, fifth review): a
     serialization within the record cap of the record shape below is
     appended to the sink with a single write followed by `fdatasync`, and
     only that durable write authorizes credential use against the
     request's original target — the upstream connection is opened and the
     credential attached only after it returns, and if it fails no
     connection is attempted and no credential is touched; the client
     receives the `audit-unavailable` refusal. A serialization that would
     exceed the cap authorizes nothing: the relay refuses the request
     before any upstream I/O with the reason `audit-unrepresentable` and
     writes, in place of the full record, the intent record in the
     bounded **digest form** of the target representation below — the same
     sequence, kind, timestamp, method classification, and decision, with
     the target identified by the byte length and sha256 of its canonical
     path and canonical query and the GraphQL metadata by its counts and
     digests — which fits the cap by construction. No record is ever
     truncated and no marker ever stands in for one: a record is written
     whole in one of the tagged forms, or not at all. Every other refusal
     of a seen request line likewise writes one intent record carrying
     `decision: refused` and the reason of the check that refused it —
     never `audit-unrepresentable`, which names only the case where size
     alone stopped an otherwise authorizable request (amended 2026-09-03,
     #117, sixth review) — and never proceeds: a request whose target
     validated and was then refused by classification, the denylist, the
     method policy, a malformed header, framing, or body, an unclassifiable
     GraphQL document, or any budget but the audit budget is recorded in
     the full form when that fits and the digest form otherwise; a request
     line the parser rejected before its target validated — an unknown or
     oversized method token, a non-origin-form target, a malformed path or
     query — is recorded in the **invalid form**, which names the failed
     stage and identifies the input by length and digest and can carry no
     canonical target because none exists. A refusal for the audit budget
     itself writes nothing, below. When a REST
     `GET`/`HEAD` answer is a redirect that item 5 decides to follow, the
     hop is authorized the same way (fourth review): the relay serializes
     a **redirect-intent record** — correlated to the intent by sequence
     number, carrying the hop number (1 to 3), the canonical target path,
     and the redacted query representation of the record shape below,
     never a `Location` byte literally — measures it, and, when it fits
     the cap, appends and `fdatasync`s it and attaches the credential to
     that hop only after the write returns; if the append fails the hop is
     not sent, no credential touches it, and the relay enters
     audit-unavailable under the rules below. A redirect-intent record that
     would exceed the cap is not written, and the hop is refused with the
     reason `audit-unrepresentable` (fifth review): the request ends there,
     and the refusal is recorded where every refused hop is recorded — as
     that hop's entry in the completion record's summary, correlated by
     sequence and hop number — so a hop refused by item 5's policy and a
     hop refused as unrepresentable are recorded alike, and a
     redirect-intent record exists exactly for the hops whose credential
     use was authorized. That is the whole of what an intent or
     redirect-intent record proves (amended 2026-09-03, #117, sixth
     review): the relay committed to attaching the credential to that
     target, never that the request was sent — a crash after the record's
     `fdatasync` and before the send leaves a target that may or may not
     have been contacted, which is exactly the `indeterminate` outcome of
     the publication rules below, and no record, message, or summary field
     claims that a hop definitely occurred. After the upstream operation
     finishes — delivered, refused
     at the response gate, redirected and refused, timed out, or aborted —
     the relay appends a **completion record** correlated to the intent by
     sequence number, carrying the outcome, the upstream status, the
     actual request and response byte counts, and the request's redirect
     decisions of item 5 as one bounded `redirects` array of at most four
     entries — one per redirect answered, followed or refused; three
     followed hops and then a fourth redirect answer refused at the hop
     limit is the longest case (fifth review); empty when none. The
     completion record copies no routing metadata — no path, query, or
     GraphQL field — because the intent and redirect-intent records
     already carry it, and a refused hop's `Location` enters it only as
     the bounded scheme classification and host representation of the
     record shape below, never as text (sixth review), so its size is
     bounded by construction and it is never refused for size, whatever
     the upstream put in `Location`. The array is a triage summary; the
     redirect-intent records are the authorization, and a crash at any
     point leaves the last target whose credential use was authorized
     identifiable from the sink alone — the intent's path when no
     redirect-intent record follows it, otherwise the path of the highest
     hop number. A request therefore costs two to five records — one
     intent, zero to three redirect-intents, one completion — or one
     record when refused, and the Run at most one terminal record. If the
     completion append fails, the intent and redirect-intent records
     already record what was authorized; the relay enters the
     audit-unavailable state and forwards nothing further. Capacity is
     reserved up front so the relay can never send a hop or finish an
     upstream call and then lack room for its record: under the relay's
     lock and before its intent is written, a request being refused
     reserves one record's room at the 4 KiB cap and a request being
     authorized reserves five — one intent, three redirect-intent, one
     completion; the difference between the reservation and the bytes
     actually written is released only once the request's last record —
     the refused intent, or the completion — has been written, never
     earlier, since a released reservation is claimable by a concurrent
     request and a hop must never find its reserved room gone; and the
     terminal record's capacity, reserved at sink creation, is never
     released. When a reservation would cross the 16 MiB file cap the
     relay enters the **audit-budget-exhausted** state (amended
     2026-09-03, #117, fifth review): it authorizes no further upstream
     request and writes no further per-request record — every later
     request receives item 6's stable budget refusal without any record —
     while every request already holding its reservation finishes
     normally, hops and completion included, its room having been reserved
     before the cap was reached; acceptance continues within the
     connection budget, and the exhaustion is noted for the terminal
     record, which is written only at shutdown, in its reserved room,
     never at the moment of exhaustion. The `audit-unrepresentable`
     refusal and the audit-budget-exhausted state are refusals of the
     sink's own limits: neither weakens the host, method, path, query, or
     denylist checks that precede them, and a request must pass all of
     those before its authorization record is ever serialized — its
     refusal record, by contrast, is serialized for any seen request line,
     in the form the input admits (sixth review).
   - *Record shape — one explicit schema per kind, redacted by construction
     (amended 2026-09-03, #117, fifth review)*: every record is one line of
     ASCII-escaped JSON, so invalid UTF-8 and control bytes cannot reach
     the file unescaped, and every field named mandatory below is present
     in every record of its kind — none may be dropped, shortened, or
     replaced by a marker to fit the cap. Common to every record: the
     record kind (`intent`, `redirect-intent`, `completion`, `terminal`)
     and a timestamp. Common to every per-request record: the request's
     sequence number, assigned to every seen request line (item 6),
     1-based and dense, so the highest sequence issued equals
     `requests_seen` and a
     sequence with no intent record is a request refused without a record
     under the audit-budget-exhausted or audit-unavailable state.
     - **Target representation**, tagged by `form` and total over every
       seen request line (amended 2026-09-03, #117, sixth review): the
       method is always a **closed classification** — `GET`, `HEAD`,
       `POST`, `CONNECT`, `OPTIONS`, `TRACE`, `PUT`, `PATCH`, `DELETE`, or
       `other`, the last carrying the token's byte length and sha256 in
       place of any literal, so no record holds an unbounded method
       string; a validated target's method is necessarily one of the first
       three. The three forms:
       - the **full form** (`form: full`), the only form that can
         authorize: the method; the canonical path; and the query reduced
         to its canonical (re-encoded) parameter names in wire order plus
         each decoded value's byte length and sha256 — literal values only
         for the enumerated routing parameters `page`, `per_page`,
         `state`, `sort`, `direction`, and only when printable ASCII under
         256 bytes, omitted otherwise while the length and digest stay.
         For `POST /graphql` the GraphQL metadata is tagged `parsed: true`
         with the operation type; the operation name when at most 128
         bytes, otherwise its byte length and sha256 in its place; and the
         variables as name, JSON type, byte length, and sha256 of the
         canonical encoding — literal values only for `owner`, `name`,
         `repo`, `number`, `first`, `last`, `states`, under the same
         printable cap — or tagged `parsed: false` with nothing more when
         the body was not JSON, not a classifiable single-operation
         document, or never read because an earlier check refused the
         request; for any other request the GraphQL metadata is `null`.
         Every optional literal is therefore either present whole under a
         fixed cap or absent, never cut. The full form is measured before
         it is written and has no static bound.
       - the **digest form** (`form: digest`), for a validated target
         whose full form would not fit the cap: the method; the byte
         length and sha256 of the canonical path; the pair count, byte
         length, and sha256 of the canonical query; and the GraphQL
         metadata as `parsed: true` with the operation type, the variable
         count, and the sha256 of the canonical variables encoding, or
         `parsed: false`, or `null`, under the rule above. A fixed set of
         fixed-size fields.
       - the **invalid form** (`form: invalid`), for a seen request line
         that never yielded a validated target: the `stage` that refused
         it, from the closed set `request-line`, `version`, `target-form`,
         `method`, `path`, `query`, in the order item 6 validates;
         the method classification; the byte length and sha256 of the raw
         request-target as item 6 delimits it; and GraphQL metadata
         `null`. Nothing is repaired, canonicalized, or copied: no
         canonical path is invented for a path that failed, no raw target
         byte is recorded, and the digest identifies the input without
         reproducing it.
       Only the full form authorizes; the digest and invalid forms never
       authorize anything, whatever decision field accompanies them.
     - **`intent`** (mandatory: sequence, kind, timestamp, decision, the
       reason code on refusal, target): `decision` is `authorized` or
       `refused`, the reason a code from a closed enum, never client text;
       an `authorized` record carries the target in the full form only,
       plus the budgets reserved. The full form is serialized and measured
       before it authorizes anything; when it would exceed the record cap
       the record is written in the digest form with decision `refused`
       and reason `audit-unrepresentable`, the reason reserved for that
       case alone. A `refused` record carries the reason of the check
       that refused it and the target in the form the input admits —
       full when that fits, digest when it does not, invalid when the
       target never validated (sixth review) — so a malformed request
       keeps its malformed-input reason and a policy refusal keeps its
       policy reason, and neither ever acquires a target it did not have.
     - **`redirect-intent`** (mandatory: sequence, kind, timestamp, hop
       number 1 to 3, decision `authorized`, target): the hop's target in
       the full form only. There is no refusal form for a hop: a hop whose
       record would not fit is refused, every refused hop is recorded in
       the completion record's summary, and a redirect-intent record
       exists exactly for the hops whose credential use was authorized —
       proof of the authorization, never of the send (sixth review).
     - **`completion`** (mandatory: sequence, kind, timestamp, outcome,
       upstream status or `null`, request bytes, response bytes,
       `redirects`): the outcome from a closed enum (`delivered`,
       `refused-gate`, `refused-redirect`, `timeout`, `upstream-error`,
       `aborted`), the final upstream status, the actual byte counts, and
       the `redirects` array of at most four entries — each the hop
       number, the upstream status, the decision (`followed` or
       `refused`), the reason code, and the resolved `Location`'s scheme
       and host in the bounded representation of the sixth review: the
       **scheme** as a closed classification — `https`, `http`, `other`
       (any other syntactically valid scheme, however long), `invalid`
       (a `Location` that is not a single parseable URI reference, or
       whose scheme part is not a scheme token), `absent` (no `Location`
       header) — and the **host** as a validity status with a bounded
       payload — `valid` (a reg-name or IP literal in the RFC 3986 host
       character set of at most 253 bytes, recorded literally),
       `oversized` (in that character set but longer, recorded as byte
       length and sha256), `invalid` (anything else — a host outside that
       character set, an authority carrying user-info or percent-encoding,
       an empty host — recorded as the byte length and sha256 of the
       authority as the relay delimited it), `absent` (no authority) —
       never a port, user-info, path, query, or fragment, and never text
       the upstream chose. No target, query, or GraphQL field is copied
       here. The schema is closed and every field bounded, so a completion
       record fits the cap by construction and is never refused for size,
       whatever the upstream put in `Location`; the implementation asserts
       the bound with a four-entry fixture at every field's maximal width.
     - **`terminal`** (mandatory: kind, timestamp, reason, the exhaustion
       flags, the counters): the reason acceptance ended (`agent-exit` or
       `connection-budget-exhausted`); the `connection_budget_exhausted`,
       `request_budget_exhausted`, and `audit_budget_exhausted` flags; and
       item 6's counters `accepted`, `busy_refused`, `no_request`,
       `requests_seen`, and `requests_charged`. Written at most once per
       Run, only at shutdown, and only when the relay is not
       audit-unavailable (below). Fixed fields, bounded by construction.
     Never recorded, in any kind: credentials, the sentinel,
     `Authorization` or any other header, request bodies, response bodies,
     upstream error text, refusal message text, repository content, a
     `Location` path, query, port, or user-info, a raw request-target, a
     method token, or any bytes copied from the client or the upstream
     beyond the fields above. A record is at most 4 KiB, enforced before
     the write and never by cutting — a full-form intent that would exceed
     it becomes a digest-form refusal, a redirect-intent that would exceed
     it refuses its hop, and the digest and invalid intent forms and the
     completion and terminal kinds cannot exceed it — and the file at most
     16 MiB per Run under the reservation rules above, so a refusal can
     never grow the log past its cap.
     - *Serialized bound of the fixed forms (amended 2026-09-03, #117,
       sixth review)*: every field of the digest form, the invalid form,
       the completion, and the terminal belongs to one of five classes
       with a fixed maximal width in ASCII-escaped JSON — a key or a
       closed-enum value of at most 32 bytes; a sha256 as exactly 64
       lowercase hexadecimal bytes; the timestamp as RFC 3339 UTC at
       microsecond precision, exactly 27 bytes; an integer of at most 20
       digits (every counter and length is bounded far below that); and a
       host literal of at most 253 bytes from the RFC 3986 host character
       set. None of these contains a byte JSON must escape, so escaping
       adds nothing to a fixed form, and the punctuation is fixed by the
       schema — two quotes, a colon, and a comma per field, braces and
       brackets per object and array. With every field at its widest, the
       longest fixed form is a four-entry completion whose four hosts are
       253-byte literals: under 3,400 bytes, more than 700 bytes under the
       cap; the digest-host completion is under 2,900 bytes and the
       invalid-form, digest-form, and terminal shapes each under 1,400.
       The implementation asserts each of those maximal serializations
       under the cap as a fixture, and any schema change that adds a field
       re-derives the bound.
   - *Audit unavailable*: the state a failed audit write (error, disk full,
     closed descriptor, short write, a failed `fdatasync`) puts the relay
     in. The process stays up and keeps answering the socket; it forwards
     nothing, attaches the credential to nothing, and returns the fixed
     `audit-unavailable` refusal without writing again — no completion
     record for a request in flight, no redirect-intent, and no terminal
     record at shutdown either: the state permits no further sink write of
     any kind (amended 2026-09-03, #117, fourth review), and it takes
     precedence over every other shutdown rule (fifth review): a
     connection-budget exhaustion or an agent exit that follows still ends
     acceptance, closes the listener, unlinks the socket, and runs the
     shutdown sequence of item 10, but writes no terminal record and
     touches no credential. The failure is reported to the driver outside
     the sink — a structured `theozolith.error` naming the record kind
     that failed, the request sequence number, and the hop number where
     one applies (never a path, query, or byte of the record) — and the
     driver holds that report as the audit-failure accounting the
     published summary relies on. The relay's exit status is not the audit
     signal: the relay exits with status zero whenever it completed the
     shutdown sequence with whatever writes the state permitted, and the
     exit report below carries the audit state, so the driver never reads
     a healthy audit into a zero status or a crash into a non-zero one.
     The Run's evidence records `gh_audit: failed`, and the Run continues
     on the task in its prompt.
   - *Publication*: after the agent phase ends and the relay has exited, the
     driver fsyncs the sink, writes `gh-audit.summary.json` beside it —
     record count, byte count, sha256, request counts by outcome (`complete`:
     intent with its completion; `refused`: refused before any upstream I/O,
     the `audit-unrepresentable` refusal included; `incomplete`: intent
     without a completion in a Run for which the driver holds the relay's
     audit-failure report — the relay was alive and, by the
     audit-unavailable rules, could write nothing more, so every intent the
     report leaves uncompleted is `incomplete`, whether the credential had
     been sent or not; `indeterminate`: intent without a completion and no
     audit-failure report — the relay died between intent and completion,
     so whether the upstream connection was opened is unknown; a crash
     after a redirect-intent's `fdatasync` and before its send is the
     same case, the hop's authorization recorded and its contact unknown,
     sixth review), aggregate
     request and response bytes, redirect-intent records written, redirects
     followed and refused, `records_parsed` by kind — the driver's own
     count of what it parsed, labeled as such and never presented as the
     relay's counters — the terminal record's counters and flags (item 6),
     copied only from a `present` terminal record and reported unknown
     (`null`) whenever the terminal record is `missing` or `malformed`,
     never as zero and never reconstructed from the parsed records
     (amended 2026-09-03, #117, fifth review), budgets hit, the
     driver-observed termination reason (`clean`, `exhausted`, `killed`,
     `crashed`, `orphaned`), and the audit-failure report itself (`null`,
     or the record kind, sequence number, and hop the relay named) — and
     publishes both into the evidence bundle from the sink. The
     termination reason is read from the relay's **exit report** (fifth
     review) — one structured line the relay sends the driver over the
     same channel as the audit-failure report, last before it exits,
     naming what ended acceptance (`agent-exit` or
     `connection-budget-exhausted`) and the audit state (`ok`,
     `budget-exhausted`, or `unavailable`) — together with the exit status
     and the driver's own observation: `clean` is an exit report naming
     `agent-exit` with status zero after the driver ended the agent phase;
     `exhausted` is an exit report naming `connection-budget-exhausted`
     with status zero, whether or not the agent phase had ended; `killed`
     is the bounded wait elapsing; `crashed` is every other exit — a
     non-zero status, no exit report, or a report the driver's observation
     contradicts; `orphaned` is the boot sweep. The exit report carries no
     counters — those live in the terminal record alone, so one count has
     one source — and it never substitutes for the terminal record, which
     is the sink's own evidence. An `indeterminate` request is therefore
     always distinguishable from one that never passed authorization: the
     latter has a `refused` intent or no record at all. The summary detects
     collection errors (a truncated copy, a missed publish); it is not a
     tamper seal, since nothing untrusted ever reaches the sink.
   - *Short writes and malformed records (amended 2026-09-03, #117)*: a
     short write is an audit failure like any other — the relay enters
     audit-unavailable and writes nothing further, so the sink can end in a
     partial line but never contains a partial line followed by a complete
     one. Publication never depends on the sink parsing: the driver copies
     the sink byte-for-byte and digests the bytes, then parses it line by
     line for the summary, which reports `records_parsed`, the byte offset
     and length of any unparseable tail (a line without its newline, or a
     line that is not a complete JSON object of a known kind), and the
     terminal record's state — `present`, `missing`, or `malformed` — from
     the bytes alone, separately from the driver-observed termination
     reason and separately from the audit-failure report (fifth review),
     so every combination is diagnosable from the summary: a clean exit
     with no terminal record, a crash with a torn one, and the three
     shapes a failed terminal write leaves — no bytes (`missing`, with the
     report naming kind `terminal`), a torn record (`malformed`, same
     report), or a complete record whose `fdatasync` failed (`present`,
     same report — the bytes were read back, their durability is unproven,
     and the report says so). A request whose completion record is the
     unparseable tail counts as `incomplete`, never `complete`. Healthy is
     a conjunction, never one field: termination `clean` or `exhausted`,
     terminal `present`, report `null`. A Run that went audit-unavailable
     before shutdown publishes with the terminal record `missing` and the
     audit-failure report present, whatever its termination reason —
     `exhausted` beside a report is an exhausted, unhealthy Run, never a
     healthy one — distinct from a crash (terminal `missing`, no report,
     termination `crashed`). The sink is deleted only once the bundle is
     durably pushed: while a push fails it stays and the existing evidence
     retry carries it; a sink orphaned by a driver crash is published by
     the boot-time evidence sweep as `terminated: orphaned`, its counters
     unknown, and then removed. A `gh` call counter joins progress
     telemetry. Full input reproducibility is knowingly given up: requests
     are the record, and responses remain recoverable from GitHub.
9. **Run Contract surface — `schema_version` 2, the accepted successor that
   the implementation PR activates (amended 2026-09-03, #117, fourth
   review).** The currently implemented Run Contract is `schema_version` 1,
   and every place that states the current value — BENCH-CONTRACT.md's key
   list, ADR-0054 decision 4, and the code constant the drift-lock test pins
   to the spec's wording — says 1 until the implementation PR lands. Version
   2 is reserved for the surface below and is activated by that PR alone,
   which lands atomically, in one change: the runtime constant, the job-dir
   layout (socket present, Context Tree paths gone), the prompt-renderer
   signature (`dependencies`), the relay entry point, the bench behavior
   (`live` and `none`), the current-version wording in BENCH-CONTRACT.md and
   ADR-0054, and the Changelog's activation entry — so the drift-lock test
   passes before the change and after it, and no red state exists for anyone
   to tolerate, weaken, or skip. Under version 2 the job dir gains the socket,
   the prompt renderers take the ordered dependency entries of item 1 (a
   `dependencies` sequence of `DependencyEntry(number, state)` in place of
   the `deps_present` flag — the public signature the bench replays),
   `worker`'s public API exports the relay as an entry point with an injectable
   upstream, and the bench driver runs the identical policy — ingress
   contract included — never a copy. Two
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
   record it produces is a `refused` intent — the ingress refusals under
   their own reasons and in their own tagged forms, exactly as in
   production, and every request that validates under the no-upstream
   reason (amended 2026-09-03, #117, sixth review); no mode ever contacts
   an upstream for a request the ingress refused.
10. **Lifecycle and failure semantics, in order.** Configure (the slot is
    declared; ADR-0047 refuses an unbound or unreadable binding, and item 3
    refuses an unsupported host, at config load) → driver boot: host check,
    token-form check, preflight (item 3; no claim or Review Run before all
    three pass) → per Run, in a job directory and a driver-owned trusted
    directory both keyed by this Run's `run_id`: create the audit sink (item
    8) → bind the socket at the job-dir root — the job directory is freshly
    created for this `run_id`, so an entry already at the socket path is a
    loud pre-work infra-class failure, never unlinked blindly → launch the
    agent → per connection: charge the connection budget at accept, before
    any read (item 6) → per request: strip, classify, reserve budgets and
    audit capacity, append and sync the intent record, open the upstream
    connection with the credential, apply the redirect rule per hop (item
    5) — appending and syncing a redirect-intent record before each followed
    hop's credential use (item 8) — read the whole response through the gate
    (item 6), deliver or refuse, append the completion record (item 8) →
    shutdown, one sequence whatever ends acceptance (amended 2026-09-03,
    #117, fifth review): acceptance ends exactly once, on the first of
    agent exit or connection-budget exhaustion — close the listener and
    unlink the socket — then the connections already accepted are drained:
    on agent exit in-flight upstream requests are aborted (a client
    mid-request sees a closed connection; each aborted request gets a
    completion record with outcome `aborted`), on connection-budget
    exhaustion they finish under their own timeouts while the agent is
    still alive, and an agent exit arriving during that drain aborts what
    remains; every completion record the audit state permits is written
    (a failed append makes the relay audit-unavailable, and the driver's
    audit-failure accounting of item 8 covers every uncompleted intent);
    then, with no accepted work outstanding, the terminal record is written
    exactly once, into its reserved room, naming what ended acceptance and
    carrying the `audit_budget_exhausted` flag when that state was reached
    — or not at all when the relay is audit-unavailable, which no later
    event overrides; any spool is deleted; the exit report is sent; the
    relay exits with status zero. Audit-budget exhaustion is not a shutdown
    trigger: it stops authorizations and per-request records, and the
    relay keeps answering until acceptance ends as above. The driver waits
    a bounded interval for the exit, then kills the relay and records
    `killed` → publish the sink (item 8) → run gates with no socket present
    →
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
    - *Crash points*: a relay exit before agent exit is detected as child
      exit — a zero status with an exit report naming
      `connection-budget-exhausted` is the `exhausted` termination of item
      6, read from the report and never inferred from the terminal record,
      which an audit-unavailable relay cannot have written (fifth review);
      any other early exit or hang is a crash: the driver unlinks the
      socket, deletes the spool, records `crashed`, and does not restart the
      relay (the agent's further calls fail at connect; the Run continues
      on its prompt); a crash after an intent record and before the
      upstream connection, after a redirect-intent record's `fdatasync`
      and before that hop is sent, during the upstream request, between
      redirect hops, or after completion but before the completion record
      all leave the same evidence — an intent, with whatever
      redirect-intent records preceded the crash, and no completion —
      which the summary reports as `indeterminate`; the credential is
      treated as possibly used against every recorded target, none of
      them claimed contacted (sixth review), and the last target whose
      use was authorized is the highest-hop redirect-intent record's path,
      or the intent's when none follows. A
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
    - *Upgrade from `schema_version` 1 (amended 2026-09-03, #117)*: the
      upgrade is deliberate at both ends. The driver writes only the version
      2 layout — it never materializes `input/issue/`, the GitHub-derived
      parts of `input/pr/`, or `input/deps/`, and a version 2 job directory
      found to contain any of them before launch refuses the Run pre-work as
      infra-class (a driver-authored directory that is not what the driver
      writes is a bug or tampering, never something to clean and continue).
      Version 1 job directories and trusted snapshots left on a node from
      before the upgrade are orphans: the boot sweep publishes each under
      the contract its own manifest names — the Context Tree snapshot
      included, since that was that Run's real input — and then removes
      them; nothing re-runs, reads, or migrates them. Mixed versions fail
      closed everywhere they can meet: a manifest stamped 1 is never
      launched by a version 2 driver, an image whose `format-output` CLI
      accepts only version 1 refuses a version 2 manifest at its first
      invocation exactly as today's skew rule says, and the bench refuses a
      job directory whose manifest version and layout disagree. There is no
      compatibility window and no dual-version driver.
11. **Test obligations for the implementation PR.** With the lexer fixture
    corpus and the pinned-`gh` wire shapes of item 4, the implementation
    must land tests for:
    - content trust and driver authority (negative, driver-side) — a
      well-formed `theozolith:verdict` block in a PR-conversation comment
      from a COLLABORATOR, CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, FIRST_TIMER,
      NONE, or association-less author, and in a review comment or review
      body from any author, never becomes the selected verdict, never
      designates the reset or resume commit or cherry-pick set, never moves a
      label, never affects a claim, and never triggers a state transition,
      while an OWNER or MEMBER comment's block still does; the same comment
      is readable through the relay in the same Run (visibility proven
      broad, authority proven narrow, in one test); a Based-on zone planted
      in a comment rather than the PR body is ignored; the driver's
      verdict, base, label, and claim reads are shown to run outside the
      relay with the driver credential; and the prompt's retrieved-content
      rule is present byte-exact in both renderers;
    - dependency prompt schema — golden prompts for no edges (byte-identical
      to the edge-less prompt), one open blocker, a mixed chain of
      `open`, `completed`, and `not_planned` entries in topological order,
      and a chain with the Chained Base note, for both renderers through
      the public `dependencies` signature; the state derivation from
      `state`/`state_reason` including a closed issue with no reason;
    - host and token contract — a GHES host in every canonicalizer spelling
      refused at config load and at driver boot before any preflight, relay,
      socket, container, claim, or Review Run, with the V1 message; no
      classic-token fallback (a `ghp_`, OAuth, App, and unrecognized token
      form each refused by name; the driver PAT never read by the relay under
      any failure); the preflight — each missing capability failing by name
      on a capability-specific 403, a 401 on any probe failing as the token,
      a rate-limit 403 named as such, empty issue, PR, check, status, and
      workflow collections passing, an empty repository (409, empty listing,
      and missing default branch each) refused pre-work as a workspace
      condition and not a capability, a probe commit deleted between the
      listing and the commit-scoped probes re-resolved once and then failing
      by name, the Actions-unavailable case recorded and passing, the boot
      report per capability with `not-probed` shown whenever commit-scoped
      probes did not execute and no "all verified" summary in that case,
      the preflight record written outside any Run sink with no credential,
      header, or response content, expired and unreadable tokens, an
      accidentally broad token passing with no message overclaiming, and no
      credential byte in any message;
    - ingress (hostile client, never `gh`) — a slow-loris head and a
      slow body each closed at the read limit; idle connections closed;
      connections beyond the cap refused unread with the counter moving;
      a pipelined second request on one connection discarded, never
      forwarded; request-line, header-count, header-byte, path, and query
      limits each at exactly-at and one-over; `HTTP/1.0`, `HTTP/2` preface,
      absolute-form, authority-form, asterisk-form, and fragment targets
      refused; `CONNECT`, `OPTIONS`, `TRACE`, `PUT`, `PATCH`, `DELETE`, a
      `POST` off `/graphql`, and a body on `GET` refused; obsolete folding,
      CR/LF/NUL/control/non-ASCII bytes in names and values, duplicate and
      conflicting `Content-Length`, `Content-Length` with
      `Transfer-Encoding`, chunked with extensions or trailers, `gzip` and
      `identity` transfer codings, `Expect`, `Upgrade`, and `TE` each
      refused — the classic smuggling shapes (CL.TE, TE.CL, TE.TE) proven to
      produce exactly one refused request and zero upstream requests; the
      encoded-denylist matrix — `%2F`, `%5C`, `%2e%2e`, `%00`, `%25`,
      double encoding, backslash, `//`, trailing slash, `.` and `..`
      segments, and a non-ASCII byte — each refused before the denylist
      rather than normalized past it, and the canonical spelling of a
      denylisted path refused by the denylist; every stripped client header
      proven absent upstream and every allowlisted one forwarded once; the
      upstream request byte-compared against reconstruction from validated
      parts; every stripped response header proven absent downstream,
      `Location` never delivered, framing recomputed, `Content-Length`
      omitted on `HEAD`; a `gzip`, `br`, `deflate`, and unknown
      `Content-Encoding` response each refused undecoded with no downstream
      byte; a request line at exactly the limit seen and refused with a
      record, one byte over closed with no seen request line and
      `no_request` moved (sixth review);
    - connection budget — a client opening and closing empty connections,
      sending partial request lines, and letting reads time out at a high
      sequential rate, each charged exactly one unit at accept; busy
      refusals above the open cap charged the same; the accept that spends
      the last unit served, the listener closed and the socket unlinked
      before the next accept, every later connect failing in the kernel,
      no accept-and-refuse loop; the connections held at exhaustion
      finishing under their own timeouts with their completion records,
      the single terminal record written after them naming
      `connection-budget-exhausted`, the exit report naming the same, and
      the driver recording `exhausted` from the report, not `crashed`;
      more than 2,000 parsed request lines within 4,000 accepted
      connections — every line past the request budget answered with the
      stable refusal and recorded as a refused intent while the audit
      budget lasts, `requests_seen` and `requests_charged` exact and
      separate, `requests_charged` never above 2,000, and
      `request_budget_exhausted` set — with at most (connection budget −
      request budget) such refusals before acceptance ends; the counters
      exact, bounded by the budget, present in the terminal record, and
      copied rather than recomputed into the summary; and the socket,
      spool, and sink cleanup unchanged on this exit path;
    - query grammar — byte-exact pinned-`gh` fixtures pinning the upstream
      request line the relay sends: `gh search issues` and `gh search prs`
      with `repo:OWNER/REPO` and `is:`, `state:`, `author:`, and `label:`
      qualifiers including quoted multi-word values; `gh api` `GET
      search/issues` with `-f q` carrying spaces, colons, and slashes; a
      ref name carrying `/` as a query parameter in both its raw and
      encoded spelling canonicalizing to the same bytes; repeated
      parameters preserved in order with duplicates; literal percent data
      (`%2541`) forwarded once-decoded as `%2541`; `+` and `%20` both
      yielding `%20`; `Link`-header pagination follow-ups; and the negative
      cases — `%`, `%2`, `%G1`, a trailing `%`, `%00`, a raw space, quote,
      backslash, brace, control byte, or non-ASCII byte, an empty pair, an
      empty name, a bare `?`, and more than 32 pairs — each refused with no
      upstream request; a denylisted path refused with any query, a
      permitted path permitted with any well-formed query, and no query
      value ever changing a classification or denylist decision;
    - write-ahead audit — no credentialed request without a durable intent
      record (an injected intent-write failure, and an injected `fdatasync`
      failure, each prove no upstream connection is opened); no
      credentialed redirect hop without a durable redirect-intent record
      (an injected redirect-intent-write failure on hop one, two, and three
      each proves that hop is never sent, no credential reaches it, and the
      relay freezes audit-unavailable with no completion and no terminal
      record); intent, redirect-intent, and completion records correlating
      exactly by sequence and hop number under concurrent requests whose
      records interleave, the sequence dense over parsed request lines; a
      completion-write failure leaving the intent and freezing the relay in
      audit-unavailable while the process keeps refusing; the reservation
      that guarantees room for three redirect-intents and the completion at
      the file cap, unused capacity released only after the request's last
      record and never claimable by a concurrent request before it; crashes
      injected after intent, during upstream, before and after each of
      three redirect hops, and after completion each leaving an
      `indeterminate` request whose last authorized target is identifiable
      from the sink alone; a crash injected after a redirect-intent's
      `fdatasync` and before its send leaving the hop's authorization
      recorded, its contact unknown, the request `indeterminate`, and no
      summary field, message, or count claiming the hop occurred (sixth
      review); an audit-unavailable Run publishing with the
      terminal record `missing`, the audit-failure report present, and every
      uncompleted intent counted `incomplete`, never `indeterminate`; a
      short write and a torn final line each freezing the relay and
      publishing with the unparseable tail's offset reported and the
      affected request counted `incomplete`; the redirect accounting —
      zero, one, three, and four entries in one completion record beside
      the matching redirect-intent records, three followed hops and then a
      fourth redirect answer refused at the hop limit producing four
      entries, exactly three redirect-intent records, and no fourth
      redirected upstream request, and a four-entry completion record with
      a 256-byte host in every entry under the cap by construction; no
      `Location` path or query byte in any record; the preflight writing no
      per-Run record; audit-path tampering — attempted unlink,
      replacement, truncation, symlink substitution, a forged pre-existing
      log at the sink path, concurrent writers, and a crash during final
      publication — each leaving the published record intact or the failure
      visible;
    - audit representability — a 4,096-byte path at the admission limit,
      the maximum query metadata (32 pairs at their length limits), and
      oversized GraphQL metadata (a variable set whose names, types,
      lengths, and digests exceed the cap) each admitted by the parser and
      refused as `audit-unrepresentable` with the refusal-form intent
      record present, no upstream connection, and no credential use; a
      full-form record serialized to exactly the cap written whole and one
      byte over refused in the digest form with every mandatory field
      retained and no credential use after any digest-form or
      invalid-form record; a followed redirect whose redirect-intent record
      would exceed the cap refused at that hop with no redirect-intent
      record, no credential on the hop, and the completion's entry
      carrying `audit-unrepresentable`; every mandatory field present in
      every record of every kind across the whole fixture corpus, with no
      marker or shortened field anywhere in the sink; and the completion
      and terminal kinds shown under the cap at their maximal shape;
    - refusal totality (sixth review) — `GET /%G1`, a malformed query
      escape, an absolute-form, authority-form, and asterisk-form target,
      a fragment, an `HTTP/1.0` version, a 5,000-byte method token in a
      request line under 8 KiB, and an empty request line each refused
      with an invalid-form intent record naming the stage that refused
      it, the method classification (`other` with byte length 5,000 and
      the digest for the long token), the raw request-target's byte
      length and sha256, no canonical path, no raw byte, and the
      malformed-input reason — never `audit-unrepresentable` — with no
      upstream connection; a `POST /graphql` with invalid JSON, a
      multi-operation document, and an unclassifiable document each
      refused with a full-form record carrying the canonical target and
      GraphQL metadata `parsed: false` under its own reason; a validated
      target refused by the denylist, the method policy, or a header,
      framing, or body check whose full form would not fit recorded in
      the digest form under the original reason; every mandatory field
      present in every refusal record; the same fixture corpus, ingress
      behavior, and records produced by the production driver and the
      bench driver in `live` and `none` modes, `none` contacting no
      upstream for any input; and the shutdown, audit-failure,
      reservation, retention, and recovery fixtures above passing
      unchanged under the tagged forms, which change what a refusal
      record contains and never when a record is written or what any
      other record proves;
    - bounded redirect metadata (sixth review) — a refused `Location`
      with a 5,000-byte scheme recorded as scheme `other` or `invalid`
      and nothing longer; a missing `Location`, a duplicated one, an
      unparseable one, and a scheme-relative one each recorded under the
      matching classification; a host of 253 bytes recorded literally, of
      254 bytes as `oversized` with length and digest, and hosts carrying
      user-info, percent-encoding, a control or non-ASCII byte, a `"`, or
      a `\` each recorded as `invalid` with length and digest and no
      escaped byte in the record; every entry's port, user-info, path,
      query, and fragment absent; a four-entry completion at every
      field's maximal width — four 253-byte literal hosts, the widest
      enum values, 20-digit integers — serialized under the cap with its
      byte count asserted, and the digest-host, invalid-form,
      digest-form, and terminal maximal shapes likewise; and every
      mandatory completion field present, with no whole-record truncation
      and no missing completion for any hostile `Location`;
    - one terminal, one shutdown — audit-budget exhaustion with concurrent
      requests holding reservations, each finishing with its hops and
      completion written and every later request refused with the stable
      message and no record, followed by connection exhaustion and the
      ordinary drain, with exactly one terminal record written last,
      naming `connection-budget-exhausted` with `audit_budget_exhausted`
      set; an audit failure before connection exhaustion — the listener
      still closing and the socket unlinked at exhaustion, no terminal
      record written, the exit report naming `connection-budget-exhausted`
      with the audit state `unavailable`, the driver recording `exhausted`
      beside the report, the counters unknown, and the summary never
      labeling the Run healthy; an agent exit arriving during an
      exhaustion drain aborting what remains with one terminal record; a
      terminal short write, a torn terminal, and a terminal `fdatasync`
      failure each reported from the bytes as `missing`, `malformed`, or
      `present` with the report naming kind `terminal`, distinct from the
      termination reason; and crashes and orphan publication with absent
      final counters — every counter `null` in the summary,
      `records_parsed` labeled as the driver's own parse, and no zero or
      reconstructed count anywhere;
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
    - lifecycle, concurrency, and recovery — shutdown with active requests;
      socket, sink, and spool cleanup after every exit path; a pre-existing
      socket-path entry in a fresh job directory failing loud; local and
      completion retries each getting a fresh `run_id`, job directory,
      socket, relay child, audit sink, spool lifecycle, and evidence bundle,
      with completion carryover containing only the enumerated state;
      parallel Runs on one node isolated across every one of those; a
      request flood and a parser crash taking only the relay child, the
      driver and Run surviving; driver restart and orphan sweep publishing
      the sink as `orphaned` and deleting the spool; container interruption
      running the shutdown path; evidence-push failure retaining the sink
      through the retry and deleting it only after a durable push; no relay
      reachable during gate jobs;
    - upgrade — a version 1 job directory left from before the upgrade
      published by the boot sweep under its own manifest and removed, never
      launched; a version 2 job directory containing any retired Context
      Tree path refused pre-work; a manifest stamped 1 refused by a version
      2 driver; a version 1 `format-output` CLI refusing a version 2
      manifest; the bench refusing a manifest whose version and layout
      disagree;
    - bench — `live` and `none` parity through the exported entry point,
      the same ingress, policy, gate, redirect, audit, representability,
      refusal-totality, bounded-redirect-metadata, shutdown, counter, and
      redirect-accounting fixtures passing unchanged
      against the bench driver in both modes, `none` producing only
      `refused` intents and the recorded `gh_upstream: none`, `live`
      against a scratch repository exercising the host pin with the
      bench-owned credential source never in argv;
    - and, throughout, no credential in argv, the agent's environment, logs,
      errors, evidence, audit metadata, request echoes, redirect targets, or
      refused requests; every refusal and diagnostic message byte-exact and
      never claiming the token cannot write; and the `schema_version` 2
      activation — the drift-lock test green before it (constant 1,
      current-version wording 1) and after it, with the activation PR
      moving the constant, the job-dir layout, the renderer signature, the
      relay entry point, the bench behavior, the current-version wording,
      and the Changelog entry in one change, together with the new prompt
      shape.

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
  rather than maintained beside a second source; the agent sees the whole
  conversation, non-member comments included, while no comment of any
  author can move a verdict, a commit, a label, a claim, or a transition —
  visibility and authority are decided separately and each is stated
  plainly; the socket's parser is specified against hostile bytes, so the
  read-only, host-pin, denylist, and byte-budget guarantees hold for any
  client the container can run, and a smuggled, encoded, or compressed
  shape has a defined refusal rather than an implementer's guess; an empty
  repository fails at boot as a workspace condition, and a boot report never
  says "verified" about a probe that did not run; the dependency closure has
  one shape in every document and the renderer signature; every credentialed
  redirect hop is preceded by its own durable record, so the sink alone
  names the last authorized target after any crash; a connection flood ends
  in a closed listener, never a busy loop; the query grammar accepts every
  search the pinned `gh` emits and refuses what it cannot decode
  unambiguously; every audit record kind has a schema that is checked
  before it authorizes anything, so a record that exists says exactly what
  was authorized and a request the sink cannot describe is refused rather
  than half-recorded; every refused request line, a malformed one
  included, leaves a bounded record naming the stage that refused it and
  identifying the input by digest, without repairing, copying, or
  inventing a target, and no `Location` the upstream can send enlarges or
  displaces a completion record; the audit's claim is stated at exactly
  its strength — authorization, never transmission — so no summary field
  asserts a send the relay cannot prove; acceptance ends once and the
  terminal record is written once, however it ends, and a missing terminal
  leaves the counters unknown rather than wrong; and no document states
  the successor schema version as current before the code does.
- **Negative**: a Run's inputs are no longer fully reproducible from its
  evidence bundle; a second GitHub credential per worker type is operator
  work, and its permission matrix is longer than the first draft promised; an
  operator who supplies a broader token silently downgrades the relay's
  classifier from defense in depth to the sole write barrier, and the product
  cannot detect it; GitHub Enterprise Server operators cannot run workers in
  V1; the relay is new trusted code — an HTTP parser fed by the agent, a
  GraphQL lexer whose fixture corpus must move with every `gh` bump, a
  redirect validator, and a response gate holding up to 64 MiB per Run in
  memory or spool; every request costs two to five audit records and one
  `fdatasync` before each credentialed hop; the driver gains a per-Run
  storage lifecycle for the sink and the spool; raw CI log and artifact
  bytes are not served in v1;
  review mode's constructible-without-GitHub property now yields a recorded
  mode deviation rather than production fidelity; a bench run at full
  fidelity needs the bench side to provision real GitHub objects; a
  prompt-injected session can be steered by any GitHub or internet content
  it reads — accepted, with instructions as mitigation and the human merge
  gate as the safeguard, never claimed eliminated; the relay token's scope
  is a disclosure scope the product cannot verify, so token binding carries
  operator doctrine the product can only document; the ingress contract
  refuses some technically valid HTTP (keep-alive, pipelining, chunk
  extensions, compressed responses, every non-allowlisted header), which
  binds the relay to the pinned `gh` version's wire behavior and makes a
  `gh` bump a relay-fixture change; one request per connection costs a
  connection setup per `gh` call; after the connection budget the agent's
  `gh` sees a connect failure instead of the stable refusal message;
  canonical query re-encoding rewrites the client's spelling, so a fixture
  pins the relay's form rather than the client's bytes; a target the
  parser admits can still be refused because its record would not fit — a
  case only hostile input reaches, since the pinned `gh` stays far under
  both limits; the relay carries a second channel to the driver, the
  audit-failure and exit reports, beside the sink; after any audit failure
  the Run's counters are unknown, because the only record that carries
  them is one the state forbids; the audit schema carries three tagged
  target forms, a method classification, and a scheme and host
  representation that the implementation must keep total by construction,
  and a refused hostile `Location` or malformed request line is
  diagnosable only by classification and digest, never by its text; and
  the accepted successor
  `schema_version` stays reserved until one implementation PR lands its
  activation whole, which sizes that PR.
- **Neutral**: amends ADR-0013 (channel clause), ADR-0017 (the Context
  Tree amendments' visibility filter is retired and the authorized-source
  rule for driver-parsed machine blocks restated as the surviving boundary),
  ADR-0053 (Context Tree closure and Review Run parity clauses; the exact
  dependency-entry schema), ADR-0056 (the read surface is no longer bound to
  one repository; cross-repo Dependency Edges stay malformed on the grounds
  that survive), and ADR-0054 with BENCH-CONTRACT.md (`schema_version` 2
  reserved as the accepted successor of the current 1, activated atomically
  by the implementation PR; relay entry point, upstream modes, ingress
  contract); ADR-0046, ADR-0010,
  and ADR-0056's host canonicalizer are explicitly unchanged; the per-Run
  `run_id`, job directory, and evidence bundle contract is reused, not
  extended; the glossary gains GitHub Relay and retires Context Tree; the
  ADR-0017 "context is read at checkout" clause now means the agent reads it
  live through the relay; the preflight proves presence of reads, not absence
  of writes — the only proof GitHub's API offers.

## Alternatives Considered

- **Keep the Context Tree beside `gh`**: rejected — two sources for the same
  conversation confuse the agent about which is canonical, and the snapshot's
  determinism benefit is worth less than the maintenance of a second surface;
  the prompt's inline task statement keeps the bench runnable without it.
- **Keeping the OWNER/MEMBER visibility filter at the relay**: rejected
  (2026-09-03, #117) — it would require the relay to parse and rewrite
  every REST and GraphQL response shape that can carry a comment, which is
  GitHub's schema again; it protects against nothing the agent's internet
  access does not already expose; and it conflates two questions the human
  ruled separately — what the agent may see (broad) and what may move the
  pipeline (only the driver, from authorized sources). The filter survives
  exactly where it was load-bearing: the driver's own selection of machine
  blocks.
- **Claiming that instructions prevent prompt injection**: rejected — no
  instruction makes retrieved text inert to a model; the product states the
  structural guarantees it has (no write outside the Output Proposal, no
  driver action from unauthorized content) and records the rest as an
  accepted residual risk absorbed by the human gate.
- **Forwarding raw client bytes upstream after inspection**: rejected
  (2026-09-03, #117) — an inspected byte stream is still the client's
  framing, and every smuggling shape is a disagreement between two parsers
  about where a request ends; rebuilding the upstream request from
  validated parts leaves exactly one parser's opinion on the wire.
- **Keep-alive and pipelining on the socket**: rejected — a second request
  on a connection is the surface request smuggling needs; one request per
  connection costs a Unix-socket connect per `gh` call and removes the
  class.
- **Normalizing ambiguous paths instead of refusing them**: rejected — a
  normalizer is a second opinion about what the client meant, and the
  denylist would be applied to that opinion; refusing every spelling that
  needs normalization keeps the denylist applied to the only spelling that
  exists.
- **Decoding compressed upstream responses under the byte gate**: rejected
  (2026-09-03, #117) — identity encoding is a request the relay controls,
  and a decoder inside the gate is more trusted code holding attacker-shaped
  input for a case the upstream should never produce; a compressed response
  is refused as an anomaly.
- **Recording redirect hops only in the completion record** (the third
  review's shape): rejected (2026-09-03, #117, fourth review) — a followed
  hop attaches the credential to a target the intent record never named, so
  a summary written after the fact is not write-ahead for that hop, and a
  crash between hops would leave the sink unable to name the last target
  whose credential use was authorized; the accounting objection that
  produced the completion-only
  shape is met by correlating each redirect-intent record by sequence and
  hop number, which interleaves safely under concurrency, and by reserving
  three hops' capacity per request.
- **Truncating an oversize record to a fixed marker** (the fourth review's
  shape): rejected (2026-09-03, #117, fifth review) — a marker written in
  place of an intent or redirect-intent records that a request or hop was
  authorized without saying against what, so the write-ahead
  invariant would be satisfied by a record that names no target;
  serializing and measuring before the write, refusing what does not fit,
  and giving every kind a schema whose mandatory fields cannot be dropped
  makes every record that exists say what it authorized.
- **Raising the record cap so every admitted target fits**: rejected — the
  path and query limits bound the ingress and the record cap bounds the
  sink, and they answer different questions; a cap sized to the largest
  admissible target would let a hostile client spend the audit budget in
  a fraction of the requests, while the pinned `gh` never approaches
  either limit, so the honest shape is a separate `audit-unrepresentable`
  refusal that only hostile input reaches.
- **A refusal-form redirect-intent record for an unrepresentable hop**:
  rejected — a hop refused by policy already writes no redirect-intent
  record and lives in the completion summary; giving the representability
  refusal a record of its own would make two kinds of refused hop with
  two shapes, and a redirect-intent record that exists would no longer
  mean that hop's credential use was authorized.
- **Writing the terminal record at audit-budget exhaustion**: rejected —
  reserved requests finish after that moment and their completions would
  follow the terminal, exhaustion is not the end of acceptance, and a
  later agent exit or connection exhaustion would then face a second
  terminal write or none; one terminal, written last, carries the
  exhaustion as a flag.
- **Deriving the termination reason or the counters from the terminal
  record**: rejected — the terminal record is sink evidence an
  audit-unavailable relay cannot write, so an exhausted relay in that
  state would be reported as crashed or its counters invented; the exit
  report carries how acceptance ended, the terminal record alone carries
  the counters, and the summary reports unknown where the record is
  absent rather than zero or a reconstruction.
- **A three-entry redirect summary**: rejected — three followed hops can
  be answered by a fourth redirect the hop limit refuses, and that refusal
  is a decision the summary must show; four entries, three
  redirect-intent records.
- **One refusal form requiring a literal method and a canonical target**
  (the fifth review's shape): rejected (2026-09-03, #117, sixth review) —
  a request line the parser rejects has no canonical path, query, or
  GraphQL metadata to record, so a single form would force the relay to
  invent, repair, or copy input to fill it, and a literal method field is
  unbounded; the tagged forms make every refusal representable from
  exactly what the parser established, and `audit-unrepresentable` keeps
  its one meaning.
- **Recording the raw request-target or method token of a malformed
  request**: rejected — the bytes are unbounded up to the request-line
  limit, may carry a credential the client put there, and the sink is a
  security record, not a debugging capture; the byte length and sha256
  identify the input for correlation without reproducing it.
- **Repairing invalid input to record a canonical path**: rejected — a
  repaired path names a target the relay never validated and would make
  the record claim more than the parser knew; the invalid form names the
  failed stage and nothing beyond it.
- **A literal scheme and a host cut at 256 bytes** (the fifth review's
  completion entry): rejected — a cut host is uncontrolled upstream text
  partially copied, a literal scheme is unbounded, and a `Location` that
  does not parse has neither; a closed scheme classification and a host
  validity status with a literal only when valid keep the completion
  bounded by construction for any `Location`.
- **Treating an intent or redirect-intent record as proof that the
  request was sent**: rejected — no write-ahead record can prove a later
  send, and only the completion, written after the fact, says what
  happened; the record proves the authorization, the crash after its sync
  and before the send is the `indeterminate` outcome the summary already
  distinguishes, and no new protocol (a pre-send marker, a post-send
  record) is added, since each would reopen the ordering questions the
  fifth review closed.
- **Stating `schema_version` 2 as current before the implementation lands,
  with a red drift-lock as the interim**: rejected (2026-09-03, #117, fourth
  review) — the drift-lock exists to make disagreement between the code
  constant and the current-version wording a failure, a document that states
  the future value as current defeats it, and weakening the test to tolerate
  the interim would remove the guard exactly when it matters; the successor
  is described as reserved, and activation is one atomic change.
- **An accept-and-refuse loop after connection-budget exhaustion**: rejected
  — each refused connection still costs an accept, a write, and a close, so
  a client looping on connect keeps the relay busy for the Run's lifetime;
  closing the listener and unlinking the socket moves every later attempt to
  the kernel and ends the loop at a fixed cost.
- **Charging only connections that carry a request line**: rejected — an
  empty connection, a partial request line, and a timeout each cost the
  relay an accept and a timer without ever reaching the request budget, so a
  client could churn them without bound; charging at accept makes every
  accepted connection finite.
- **One grammar for path and query**: rejected — the path's rules refuse
  encoded separators because a decoded `/` changes what the denylist sees,
  while a query value is opaque to policy and legitimately carries `:` and
  `/` (`repo:OWNER/REPO`, a ref name); one grammar either refuses every
  `gh search` or admits an encoded separator into the path.
- **Treating `+` in the query as a literal plus**: rejected — GitHub decodes
  its query strings with the form convention and the pinned `gh` encodes a
  space as `+`, so any other reading sends a different search than the
  client meant; the canonical re-encoding removes the ambiguity by never
  emitting a bare `+`.
- **Forwarding the query untouched**: rejected — the audit's query
  representation and the upstream bytes must describe the same decoded
  values, and a malformed escape or control byte would then be GitHub's to
  interpret rather than the relay's to refuse.
- **Passing the preflight on an empty repository**: rejected — "every empty
  result passes" applied to the commit listing would mark Checks and Commit
  statuses verified without ever probing them, and a worker cannot check
  out a repository with no commit anyway; the honest outcome is a workspace
  refusal, and the boot report never says "verified" about a probe that did
  not run.
- **Dependency numbers only in the prompt**: rejected (2026-09-03, #117) —
  the agent needs a blocker's state to know whether to read its unlanded
  PR's Decisions Section or its merged code, ADR-0053 already promised the
  state, and a bare list would make each Run spend `gh` calls rediscovering
  facts the driver just computed; numbers with a title or PR number were
  rejected the other way — anything beyond the state is content the agent
  reads live.
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

## Amendments

- **2026-09-03 (#117, second review)**: V1 supports GitHub.com only — a
  GHES host fails closed at config load and driver boot, no classic-token
  fallback exists (decision 3, 5). The audit became write-ahead with an
  audit-unavailable state and outcome counts (decision 8), GraphQL `POST`
  redirects are refused and REST redirects followed only method-preserving
  with every hop revalidated (decision 5), responses are gated whole before
  delivery (decision 6), every retry is a fresh Run with a fresh job
  directory, socket, relay, and sink (decision 10), and the credential
  guarantee is stated conditionally on operator provisioning (decision 3).
- **2026-09-03 (#117, third review)**: content trust ruled — non-member
  GitHub content is visible to agents, the Context Tree's OWNER/MEMBER
  visibility filter is retired with the tree, driver-parsed machine blocks
  still come only from the pipeline's authorized sources, retrieved content
  is evidence never instruction, and prompt injection is an accepted
  residual risk (decision 1). The socket is specified as a hostile HTTP
  ingress with exact limits, rejections, path canonicalization, upstream
  reconstruction, header allowlists, and compressed-response refusal
  (decision 6). The write-ahead invariant is scoped to agent-originated
  requests with a separate preflight record, redirect decisions live in one
  bounded array per completion record, and short writes and torn records
  have defined publication semantics (decisions 3, 5, 8). The credential's
  confidentiality boundary is operator doctrine, the preflight refuses an
  empty repository and reports per capability, the dependency closure has
  one exact prompt schema, and the `schema_version` 1→2 upgrade fails closed
  on mixed versions (decisions 1, 3, 9, 10). Test obligations extended
  accordingly (decision 11).
- **2026-09-03 (#117, fourth review)**: `schema_version` 2 is the accepted
  successor of the current 1, activated atomically by the implementation PR
  — constant, layout, renderer signature, relay entry point, bench
  behavior, current-version wording, and Changelog entry in one change — and
  no document states 2 as current before then (decision 9). Every followed
  redirect hop is authorized by its own durable redirect-intent record
  before the credential is attached, the completion record's `redirects`
  array stays as a summary, capacity is reserved for three hops per request
  and released only after completion, and the audit-unavailable state
  writes no terminal record — the relay reports the failure to the driver
  outside the sink, and the summary's `incomplete` count rests on that
  report (decisions 5, 8, 10). Every accepted connection is charged against
  a finite per-Run connection budget whose exhaustion closes the listener
  and ends the relay clean as the `exhausted` termination (decisions 6, 8,
  10). The query has its own canonicalization grammar, separate from the
  path's — order and duplicates preserved, decoded once with form
  semantics, malformed escapes and control bytes refused, reserved
  characters as data, canonical re-encoding, no part in policy — and the
  pinned-`gh` fixture corpus covers the search and query shapes (decisions
  4, 6). Test obligations extended accordingly (decision 11).
- **2026-09-03 (#117, fifth review)**: every audit record kind has an
  explicit schema with mandatory fields; an intent or redirect-intent is
  serialized and measured before it authorizes anything, a full form that
  would exceed the cap becomes the bounded refusal form (intent) or
  refuses the hop (redirect-intent) as `audit-unrepresentable`, and no
  truncation marker exists; the completion record copies no routing
  metadata and admits four redirect entries — three followed hops plus
  the hop-limit refusal (decisions 5, 6, 8). One shutdown sequence:
  acceptance ends once, accepted work drains, and the terminal record is
  written at most once, last, in its reserved room — never at
  audit-budget exhaustion, which only stops authorizations and
  per-request records, and never under audit-unavailable, which takes
  precedence even over connection exhaustion; the relay's exit report
  carries how acceptance ended and the audit state, the terminal record
  alone carries the counters, `requests_seen` is distinguished from
  `requests_charged`, and the summary reports counters unknown when the
  terminal record is absent (decisions 6, 8, 10). Test obligations
  extended accordingly (decision 11).
- **2026-09-03 (#117, sixth review)**: the refusal schemas are total over
  rejected input — the target representation is tagged (full, digest,
  invalid), the method is a closed classification with length and digest
  for unknown tokens, GraphQL metadata is explicitly `parsed: false` when
  a body could not be classified, a request line rejected before its
  target validated is recorded in the invalid form naming the failed
  stage with the raw target's length and digest, and
  `audit-unrepresentable` names only the oversized otherwise-authorizable
  case while every other refusal keeps its own reason (decisions 6, 8,
  9). The completion's redirect entries carry a closed scheme
  classification and a host validity status with a literal only when
  valid, and the serialized bound of every fixed form is stated
  (decisions 5, 8). The evidence wording is exact: an intent or
  redirect-intent record proves that credential use was authorized, never
  that a request was sent (decisions 8, 10). Test obligations extended
  accordingly (decision 11).

## Relevant PRs

- #117 — records this decision and the amendments it makes to ADR-0013,
  ADR-0017, ADR-0053, ADR-0054, ADR-0056, the pipeline spec, the bench
  contract, and the glossary; its first review folded in the five hardening
  items named in the provenance, its second review carried the
  GitHub.com-only ruling and the write-ahead audit, GraphQL-redirect,
  response-gate, per-Run retry, and credential-guarantee corrections, and its
  third review carried the content-trust ruling and completed the ingress,
  audit, confidentiality, preflight, dependency-schema, and upgrade
  contracts, and its fourth review reserved `schema_version` 2 as the
  accepted successor with atomic activation, made every redirect hop
  write-ahead, bounded connection attempts, and gave the query its own
  grammar, and its fifth review gave every audit record an explicit
  size-checked schema, one terminal and shutdown model, separate request
  counters, and four-entry redirect accounting, and its sixth review made
  the refusal schemas total over malformed input, bounded the
  refused-redirect metadata, and stated the audit's evidence claim
  exactly.
