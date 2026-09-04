# Deploying securo

Argo CD watches `deploy/` on `main` and applies whatever plain manifests it
finds there via Kustomize. **This directory is GENERATED** (except
`ciliumnetworkpolicy.yaml`, `kustomization.yaml`, `values.yaml`, and this
file) — do not hand-edit `manifests.yaml`, it will be overwritten.

`manifests.yaml` is `helm template` output from `charts/securo`, rendered
with `values.yaml` in this directory, with both image references pinned to
a digest. It is written by the `deploy` job in
`.github/workflows/release.yml`, which runs after `build-backend` and
`build-frontend` on every published release — the commit that writes the
digests *is* the deploy, same pattern and same reasoning as
`luqmanbello/worth`'s `deploy/` (homelab ADR-0140).

Argo's root Application template for this app can pass only a
`releaseName` — no values, no valueFiles, no parameters — which is why
`deploy/` holds rendered manifests instead of pointing Argo at the chart
directly with an external values file. All configuration that would
normally live in a Helm values override lives in `deploy/values.yaml`
instead, baked into the render.

## Regenerate locally

```sh
helm template securo charts/securo -f deploy/values.yaml \
  --namespace securo --skip-tests > deploy/manifests.yaml
```

The release name **must** be `securo`: `charts/securo/templates/_helpers.tpl`
collapses `securo.fullname` to the release name whenever it already
contains the chart name, so this is what makes resources come out named
`securo`, `securo-backend`, `securo-postgresql`, etc. — the names
`deploy/ciliumnetworkpolicy.yaml`'s selectors and this file's `DATABASE_URL`
below both assume. `--skip-tests` drops the chart's `helm.sh/hook: test`
pod (a `wget` connectivity check meaningless outside `helm test`); without
it, `helm template` still renders it as a plain `Pod` that Argo would then
try to keep running forever.

A locally regenerated `manifests.yaml` still needs the images repointed
from `:unset` to `@sha256:...` by hand or by re-running the digest-pinning
step from the workflow — see `.github/workflows/release.yml`'s `deploy`
job. Outside the initial bootstrap commit (see below), never commit a
`:unset`-tagged or otherwise tag-pinned image; a validator fails the
deploy if any `ghcr.io/luqmanbello/securo-` image reference lacks
`@sha256:`.

## Bootstrap gap

The `manifests.yaml` committed alongside this README was rendered locally
for verification only, with the placeholder `unset` tag still in place —
there was no release to pull a real digest from at the time this directory
was created. If Argo CD syncs this commit before the next GitHub Release is
published, both application pods will sit in `ImagePullBackOff` until the
`deploy` job runs and writes real digests over it. This is a one-time gap
in the bootstrap sequence, not a recurring issue: every release after the
first fixes it.

## Secrets

Three Secrets, provisioned imperatively on the cluster with `kubectl create
secret generic` — **never plain `kubectl apply -f -`**. Client-side apply stamps
the whole object, values included, into the `last-applied-configuration`
metadata annotation, which is a live leak in existing Secrets elsewhere in this
estate. Use `create`, or `apply --server-side` if you must apply. No generated
Secret manifest enters Git.
fed over stdin, the same contract as `worth-runtime`, `worth-accessbank`,
and `paperless-runtime`. No generated Secret manifest enters Git.

| Secret | Keys | Consumed by |
|---|---|---|
| `securo-runtime` (namespace `securo`) | `SECRET_KEY` — nothing else | backend, celery-worker, celery-beat, migration Job |
| `securo-postgres` (namespace `securo`) | `DATABASE_URL` | backend, celery-worker, celery-beat, migration Job |
| `securo-openexchangerates` (namespace `securo`) | `OPENEXCHANGERATES_APP_ID` | backend, celery-worker, celery-beat — deliberately NOT the migration Job |

### Image pull credentials: none needed, and that is deliberate

Both images are public — verified 2026-08-25 by anonymous token pull against
ghcr.io, HTTP 200 with no credentials. Packages published by Actions inherit
the repository's visibility, and `luqmanbello/securo` is public.

**If that ever changes, patch `imagePullSecrets: [{name: ghcr-pull}]` back onto
the four Deployments and the migration Job** in `deploy/kustomization.yaml`.
`postgres` and `redis` pull public docker.io images and never need it.

An earlier revision carried those patches pre-emptively and they were removed
on purpose. A Secret reference the cluster ignores is not free: kubelet warns
on every pull and falls back to an anonymous one, so the namespace carries a
standing warning while nothing is wrong — and a warning that is always there is
how a real `ImagePullBackOff` later goes unread. Making the images private
would be a deliberate act, and the manifest change belongs with it rather than
installed years in advance where a reader cannot tell whether it is
load-bearing.

