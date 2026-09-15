# Bookit multi-environment GitOps workflow

## Purpose and scope

This document describes how Bookit's application source, container images,
Kubernetes manifests, Argo CD applications, secrets, infrastructure,
observability, autoscaling, databases, and backups fit together.

It is both an implementation guide and a gap analysis. Statements marked
**Current** describe behavior represented by files in this repository.
Statements marked **Required before production** describe controls that are not
fully implemented merely by applying the current YAML.

The most important distinction is:

```text
bookit repository
  application source, tests, image builds, bootstrap and promotion workflows

bookit-k8s repository
  desired Kubernetes state, environment overlays, regional overlays,
  Argo CD ApplicationSets and encrypted SealedSecret resources
```

Argo CD should never deploy directly from the application repository. The
application pipeline builds artifacts and proposes desired-state changes in the
GitOps repository.

---

## 1. End-to-end architecture

### Development path

```text
developer push to bookit/dev
        |
        v
CI validation: lint, types, cargo check, tests
        |
        v
detect changed services
        |
        v
build GHCR images tagged commit-<dev SHA>
        |
        v
checkout bookit-k8s/dev
        |
        +--> update apps/overlays/dev image references
        |
        +--> seal development secrets against each dev cluster key
        |
        v
push GitOps change to bookit-k8s/dev
        |
        v
Argo CD dev ApplicationSet reconciles the dev regional overlay
```

### Production path

```text
review and merge application dev -> main
        |
        v
main validation only; main does not rebuild images
        |
        v
manual "Promote Tested Images to Production"
        |
        +--> full tested dev SHA
        +--> explicit services
        +--> PROMOTE-PRODUCTION confirmation
        +--> GitHub production-environment approval
        |
        v
resolve commit-<SHA> tags to immutable sha256 image digests
        |
        v
create promote/* branch and PR against bookit-k8s/main
        |
        v
GitOps render check + human review + protected-branch merge
        |
        v
Argo CD production ApplicationSet reconciles regional overlays
```

Production therefore consumes the same artifact tested in development. A pod
restart cannot select an unrelated development image because the production
Kustomization records a content digest rather than `latest`.

---

## 2. Git branches, GitHub environments, and concurrency

| Concern | Development | Production |
|---|---|---|
| Application branch | `dev` | `main` |
| GitHub environment | `development` | `production` |
| GitOps branch | `dev` | `main` |
| Application overlay | `apps/overlays/dev` | `apps/overlays/prod` |
| Regional path | `apps/regions/dev/us-east` | `apps/regions/prod/<region>` |
| Argo CD revision | `dev` | `main` |
| Image creation | yes | no; promote tested digest |
| Deployment approval | optional | required reviewers |

Workflow concurrency prevents two operations from racing against the same
environment:

- CI/GitOps update group: `ci-gitops-<branch>`;
- bootstrap group: `bootstrap-argocd-<environment>`;
- production promotion group: `production-promotion`.

Jobs are queued instead of canceling an active deployment. This matters because
canceling between image publication and the GitOps commit can leave a partially
completed release.

---

## 3. Kubernetes manifest organization

### `apps/base`

The application base contains reusable resources:

- Deployments and Services for HTTP, WebSocket, gateway, lock, search, payment,
  notification and web workloads;
- Ingress and gRPC Ingress;
- HorizontalPodAutoscalers;
- application RBAC;
- `ServiceMonitor` discovery;
- `bookit-config` defaults;
- central Kustomize image names.

Base manifests should not contain environment credentials, region-specific DNS
names, cluster URLs, or plaintext Kubernetes Secrets.

### `apps/overlays/dev` and `apps/overlays/prod`

The environment overlays import `apps/base` and modify environment-wide state:

- non-secret values such as `NODE_ENV`;
- development ingress hostnames and TLS secret names;
- labels used to distinguish telemetry and ownership;
- image tags or production image digests written by automation.

### `apps/regions/<environment>/<region>`

Regional overlays import their environment overlay and add:

- a region label;
- the `secrets/` Kustomization containing ciphertext encrypted for exactly that
  cluster's Sealed Secrets controller.

The deployment workflow passes the selected GitHub environment and each
`DEPLOY_REGIONS` entry to `seal-cluster-secrets.sh`. The script derives and
seals `BOOKIT_ENVIRONMENT` and `BOOKIT_REGION`; they are not separately entered
as GitHub secrets and are never stored in a ConfigMap.

Regional overlays are siblings of environment overlays. They cannot live below
the environment directory while importing it because that produces a Kustomize
load cycle.

### `infra/base`

The infrastructure base currently composes:

- Metrics Server;
- RabbitMQ custom HA manifest;
- backup CronJobs;
- Prometheus/Grafana;
- OpenTelemetry Collector;
- Tempo;
- ExternalDNS.

