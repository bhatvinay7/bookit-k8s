# Load Testing, Benchmarking, and Observability Guide

This comprehensive guide outlines the test cases, tool stacks, and step-by-step execution strategies to benchmark your high-throughput gRPC, Redis, and event-driven backend engine, and capture performance metrics using OpenTelemetry.

---

## 🛠️ 1. The Load Testing Tool Stack

To properly generate realistic load across your specific technological layers, you require tools that natively support high-concurrency protocols:

### gRPC Gateways & Internal Engines
*   **Ghz:** A dedicated, ultra-fast CLI benchmarking tool written in Go specifically for gRPC. It handles high concurrency and multiplexing over HTTP/2 with minimal resource overhead.
*   **k6 (by Grafana):** A highly flexible tool that supports native gRPC modules, allowing you to write complex, multi-stage user behavior scripts in JavaScript.

### Redis Layers
*   **redis-benchmark:** The native utility built directly into Redis. It lets you simulate thousands of concurrent clients running pipelined commands, complex `ZSET` transactions, or custom Lua scripts.

---

## 🧪 2. Test Cases Framework

### Test Case 1: The "Peak Spike" Test (Autoscaling Validation)
*   **Objective:** Force the infrastructure to autoscale by triggering resource limits.
*   **Method:** Ramp to a defined request rate over one minute, then hold that rate for at least five minutes. A short burst completes before the HPA control loop can add and ready a pod, so it is not an autoscaling test.
*   **Metrics to Track:** Horizontal Pod Autoscaler (HPA) reaction time, pod initialization cold-start overhead, and error rates during container replication.

### Test Case 2: The "Race Condition & Lock" Test (Data Consistency)
*   **Objective:** Validate that concurrent updates to the same resource do not create race conditions or dirty reads.
*   **Method:** Pace concurrent `LockSlot` calls for one isolated Bookit schedule/seat. The test starts with the seat free, verifies one lock remains after each stage, releases it, and verifies it can be acquired again before the next stage.
*   **Metrics to Track:** gRPC transport errors, Redis Lua latency, distributed lock acquisition/conflict behaviour, cleanup correctness, and final seat availability.

### Test Case 3: The "Endurance Soak" Test (Memory Leak Detection)
*   **Objective:** Ensure long-term stability and catch memory/resource leaks within Rust Actix/DashMap registries or Node.js workers.
*   **Method:** Maintain a steady, unyielding load profile (e.g., 2,000 requests per second) for an extended period of 2 to 4 hours.
*   **Metrics to Track:** Heap memory trendlines, active file descriptors, thread counts, and WebSocket channel closure rates.

---

## 📊 3. Execution Blueprint & Commands

### Step A: Bombarding gRPC Infrastructure with Ghz
Run the following script from your terminal to stream load directly against your exposed Protobuf endpoints:

```bash
ghz --insecure \
  --proto=../apps/gateway-keeper/proto/locking.proto \
  --call=locking.SlotLockingService.LockSlot \
  --data='{"showtime_id":123, "seat_ids":[456], "user_id":999, "total_seat_count":100, "seat_indices":[0]}' \
  --connections=100 \
  --concurrency=1000 \
  --rps=50 \
  --max-duration=10m \
  --total=50000 \
  --cpus=4 \
  grpc.dev.bookit4u.shop:443
```

Use only a disposable development schedule/seat and follow the run with
`locking.SlotLockingService.UnlockSlot` using the same payload. The rate and
duration keep the workload sustained long enough to observe HPA behaviour;
`--total` prevents the test from exceeding its approved request budget, while
`--max-duration` fails it if that budget cannot complete in the approved time.

### Step B: Stress-Testing the Redis Infrastructure (development only)
Isolate the caching and queuing pipeline layer to uncover memory or network limitations:

```bash
redis-benchmark -h localhost -p 6379 -c 100 -n 100000 -t set,get,lpush
```

Never run this direct benchmark against production Redis. Application scenarios
already exercise the production Redis path without adding unrelated synthetic
writes.

### Step C: OpenTelemetry (OTel) & Distributed Tracing Analysis
As load flows through the system, inspect your distributed tracing visualization interface (Jaeger, Zipkin, or Grafana Tempo):
1.  **Trace Context Propagation:** Confirm that your `trace_id` seamlessly crosses from the gRPC edge gateway, through RabbitMQ event logs, down into background worker consumers.
2.  **Span Bottleneck Analysis:** Pinpoint long horizontal span bars. If database operations take 40ms but the full gRPC span logs 200ms, investigate network latency or internal engine channel blockages.

---

## 📝 4. Metrics Documentation for Your Resume

Condense your experimental findings into high-impact, quantitative metrics to highlight on your portfolio or resume:

*   *“Architected and load-tested a high-throughput gRPC framework using Ghz, validating engine stability under peak spikes exceeding 10,000+ RPS.”*
*   *“Integrated OpenTelemetry distributed tracing across microservices to isolate and resolve deep infrastructure bottlenecks, decreasing p99 response times by 35%.”*
*   *“Configured Kubernetes HPA thresholds based on custom system metrics, allowing backend infrastructure to dynamically scale from 2 to 15 pods during high-concurrency event loops.”*

---

## 5. Automated manual test pipeline

The application repository provides **Manual Load Test** in GitHub Actions. It is
deliberately `workflow_dispatch` only: a load test is never started by a normal
commit or pull request. Run it from `deployment1` for development, or from
`main` for production with `confirm_production=RUN-LOAD-TEST`.