Recording the mistake that produced the earlier version, because the shape of
it recurs: the first check ran `docker manifest inspect` against tag
`v0.14.4-securo-1`, which does not exist — `docker/metadata-action` strips the
`v`, so the published tags are `0.14.4-securo-1` and `latest`. A missing tag
and an unauthorised pull fail in ways that look alike. List what exists before
concluding anything from what is absent.

`deploy/values.yaml` sets `global.existingSecret: securo-runtime`, which
disables `charts/securo/templates/common/secret.yaml` (the chart's own
generated Secret) entirely — backend, worker, and beat all read from
`securo-runtime` instead, through the exact same `envFrom`/`secretRef`
branch the chart already has in each of their Deployments (and in the
migration Job). No chart feature was invented to make this work.

**Three objects, not one, and the split is about sensitivity.** The chart
offers a single `global.existingSecret` knob, and setting it disables the
chart's own generated Secret entirely — so by default everything would have to
live in one object. `deploy/kustomization.yaml` patches a second and third
`secretRef` onto the consumers instead, using the same `envFrom` list the
chart already renders. No chart feature was invented.

**Correction 2026-08-25:** an earlier version of this section justified the
split on rotation cost, saying `DATABASE_URL` is touched by routine operations.
It cannot be rotated at all as the chart stands — see the Postgres password
note below. The split is still right, on sensitivity rather than rotation.

The three differ in what compromising each one yields:

- `SECRET_KEY` must **never** change. It signs every session token and derives
  the Fernet key that encrypts the stored bank credential, so rotating it
  strands that credential and forces a manual reconnect through the UI.
- `DATABASE_URL` is the least sensitive of the three: the password inside it is
  a published constant (below), so the object is a wrapper around a value that
  is not secret. It is separate so that a future move to an external database —
  the one change that WOULD make it a real credential — does not mean editing
  the object that holds `SECRET_KEY`.
- `OPENEXCHANGERATES_APP_ID` is free to rotate whenever.

Under one combined Secret, rotating the cheap FX key means recreating the
object holding the key that must not change, and one slip does it silently.
Separate objects mean no routine operation has any reason to open
`securo-runtime` at all.

**The asymmetry is deliberate — do not tidy it into uniformity.**
`DATABASE_URL` reaches all four consumers including the migration Job, which
cannot connect without it and fails loudly; that is the safe direction. The FX
key reaches only the three long-running workloads, because the Job has no use
for it and a missing FX key is a safe failure anyway
(`openexchangerates_app_id` defaults to `""` and conversion goes quiet).

`DATABASE_URL` looks like plain configuration, but the chart only exposes it as
`secret.databaseUrl`. With no value supplied the backend falls back to its own
default (`postgresql+asyncpg://postgres:postgres@localhost:5432/securo`) and
CrashLoops — loud, which is what we want.

```sh
# SECRET_KEY: mint once, before the bank is first connected, and never again.
# At least 256 bits from a CSPRNG. The salt in crypto.py is a constant, so the
# derived key's entropy is exactly this value's entropy and nothing else.
kubectl create secret generic securo-runtime -n securo \
  --from-literal=SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')" \


kubectl create secret generic securo-postgres -n securo \
  --from-literal=DATABASE_URL="postgresql+asyncpg://postgres:postgres@securo-postgresql:5432/securo" \


kubectl create secret generic securo-openexchangerates -n securo \
  --from-literal=OPENEXCHANGERATES_APP_ID="<App ID from the vault item>" \

```

The app refuses to start if `SECRET_KEY` is a known placeholder or shorter than
32 characters (`backend/app/main.py`). That guard exists because the failure it
prevents is invisible: the chart's own default is a published string, the pods
come up Healthy, and the only symptom is that sessions are forgeable and the
encryption was never real.

The backend, celery-worker, and celery-beat Deployments all run the same
image and read the same `Settings` object (`backend/app/core/config.py`),
so all three need the same env vars — `SECRET_KEY` for auth token signing
in the backend and worker (invalidation on rotation affects both),
`OPENEXCHANGERATES_APP_ID` read by the worker on scheduled FX syncs, and
`DATABASE_URL` by all three including the migration Job.

## Access Bank (Nigeria) import

Read-only balance/transaction import, the reason this fork exists.
`deploy/values.yaml` sets `ACCESSBANK_ENABLED=true` and
`ACCESSBANK_IMPORT_CURRENCIES=USD` (see `backend/app/providers/accessbank.py`
and `backend/app/core/config.py` for what those control) and pins
`SUPPORTED_CURRENCIES` to the backend's default list including NGN.
Per-connection bank credentials are supplied by the user through the app
itself and stored encrypted on the connection row — there is no bank
credential Secret to provision here, unlike `worth-accessbank`.

## Network policy