Some resources depend on CRDs that are not installed by the resource itself. A
`ServiceMonitor` or Argo `Application` is valid only after its corresponding
operator/CRD exists.

`infra/features/operator-db` contains the optional `PostgresCluster`,
`PerconaServerMongoDB`, and `RedisFailover` data plane. It is excluded from the
default base. This prevents managed cloud connection URLs from coexisting with
unintended in-cluster databases. Enable it explicitly only after installing its
operators and creating operator-specific credentials such as
`mongodb-operator-users`; the application-facing URLs remain in
`backend-secrets`.

### `infra/overlays/dev` and `infra/overlays/prod`

These overlays currently add environment labels and otherwise import the common
infrastructure base. Production-specific storage, replicas, retention, DNS
ownership, and resource sizing should be expressed as overlay patches rather
than changes to the base.

### Optional `infra/base/custom-db-ha`

This directory is a valid standalone Kustomize package containing manual
StatefulSet-based MongoDB, PostgreSQL, RabbitMQ, and Redis deployments. It is
intentionally not referenced by `infra/base/kustomization.yaml`, so normal Argo
sync does not install it.

Do not deploy both an operator-managed database and its custom StatefulSet
alternative under the same service identity. Choose exactly one data-plane
implementation per environment.

---

## 4. Argo CD application model

`argocd/dev-apps.yaml` defines development ApplicationSets. Application sources
use:

```yaml
targetRevision: dev
path: apps/regions/dev/us-east
```

`argocd/prod-apps.yaml` defines production ApplicationSets. Sources use:

```yaml
targetRevision: main
path: apps/regions/prod/us-east
```

or the equivalent EU path. Infrastructure applications point at
`infra/overlays/dev` or `infra/overlays/prod`.

Automated sync has two important behaviors:

- `selfHeal: true` restores live resources changed outside Git;
- `prune: true` deletes objects removed from the selected Git path.

`CreateNamespace=true` permits Argo CD to create the destination namespace.
Production branch protection is therefore part of the Kubernetes safety model:
a merged deletion can become a cluster deletion through pruning.

### Multi-cluster registration

Argo CD can deploy only to its in-cluster API endpoint or a previously
registered remote cluster. Every generator element must map to a real Argo CD
cluster secret.

For each environment verify:

```bash
argocd cluster list
kubectl get applications,applicationsets -n argocd
```

The workflow kubeconfig context name, kubeconfig cluster name, Kubernetes API
server URL, Argo CD cluster name, and ApplicationSet `server` value are related
but separate configuration fields. Bootstrap validates the first three before
making a cluster write.

---

## 5. Accounts, identities, and RBAC

Several account types participate in deployment.

### GitHub identities

- `GITHUB_TOKEN`: scoped to the application repository and used for normal
  checkout/GHCR operations where permitted.
- `GITOPS_TOKEN`: fine-grained token with contents read/write on
  `bookit-k8s`; production uses it to push a promotion branch and create a PR,
  not to push `main`.
- `GHCR_PULL_TOKEN`: package-read token sealed into `ghcr-secret` for cluster
  image pulls.
- GitHub environments: `development` and `production` isolate secrets and
  variables. Production must require reviewers.

### Kubernetes bootstrap identity

The environment-scoped `KUBECONFIG` contains one or more contexts. Its identity
must have sufficient rights to install Argo CD, CRDs, cluster-scoped RBAC,
ingress and cert-manager. Because this is highly privileged, it should use
short-lived cloud authentication where possible and should not be shared
between dev and prod.

### Workload ServiceAccount

`apps/base/rbac.yaml` creates `bookit-app`, a namespaced Role, and a RoleBinding
that can read Secrets and ConfigMaps.

**Current gap:** application Deployment specs do not presently set
`serviceAccountName: bookit-app`, so pods use the namespace's `default`
ServiceAccount. Also, applications consume Secrets through `envFrom`, which does
not require runtime Kubernetes API read permission. Granting `get/list/watch` on
all namespace Secrets may therefore be unnecessary and broader than needed.

**Required before production:** either assign `bookit-app` where the application
really calls the Kubernetes API and narrow its Role to named resources, or
remove the unused Role/RoleBinding. Disable automatic service-account token
mounting for workloads that do not call Kubernetes.

### Infrastructure ServiceAccounts

- Metrics Server has cluster-scoped RBAC to read node and pod resource metrics
  and publish the aggregated Metrics API.
- ExternalDNS has a ServiceAccount, ClusterRole and ClusterRoleBinding to watch
  Services, Ingresses, Endpoints, Pods and Nodes. Its cloud DNS permissions must
  be supplied separately and restricted to the Bookit hosted zone.
- Operators create their own controllers and workload identities according to
  their installation method.

---

## 6. Secrets and Sealed Secrets