The workflow uses the existing environment-scoped `NEXT_PUBLIC_API_URL`,
`GRPC_SERVER_URL` (or `GATEWAY_KEEPER_GRPC_URL`) and `KUBECONFIG` secrets. It
does not create, print, or copy any application credential. Redis benchmarking
runs inside an existing `redis-ha` pod, where its password is already injected;
the password never reaches the Actions log.

| GitHub Action test case | Work performed | Safety boundary |
| --- | --- | --- |
| `peak-spike` | k6 ramps from zero to the selected **target RPS** in one minute, holds it for the selected duration, and ramps down for one minute. It writes a machine-readable summary. | Read-only `GET` only. Use a safe real endpoint and set its exact expected HTTP status/body marker; `/health` is only a connectivity check. |
| `race-lock` | Ghz sends a sustained gRPC request rate to one supplied isolated test seat, captures a JSON report, checks that every request completed with gRPC `OK`, and always releases the lock. | Development only. It requires a valid `grpc_payload` for a disposable schedule, seat and user. Never use a real event seat. |
| `single-seat-lock-ramp` | Runs bounded gRPC lock-attempt stages of **5,000 → 15,000 → 20,000 → 30,000 → 50,000 → 600,000**, rate-limited to fill the selected 5m+ stage duration. It verifies gRPC transport results and lock cleanup after every stage, opens WebSocket observers, and uploads one Ghz JSON report per stage. | Development only. It validates and releases the supplied seat before every stage and performs a final unlock even when a stage fails. |
| `endurance-soak` | k6 holds a constant target request rate for the selected duration (up to the 5-hour Action limit) and writes a machine-readable summary. | Read-only `GET` only. Start with a low RPS and increase only after reviewing the Grafana data. |

`max_vus` and `target_rps` each accept `1` through `5000`; the defaults are
250 VUs and 50 RPS. `max_vus` is a client ceiling, while `target_rps` is the
offered load being measured. The workflow accepts only a sustained duration of
5m through 4h, validates the branch and production confirmation before it
contacts the cluster, and blocks the separate direct Redis benchmark outside
`deployment1`. The k6 thresholds fail the run if five percent or more requests
fail, p95 exceeds two seconds, fewer than 99% of checks match the expected
status/body, or the runner cannot sustain the scenario.

The staged single-seat ramp uses **request totals**, not 600,000 simultaneous
connections. Its `lock_ramp_concurrency` is independently bounded to 5,000 and
each total is rate-limited across the selected stage duration (ten minutes by
default). This gives HPA time to observe resource utilisation and create ready
pods before the stage completes.
This matters for correctness: after the first successful lock, Gateway Keeper's
per-seat admission gate rejects conflicting requests before they enter RabbitMQ,
Redis, Lock Server or WebSocket fan-out. Therefore the 600,000-attempt stage
proves gateway contention handling, latency and error behavior; it must not be
interpreted as 600,000 Lock Server mutations. The workflow still keeps
WebSocket clients subscribed and records all services that do receive accepted
lock/unlock events. To load Lock Server and WebSocket at that volume, use a
pool of isolated free seats/shows or a separately approved synthetic-event test.

### Autoscaling and Redis topology

Every stateless Bookit deployment that serves HTTP, gRPC, WebSocket, search or
queue work has an `autoscaling/v2` HPA with a minimum of one replica. CPU (70%)
and memory (80%) are measured relative to each container's resource requests.
The ingress-nginx controller and the stateless `redis-proxy` also have HPAs.
The Kubernetes `Ingress` and `LoadBalancer` objects are routing configuration,
not workloads; their controller is the component that scales.

Redis members are intentionally **not** HPA targets. A primary/replica set needs
a controlled, quorum-safe membership change, not a CPU-driven replica change.
The chart defaults to three Redis members and two proxy replicas. Set
`redis.replicas` to either `3` or `5` through the chart values and roll it out as
a planned maintenance operation; the HAProxy configuration is rendered from the
same value. The Mongo CDC worker is also kept at one replica because multiple
readers would race on the same shared change-stream resume token.

### Grafana: `testLoad`

Grafana provisions **testLoad → Bookit Load Test** automatically. Select the
time range of the workflow run to see:

- application request rate, error rate and p95 latency from OpenTelemetry span
  metrics;
- ingress request throughput and status codes;
- HPA current versus desired replicas and pod CPU utilisation relative to
  requests;
- Redis command rate, connected clients and used memory.

For `single-seat-lock-ramp`, the CI workflow also pushes non-secret stage
summaries to the in-cluster Pushgateway. Select its GitHub Actions run ID in
the dashboard's `run_id` selector to display each requested/completed stage,
target versus observed Ghz RPS, stage duration and WebSocket observer count
alongside the service telemetry, including each gRPC status result distribution.
Gateway Keeper RED metrics are also split by `k8s.pod.name`, so the dashboard
can demonstrate traffic reaching multiple ready replicas. The workflow saves
15-second HPA, pod, resource-usage and Gateway Keeper EndpointSlice snapshots
along with raw Ghz/k6 JSON reports as an Actions artifact.

The Redis exporter is scraped through a `ServiceMonitor`. Metrics remain in
Prometheus at its configured retention, so a test run is comparable with later
runs without retaining a test's raw credentials or request bodies.
