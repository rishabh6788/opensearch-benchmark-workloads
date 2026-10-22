# otel_traces

Benchmarks the OpenSearch trace-analytics ingest path using synthetic
OpenTelemetry spans, generated in-process by a custom param source. There is no
corpus to download, and no OpenTelemetry collector or Data Prepper pipeline is
needed — the workload writes the documents Data Prepper's `otel_traces`
processor would have produced.

## Document schema

Documents match Data Prepper's `otel-v1-apm-span` schema. `indices/otel-v1-apm-span.json`
is lifted from Data Prepper's own
`data-prepper-plugins/opensearch/src/main/resources/index-template/otel-v1-apm-span-index-template.json`,
including the two dynamic templates that map `resource.attributes.*` and
`span.attributes.*` to `keyword`.

Attribute keys follow Data Prepper's flattening rule: the prefix keeps its dots
and dots inside the attribute name become `@`, so OpenTelemetry's `service.name`
is indexed as `resource.attributes.service@name`.

## Trace shapes

Shapes are ports of the scenarios in [krzko/otelgen](https://github.com/krzko/otelgen)
(`internal/traces/scenarios/`). Traces are always bulk-indexed whole, because
`traceGroupFields` is derived from the root span and would otherwise be
inconsistent across requests.

| `scenario` | Spans per trace | Cardinality |
|---|---|---|
| `basic` | 3 (`basic` → `ping` → `pong`) | Low; only `network.peer.address` varies |
| `microservices` | `fanout + 1` (default 101) | High; randomized client/peer IPs, routes, methods, status codes, service names, versions, instance IDs, 2 events per span |

Measured distinct values and per-document coverage (8 seeds, ~96k docs,
`microservices` at `fanout: 100`):

| Field | Distinct | Coverage |
|---|---|---|
| `client.address` | up to `client_ip_cardinality` | 100% |
| `network.peer.address` | ~`peer_ip_cardinality` | 99% |
| `http.route` | exactly `route_cardinality` | 88% |
| `http.request.method` | 5, weighted GET-heavy | 88% |
| `http.response.status_code` | 8, correlated with span status | 88% |

One client address is drawn per **trace** and propagated to all its spans, the
way an APM pipeline enriches it. So the distinct count you observe is bounded
by the number of traces generated, not by the pool: a 96k-document sample at
`fanout: 100` contains only ~950 traces and therefore ~950 addresses. Over
500k draws the configured 100k pool is fully exercised.

HTTP attributes are omitted for `payment_service` (gRPC) and `cache_service`
(Redis), which is why coverage is 88% rather than 100%; those spans carry
`rpc.*` and `db.*` attributes instead.

Use `basic` for raw ingest throughput and `microservices` to exercise field
cardinality, nested `events`, and mixed span statuses.

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `span_index_name` | `otel-v1-apm-span` | Target index |
| `span_scenario` | `basic` | `basic` or `microservices` |
| `span_service_name` | `otelgen` | Value of `resource.attributes.service@name` |
| `span_fanout` | `100` | Child spans per trace (`microservices` only) |
| `span_error_rate` | `0.1` | Fraction of child spans with `status.code: 2` |
| `span_client_ip_cardinality` | `100000` | Distinct `client.address` values, from `100.64.0.0/10` |
| `span_peer_ip_cardinality` | `10000` | Distinct `network.peer.address` values, from `10.0.0.0/8` |
| `span_route_cardinality` | `200` | Distinct `http.route` templates |
| `span_faithful` | `false` | Reproduce otelgen's quirks instead of correcting them (see below) |
| `span_seed` | `42` | RNG seed; each client uses `seed + partition_index` |
| `span_index_bulk_size` | `999` | Documents per bulk request |

Near-realtime (`span-nrt`):

| Parameter | Default | Description |
|---|---|---|
| `span_ingest_lag_ms` | `2000` | How far behind wall clock a trace's last span ends |
| `span_lag_jitter_ms` | `500` | Jitter applied to that lag |
| `nrt_target_throughput` | `20` | **Bulk requests** per second; docs/sec is this times `span_index_bulk_size` |
| `nrt_indexing_clients` | `8` | Bulk indexing clients |
| `nrt_warmup_time_period` | `60` | Warmup seconds |
| `nrt_time_period` | `900` | Measurement seconds |

Backfill (`span-backfill`):

| Parameter | Default | Description |
|---|---|---|
| `span_clock_mode` | `spread` | `spread` scatters timestamps; `realtime` anchors them near now |
| `span_time_spread_s` | `86400` | Window to scatter timestamps over |
| `indexing_clients` | `8` | Bulk indexing clients |
| `index_iterations` | `1000` | Bulk requests per client |
| `index_target_throughput` | unset | Leave unset to run at max throughput |

Keep `span_index_bulk_size` a multiple of the scenario's spans-per-trace (3 or
`fanout + 1`); otherwise each request rounds up to a whole number of traces and
the param source logs a warning. The default of `999` is a multiple of 3.