GitHub holds plaintext environment secrets. CI never commits those plaintext
values. For every environment region, it:

1. selects a kubeconfig context;
2. ensures a Sealed Secrets controller exists in that cluster;
3. generates normal Kubernetes Secret YAML in a pipeline;
4. streams it to `kubeseal`;
5. writes encrypted `SealedSecret` YAML beneath the regional `secrets/` path;
6. commits ciphertext to the matching GitOps branch.

Each cluster owns a distinct sealing key. Production US ciphertext cannot be
assumed decryptable in production EU, even when both produce a Secret with the
same name.

Application secrets are divided into:

- `backend-secrets`: database URLs, messaging endpoints, OAuth server keys,
  SMTP, encryption and backend media credentials;
- `frontend-secrets`: public runtime/build configuration and OAuth client data;
- `bookit-secrets`: backup/object-storage credentials;
- `ghcr-secret`: image registry authentication;
- optional `custom-db-ha-secrets`: database replication and administrator
  credentials for manual StatefulSets.

Controller private keys must be backed up outside the cluster. Losing a cluster
and its sealing key prevents existing ciphertext from being decrypted during
recovery.

---

## 7. CI/CD, metrics, and separate-zone deployment

### CI validation

The application CI pipeline runs Node/TypeScript and Rust checks before images
are published. `detect-changes` maps shared package changes to affected services
so the matrix builds only required images.

Every development image receives only an immutable source tag:

```text
ghcr.io/bhatvinay7/bookit-<service>:commit-<short SHA>
```

No `latest`, `dev-latest`, or `main-latest` tag is used. Production promotion
converts the tested tag to:

```text
ghcr.io/bhatvinay7/bookit-<service>@sha256:<manifest digest>
```

The digest pins exact registry content. Production is not affected if a new dev
image is published while production pods restart.

### Zone and region deployment

The current topology models:

- development: US East;
- production: US East and EU West.

An ApplicationSet generator produces one app per regional destination. The same
production digest is normally deployed to both regions; configuration and
ciphertext remain regional.

Separate-zone resilience within one region still requires node topology rules.
**Current gap:** most application Deployments do not define pod anti-affinity,
topology spread constraints, or PodDisruptionBudgets. Three replicas do not
provide zone resilience if the scheduler places them on one node or zone.

**Required before production:** add production overlay policies using
`topology.kubernetes.io/zone`, enforce at least two/three zones where available,
and define PDBs compatible with rolling updates and cluster maintenance.

### Global traffic

ExternalDNS publishes DNS records but does not provide health-checked global
load balancing. A managed global load balancer or DNS health-check service must
route users to healthy regional ingress endpoints. Database writer promotion
must complete before write traffic moves to a standby region.

---

## 8. Metrics, logs, traces, and database-state monitoring

### Resource metrics

Metrics Server exposes `metrics.k8s.io`. HPAs use it to obtain pod CPU relative
to declared CPU requests. It also powers:

```bash
kubectl top nodes
kubectl top pods -n bookit
```

Metrics Server is not historical monitoring. If the cloud provider already
manages it, remove the GitOps copy to avoid duplicate APIService ownership.

### Prometheus and Grafana

The Prometheus/Grafana Argo Application installs `kube-prometheus-stack`, which
collects node, kubelet/container, and Kubernetes object metrics. Applications
export metrics over OTLP. The collector exposes the transformed series on port
`8889`, and the dedicated collector `ServiceMonitor` scrapes it every 15
seconds. Do not add application scrape annotations unless that process really
implements an HTTP `/metrics` endpoint.

### Logs and traces

- Fluent Bit tails CRI container logs on every node, enriches them with
  Kubernetes metadata, and forwards them through the collector to Loki.
- Applications send OTLP data to `otel-collector.monitoring:4317`.
- The collector exports traces to Tempo and exposes transformed metrics for
  Prometheus.
- Grafana configures Tempo as a data source.

**Current durability gap:** Tempo uses `/tmp` local storage and the current Loki
chart values do not describe production object storage. A pod/node replacement
can lose observability history. Production should use persistent/object storage
with separate environment retention policies.

### PostgreSQL replication monitoring

Monitor at minimum:

- primary role and Patroni leader changes;
- replica count and streaming state;
- `pg_stat_replication` write/flush/replay byte lag;
- replay time lag and WAL archive failures;
- replication slot retained WAL size;
- pgBackRest last successful full/differential backup;
- PgBouncer active/waiting connections and saturation.

Alert when a replica disconnects, replay lag exceeds the recovery-point target,
WAL disk use grows abnormally, or no successful backup exists inside the
expected window.

### MongoDB replication monitoring

Monitor:

- replica-set member state (`PRIMARY`, `SECONDARY`, recovering, unhealthy);
- primary election frequency;
- replication/oplog lag;
- oplog window compared with maximum expected outage;
- WiredTiger cache, connections, queues and disk capacity;
- Percona backup custom-resource state and restore status.

