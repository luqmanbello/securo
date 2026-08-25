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

Two Secrets, provisioned imperatively on the cluster — `kubectl apply -f -`
fed over stdin, the same contract as `worth-runtime`, `worth-accessbank`,
and `paperless-runtime`. No generated Secret manifest enters Git.

| Secret | Keys | Consumed by |
|---|---|---|
| `securo-runtime` (namespace `securo`) | `SECRET_KEY` — nothing else | backend, celery-worker, celery-beat, migration Job |
| `securo-postgres` (namespace `securo`) | `DATABASE_URL` | backend, celery-worker, celery-beat, migration Job |
| `securo-openexchangerates` (namespace `securo`) | `OPENEXCHANGERATES_APP_ID` | backend, celery-worker, celery-beat — deliberately NOT the migration Job |
| `ghcr-pull` (namespace `securo`) | (docker-registry secret) | every workload that pulls a `ghcr.io/luqmanbello/securo-*` image — the four Deployments and the migration Job, via `imagePullSecrets` added by `deploy/kustomization.yaml`'s patches (the chart has no `imagePullSecrets` knob at all) |

**`ghcr-pull` is currently NOT required, and the row above is insurance rather
than a dependency.** Verified 2026-08-25 by anonymous token pull against
ghcr.io: both `securo-backend` and `securo-frontend` return HTTP 200 with no
credentials. Packages published by Actions inherit the repository's visibility,
and `luqmanbello/securo` is public.

An earlier version of this section claimed the images land private on first
push, following worth's row of the same name. That was wrong here, and the
error is worth recording: the check used tag `v0.14.4-securo-1`, which does not
exist — `docker/metadata-action` strips the `v`, so the published tags are
`0.14.4-securo-1` and `latest` — and a "not found" was read as "private".

The `imagePullSecrets` patches are kept deliberately. They are inert while the
packages are public (a referenced-but-missing Secret only makes kubelet warn
and fall back to an anonymous pull), and if the repository or its packages are
ever made private the manifests already carry it instead of producing an
`ImagePullBackOff` with no stated cause.

`deploy/values.yaml` sets `global.existingSecret: securo-runtime`, which
disables `charts/securo/templates/common/secret.yaml` (the chart's own
generated Secret) entirely — backend, worker, and beat all read from
`securo-runtime` instead, through the exact same `envFrom`/`secretRef`
branch the chart already has in each of their Deployments (and in the
migration Job). No chart feature was invented to make this work.

**Three objects, not one, and the split is about rotation cost.** The chart
offers a single `global.existingSecret` knob, and setting it disables the
chart's own generated Secret entirely — so by default everything would have to
live in one object. `deploy/kustomization.yaml` patches a second and third
`secretRef` onto the consumers instead, using the same `envFrom` list the
chart already renders. No chart feature was invented.

The reason is that these three credentials have incompatible rotation stories:

- `SECRET_KEY` must **never** change. It signs every session token and derives
  the Fernet key that encrypts the stored bank credential, so rotating it
  strands that credential and forces a manual reconnect through the UI.
- `DATABASE_URL` is touched by routine operations — a Postgres password
  rotation, or a move to an external database.
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
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic securo-postgres -n securo \
  --from-literal=DATABASE_URL="postgresql+asyncpg://postgres:postgres@securo-postgresql:5432/securo" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic securo-openexchangerates -n securo \
  --from-literal=OPENEXCHANGERATES_APP_ID="<App ID from the vault item>" \
  --dry-run=client -o yaml | kubectl apply -f -
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
