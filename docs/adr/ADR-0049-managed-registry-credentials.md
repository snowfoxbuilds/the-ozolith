Status: ACCEPTED

Date: 2026-08-20

# ADR-0049: Managed registry credentials — authenticated base resolution and private-base pulls

## Context

`theozolith config ingest` could not resolve a **private** base image
digest. `resolve_image_digest` (ADR-0048's mechanical pin step) speaks
only anonymous pull scope: it HEADs the manifest, runs GHCR's anonymous
bearer-token flow on a `401`, and retries — but a `403` is fatal. GHCR's
failure mode for a private package is exactly that: the anonymous token
*mints fine*, then the manifest HEAD `403`s. So a Config Repo whose bases
are tag-only (the ADR-0048 doctrine — the human config carries no computed
pins) fails ingest the moment its base is private:

```
error: cannot resolve base tag 'ghcr.io/snowfoxbuilds/theozolith-run-claude:0.3.0':
       HTTP Error 403: Forbidden
```

A private base is the correct posture — a Config Repo is private, and
forcing its base image public makes no sense — but the system modeled **no
registry credential anywhere** (`grep` across `control/`, `nodedaemon/`,
`deploy/` found none). Two consumers need one: ingest, to resolve the
digest; and every container-host, to `docker build` a `FROM …@sha256:…`
whose base is private.

Dropping the digest and building from the tag is not an option.
Confidentiality (a private image) and the digest pin (integrity +
reproducibility) are orthogonal, and a tag is the one part of a "pinned
recipe" a registry can change after review. Three properties depend on the
digest: **fleet-skew visibility** (control folds the base digest into the
deterministic build tag; a mutable tag makes stale bases both unprevented
and invisible — the exact failure `builds.py` exists to prevent),
**deterministic rollback** (ADR-0048's "revert the pinned build in git"
only reproduces the deployed bits if the base is a digest), and
**credentialed-agent environment integrity** (Implementer/Reviewer Runs
execute credentialed inside this base; a mutable tag lets one push re-image
every credentialed worker with no reviewed config change). So: keep the
digest pin; make the system able to obtain and use it for a private image.

Resolution stays at ingest, not node-side. The node holds the creds, so
"let each node resolve and stamp its own digest" is tempting, but control
needs the digest *before* it computes the deterministic tag; a tag that
omits the base digest makes skew undetectable. The node still needs the
credential — but to *pull*, not to resolve.

## Decision

Stay on GHCR (this makes us a better *client* of a registry, never a host).
Add one managed credential class and thread it to the two consumers.

**The credential is a reserved-name secret in the existing Fernet store**
(ADR-0015): `registry:<host>`, value `<user>:<token>` (e.g. a GitHub
username and a PAT with `read:packages`). It is discovered by prefix — no
new settings surface, no new store. `<host>` is the registry host as the
ingest resolver keys it (`ghcr.io`, `localhost:5000`, and the normalized
`registry-1.docker.io` for Docker Hub).

**Authenticated resolution at the token step.** Attempt 1 stays
unauthenticated for everyone — the anonymous fast path public bases keep.
On the `401` challenge, when a credential is stored for the challenged
host, the token-realm request carries `Authorization: Basic b64(user:token)`;
anonymous otherwise. There is **never a third manifest attempt**: "retry
with credential after a 403" is the wrong ladder — by the 403 the challenge
is already gone (GHCR mints the anonymous token fine, *then* 403s the HEAD),
and a bare 403 with no challenge carries no realm to authenticate against.
A missing or refused credential fails loud with an actionable message
(naming the exact `secret set registry:<host>` command and the
digest-pin escape hatch), never a bare `403`. The documented limitation: a
registry that answers a bare 403 with no challenge cannot be credentialed;
no first-party registry does this.

**The hint bug is fixed in passing.** `_resolve_bases` re-raised
`IngestError` verbatim, so the ":pin it by digest" hint never fired for
HTTP errors. The actionable hint now lives in `resolve_image_digest`, where
the host is known.

**Node build-time pull.** The container-host build injects the same
credential as a `DOCKER_CONFIG` directory (a tmpfs `config.json` with the
host's `auth`, plus Docker Hub's legacy `https://index.docker.io/v1/` twin)
so `FROM <private-base>@sha256:…` can pull. On a pull failure with a cached
tmpfs `config.json`, that cache is used (mirroring `_pull_stack_secrets`);
with none, the build proceeds without a credential and a private-base
`docker build` fails into the existing per-image error path. This is never
deferred like knowledge staging: a missing pull credential cannot produce
wrong bits under the right tag, only an accurate per-image failure, and
deferral would block public-base builds during a control outage. **This
does not touch `builds.py`'s "no registry" doctrine for *derived* images**:
derived images are never pushed; only the private *base* is pulled.

**Scoping.** `secret_names_for(node)` unions in `registry:<host>` for every
worker type behind a **running** worker-type Stack on the node — the same
running-recipe rule `desired_state_for` uses for `images`, so a node may
pull only the pull credential of a base it will actually build. The
heartbeat carries `registry_secrets` as a `{host: "registry:<host>"}`
map of **names only**, filtered to credentials actually stored: a
public-base fleet sees no new key, the channel invariant holds (the value
rides the node-scoped secrets pull, never the heartbeat), and the pull is
scoped for free by `secret_names_for`.

**Worker-env exclusion.** A `registry:`-prefixed name is rejected as a
worker-type or Stack `[secrets]` binding *value* (fail-loud at config load):
the infrastructure pull credential can never be routed into workload env by
a config edit. The write surface (`PUT /api/v1/secrets` and the web form)
shape-checks a `registry:` credential — a plausible host in the name, a
`<user>:<token>` value — with an actionable 400; every other name stays
shape-blind.

**Two PRs.** PR 1 is control-side (resolution, scoping, the guards, the
wire mapping) and is independently shippable — the daemon ignores unknown
desired-state keys, so `registry_secrets` is inert until PR 2. PR 2 is the
node-side build-time injection.

## Consequences

- **Positive**: a private first-party base resolves at ingest and pulls at
  build time with one managed credential; the digest pin — and the
  fleet-skew visibility and git-revert reproducibility that ride on it — is
  unchanged; public bases still resolve anonymously with no credential
  configured; the infra credential is structurally barred from workload env.
- **Negative**: a private-base deployment must hold a GHCR pull credential
  on the Control Node (for ingest) and, transitively, on every
  container-host (for the base pull) — table stakes for private bases, now
  modeled rather than absent.
- **Neutral**: the anonymous fast path, the deterministic-tag model, image
  identity (the 4-key instruction hash excludes the wire `registry_secrets`
  reference), and `builds.py`'s no-push doctrine for derived images are
  untouched.
- **Amends ADR-0048**: the resolve step gains an authenticated path; the
  resolver takes stored `registry:<host>` credentials, keyed by host,
  bound to the default resolver via the ingest CLI. The mechanical-pin
  doctrine (tag-only bases in the human Config Repo) is preserved — the
  credential is what makes it hold for a private base.
- **Amends ADR-0015**: the secret store gains the `registry:` reserved-name
  class and the write-surface shape guard; node scoping (`secret_names_for`)
  extends to buildable bases. The "no registry" doctrine for *derived*
  images (`builds.py`) is unchanged — nothing is pushed.
- References **ADR-0006** (the private Config Repo whose base is private)
  and the ADR-0006/0015 node-build model (`FROM …@sha256:…` at derived-image
  build time needs the base's pull credential).

## Out of scope

- Private container-Stack `image =` refs (only worker-type bases are
  covered).
- Private product/knowledge sources.
- `docker push` / registries as a derived-image transport.
- Per-node distinct credentials (one credential per registry, fleet-wide).
- A self-hosted registry (we stay a GHCR *client*).

## Alternatives Considered

- **Node-side resolution + stamp**: rejected — control needs the digest
  before it computes the deterministic tag; a tag that omits the base digest
  makes fleet skew undetectable, the exact failure `builds.py` prevents.
- **Drop the digest, build from the tag**: rejected — costs fleet-skew
  visibility, deterministic rollback, and credentialed-agent environment
  integrity; a tag is the one part of a "pinned recipe" a registry can move
  after review.
- **Retry the manifest with the credential after a 403**: rejected — by the
  403 the challenge is gone; the credential belongs at the token step, on
  the 401.
- **A `~/.docker/config.json`-shaped blob as the credential value**:
  considered — the `<user>:<token>` form is simpler to validate and enter,
  and the node writes the `config.json` itself.
- **A new settings surface / a dedicated registry-credential store**:
  rejected — the reserved-name prefix in the existing Fernet store reuses
  the encrypted-at-rest, node-scoped-pull machinery with no new surface.

## Relevant PRs

- #67 — the issue this ADR resolves.
