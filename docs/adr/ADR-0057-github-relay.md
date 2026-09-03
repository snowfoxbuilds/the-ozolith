Status: ACCEPTED
Date: 2026-09-03

Provenance: issue #115 (feasibility spike with a live proof of concept; grilling 2026-09-03).

# ADR-0057: GitHub Relay — `gh` as the agent's read surface, credential-free by driver-side auth injection

## Context

Run containers hold no GitHub credential (ADR-0013), so an agent's only view of
GitHub was the Context Tree: a driver-serialized snapshot of the issue, PR,
and dependency closure mounted into the job dir (#52, ADR-0053). That kept
every input deterministic and evidence-recorded, but it gave the agent a
frozen copy of one conversation and nothing else — no other issue, no upstream
repo, no CI log, no live state — and it made the agent's GitHub interaction
a bespoke file layout rather than the tool every engineer already uses.
Issue #115 proved the alternative live: `gh` sets `http_unix_socket` and sends
plaintext HTTP over a Unix socket; a driver-owned relay on that socket injects
the `Authorization` header, re-originates TLS to GitHub, and gates every
request — a credential-free `gh` (empty config dir, a sentinel `GH_TOKEN`)
authenticated as the real account and a disallowed call was refused with a
403 the client rendered on stderr. The spike also established the design's
one hard finding: nearly every high-level `gh` command tunnels through the
single `POST /graphql` endpoint, so "which commands are allowed" is a
GraphQL-body classification problem, not a path-allowlist problem, and it
must fail closed. Workers are not yet live, so nothing here preserves
compatibility with the Context Tree shape.

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
3. **A dedicated read-only credential, never the driver PAT.** Every
   driver-bearing worker type declares a required secret slot,
   `GITHUB_READ_TOKEN` (`""` — every instantiating Stack must bind it; refused
   at config load when unbound, per ADR-0047), delivered `_FILE`-style from
   tmpfs like `GITHUB_TOKEN` and read by the driver as
   `{ROLE}_GITHUB_READ_TOKEN` with the existing precedence convention. The
   slot name travels in Candidate Bundles like every other slot. Operator
   doctrine, documented in the example worker types and not product-enforced:
   a fine-grained PAT with read-only Contents / Issues / Pull requests /
   Metadata on the bound repositories plus public-repository read. The driver's
   one-per-boot identity dry-run gains a relay-credential check — the token
   authenticates (`GET /user`) and can read the Bound Workspace
   (`GET /repos/{owner}/{repo}`) — failing loud before any claim; the product
   never probes with a mutation and cannot introspect fine-grained
   permissions. Fact recorded for operators: GitHub's primary rate limit is
   per user account and shared by every token that account owns, so
   permission separation comes from the scoped token while a separate rate
   budget needs a separate machine account — an operator optimization the
   product neither requires nor recommends; the per-Run caps below are the
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
5. **Host-pinned, credential-scoped; no repository parsing.** The relay
   re-originates only to the Bound Workspace's GitHub host (`api.github.com`,
   or a GHES host's `/api/v3` and `/api/graphql`); the harness sets `GH_HOST`
   accordingly. Which repositories are readable is bounded by the credential,
   not by a second parser: cross-repository reads within the bound set are
   acceptable, public-repository reads are a feature.
6. **Transport: a socket file inside the job dir; the relay is a per-Run driver
   child.** The socket lives at the job-dir root (never under `input/`) and
   reaches the container through the mount that already exists — no new mount,
   per-Run by construction. ADR-0013's channel clause is amended, not
   reopened: the job directory remains the only driver↔harness channel, and
   the socket file in it carries policy-filtered GitHub reads outward and
   never a credential, authority, or reporter inward. The relay runs as a
   driver child process per Run so a request flood or parser crash cannot take
   the Run's driver with it, with per-Run caps (defaults: 2,000 requests, 4
   concurrent, 1 MiB request body, 16 MiB response body, 30 s upstream
   timeout). It asks the upstream for identity encoding and hands the client
   plain bytes. Its lifetime is the agent phase only: created before launch,
   unlinked when the agent exits, so gate jobs — repo-declared commands
   running agent-authored code — never see GitHub. The harness bootstraps the
   client: it writes `http_unix_socket` into a `GH_CONFIG_DIR` outside the
   checkout (the key has no environment-variable form), and sets `GH_HOST`
   and a sentinel `GH_TOKEN` in the agent environment — `gh` refuses to run
   with no token at all, and the relay overwrites the header. The product base
   images pin a `gh` version; the relay and the client it is tested against
   ship together (a base bump moves every derived-image identity — accepted).
7. **Every worker type, one implementation.** Implementer, Reviewer, and the
   Initializer when built share one relay in the base driver and one policy;
   the only per-type variable is prompt wording. No in-container blocking
   exists or may be added: the agent owns its container, so a wrapper `gh` or
   a CLI permission rule is bypassable by construction; the relay is the
   single enforcement point and the refusal message is the feedback channel.
   Flight Decks are untouched — they hold their own credential (ADR-0019).
8. **Audit, not reproduction.** The relay writes `gh-audit.jsonl` per Run —
   timestamp, method, path, GraphQL operation type, name, and variables,
   decision and reason, upstream status, byte counts — never response bodies;
   the file joins the evidence bundle and a `gh` call counter joins progress
   telemetry. Full input reproducibility is knowingly given up: requests are
   the security-relevant record, and responses remain recoverable from GitHub.
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
   schema.

## Consequences

- **Positive**: agents read GitHub with the tool and idioms they already know,
  reaching beyond one frozen conversation; the write side is untouched, so the
  ADR-0046 guarantee — forbidden mutations unrepresentable — holds exactly as
  before; the credential in the relay cannot write even if a body evades
  classification; one prompt shape serves production and the bench; every
  read is audited; the Context Tree serializer, its evidence snapshot, and the
  navigation guide are deleted rather than maintained beside a second source.
- **Negative**: a Run's inputs are no longer fully reproducible from its
  evidence bundle; a second GitHub credential per worker type is operator
  work; the relay is new trusted code — an HTTP parser fed by the agent, and a
  GraphQL lexer whose fixture corpus must move with every `gh` bump; review
  mode's constructible-without-GitHub property now yields a recorded mode
  deviation rather than production fidelity; a bench run at full fidelity
  needs the bench side to provision real GitHub objects.
- **Neutral**: amends ADR-0013 (channel clause), ADR-0053 (Context Tree
  closure and Review Run parity clauses), and ADR-0054 with BENCH-CONTRACT.md
  (`schema_version` 2, relay entry point, upstream modes); ADR-0046 and
  ADR-0010 are explicitly unchanged; the glossary gains GitHub Relay and
  retires Context Tree; the ADR-0017 "context is read at checkout" clause now
  means the agent reads it live through the relay.

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
- **Anonymous public GitHub as the no-upstream bench mode**: rejected — the
  task repository 404s among partial answers and rate-limit noise; an explicit
  refusal teaches the agent the situation on its first call.
- **A fixture upstream serving the Context Tree for bench**: rejected —
  emulating GitHub's GraphQL schema for a moving `gh`.
- **Relay alive for the whole container**: rejected — gate steps run
  agent-authored code and need no GitHub.