Alert if there is no primary, fewer than the required voting members are
healthy, secondary lag exceeds the RPO, or the oplog window becomes too small.

### Redis replication monitoring

Monitor:

- Sentinel quorum and elected master;
- connected replicas and `master_link_status`;
- replication offset/byte lag;
- failover events and failover duration;
- memory, eviction, keyspace hits/misses, persistence status and last RDB save;
- exporter and RedisFailover operator reconciliation errors.

Redis is a regional cache in the preferred architecture. Cross-region cache
state should not be treated as authoritative booking data.

### RabbitMQ monitoring

Monitor quorum/cluster membership, queue depth, unacknowledged messages,
consumer count, publish/delivery rate, disk/memory alarms, connection churn and
federation/shovel lag where used.

---

## 9. Application autoscaling

Each main stateless workload starts with one replica and has an
`autoscaling/v2` HPA targeting 75% average CPU utilization, generally with a
range of one to five replicas.

The control loop is:

```text
container CPU usage
      |
      v
kubelet resource metrics
      |
      v
Metrics Server / metrics.k8s.io
      |
      v
HPA compares usage with container CPU request
      |
      v
Deployment replica count increases or decreases
```

Because utilization is calculated against CPU requests, correct resource
requests are mandatory. A missing request makes CPU-utilization scaling
undefined for that container; an unrealistically small request causes excessive
scaling.

HPA safety considerations:

- readiness probes must prevent new pods receiving traffic too early;
- graceful shutdown and termination periods must protect active bookings,
  WebSockets and message consumers;
- queue workers often scale better from queue depth than CPU;
- WebSocket workloads require connection draining and ingress compatibility;
- scale-down stabilization should prevent oscillation;
- minimum production replicas should normally be at least two per region;
- HPA does not add nodes—Cluster Autoscaler/Karpenter must supply node capacity.

Stateful databases should not be scaled through a normal CPU HPA. Replica
membership and failover must be controlled by the database operator or an
explicit operational procedure.

---

## 10. PostgreSQL primary and read replicas

### Provider-neutral connection secrets

The application always receives provider-neutral URL keys. The value changes by
environment/provider, but application code and Deployment YAML do not:

| Secret key | Managed cloud example | Optional in-cluster example |
|---|---|---|
| `DATABASE_URL` | `postgresql://user:pass@provider-host:5432/bookit?sslmode=require` | `postgresql://user:pass@postgres-proxy.bookit.svc.cluster.local:5432/bookit` |
| `REPLICATION_URL` | provider replication user/endpoint with TLS | primary PostgreSQL service with a dedicated replication user |
| `MONGODB_URL` | `mongodb+srv://user:pass@provider-host/bookit` | `mongodb://user:pass@mongodb-proxy.bookit.svc.cluster.local:27017/bookit?replicaSet=rs0` |
| `REDIS_URL` | `rediss://user:pass@provider-host:6379` | operator-created master/Sentinel-aware service discovered with `kubectl get svc -n bookit` |
| `REDIS_CLUSTER_URL` | managed cluster/configuration endpoint | the same as `REDIS_URL` unless Redis Cluster mode is actually enabled |

Do not copy the examples literally. Confirm the Service names created by the
installed operator, TLS mode, CA requirements, database name and authentication
format. The sealing script validates URL schemes and refuses missing values
without printing credential-bearing URLs.

`MONGODB_URL` is canonical because the Rust MongoDB package reads that name.
CI temporarily accepts the legacy GitHub secret `MONGO_DB_URL` as a fallback,
but generated Secrets expose both names with the same value for compatibility.

For Redis, `REDIS_CLUSTER_URL`, `REDIS_CLUSTER_URLS` and `REDIS_REMOTE_URL` are
optional GitHub secrets. When omitted, sealing safely aliases them to
`REDIS_URL`. Supply different values only when the selected Redis topology and
client mode require them.

Operator-generated credentials are not automatically copied into GitHub. When
using an in-cluster operator, retrieve the generated connection information,
create a least-privilege application user, store its URL in the correct GitHub
environment, and rerun sealing. Managed-cloud URLs follow the same contract.

### Operator-managed design

`infra/features/operator-db/multi-region/postgres-operator.yaml` describes one Crunchy Postgres cluster with three
instances. Patroni coordinates leader election; `synchronous_mode: true`
requests synchronous behavior within that cluster. PgBouncer supplies two proxy
replicas.

The normal endpoints should be separated conceptually:

- write endpoint -> current primary;
- read endpoint -> replicas or a replica-aware service;
- administrative endpoint -> restricted operator/maintenance access.

Applications must not send writes to a read-only endpoint. Transactions that
require read-after-write consistency should read from the primary or use an
explicit consistency mechanism.

