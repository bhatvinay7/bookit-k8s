# Bookit GitOps operations guide

This repository is the desired-state repository for Bookit. Application source,
Docker builds, and GitHub Actions live in `bhatvinay7/bookit`; Argo CD reads only
this repository.

## Promotion model

| Application branch | GitHub environment | GitOps branch | Overlay | Target |
|---|---|---|---|---|
| `dev` | `development` | `dev` | `apps/overlays/dev` | development clusters |
| `main` | `production` | `main` | `apps/overlays/prod` | production clusters |

Never merge an automated image update from `dev` directly into production.
Promote application code through a reviewed `dev -> main` pull request. The
`Promote Tested Images to Production` workflow accepts a successfully built dev
commit, resolves its image tags to immutable GHCR digests, and opens a GitOps
pull request against `main`. Production never rebuilds or consumes a floating
`latest` tag.

Protect `bookit-k8s/main` in GitHub Settings -> Branches (or Rulesets) with:

- require a pull request before merging;
- require at least one approval and dismiss stale approvals;
- require the `Render dev and production overlays` status check;
- require conversations to be resolved;
- block force pushes and deletions;
- restrict direct pushes to `main`, including GitHub Actions;
- require signed commits if every authorized maintainer supports them.

The promotion workflow pushes only `promote/*` branches. Branch protection is a
GitHub repository setting and cannot be enforced by YAML stored in this repo.

## Repository layout

```text
apps/base/                              shared stateless workloads
apps/overlays/dev/                     development configuration
apps/regions/dev/us-east/              dev regional app and ciphertext
apps/regions/prod/us-east/             prod US regional app and ciphertext
apps/regions/prod/eu-west/             prod EU regional app and ciphertext
infra/base/                             shared cluster services
infra/overlays/dev/                    development infrastructure
infra/overlays/prod/                   production infrastructure
argocd/dev-apps.yaml                   Argo CD dev ApplicationSets
argocd/prod-apps.yaml                  Argo CD prod ApplicationSets
scripts/seal-cluster-secrets.sh        cluster-specific secret sealing
```

The environment overlay owns image tags and environment configuration. A
regional overlay adds `BOOKIT_REGION` and the SealedSecret ciphertext encrypted
for that cluster.

## GitHub environments and secrets

Create two GitHub environments in the **bookit application repository**:
`development` and `production`. Production should require reviewers. Use the
same secret names in each environment but different values.

Required repository or environment secrets:

- `GITOPS_REPO`: normally `bhatvinay7/bookit-k8s`.
- `GITOPS_TOKEN`: fine-grained token with Contents read/write on only the
  GitOps repository.
- `KUBECONFIG`: base64 or plaintext multi-context kubeconfig containing only
  the clusters for that environment.
- `GHCR_PULL_TOKEN`: read-only package token used to produce `ghcr-secret`.
- `DATABASE_URL`, `REPLICATION_URL`, `MONGODB_URL`, `REDIS_URL`,
  `RABBITMQ_URL`, `GRPC_SERVER_URL`.
