# Rust Kubernetes Lock Load Test

The only supported Bookit load test is **Manual Rust Lock Load Test** in GitHub
Actions. It is `workflow_dispatch` only and can run only from the `deployment1`
branch against the development cluster.

## What the pipeline does

1. Builds `bookit-load-test-runner` for the exact Git commit and pushes the
   immutable image to GHCR.
2. Creates a temporary, non-secret ConfigMap containing the supplied isolated
   schedule/seat payload and load profile.
3. Creates one Kubernetes Job from the suspended
   `bookit-rust-lock-load` CronJob template.
4. Runs the Rust/Tokio gRPC client inside the `bookit` namespace against
   `gateway-keeper.bookit.svc.cluster.local:50052`.
5. Collects Job logs, Job/Pod YAML, HPA/pod/EndpointSlice snapshots, and
   publishes non-secret result metrics to Pushgateway for Grafana.
6. Deletes the temporary ConfigMap. The completed Job is retained for 24 hours
   for diagnosis, then Kubernetes removes it.

The CronJob is permanently `suspend: true`; it does not run on a schedule. The
GitHub workflow is the only normal trigger.

## Manual inputs

| Input | Meaning | Limit |
| --- | --- | --- |
| `duration` | Sustained test duration | 5m–4h |
| `target_rps` | Global lock attempts per second | 1–5,000 |
| `grpc_payload` | Valid `LockSlot` JSON for exactly one isolated, free development seat | Required |
| `lock_concurrency` | Independent gRPC connections/workers in the Job | 1–5,000 |
| `rust_runner_threads` | Tokio runtime threads | 1–32 |

The runner uses a global rate scheduler. It sends at most
`target_rps × duration_seconds` requests, rather than allowing every worker to
send unbounded traffic. Each worker owns its own gRPC connection, allowing the
normal Kubernetes `ClusterIP` Gateway Keeper Service to distribute connections
across ready replicas.

Use a disposable schedule and seat. The runner verifies that the seat is free
before the test, issues a final unlock, and fails the Job if no lock succeeds,
any gRPC transport error occurs, or the generator cannot maintain its configured
rate without dropping work.

## Results

Grafana **testLoad → Bookit Load Test** records the selected GitHub run ID's:

- requested, accepted, and conflict lock requests;
- gRPC transport errors and generator drops;
- observed versus configured RPS;
- Gateway Keeper request rate per pod, HPA replicas, CPU/memory, and Redis
  metrics.

The workflow artifact contains the runner's final JSON summary, pod/job YAML,
and 15-second HPA, pod, resource-usage, and EndpointSlice snapshots.

This is a backend Service-level test. It intentionally exercises the
load-balanced Gateway Keeper Service; it is not an external NGINX/Ingress load
test.