### Manual StatefulSet design

`custom-db-ha/postgres-ha.yaml` uses three
`bitnami/postgresql-repmgr` pods and Pgpool. Pod zero is the initial primary;
Repmgr manages membership/failover and Pgpool exposes `postgres-proxy`.
Credentials come from `custom-db-ha-secrets`.

### Cross-region PostgreSQL

Neither applying the operator manifest in two clusters nor applying two manual
StatefulSets creates cross-region synchronization.

Recommended model:

1. choose one production write region;
2. keep synchronous replicas inside its low-latency region;
3. stream asynchronously to a cross-region standby using a supported operator,
   managed database feature, or carefully operated PostgreSQL replication;
4. archive WAL to durable object storage;
5. monitor replay lag and WAL retention;
6. fence the old primary before promotion;
7. promote the standby through an approved runbook;
8. update database endpoints, then global application traffic;
9. rebuild the old primary as a replica before failback.

Multi-primary PostgreSQL is not implied by this repository and should not be
introduced without conflict-resolution semantics at the application level.

---

## 11. MongoDB primary and secondary replicas

### Operator-managed design

`infra/features/operator-db/multi-region/mongodb-operator.yaml` declares a Percona Server for MongoDB replica set named
`rs0` with three members. MongoDB elects one primary; remaining healthy members
serve as secondaries. The connection string should include multiple hosts and
`replicaSet=rs0` so the driver follows elections.

Read preference is an application decision:

- `primary`: strongest read-after-write behavior;
- `primaryPreferred`: allows limited failover reads;
- `secondaryPreferred` or `nearest`: useful for explicitly stale-tolerant
  workloads;
- majority write concern: safer acknowledged writes across failures.

### Manual StatefulSet design

`custom-db-ha/mongodb-ha.yaml` starts pod zero as the initial primary and other
pods as secondaries using the Bitnami replica-set startup mode. The root
password, replica key, and exporter URI come from `custom-db-ha-secrets`.

### Cross-region MongoDB

A replica set may span regions only when network latency, voting topology,
failure domains and operator support are understood. Keep an odd number of
voting members and place a majority where writes must remain available. Avoid a
two-region even split that can lose quorum ambiguously.

For independent Kubernetes clusters, a managed multi-region MongoDB service or
operator-supported external topology is safer than deploying identical
three-member replica sets and assuming they synchronize. Independent replica
sets contain independent data.

---

## 12. Redis master, replicas, and Sentinel

The operator-managed `RedisFailover` requests three Redis pods and three
Sentinels with persistent storage and an exporter. Sentinel observes the master,
reaches quorum, and coordinates replica promotion during failure.

The optional manual manifest also uses three Redis pods plus Sentinel. Pod zero
is the initial master; other ordinals start as replicas. Applications should use
a Sentinel-aware client or an HA proxy/service that discovers the elected
master. A round-robin Service directly across all Redis pods is not a safe write
endpoint because replicas reject writes.

Redis replication is normally asynchronous. A successful write acknowledged by
the old master can be lost if it fails before replication. Booking records and
payments therefore belong in PostgreSQL/MongoDB according to the data model, not
only in Redis.

For multi-region operation:

- deploy independent regional caches;
- warm or invalidate caches from authoritative events;
- use globally unique key namespaces containing the environment;
- do not stretch Sentinel quorum across unreliable WAN links;
- document cold-cache behavior during regional failover.

---

## 13. Database backups and restore

### PostgreSQL

The Crunchy custom resource declares pgBackRest full and differential schedules
with an S3-compatible Cloudflare R2 repository. However, the
`${CLOUDFLARE_R2_ENDPOINT}` and `${CLOUDFLARE_R2_BUCKET}` text inside raw YAML is
not automatically expanded by Kustomize. Environment overlay patches must set
those non-secret fields before relying on it. Credentials remain in the
cluster-specific `bookit-secrets` SealedSecret under canonical
`CLOUDFLARE_R2_*` keys; AWS-named keys are compatibility aliases for S3 clients.

The standalone `postgres-backup` CronJob writes `pg_dump` output to a shared
pod volume. An rclone sidecar waits for the completed marker and uploads the
artifact to the configured Cloudflare R2 bucket under an
environment/region/PostgreSQL prefix.

### MongoDB

The Percona resource declares a daily backup task and S3-compatible storage.
The same endpoint interpolation caveat applies. Confirm the exact secret key
schema required by the installed Percona operator version.

The standalone `mongodb-backup` CronJob follows the same pattern: `mongodump`
writes a compressed archive, then an rclone sidecar uploads it to the
environment/region/MongoDB R2 prefix.

### Redis

The Redis backup CronJob attempts to mount the first Redis PVC and upload
`dump.rdb` to R2. This needs careful verification:

- a ReadWriteOnce PVC may not mount simultaneously on another node;
- reading a live RDB file during replacement may be inconsistent;
- the assumed PVC name depends on operator naming;
- no retention, integrity check or restore test is expressed;
- `amazon/aws-cli` does not itself trigger Redis `BGSAVE`.

Prefer operator-supported backup/snapshot behavior or request a consistent RDB
through Redis and upload a copied artifact.

### Required backup standard

A production backup is complete only when it is restorable. Require:

- separate dev/prod buckets or hard access boundaries;
- region/cluster/database/timestamp prefixes;
- encryption in transit and at rest;
- object versioning, retention and deletion protection;
- least-privilege backup writers and separate restore readers;
- success, duration, size and age metrics;
- alerts for missed schedules and abnormal sizes;
- checksums or provider integrity metadata;
- documented full and point-in-time recovery procedures;
- automated restore drills into an isolated namespace/cluster;
- recorded RPO and RTO results.

Example object layout:

```text
s3://bookit-prod-db-backups/
  postgres/us-east/base/...
  postgres/us-east/wal/...
  mongodb/us-east/...
  redis/us-east/...
```

Do not run duplicate logical dumps from every application region against the
same database primary. Select a backup authority and use database-native
continuous archiving plus provider snapshots where appropriate.

---

## 14. Deployment, failover, and recovery runbooks

### Normal development deployment

1. Push to `bookit/dev`.
2. Confirm CI validation passes.
3. Confirm expected service images exist at `commit-<SHA>`.
4. Confirm only `bookit-k8s/dev` changed.
5. Confirm each dev SealedSecret was encrypted against its destination cluster.
6. Wait for Argo CD Healthy/Synced.
7. Test HTTP, gRPC, WebSocket, booking, payment event and rollback paths.

### Production promotion

1. Identify a fully tested dev commit.
2. Merge reviewed application code to `main`; main validates but does not build.
3. Run the production-promotion workflow with the full dev SHA and service list.
4. Enter `PROMOTE-PRODUCTION`.
5. Obtain GitHub production-environment approval.
6. Verify every resolved SHA-256 digest.
7. Review and merge the generated GitOps PR.
8. Deploy secondary region first where the traffic design permits it.
9. Observe metrics, logs, traces and database state.
10. Deploy the primary region and gradually admit global traffic.

### Database regional failover

1. Declare the incident and freeze automated database topology changes.
2. Determine whether the old primary is reachable; fence it from writes.
3. Measure standby replay/replication position and expected data loss.
4. Promote through the database-specific operator/runbook.
5. verify schema/migrations, credentials and application connectivity.
6. direct a small amount of application traffic to the recovered region.
7. validate writes, reads, CDC, queues and cache invalidation.
8. change global traffic only after the data plane is authoritative.
9. rebuild the failed region as a replica; do not simply reconnect two primaries.
10. record actual RPO/RTO and update alerts/runbooks.

---

## 15. Validation commands

Manifest composition:

```bash
kubectl kustomize apps/regions/dev/us-east >/dev/null
kubectl kustomize apps/regions/prod/us-east >/dev/null
kubectl kustomize apps/regions/prod/eu-west >/dev/null
kubectl kustomize infra/overlays/dev >/dev/null
kubectl kustomize infra/overlays/prod >/dev/null
kubectl kustomize infra/features/operator-db >/dev/null
kubectl kustomize infra/base/custom-db-ha >/dev/null
```

Deployment control plane:

```bash
kubectl config current-context
kubectl cluster-info
kubectl get applications,applicationsets -n argocd
kubectl get sealedsecrets -A
kubectl get crd | grep -E 'postgres|percona|redisfailover|servicemonitor'
```

Application and autoscaling:

```bash
kubectl get deploy,pods,svc,ingress -n bookit
kubectl get hpa -n bookit
kubectl describe hpa -n bookit
kubectl top pods -n bookit
kubectl get apiservice v1beta1.metrics.k8s.io
```

Database health:

```bash
kubectl get postgresclusters -n bookit
kubectl get perconaservermongodbs -n bookit
kubectl get redisfailovers -n bookit
kubectl get cronjobs,jobs -n bookit
kubectl get pvc -n bookit
```

These commands show Kubernetes/operator state. Database-native queries and a
real restore exercise are still required to prove replication and backup
correctness.

---

## 16. Distributed-systems difficulties

Running Bookit in multiple pods, zones, and regions changes failure from a
simple up/down condition into partial and uncertain failure. A service can be
healthy from Kubernetes' perspective while its dependency is unreachable,
slow, partitioned, stale, or accepting requests that cannot safely complete.

### Network partitions and partial failure

A network timeout does not reveal whether the remote operation failed. The
request might not have reached the server, might still be running, or might have
committed successfully while the response was lost.

For a booking request this creates a dangerous sequence:

