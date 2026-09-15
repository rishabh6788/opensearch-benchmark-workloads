# k8s_logs

An OpenSearch Benchmark workload of synthetically generated Kubernetes application
logs. The corpus and query set mirror the ClickHouse / OpenObserve benchmark in
`openobserve-clickhouse-benchmark` so results can be compared across engines.

- **Documents:** 500,000,000
- **Uncompressed corpus size:** 1,118,961,329,246 bytes (~1.02 TiB)
- **Index mapping:** mirrors the ClickHouse `default.k8s_logs` table — `_timestamp`
  is a `long` holding microseconds since epoch; `LowCardinality`/`String` columns
  become `keyword`; `message` is full-text (`match_only_text` where available).

## Corpus

The corpus is the NDJSON produced by the `datagen` `file` target
(`benchmark-data --target file --file-out k8s_logs-documents.json`). Point the
workload at it with:

```
--workload-params '{"document_file":"k8s_logs-documents.json","base_url":"<url-or-empty-for-local>"}'
```

`document-count` and `uncompressed-bytes` default to the values above; override
via `document_count` / `document_uncompressed_size_in_bytes` if you regenerate a
different-sized corpus. Set `document_compressed_size_in_bytes` (and a `.bz2`
`document_file`) when serving a compressed corpus.

## Test procedures

| Name | Default | Description |
| --- | --- | --- |
| `k8s_logs` | yes | Index the corpus, force-merge, then run the DSL query set (q0–q10 plus top-100 `*-rows` variants). |
| `ppl` | no | Index the corpus, then run the equivalent PPL query set. Requires the SQL/PPL plugin. |

## Query set (from `queries/queries.json`)

| id | Operation | Intent |
| --- | --- | --- |
| q0 | `q0-trace-id` | Single `trace_id` lookup |
| q1 | `q1-span-id` | Single `span_id` lookup |
| q2 | `q2-rare-token` | Rare whole-token in `message` |
| q3 | `q3-common-token` | Common token (`failed`) in `message` |
| q4 | `q4-container-and-trace-id` | Container + `trace_id` |
| q5 | `q5-container-and-token` | Container + rare token |
| q6 | `q6-double-token` | Two tokens (common AND rare) |
| q7 | `q7-pod-name` | High-cardinality `pod_name` equality |
| q8 | `q8-histogram-1h` | Events-over-time, 1h buckets |
| q9 | `q9-top-namespaces` | Top-N namespaces by volume |
| q10 | `q10-filtered-histogram-1h` | Token filter + 1h histogram |

q0–q7 also have `*-rows` operations returning the top 100 rows sorted by
`_timestamp` desc. q8/q10 bucket on the numeric `_timestamp` with a
`histogram`/`span` interval of 3,600,000,000 µs (1 hour). Query anchors
(`trace_id`, `span_id`, tokens, container, pod, time range) are `--workload-params`
overridable; defaults match `queries.json`.

## Common parameters

`number_of_shards` (1), `number_of_replicas` (0), `bulk_size` (500),
`bulk_indexing_clients` (8), `search_clients` (1), `warmup_iterations` (200),
`test_iterations` (100), `target_throughput` (2), `refresh_interval` (10s).