- `SLOT_NAME`, `PUBLICATION`, `INVITATION_ENCRYPTION_KEY`.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURE`, `SMTP_USER`, `SMTP_PASS`,
  `SMTP_FROM`.
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
- `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`,
  `CLOUDINARY_API_SECRET`.
- `CLOUDFLARE_R2_ACCOUNT_ID`, `CLOUDFLARE_R2_ACCESS_KEY_ID`,
  `CLOUDFLARE_R2_SECRET_ACCESS_KEY`, `CLOUDFLARE_R2_ENDPOINT`,
  `CLOUDFLARE_R2_BUCKET`, `CLOUDFLARE_R2_PUBLIC_URL`.
- `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_SOCKET_URL` and the remaining frontend
  build-time secrets used by `.github/workflows/ci-cd.yaml`.

Required GitHub environment variables:

| Environment | `DEPLOY_REGIONS` | `KUBE_CONTEXTS` |
|---|---|---|
| development | `us-east` | `bookit-dev-us-east` |
| production | `us-east,eu-west` | `bookit-prod-us-east,bookit-prod-eu-west` |

The two lists are positional and must have equal lengths. Context names must
exactly match `kubectl config get-contexts -o name` in that environment's
`KUBECONFIG`.

Do not use one kubeconfig across development and production. Use service
accounts with the minimum bootstrap permissions, short-lived credentials where
the provider supports them, and GitHub environment approval for production.

## Sealed Secrets lifecycle

Each cluster runs its own Sealed Secrets controller and therefore owns a
different encryption key. The CI workflow calls
`scripts/seal-cluster-secrets.sh` once per kubeconfig context. It writes only
encrypted `SealedSecret` resources under that cluster's regional overlay.

Bootstrap order for a new cluster:

1. Add the cluster context to the correct environment kubeconfig.
2. Install Argo CD and the Sealed Secrets controller.
3. Wait for `sealed-secrets-controller` to become Ready.
4. Add a regional overlay and an ApplicationSet generator element.
5. Add the matching region and context to the GitHub environment variables.
6. Run the application CI workflow to generate and commit ciphertext.
7. Confirm `backend-secrets`, `frontend-secrets`, `bookit-secrets`, and
   `ghcr-secret` exist in namespace `bookit`.
8. Sync the regional Argo CD application.

Ciphertext is safe to store in Git, but controller private keys are not. Back up
each controller key to a restricted secret manager. Secret rotation means
updating the GitHub environment secret and rerunning CI. Never commit a
plaintext Kubernetes Secret, `.env`, or kubeconfig.

## Argo CD and cluster registration

The current dev cluster uses the in-cluster API URL. Production defines an
in-cluster US endpoint and an example EU endpoint in `argocd/prod-apps.yaml`.
Replace example URLs with real Argo CD cluster registrations before bootstrap:

```bash
argocd cluster add bookit-prod-us-east --name prod-cluster-us-east
argocd cluster add bookit-prod-eu-west --name prod-cluster-eu-west
argocd cluster list
```

The names, URLs, ApplicationSet elements, and kubeconfig contexts are separate
identifiers; verify all four. Prefer one Argo CD control plane per environment.
If a single production Argo CD manages both regions, make the control plane HA
and ensure a regional outage does not prevent recovery of the surviving region.

Automated prune and self-heal are enabled. Production changes should still be
protected by the GitHub environment review and branch protection on `main`.

## Database topology, synchronization, and backups

Treat multi-region application deployment and multi-region database deployment
as separate designs. Kubernetes database operators provide HA inside one
cluster; applying identical custom resources to two clusters does **not** create
cross-region replication.

Recommended production topology:

- PostgreSQL: one write region, synchronous replicas only within that region,
  and asynchronous physical/logical replication to the standby region. Promote
  through an explicit runbook to prevent split brain.
- MongoDB: use a replica set spanning supported failure domains only when
  latency and operator support have been validated; otherwise use one primary
  region plus backup/restore or a managed multi-region service.
- Redis: keep caches regional. Do not use cache replication as authoritative
  data synchronization.
- RabbitMQ: keep queues regional and use federation/shovel only for explicitly
  designed event flows. Do not stretch a normal quorum queue across high-latency
  regions.

`infra/features/operator-db/multi-region/postgres-operator.yaml` and
`mongodb-operator.yaml` are optional database custom resources, not operator
installers. The entire `infra/features/operator-db` package is excluded from the
default base so managed cloud URLs do not accidentally compete with in-cluster
databases.
Install compatible CRDs/operators first and pin supported versions. Values such
as `${CLOUDFLARE_R2_ENDPOINT}` inside a Kubernetes manifest are not expanded by
Kustomize; environment overlays must patch the non-secret endpoint and bucket
fields before enabling operator-managed R2 backups. Credentials remain in the
cluster-specific `bookit-secrets` SealedSecret. S3 tools receive AWS-named
aliases derived from the canonical `CLOUDFLARE_R2_*` GitHub secrets.

`db-backup-cronjob.yaml` runs `pg_dump`/`mongodump` into a pod-local shared
volume and uploads completed artifacts to Cloudflare R2 with rclone. This is a
logical off-cluster copy, but it is not sufficient by itself. Do not consider
database backup complete until all of these pass:

- encrypted upload to an environment-specific, versioned R2 bucket;
- separate prefixes for environment, cluster, database, and timestamp;
- retention and object-lock policy independent of the cluster;
- weekly full plus daily incremental/differential schedule where supported;
- backup success/failure metrics and alerts;
- documented credentials with write-only backup and read-only restore roles;
- scheduled restore tests into an isolated cluster;
- measured RPO and RTO, recorded after every recovery exercise.

Avoid running duplicate logical backup schedules against the same primary from
every region. Elect one backup source and keep a second provider-native snapshot
policy if available.

### Optional custom database HA manifests

`infra/base/custom-db-ha` is now a renderable, optional Kustomize package. It is
not referenced by `infra/base/kustomization.yaml`, so normal dev/prod sync does
not create these databases. Before enabling it, seal a `custom-db-ha-secrets`
Secret in the target `bookit` namespace with these keys:

- `mongodb-root-password`, `mongodb-replica-set-key`, `mongodb-exporter-uri`;
- `postgres-admin-password`, `postgres-password`, `repmgr-password`,
  `postgres-exporter-dsn`, `pgpool-admin-password`;
- `rabbitmq-erlang-cookie`, `rabbitmq-username`, `rabbitmq-password`;
- `redis-password`.

Then render and server-dry-run it against a disposable cluster:

```bash
kubectl kustomize infra/base/custom-db-ha >/tmp/custom-db-ha.yaml
kubectl apply --server-side --dry-run=server -f /tmp/custom-db-ha.yaml
```

Activate it only through a dedicated environment/region overlay after storage
classes, anti-affinity, disruption budgets, backups, restore testing, and
resource capacity have been approved. These StatefulSets provide in-cluster
replication; they do not implement cross-region database synchronization.

## Global load balancing and failover

ExternalDNS publishes Kubernetes ingress/service addresses; it does not provide
health-checked global traffic steering. Put a managed global load balancer or
authoritative DNS health-check service in front of regional ingress endpoints.

Use these pools:

- development: `dev.boookit.shop` -> dev US ingress only;
- production primary: `boookit.shop`, `api.boookit.shop`,
  `ws.boookit.shop`, `grpc.boookit.shop` -> US pool;
- production failover: the same hostnames -> EU pool after health checks fail.

Use `/health` or `/readyz` endpoints that test required dependencies without
performing writes. WebSocket and gRPC health checks need protocol-appropriate
configuration. Keep DNS TTL low enough for the target RTO but not so low that
resolver traffic becomes excessive. Database writer promotion must happen
before directing write traffic to the standby region.

The checked-in ExternalDNS manifest is currently configured for AWS-compatible
DNS and contains no credentials. Select exactly one DNS provider, seal its
credentials per environment, give each cluster a unique TXT owner ID, and test
record ownership before enabling deletion (`policy=sync`).

## Metrics, logs, traces, and alerting

Metrics Server remains in `infra/base` because the application defines CPU
HPAs. It supplies the Kubernetes Resource Metrics API and `kubectl top`; it does
not replace Prometheus. If the Kubernetes provider already installs Metrics
Server, remove this repository's copy from the relevant infrastructure overlay
to avoid duplicate APIService ownership.

The observability stack consists of Prometheus/Grafana, OpenTelemetry, Tempo,
Loki, and application ServiceMonitors. Before production rollout:

- use environment/region labels on every signal;
- separate dev and prod alert routes and credentials;
- use persistent object storage and retention appropriate to the environment;
- alert on Argo sync failure, pod availability, HPA saturation, certificate
  expiry, backup failure, replication lag, queue depth, and regional ingress;
- build SLOs for availability, latency, error rate, and booking success;
- verify dashboards contain Bookit metrics—the legacy monitoring ConfigMap
  still contains old `chess_*` examples and must not be treated as complete.

## Deployment and recovery checklist

Development:

1. Push application changes to `dev`.
2. Confirm validation and image builds succeed.
3. Confirm the GitOps commit lands on `bookit-k8s/dev` only.
4. Confirm ciphertext was sealed against the dev context.
5. Wait for the dev Argo applications to become Healthy and Synced.
6. Run smoke, booking, WebSocket, gRPC, and rollback tests.

Production:

1. Open and approve the application `dev -> main` PR.
2. Run `Promote Tested Images to Production` with the full tested dev SHA and
   only the services built at that SHA (or `all` after a full dev build).
3. Enter `PROMOTE-PRODUCTION` and approve the GitHub `production` environment.
4. Review the generated `bookit-k8s/main` PR and its resolved image digests.
5. Merge only after the GitOps render check and required approval pass.
6. Sync the secondary region first and run smoke tests.
7. Sync the primary region and observe SLO/error-budget dashboards.
8. Enable or update global traffic only after regional health is proven.
9. Record the source SHA, image digests, GitOps commit, database migration, and
   rollback point.

Recovery must be rehearsed. A regional exercise is successful only when the
database role, application traffic, secrets, queues, monitoring, and rollback
are all verified—not merely when pods are Running.

## Validation commands

Run before every GitOps PR:

```bash
kubectl kustomize apps/regions/dev/us-east >/dev/null
kubectl kustomize apps/regions/prod/us-east >/dev/null
kubectl kustomize apps/regions/prod/eu-west >/dev/null
kubectl kustomize infra/overlays/dev >/dev/null
kubectl kustomize infra/overlays/prod >/dev/null
kubectl apply --dry-run=client -f argocd/dev-apps.yaml
kubectl apply --dry-run=client -f argocd/prod-apps.yaml
```

For cluster readiness, also run `kubectl top nodes`, `kubectl get hpa -A`,
`kubectl get applications,applicationsets -n argocd`, and a restore test. A
successful manifest render proves syntax and composition, not runtime safety.