`deploy/ciliumnetworkpolicy.yaml` is hand-written, not rendered — the chart
has no NetworkPolicy of its own. It is a `CiliumNetworkPolicy` rather than
a plain `NetworkPolicy` for the same reason as worth's: the Cilium Gateway
is host-network Envoy carrying the reserved `ingress` identity, which an
ipBlock rule never matches, and `fromEntities` is what actually speaks that
identity model. Full reasoning and per-rule justification is in the file's
own comments.

Unlike worth (one pod, one policy), securo is six workloads — frontend,
backend, celery-worker, celery-beat, postgres, redis, plus a one-shot
migration Job — that must reach each other over the network as well as out
to the Gateway and the internet. The file is one `CiliumNetworkPolicy` per
workload so each rule stays scoped to, and commented against, the specific
peer it exists for.

## Other things worth knowing

- **The `deploy` job renders from `main`'s current HEAD, not from the
  released tag.** If commits land on `main` between a release being
  published and the `deploy` job's checkout, the rendered manifests carry
  those commits' non-image changes (chart templates, `deploy/values.yaml`)
  too, not just the two new digests. This is the same skew worth's
  `deploy.yml` accepts for the same reason: `deploy/` is meant to track
  `main`, and the alternative (checking out the tag) would render a chart
  that Argo CD then has to reconcile against a `main` that has already
  moved past it.
- **No `ghcr.io/luqmanbello/securo-backend` or `-frontend` package exists
  yet.** As of this commit there is no release that has pushed either
  image under this fork's GHCR namespace, so the first `deploy` job run
  both creates them and writes their digests. GHCR packages are private by
  default on creation — after that first release, someone needs to check
  (or set) the packages' visibility and confirm `ghcr-pull` is provisioned
  before Argo CD's sync can pull either image.

## Known chart limitations (not worked around here)

- **Postgres/redis storage is not configurable via values.** Both
  StatefulSets (`charts/securo/templates/postgresql/statefulset.yaml`,
  `.../redis/statefulset.yaml`) hardcode their `volumeClaimTemplates` —
  8Gi/RWO for postgres, 2Gi/RWO for redis, no `storageClassName` field at
  all. Unlike `persistence.attachments` (which does support
  `storageClass`/`size`/`accessMode` from values), there is no equivalent
  knob here. `deploy/values.yaml` does **not** set a `postgresql.storage*`
  or `redis.storage*` value, because the chart would silently ignore it —
  a value that does nothing is worse than no value. RWO is satisfied by
  the hardcoding; storage class falls to the cluster's default
  StorageClass, which is `local-path` on every sibling app's PVC in this
  estate, but that assumption is carried over from `worth`, not verified
  against this cluster from where this was written. Making postgres storage
  configurable (mirroring the `persistence.attachments` pattern) is a
  follow-up to the chart itself, out of scope for this directory.
- **`FRONTEND_URL`'s scheme follows `global.tls`, currently `false`.** If
  the `homelab`/`local` Gateway listener this app's `HTTPRoute` targets
  terminates TLS, this should be `true` instead — left `false` here to
  match worth's plain-HTTP-behind-the-gateway pattern, not verified against
  the actual listener config from where this was written.

## The Postgres password is not a secret, and the network policy is what defends it

`charts/securo/templates/postgresql/statefulset.yaml` sets `POSTGRES_USER` and
`POSTGRES_PASSWORD` as literal `value:` fields — both `postgres` — not from any
Secret. They are therefore published constants in a public repository, and they
cannot be rotated without patching the chart.

So `securo-postgres` wraps a value that is not secret, and **the only thing
protecting the database is network reach.** That database is about to hold
roughly 90 days of bank transaction history which cannot be re-fetched, so the
fence matters more than it would for a cache.

`deploy/ciliumnetworkpolicy.yaml` is that fence. Verified against the rendered
manifests on 2026-08-25 — every selector below matches labels the pods actually
carry, which is the failure mode that matters most here: a policy whose selector
matches nothing is worse than no policy, because it looks like protection.

