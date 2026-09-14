# Distributed tracing coverage

The shared Rust SDK is initialized by all nine backend services. It exports
traces and metrics through OTLP gRPC to the collector on port 4317. Next.js uses
`@vercel/otel` in `apps/web/src/instrumentation.ts` and OTLP HTTP/protobuf on 4318.
The Rust SDK/exporter/tracing bridge versions remain compatible; this change
does not upgrade the entire OpenTelemetry stack.

| Path | Instrumentation |
| --- | --- |
| HTTP gateway, HTTP server, search HTTP, WebSocket handshake | INFO server spans, W3C parent extraction, templated route, method, status and elapsed time; 5xx marks the span as an error |
| Gateway → HTTP server | Client span and W3C header injection |
| Gateway → search gRPC; WebSocket → gateway gRPC | Client and server spans, metadata propagation; failed RPCs produce error logs |
| WebSocket messages | Separate span for each recognized message; downstream locking RPCs are children |
| Gateway actor → RabbitMQ → lock worker | Actor preserves request context, producer injects AMQP headers, consumer restores parent |
| HTTP → PostgreSQL outbox → publisher → payment → outbox → notification | `_trace_context` persists with each outbox payload; each publish/consumer span continues the trace through AMQP headers |
| MongoDB CDC → Redis Stream → search | A trace starts for each change-stream event; its W3C context accompanies the Redis Stream payload |
| Next.js server | Framework server/fetch spans and JSON completion/error logs |

Rust tracing events inside an instrumented operation include top-level
`service_name`, `trace_id`, and `span_id`. Background startup logs without an
active span do not invent trace IDs. The formatter reads the event's span from
the subscriber registry; calling `Span::current()` inside a formatter is
reentrant and cannot reliably retrieve context.

Tempo stores traces, Loki stores logs, and Prometheus stores metrics. Fluent Bit
ships container logs directly to Loki with namespace/container/pod labels.
Grafana's trace-to-log link searches the trace ID across the `default` namespace,
so errors in child spans are included. A trace with no error logs will only show
its normal logs. The reverse Loki link resolves the JSON trace ID to Tempo.

The collector's spanmetrics connector emits request/error counts and duration
histograms. The **Bookit Distributed Tracing** dashboard shows server/consumer
rate, failed operations, and p50/p95/p99 latency by service and span name.
Client/internal spans are excluded from those charts to avoid double-counting
an inbound request within a service.

## Coverage limits

- HTTP elapsed time ends when response headers are returned, not when a streaming
  response finishes transferring. Gateway proxy spans include its buffered body.
- Browser user interactions are not instrumented. A browser request starts a
  server trace unless it supplies valid W3C headers. WebSocket messages start
  independent server traces; the message protocol has no browser trace carrier.
- Individual SQL/MongoDB/Redis operations are not automatically instrumented.
  External payment/PDF operations have selected client spans, not universal
  outbound-client instrumentation.
- CDC cannot reconstruct the original database writer's request context. Redis
  Pub/Sub broadcasts and background seat-layout maintenance do not carry trace
  context. They are not an end-to-end continuation of the originating request.
- Sampling follows the SDK's parent-based policy and environment configuration.
  Unsampled requests can have log IDs without a retained Tempo trace; span-derived
  metrics describe received spans rather than an independent exact traffic count.
- Export queues are bounded and in memory. Abrupt process/collector shutdown can
  lose buffered telemetry; application-wide graceful shutdown/flush is not added.
- New storage uses a 10 GiB Tempo PVC for both blocks and WAL. Existing `/tmp`
  traces are not migrated. This is a single-replica store, not an HA deployment.

## Validation and release

```sh
cargo test -p bookit-telemetry
cargo check --workspace
cargo clippy --workspace -- -D warnings
npm run check-types --workspace=web
```

The telemetry regression tests use an in-memory exporter and captured JSON logs.
They verify concurrent request isolation, incoming and outgoing W3C context,
server duration/error status, gRPC parentage, durable carrier restoration, and
matching log IDs for root and child spans. CI runs these tests and rebuilds all
Rust services when the shared telemetry package changes.

Application images and the separate `bookit-k8s` repository must both be released.
GitOps config alone cannot instrument old application images. See
`bookit-k8s/docs/observability.md` in that repository for live verification.
Local compile/render checks do not prove production ingestion or resolve a
runtime Loki connection error without cluster evidence.

References: [Next.js OpenTelemetry](https://nextjs.org/docs/app/guides/open-telemetry),
[collector 0.90 spanmetrics configuration](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.90.0/connector/spanmetricsconnector),
[Fluent Bit 2.2 Loki output](https://docs.fluentbit.io/manual/2.2/pipeline/outputs/loki).
