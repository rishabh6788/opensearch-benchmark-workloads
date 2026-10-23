# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.

import copy
import logging
import random

from osbenchmark.workload.params import ParamSource

from .spangen import TraceGenerator


def register(registry):
    registry.register_param_source("otel-span-bulk-param-source", OtelSpanBulkParamSource)


class OtelSpanBulkParamSource(ParamSource):
    """Bulk-indexes synthetic OpenTelemetry spans in Data Prepper's raw-span
    schema, so the trace-analytics ingest path can be benchmarked without
    standing up a collector or a Data Prepper pipeline.

    Document generation lives in spangen.py; this class only adapts it to the
    ParamSource contract.
    """

    def __init__(self, workload, params, **kwargs):
        super().__init__(workload, params, **kwargs)
        logging.getLogger(__name__).info("Workload: [%s], params: [%s]", workload, params)
        self._bulk_size = params.get("bulk-size", 1000)
        self._index_name = params.get("index_name", "otel-v1-apm-span")
        self._scenario = params.get("scenario", "basic")
        self._service_name = params.get("service_name", "otelgen")
        self._fanout = params.get("fanout", 100)
        self._time_spread_s = params.get("time_spread_s", 3600)
        self._error_rate = params.get("error_rate", 0.1)
        self._faithful = params.get("faithful", False)
        self._clock_mode = params.get("clock_mode", "realtime")
        self._ingest_lag_ms = params.get("ingest_lag_ms", 2000)
        self._lag_jitter_ms = params.get("lag_jitter_ms", 500)
        self._client_ip_cardinality = params.get("client_ip_cardinality", 100_000)
        self._peer_ip_cardinality = params.get("peer_ip_cardinality", 10_000)
        self._route_cardinality = params.get("route_cardinality", 200)
        self._duration_median_ms = params.get("duration_median_ms", 15)
        self._duration_sigma = params.get("duration_sigma", 1.1)
        self._seed = params.get("seed", 42)
        self._generator = self._build_generator(self._seed)

        spans_per_trace = self._generator.spans_per_trace
        if self._bulk_size % spans_per_trace:
            logging.getLogger(__name__).warning(
                "bulk-size [%d] is not a multiple of spans-per-trace [%d] for scenario [%s]; "
                "each bulk request will be rounded up to a whole number of traces. "
                "Set bulk-size to a multiple of %d to keep request sizes exact.",
                self._bulk_size, spans_per_trace, self._scenario, spans_per_trace)

    def _build_generator(self, seed):
        return TraceGenerator(
            rng=random.Random(seed),
            scenario=self._scenario,
            service_name=self._service_name,
            fanout=self._fanout,
            time_spread_s=self._time_spread_s,
            error_rate=self._error_rate,
            faithful=self._faithful,
            instance_id=f"benchmark-instance-{seed}",
            clock_mode=self._clock_mode,
            ingest_lag_ms=self._ingest_lag_ms,
            lag_jitter_ms=self._lag_jitter_ms,
            client_ip_cardinality=self._client_ip_cardinality,
            peer_ip_cardinality=self._peer_ip_cardinality,
            route_cardinality=self._route_cardinality,
            duration_median_ms=self._duration_median_ms,
            duration_sigma=self._duration_sigma,
        )

    def partition(self, partition_index, total_partitions):
        """Return a per-partition copy seeded distinctly.

        Unlike vectorsearch's RandomBulkParamSource, this must not return self:
        a seeded RNG shared across clients would make every client emit an
        identical attribute stream. Trace and span IDs come from a CSPRNG in
        spangen, so they stay unique regardless of the seed.
        """
        source = copy.copy(self)
        # pylint: disable=protected-access
        source._generator = self._build_generator(self._seed + partition_index)
        return source

    def params(self):
        docs = self._generator.docs_for_bulk(self._bulk_size)
        metadata = {"_index": self._index_name}

        bulk_data = []
        for doc in docs:
            bulk_data.append({"create": metadata})
            bulk_data.append(doc)

        return {
            "body": bulk_data,
            # Report the real document count: a trace is an indivisible group
            # of spans_per_trace documents, so the requested bulk-size can only
            # be rounded up to a whole number of traces.
            "bulk-size": len(docs),
            "action-metadata-present": True,
            "unit": "docs",
            "index": self._index_name,
            "type": "",
        }