| Endpoint | Ingress allowed from | Port |
|---|---|---|
| `securo-postgresql` | backend, celery-worker, migration Job — **nothing else** | 5432/TCP |
| `securo-redis` | backend, celery-worker, celery-beat | 6379/TCP |
| `securo-backend` | frontend, plus the `host` entity for kubelet probes | 8000/TCP |
| `securo-frontend` | `ingress` (the Gateway's Envoy) and `host` | 8080/TCP |

A CiliumNetworkPolicy with an `ingress:` section makes that endpoint
default-deny for ingress, so anything absent from the table is denied — including
pods in other namespaces and anything else on the cluster.

**celery-beat is deliberately NOT allowed to reach Postgres.** It runs
`celery -A app.worker beat` against an in-config `beat_schedule`, which uses
Celery's default file-based scheduler and needs the broker only. Do not add it
"for symmetry" — it would be a hole with no purpose.

Postgres egress is DNS only; it never initiates an outbound connection.

**This depends on Cilium actually enforcing CiliumNetworkPolicy.** These are
`cilium.io/v2` resources; on a cluster without Cilium they are inert CRDs that
apply cleanly and protect nothing. Confirm enforcement is live before treating
the table above as true — and before connecting a real bank account.

## Agents / LLM (enabled 2026-08-25)

`config.agentsEnabled` and `mcpServer.enabled` are both **true**. This turns on
four things: the agents API on the backend, the `securo-mcp-server` Deployment,
two PVCs (`securo-agent-knowledge`, `securo-agent-embedding-models`), and their
mounts on backend, worker, beat and mcp-server.

**`AGENTS_MCP_JWT_SECRET` must exist in `securo-runtime` BEFORE this is
deployed.** It is not optional and not merely advisable:

- It is the only thing authenticating MCP tool calls. `backend/mcp_server/auth.py`
  verifies an HS256 JWT against it and checks nothing else.
- The tools it guards (`backend/mcp_server/tools/`) read and write transactions,
  accounts, budgets, payees and proposals directly against the database.
- Both its defaults — `change-me-in-production` in `app/agents/config.py` and
  `dev-mcp-secret-change-in-production` in `charts/securo/values.yaml` — are
  published in this repository.

`_assert_mcp_jwt_secret_is_usable` in `backend/app/main.py` refuses to start on
either default, or on anything under 32 characters, whenever `AGENTS_ENABLED` is
true. So deploying without the Secret is a **CrashLoopBackOff, not a silent
hole** — which is the intended failure, but it does mean the order is strict:
Secret first, then push. Deploying first takes the app down.

With agents off, the guard does not fire at all, so this adds nothing to
deployments that do not use the feature.

### What is deliberately NOT exposed

The chart welds a public `/mcp` HTTPRoute rule to `mcpServer.enabled` with no
separate toggle. `kustomization.yaml` removes it. The in-app agent uses the
in-cluster `AGENTS_BUILTIN_MCP_URL`; the public URL is for external MCP clients
(Claude Desktop, n8n), which nothing here uses. Restoring it is a decision, and
`ciliumnetworkpolicy.yaml` must grow `fromEntities: [ingress]` on the
`securo-mcp-server` policy in the same change or the route will exist and
silently fail.

The release workflow asserts the rendered route has exactly two rules with
`/mcp` second, and that the removal patch is still present — because the patch
is positional (`/spec/rules/1`) and a reordered chart would otherwise delete
the frontend catch-all instead, taking the UI off the Gateway with every pod
still Healthy.

### The OpenRouter key is not here

It is entered in the UI and stored per connection as
`LlmConnection.api_key_encrypted`, encrypted with `SECRET_KEY` — the same
protection as the bank credential. It must never be added to `values.yaml`,
the ConfigMap, or any Secret.

Embeddings stay `native` (fastembed, in-process): OpenRouter has no
`/v1/embeddings` endpoint. The ~120MB ONNX model downloads to the
embedding-models PVC on first knowledge-base use and costs nothing until then.

## Release tags: `+securo.N`, never `-securo-N`

Fork releases are tagged `v<upstream version>+securo.<n>` — `v0.15.0+securo.2`,
not `v0.15.0-securo-1`. The separator is load-bearing.

`release.yml` injects the tag verbatim as `VITE_APP_VERSION`, so the tag *is*
the version the UI reports. `frontend/src/hooks/use-latest-release.ts` polls
**upstream's** releases (`securo-finance/securo`) and
`frontend/src/lib/semver.ts` compares the two.

Under semver, everything after `-` is a **prerelease** and sorts *below* the
plain version, while everything after `+` is **build metadata** and is ignored
for precedence entirely. So `0.15.0-securo-1` reads as "an early draft of
0.15.0", upstream's plain `0.15.0` always sorts higher, and the Server update
dialog announced "New version available: v0.15.0" while running exactly that
upstream code — on every load, with an upgrade command that does not even apply
to this deployment. `0.15.0+securo.2` compares equal to `0.15.0`, so the dialog
stays quiet, and a genuine `0.15.1` or `0.16.0` still raises it.

`frontend/src/lib/semver.test.ts` pins all three behaviours, including the old
dash format, so the regression cannot return silently.

Checked, not assumed: `helm package --version 0.15.0+securo.2` keeps the `+` in
the archive filename, so `build-helm`'s `helm push securo-$VERSION.tgz` still
finds it. OCI tags cannot hold a `+`, and Helm rewrites it to `_` when pushing —
that is Helm's own behaviour, not something this repo configures.