## otelgen fidelity

By default the generator **corrects** two bugs in otelgen rather than
reproducing them:

- otelgen never calls `WithSpanKind`, so every span it emits is
  `SPAN_KIND_INTERNAL` and the intended kind is stranded in a `span.kind`
  attribute. This workload sets real `SPAN_KIND_CLIENT`/`SPAN_KIND_SERVER`
  values.
- otelgen attaches `service.version` and `service.instance.id` to every span,
  though both are resource-level semantic conventions. This workload puts them
  in `resource.attributes.*`.

Set `span_faithful: true` to reproduce otelgen's behaviour exactly, which is
what you want when diffing against documents from a real otelgen → Data Prepper
pipeline.

## Test procedures

| Procedure | Clock | Pacing | Use |
|---|---|---|---|
| `span-nrt` (default) | `realtime` | `target-throughput` for `time-period` | Steady-state near-realtime ingest |
| `span-backfill` | `spread` | max throughput, `iterations` | Build a historical corpus |
| `span-backfill-then-nrt` | both | backfill, then steady rate | Measure NRT on top of a real corpus |
| `span-backfill-with-merge` | `spread` | max throughput, then force-merge | Backfill and consolidate segments |

`realtime` anchors each trace on its **end** time, a jittered `ingest_lag_ms`
behind now, so timestamps advance monotonically with the run and never land in
the future — a `microservices` trace covers several seconds of simulated work,
so anchoring on the start time would push its last spans past wall clock.

Monotonic timestamps also mean each new segment's `startTime`/`endTime` range
does not overlap older segments, so time-range queries can prune by segment.
`spread` destroys that property, which is realistic for a backfill but not for
steady-state measurement.

## Running

```sh
opensearch-benchmark execute-test \
  --target-hosts localhost:9200 \
  --workload-path ./otel_traces \
  --pipeline benchmark-only \
  --test-mode
```

High-cardinality variant:

```sh
opensearch-benchmark execute-test \
  --target-hosts localhost:9200 \
  --workload-path ./otel_traces \
  --pipeline benchmark-only \
  --workload-params span_scenario:microservices,span_index_bulk_size:1010,nrt_indexing_clients:16
```

## Sizing

Measured on the generator, with the default cardinalities:

| Scenario | JSON bytes/doc | Compressed `_source` floor | Generation rate |
|---|---|---|---|
| `basic` | 836 | ~52 B/doc | 122k docs/s/core |
| `microservices` | 1460 | ~85 B/doc | 78k docs/s/core |

The compressed figure is DEFLATE over 60 KB blocks, approximating how Lucene
stores `_source`. Treat it strictly as a **floor on total index size**: it
excludes doc values, postings, and the terms dictionary, which for a schema with
this many `keyword` fields is often larger than `_source` itself. Do not size a
cluster from it.

Generation rates are for the generator alone, excluding JSON serialisation and
HTTP, so budget roughly 3-5x that cost per client in practice and expect to need
`--worker-ips` to spread load generators across hosts beyond ~200k docs/s.

### Calibrating for an index-size target

Per-document on-disk size depends on cardinality, shard count, replicas, and
codec, so measure it rather than predict it:

```sh
opensearch-benchmark execute-test \
  --target-hosts <host>:9200 --workload-path ./otel_traces \
  --pipeline benchmark-only --test-procedure span-backfill \
  --workload-params span_scenario:microservices,index_iterations:50000

curl -s '<host>:9200/_cat/indices/otel-v1-apm-span?v&bytes=b&h=docs.count,pri.store.size'
```

Divide to get bytes/doc, then scale to the target. Calibrate with at least a few
tens of millions of documents and after a `refresh`, so merges have settled.

Raising `span_client_ip_cardinality`, `span_peer_ip_cardinality` and
`span_route_cardinality` all increase bytes/doc. `basic` compresses far harder
than `microservices`, so it needs many more documents to reach the same size on
disk — prefer `microservices` for index-size targets, and `basic` only for raw
ingest-rate tests.

At multi-terabyte scale a single static index is the wrong shape: size shards
at 30-50 GB, which puts 100 TB at roughly 2000 shards. That needs rollover or
date-named indices, which this workload does not yet do — it writes to one
index named by `span_index_name`.

During a long backfill, set `span_index_refresh_interval` high (or `-1`) and
`target_index_replica_shards: 0`, then restore both before the NRT phase.

`spangen.py` has no OpenSearch Benchmark dependency, so the generator can be
exercised on its own:

```sh
python -c "
import random, json
from otel_traces.spangen import TraceGenerator
g = TraceGenerator(rng=random.Random(42), scenario='basic')
print(json.dumps(g.docs_for_bulk(3)[1], indent=2))
"
```