```text
client -> booking API -> payment/database
                         operation commits
client <- timeout before response arrives
client retries
```

If retries are not idempotent, the same user can be charged or booked twice.
Every externally retryable write should carry a stable idempotency key, store
the operation result transactionally, and return the previous result for a
duplicate key.

Kubernetes readiness probes should distinguish between:

- process liveness: should Kubernetes restart the process?;
- local readiness: can this pod accept new traffic?;
- dependency degradation: can read-only or reduced functionality continue?;
- regional health: should the global load balancer send new users here?

Restarting every pod during a database partition can amplify an outage and is
not a substitute for dependency-aware behavior.

### Consistency, availability, and latency

During a partition, a system cannot guarantee both immediate consistency and
availability for every operation. Bookit should choose per operation:

- seat inventory and confirmed bookings favor consistency;
- search indexes, dashboards and recommendations can tolerate bounded staleness;
- caches favor availability because authoritative state lives elsewhere;
- payment state must be reconciled against the payment provider and ledger.

Cross-region synchronous writes reduce possible data loss but add wide-area
latency and can make the write path unavailable when a region or link fails.
Asynchronous replication improves latency and regional independence but has a
non-zero recovery point: recent acknowledged writes might not yet exist in the
standby region.

RPO and RTO must therefore be business decisions, not accidental database
defaults.

### Split brain and leader fencing

Automatic failover is unsafe if the former database primary can continue
accepting writes. Two writable primaries create divergent histories that are
difficult or impossible to merge safely.

A promotion procedure must include fencing:

1. revoke or block application access to the old writer;
2. confirm quorum/lease ownership;
3. determine the standby's last replayed position;
4. promote exactly one new writer;
5. update discovery/endpoints;
6. rebuild the former writer from the authoritative timeline.

DNS changes alone are not fencing. Existing connections, cached DNS responses,
background workers and direct database URLs can continue writing to the old
region.

### Exactly-once processing is not a transport guarantee

RabbitMQ, Redis streams and most messaging systems normally provide at-least-once
delivery when reliability is enabled. A consumer can finish its database write
and crash before acknowledging the message, causing redelivery.

Consumers must be idempotent. Common patterns include:

- inbox table keyed by event ID;
- unique database constraint on the business operation;
- transactional outbox for publishing events after state changes;
- retry counters and exponential backoff;
- dead-letter queues for poison messages;
- reconciliation jobs for events stuck between systems.

Acknowledging before durable processing risks message loss. Acknowledging after
processing risks duplicates. Application design must handle the duplicate case.

### Ordering and concurrent booking

Messages may be reordered across partitions, queues, retries or regions. Event
timestamps do not provide a safe total order because clocks differ.

Use aggregate identifiers and monotonic versions where order matters. For
example, each show/seat state transition can carry a version, and consumers can
reject an older transition after a newer one has been applied.

Seat allocation requires an atomic authority. Redis locks can reduce contention
but should not be the only proof of ownership. Use bounded lock leases, unique
fencing tokens and a final database constraint/transaction so that a paused
lock holder cannot commit after its lease has expired.

### Distributed transactions and sagas

A booking can touch seat inventory, payment, notification, search and audit
systems. A normal SQL transaction cannot atomically cover all of them.

Model the operation as a state machine or saga:

```text
PENDING -> SEAT_HELD -> PAYMENT_AUTHORIZED -> CONFIRMED
    |             |              |
    +-> EXPIRED   +-> RELEASED    +-> REFUND_REQUIRED
```

Persist every transition, make commands idempotent, define compensating actions
and run reconciliation for states that remain incomplete beyond a deadline.
Notifications should reflect committed state and should never be the authority
for whether a booking succeeded.

### Cache invalidation and stale reads

Regional Redis caches can return stale availability after a booking commits.
Time-to-live alone does not guarantee an acceptable oversell window.

Use authoritative database validation at commit time and publish invalidation
events through an outbox. Cache keys should include environment, city/show and
schema/version components. During event-stream lag, systems should prefer a
database read or explicitly report stale data rather than confirming from cache.

Cache warming after regional failover can produce a thundering herd. Rate-limit
misses, coalesce duplicate loads and introduce traffic gradually.

### Replication lag and read-after-write behavior

Sending reads to replicas improves capacity but can make a user's newly created
booking appear missing. Options include:

- route session-critical reads to the primary for a bounded interval;
- carry a commit position and wait until a replica has replayed it;
- display a pending state backed by the authoritative transaction;
- use replica reads only for stale-tolerant endpoints.

Replication-lag thresholds must be measured in both bytes and time. A replica
with low byte lag can still be operationally stale if writes have stopped or
replay is paused.

### Clock skew and time

Wall-clock timestamps from different nodes are not a reliable ordering
mechanism. NTP reduces skew but does not eliminate it, and clocks can jump.

