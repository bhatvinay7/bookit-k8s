# Verify traces, logs, storage and latency

Argo CD manages the manifests; Grafana queries Loki/Tempo directly. Application
pods live in `bookit`, monitoring pods in `monitoring`, and the Helm Application
objects in `argocd`.

The development Grafana host is `https://grafana.dev.bookit4u.shop`; production
uses `https://grafana.bookit4u.shop`. Check the rendered Ingress for the environment
before using a host. Argo CD's UI is separate from Grafana.

## Release and readiness

Release the application SDK changes and these GitOps changes together. CI must
build the new service images and update their image references before Argo sync.
Sync the infra application, `loki-stack`, `kube-prometheus-stack`, and application
workloads. The collector/Fluent Bit pod revision annotations trigger rollout for
this pipeline change. Later inline ConfigMap edits require a pod rollout too.

```sh
kubectl -n monitoring get pods,svc,pvc
kubectl -n monitoring rollout status deployment/otel-collector
kubectl -n monitoring rollout status deployment/tempo
kubectl -n monitoring rollout status daemonset/fluent-bit
kubectl -n monitoring get endpointslice -l kubernetes.io/service-name=loki-stack
kubectl -n monitoring logs deployment/otel-collector --since=10m --tail=100
kubectl -n monitoring logs daemonset/fluent-bit --since=10m --tail=100
```

The `tempo-data` PVC and Loki's StatefulSet PVC must be **Bound**. Both use
`do-block-storage`. Tempo blocks and WAL reside at `/var/tempo` on a 10 GiB PVC;
trace retention is 360 hours. Loki has a separate 10 GiB PVC. Retention does not
guarantee that the volume has room for 15 days at every traffic rate. Old Tempo
data from `/tmp` is not automatically migrated when the pod is replaced.

## Grafana links and a controlled request

In Grafana, check **Connections → Data sources**: Tempo UID `tempo`, Loki UID
`loki`, Prometheus UID `prometheus`. Use **Save & test**. Expected internal URLs:

| Datasource | URL |
| --- | --- |
| Tempo | `http://tempo.monitoring.svc.cluster.local:3200` |
| Loki | `http://loki-stack.monitoring.svc.cluster.local:3100` |

If Save & test fails, inspect Grafana logs and Loki endpoints; a query mismatch
does not explain an actual DNS, timeout or connection-refused error:

```sh
kubectl -n monitoring logs deployment/kube-prometheus-stack-grafana -c grafana --since=10m --tail=100
kubectl -n monitoring logs statefulset/loki-stack --since=10m --tail=100
kubectl -n monitoring get networkpolicy
```

Send a safe request with a known fresh W3C context, using your environment's API
URL. `/health` passes through the gateway to the HTTP server:

```sh
trace_id=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
curl -i -H "traceparent: 00-${trace_id}-1234567890abcdef-01" https://api.dev.bookit4u.shop/health
printf '%s\n' "$trace_id"
```

After the export batches arrive, find that trace ID in Tempo Explore. Expect a
gateway server span, gateway client span, and HTTP server span with the same
trace ID and correct parent relationships. Click the span's logs link. It uses:

```logql
{namespace="bookit"} |= "YOUR_TRACE_ID"
```

For errors only, use parsed merged JSON fields:

```logql
{namespace="bookit"} |= "YOUR_TRACE_ID" | json | event_level="ERROR"
```

The automatic link intentionally includes the whole trace, so child-service
errors are visible. A successful health request should not create an error log.
For a real failed request, verify its response status, error span and matching
error log without triggering a payment or booking as a test. Old logs without
trace IDs cannot acquire correlation retroactively.

## Latency and durability

Open **Bookit Distributed Tracing**. It uses
`bookit_traces_spanmetrics_calls_total` and
`bookit_traces_spanmetrics_duration_seconds_bucket`. Allow a few minutes for rate
queries to accumulate samples. Rates and p50/p95/p99 durations are grouped by
service/operation. HTTP duration ends at response headers; consumer duration
covers message processing, not time waiting in the broker.

For a persistence check during an approved maintenance window, record a trace ID
and log line, let them flush, restart the respective pod, and confirm both are
still queryable. A Bound PVC alone is not evidence that a particular trace/log
was stored. Do not delete the PVC during this check.

Local validation:

```sh
python3 scripts/validate-monitoring.py
python3 scripts/test-bootstrap-monitoring.py
```

These render dev/prod overlays and check namespace/service references, datasource
UIDs, correlation regexes, metric names, exporter protocols and PVC mounts.
They do not test cluster networking or actual ingestion. Browser spans, every
database/cache query, and Redis Pub/Sub propagation remain outside the current
application coverage. See the application repository's `TELEMETRY.md`.