Use database-generated versions, sequence numbers or logical positions for
ordering. Use monotonic clocks for process-local durations. Expiring seat holds
should have one authoritative time source and tolerate small skew at boundaries.

### Retries, overload, and cascading failure

Retries multiply traffic precisely when dependencies are unhealthy. Unbounded
retries can turn a partial failure into a complete outage.

Clients and services need:

- bounded timeouts shorter than upstream deadlines;
- exponential backoff with jitter;
- maximum retry counts and retry budgets;
- circuit breakers for sustained dependency failure;
- bulkheads/separate concurrency pools for unrelated dependencies;
- admission control and load shedding;
- queue-size limits and backpressure.

Autoscaling is slower than an instantaneous traffic spike and cannot repair a
saturated database. Capacity limits should protect the database rather than
allowing application replicas to create unlimited connections.

### Schema and protocol evolution

During rolling and regional deployments, old and new application versions run
simultaneously. Database migrations and events must be backward/forward
compatible across that window.

Use expand-and-contract migrations:

1. add nullable columns/new tables or fields;
2. deploy code that can read both old and new representations;
3. backfill asynchronously and observably;
4. switch writers/readers;
5. remove old fields only after every region and rollback window has passed.

Event schemas need versions and tolerant readers. Do not rename/remove a field
while queued old events or lagging regional consumers can still deliver it.

### Deployment skew between regions

Argo CD does not guarantee an atomic multi-region rollout. One region can be on
the new version while another remains old or unhealthy.

APIs, database schemas and messages must tolerate version skew. Roll out the
secondary/canary region first, observe it, then move primary traffic. Define a
maximum supported skew duration and an abort/rollback decision before rollout.

### Observability across asynchronous boundaries

A single user action crosses HTTP, gRPC, WebSocket, queue and database
boundaries. Without propagated correlation data, an incident becomes a set of
unrelated logs.

Propagate trace IDs, request IDs, user-safe booking IDs, event IDs and source
region through every protocol. Metrics must include bounded-cardinality labels
such as environment, region, service, operation and result. Never use user IDs,
seat IDs or raw URLs as unrestricted metric labels.

Monitor symptoms and correctness, not only pod health:

- booking success and duplicate-rejection rate;
- payment/booking reconciliation backlog;
- message age and dead-letter count;
- replication lag and primary changes;
- cache invalidation lag;
- backup age and restore-test result;
- regional error rate and global failover state.

### Disaster recovery versus high availability

Three replicas in one cluster provide local availability, not disaster
recovery. Argo CD can recreate manifests but cannot recreate database contents,
Sealed Secrets private keys, external DNS state or object-storage credentials.

Disaster recovery requires copies outside the failed trust/failure domain,
documented bootstrap credentials, tested database restores, controller-key
recovery, DNS control and a sequence that avoids starting applications before
authoritative data and secrets are available.

### Cost and operational complexity

Multi-region systems multiply clusters, controllers, certificates, dashboards,
alerts, backups, network paths and failure combinations. A topology that has not
been exercised tends to fail during the first real incident.

Add complexity only for a measured availability/RTO requirement. Prefer managed
database replication when the team cannot staff continuous operator ownership.
Schedule game days for lost pods, lost nodes, broken DNS, delayed queues,
database replica lag, region isolation, secret-key recovery and restore from
backup.

---

## 17. Production readiness checklist

- [ ] GitHub `production` environment requires reviewers.
- [ ] `bookit-k8s/main` blocks direct pushes and requires GitOps validation.
- [ ] Dev and prod use separate kubeconfigs, endpoints and credentials.
- [ ] Every remote cluster is registered in the correct Argo CD control plane.
- [ ] Sealed Secrets controller keys are backed up securely per cluster.
- [ ] Application ServiceAccount/RBAC is either used and narrowed or removed.
- [ ] Operators and CRDs are installed before their custom resources.
- [ ] Exactly one database implementation is enabled per data service.
- [ ] PostgreSQL write/read endpoints and promotion procedure are tested.
- [ ] MongoDB replica topology, read preference and write concern are tested.
- [ ] Redis clients discover the Sentinel-elected master correctly.
- [ ] Cross-region replication is explicitly configured and lag is alerted.
- [ ] Backups leave the cluster and scheduled restore tests succeed.
- [ ] Production Tempo/Loki/Prometheus data uses durable storage.
- [ ] HPA, node autoscaling, PDBs and topology spread work together.
- [ ] Global load balancer health checks cover HTTP, gRPC and WebSocket paths.
- [ ] Regional failover and failback exercises meet documented RPO/RTO.

The GitOps repository defines desired state, but availability is achieved only
when that state is combined with correctly configured cloud infrastructure,
operators, identity boundaries, monitoring, tested backups and rehearsed
recovery procedures.
