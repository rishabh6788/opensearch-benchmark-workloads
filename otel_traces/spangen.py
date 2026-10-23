# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.

"""Synthetic OpenTelemetry span documents in the Data Prepper raw-span schema.

Generates the documents that Data Prepper's ``otel_traces`` processor writes to
the ``otel-v1-apm-span`` index, without running a collector or an OTLP pipeline.
The trace shapes are ports of the scenarios in krzko/otelgen
(``internal/traces/scenarios/``).

This module has no OpenSearch Benchmark dependency so it can be exercised and
unit tested standalone; ``workload.py`` wraps it in a ParamSource.
"""

import math
import secrets
import time
from dataclasses import dataclass, field

MS = 1_000_000  # nanoseconds per millisecond

# Hardcoded placeholders from otelgen's basic scenario (basic.go:15-19).
FAKE_IP = "1.2.3.4"
FAKE_NS = "Demo"
FAKE_VER = "1.2.3"
FAKE_CLIENT_IP = "203.0.113.195"
FAKE_ROUTE = "/api/v1/order"

# otelgen's microservices scenario service list (microservices.go:16-21).
SERVICES = [
    "api_gateway", "auth_service", "user_service", "product_service",
    "inventory_service", "order_service", "payment_service", "shipping_service",
    "notification_service", "recommendation_service", "search_service",
    "analytics_service", "logging_service", "cache_service", "config_service",
    "monitoring_service",
]

# Services whose spans carry non-HTTP semantics, so they get no http.* attributes.
NON_HTTP_SERVICES = frozenset({"payment_service", "cache_service"})

# IP pools. Client addresses come from RFC 6598 shared address space, which is
# what NAT'd client traffic plausibly looks like; peers come from RFC 1918
# private space, as service-to-service calls do.
CLIENT_IP_BASE = (100 << 24) | (64 << 16)  # 100.64.0.0/10
CLIENT_IP_RANGE = 1 << 22
PEER_IP_BASE = 10 << 24                    # 10.0.0.0/8
PEER_IP_RANGE = 1 << 24

ROUTE_SHAPES = (
    "/api/v1/{resource}",
    "/api/v1/{resource}/{{id}}",
    "/api/v1/{resource}/search",
    "/api/v2/{resource}",
    "/api/v2/{resource}/{{id}}/items",
    "/internal/{resource}/health",
)
ROUTE_RESOURCES = (
    "order", "user", "product", "inventory", "payment", "shipment",
    "cart", "invoice", "session", "notification",
)


def _weighted_pool(pairs):
    """Expand (value, weight) pairs into a flat tuple for O(1) sampling.

    Cheaper per draw than rng.choices() at the rates this generator runs at.
    """
    pool = []
    for value, weight in pairs:
        pool.extend([value] * weight)
    return tuple(pool)


HTTP_METHOD_POOL = _weighted_pool(
    (("GET", 60), ("POST", 25), ("PUT", 8), ("DELETE", 4), ("PATCH", 3)))
OK_STATUS_POOL = _weighted_pool(((200, 85), (201, 10), (204, 5)))
ERROR_STATUS_POOL = _weighted_pool(
    ((500, 50), (503, 20), (502, 10), (400, 10), (404, 10)))


def format_ip(value):
    return f"{(value >> 24) & 255}.{(value >> 16) & 255}.{(value >> 8) & 255}.{value & 255}"

# OTLP status codes: 0 unset, 1 ok, 2 error.
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

SCENARIOS = ("basic", "microservices")

# realtime: anchor every trace just behind wall-clock now, as a live pipeline
#           would. Timestamps advance monotonically with the run.
# spread:   scatter traces uniformly over the trailing time_spread_s. Use for
#           backfilling a historical corpus, not for steady-state measurement.
CLOCK_MODES = ("realtime", "spread")


def new_trace_id():
    """128-bit trace ID. Uses a CSPRNG rather than the seeded RNG so that
    parallel clients never produce colliding IDs."""
    return secrets.token_hex(16)


def new_span_id():
    """64-bit span ID. CSPRNG, for the same reason as new_trace_id()."""
    return secrets.token_hex(8)


def iso8601(nanos):
    """Format unix nanoseconds the way Data Prepper does, at the nanosecond
    precision the index template's date_nanos fields expect."""
    secs, rem = divmod(nanos, 1_000_000_000)
    return f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(secs))}.{rem:09d}Z"


def flatten(prefix, attrs):
    """Data Prepper keeps the prefix dots and replaces dots *inside* the
    attribute key with '@': service.name -> resource.attributes.service@name
    """
    return {f"{prefix}.{key.replace('.', '@')}": value for key, value in attrs.items()}


@dataclass
class Span:
    name: str
    kind: str
    start: int
    end: int
    span_id: str = field(default_factory=new_span_id)
    parent_span_id: str = ""
    status_code: int = STATUS_UNSET
    status_message: str = ""
    attributes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)


class TraceGenerator:
    """Builds whole traces and renders them as raw-span documents.

    Traces are always emitted whole: a span document is only meaningful
    alongside its siblings, because traceGroupFields is derived from the root
    span and would otherwise be inconsistent across bulk requests.
    """

    def __init__(self, rng, scenario="basic", service_name="otelgen",
                 fanout=100, time_spread_s=3600, error_rate=0.1,
                 faithful=False, instance_id="benchmark-instance",
                 clock_mode="realtime", ingest_lag_ms=2000, lag_jitter_ms=500,
                 client_ip_cardinality=100_000, peer_ip_cardinality=10_000,
                 route_cardinality=200, duration_median_ms=15, duration_sigma=1.1):
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}, expected one of {SCENARIOS}")
        if clock_mode not in CLOCK_MODES:
            raise ValueError(f"unknown clock_mode {clock_mode!r}, expected one of {CLOCK_MODES}")
        if not 1 <= client_ip_cardinality <= CLIENT_IP_RANGE:
            raise ValueError(
                f"client_ip_cardinality must be in 1..{CLIENT_IP_RANGE} (size of 100.64.0.0/10)")
        if not 1 <= peer_ip_cardinality <= PEER_IP_RANGE:
            raise ValueError(
                f"peer_ip_cardinality must be in 1..{PEER_IP_RANGE} (size of 10.0.0.0/8)")
        if route_cardinality < 1:
            raise ValueError("route_cardinality must be >= 1")
        if duration_median_ms <= 0:
            raise ValueError("duration_median_ms must be > 0")
        if duration_sigma <= 0:
            raise ValueError("duration_sigma must be > 0")
        self._rng = rng
        self._scenario = scenario
        self._service_name = service_name
        self._fanout = fanout
        self._time_spread_s = time_spread_s
        self._error_rate = error_rate
        self._faithful = faithful
        self._instance_id = instance_id
        self._clock_mode = clock_mode
        self._ingest_lag_ms = ingest_lag_ms
        self._lag_jitter_ms = lag_jitter_ms
        self._client_ip_cardinality = client_ip_cardinality
        self._peer_ip_cardinality = peer_ip_cardinality
        self._route_cardinality = route_cardinality
        self._duration_median_ms = duration_median_ms
        self._duration_sigma = duration_sigma

    def _duration(self):
        """A span's duration, in nanoseconds.

        otelgen sleeps rand.Intn(100) whole milliseconds. That quantises every
        duration to 1 ms (only 100 distinct values), emits ~1% zero-length
        spans, and -- summed over `fanout` children -- gives roots a
        near-constant duration with no latency tail at all, so p50 == p99.

        Outside faithful mode durations are log-normal, which is how real span
        latencies are distributed: a tight body with a long right tail.
        """
        if self._faithful:
            return self._rng.randrange(100) * MS
        mu = math.log(self._duration_median_ms * MS)
        # Floor at 1us so no span is zero-length.
        return max(int(self._rng.lognormvariate(mu, self._duration_sigma)), 1_000)

    def _client_ip(self):
        if self._faithful:
            return FAKE_CLIENT_IP
        return format_ip(CLIENT_IP_BASE + self._rng.randrange(self._client_ip_cardinality))

    def _peer_ip(self):
        if self._faithful:
            return FAKE_IP
        return format_ip(PEER_IP_BASE + self._rng.randrange(self._peer_ip_cardinality))

    def _route(self):
        """Derive a route template from a single draw, so route_cardinality is
        exact and reproducible for a given seed."""
        if self._faithful:
            return FAKE_ROUTE
        index = self._rng.randrange(self._route_cardinality)
        shape = ROUTE_SHAPES[index % len(ROUTE_SHAPES)]
        resource = ROUTE_RESOURCES[(index // len(ROUTE_SHAPES)) % len(ROUTE_RESOURCES)]
        route = shape.format(resource=resource)
        # Past the natural shape x resource combinations, suffix a version
        # segment so higher cardinalities stay reachable.
        bucket = index // (len(ROUTE_SHAPES) * len(ROUTE_RESOURCES))
        return route if bucket == 0 else f"{route}/v{bucket}"

    def _method(self):
        if self._faithful:
            return "POST"
        return HTTP_METHOD_POOL[self._rng.randrange(len(HTTP_METHOD_POOL))]

    def _status_code(self, errored):
        if self._faithful:
            return 200
        pool = ERROR_STATUS_POOL if errored else OK_STATUS_POOL
        return pool[self._rng.randrange(len(pool))]

    @property
    def spans_per_trace(self):
        return 3 if self._scenario == "basic" else self._fanout + 1

    def resource_attributes(self):
        attrs = {"service.name": self._service_name}
        if not self._faithful:
            # service.version and service.instance.id are resource-level
            # conventions. otelgen puts them on every span; --faithful keeps
            # that behaviour, otherwise they belong here.
            attrs["service.version"] = FAKE_VER
            attrs["service.instance.id"] = self._instance_id
        return attrs

    def _anchor_end(self):
        """Wall-clock instant at which the trace's last span should end.

        Anchoring on the *end* rather than the start is what keeps timestamps
        out of the future: a span is only exported once it has finished, and a
        microservices trace covers several seconds of work.
        """
        now = time.time_ns()
        if self._clock_mode == "realtime":
            # Model collector batching plus pipeline latency: spans land a
            # short, jittered interval behind now.
            jitter = self._rng.randint(-self._lag_jitter_ms, self._lag_jitter_ms)
            return now - max(self._ingest_lag_ms + jitter, 0) * MS
        return now - self._rng.randrange(max(self._time_spread_s, 1)) * 1_000_000_000

    @staticmethod
    def _shift(spans, anchor_end):
        """Translate a trace built on a zero-based clock onto the wall clock."""
        delta = anchor_end - max(span.end for span in spans)
        for span in spans:
            span.start += delta
            span.end += delta
            for event in span.events:
                event["time_unix_nano"] += delta
        return spans

    def _basic_trace(self):
        """Port of BasicScenario (basic.go). root -> ping -> pong, where ping
        *encloses* pong because the Go code defers its End()."""
        start = 0
        ping_ns = self._duration()
        pong_ns = self._duration()

        # otelgen folds the rate-limiter wait into the root span: it calls
        # limiter.Wait() after the scenario but before root.End()
        # (traces.go:98-104), so the root outlives its children.
        # The limiter wait is scheduler time, not work, so it stays uniform.
        throttle_ns = self._rng.randrange(50) * MS

        # otelgen's basic.go never calls SetStatus, so every span it emits has
        # an unset status. That leaves status.code with a single value across
        # the whole index and makes error_rate a no-op, so outside faithful
        # mode the failure is modelled: pong fails, and the client span and
        # root observe it.
        errored = not self._faithful and self._rng.random() < self._error_rate
        if self._faithful:
            code, message = STATUS_UNSET, ""
        elif errored:
            code, message = STATUS_ERROR, "ping-pong failed"
        else:
            code, message = STATUS_OK, ""

        ping_end = start + ping_ns + pong_ns
        root = Span(name="basic", kind="SPAN_KIND_INTERNAL",
                    start=start, end=ping_end + throttle_ns,
                    status_code=code, status_message=message)

        def peer_attrs(peer):
            attrs = {
                "service.namespace": FAKE_NS,
                "network.peer.address": self._peer_ip(),
                "peer.service": peer,
                "telemetry.sdk.language": "go",
            }
            if self._faithful:
                attrs["service.version"] = FAKE_VER
                attrs["service.instance.id"] = self._instance_id
            return attrs

        ping = Span(
            name="ping",
            # otelgen never calls WithSpanKind, so every span is INTERNAL and
            # the intended kind is stranded in a span.kind attribute.
            kind="SPAN_KIND_INTERNAL" if self._faithful else "SPAN_KIND_CLIENT",
            start=start,
            end=ping_end,
            parent_span_id=root.span_id,
            status_code=code,
            status_message=message,
            attributes={**peer_attrs("ping-pong-server"),
                        **({"span.kind": "client"} if self._faithful else {})},
        )
        pong = Span(
            name="pong",
            kind="SPAN_KIND_INTERNAL" if self._faithful else "SPAN_KIND_SERVER",
            start=start + ping_ns,
            end=start + ping_ns + pong_ns,
            parent_span_id=ping.span_id,
            status_code=code,
            status_message=message,
            attributes={**peer_attrs("ping-pong-client"),
                        **({"span.kind": "server"} if self._faithful else {})},
        )
        return [root, ping, pong]

    def _microservices_trace(self):
        """Port of MicroservicesScenario (microservices.go). One root with
        `fanout` flat children, each tagged with a randomly chosen service."""
        start = 0
        root_route = self._route()
        # The originating client address is propagated onto every span of the
        # trace, as APM pipelines enrich it. Keeping it on the root alone would
        # leave client_ip_cardinality unexercised at 1 doc in fanout+1.
        client_ip = self._client_ip()
        root = Span(
            name="complex_request",
            kind="SPAN_KIND_INTERNAL" if self._faithful else "SPAN_KIND_SERVER",
            start=start,
            end=start,  # patched once the children are laid out
            status_code=STATUS_OK,
            attributes={
                "http.request.method": self._method(),
                "http.route": root_route,
                "http.response.status_code": self._status_code(errored=False),
                "url.scheme": "https",
                "url.full": f"https://api.example.com{root_route}",
                "url.path": root_route,
                "client.address": client_ip,
                "client.port": 56789 if self._faithful else self._rng.randrange(1024, 65536),
                "user_agent.original": "ExampleApp/1.0",
                "http.request.body.size": 2048 if self._faithful else self._rng.randrange(128, 16384),
                "service.name": f"{self._service_name}_api_gateway",
            },
        )

        spans = [root]
        cursor = start
        # Drawn independently rather than as "any child errored": with fanout
        # 100 at a 10% child rate, the latter marks ~99.997% of traces failed
        # and traceGroupFields.statusCode becomes constant again. A request
        # fails when a critical-path call fails, not when any fan-out call does.
        root_errored = not self._faithful and self._rng.random() < self._error_rate
        for _ in range(self._fanout):
            service = self._rng.choice(SERVICES)
            duration = self._duration()
            errored = self._rng.random() < self._error_rate
            attrs = {
                "service.name": f"{self._service_name}_{service}",
                "service.version": f"1.{self._rng.randrange(10)}.0",
                "service.instance.id": f"{service}-instance-{self._rng.randrange(5)}",
                "process.runtime.name": "OpenJDK Runtime Environment",
                "process.runtime.version": "11.0.9+11-Ubuntu-0ubuntu1.20.04",
            }
            if not self._faithful:
                # Every call has a peer and a originating client, so these
                # belong on all child spans. otelgen omits both, which is why
                # they are gated: faithful mode must not add fields otelgen
                # never emitted.
                attrs["network.peer.address"] = self._peer_ip()
                attrs["client.address"] = client_ip
            if not self._faithful and service not in NON_HTTP_SERVICES:
                attrs["http.request.method"] = self._method()
                attrs["http.route"] = self._route()
                attrs["http.response.status_code"] = self._status_code(errored)
            attrs.update(self._service_attrs(service, errored))
            spans.append(Span(
                name=f"{service}_operation",
                kind="SPAN_KIND_INTERNAL" if self._faithful else "SPAN_KIND_CLIENT",
                start=cursor,
                end=cursor + duration,
                parent_span_id=root.span_id,
                status_code=STATUS_ERROR if errored else STATUS_OK,
                status_message="Operation failed" if errored else "Operation successful",
                attributes=attrs,
                # Held as integers so _shift can translate them; rendered to
                # ISO8601 in render().
                events=[
                    {"name": "operation_started", "time_unix_nano": cursor},
                    {"name": "operation_ended", "time_unix_nano": cursor + duration},
                ],
            ))
            # otelgen sleeps inside each child, so the children run back to
            # back rather than concurrently.
            cursor += duration

        root.end = cursor
        # otelgen always marks the root Ok (microservices.go:102), which leaves
        # traceGroupFields.statusCode constant. Outside faithful mode the root
        # reflects its children, so trace-level error queries have something to
        # select on.
        if root_errored:
            root.status_code = STATUS_ERROR
            root.status_message = "downstream operation failed"
            root.attributes["http.response.status_code"] = self._status_code(errored=True)
        return spans

    def _service_attrs(self, service, errored):
        """Per-service extra attributes (microservices.go:60-89)."""
        if service == "api_gateway":
            if self._faithful:
                return {"http.route": FAKE_ROUTE, "http.response.status_code": 200}
            return {}  # method/route/status already set for all HTTP services
        if service == "auth_service":
            return {"enduser.id": f"user-{self._rng.randrange(1000)}",
                    "enduser.role": "customer"}
        if service == "cache_service":
            return {"db.system": "redis",
                    "db.operation.name": "SET" if self._faithful
                    else self._rng.choice(("GET", "SET", "DEL", "EXPIRE", "INCR"))}
        if service == "payment_service":
            return {"rpc.system": "grpc", "rpc.service": "PaymentService",
                    "rpc.method": "ProcessPayment",
                    "rpc.grpc.status_code": 0 if not errored else 13}
        return {}

    def generate_trace(self):
        """Return one trace's spans, root first, placed on the wall clock."""
        spans = self._basic_trace() if self._scenario == "basic" else self._microservices_trace()
        return self._shift(spans, self._anchor_end())

    def render(self, spans):
        """Render a trace's spans as raw-span documents."""
        trace_id = new_trace_id()
        root = spans[0]
        resource_attrs = flatten("resource.attributes", self.resource_attributes())
        trace_group_fields = {
            "endTime": iso8601(root.end),
            "durationInNanos": root.end - root.start,
            "statusCode": root.status_code,
        }

        docs = []
        for span in spans:
            docs.append({
                "traceId": trace_id,
                "spanId": span.span_id,
                "parentSpanId": span.parent_span_id,
                "traceState": "",
                "name": span.name,
                "kind": span.kind,
                "serviceName": self._service_name,
                "startTime": iso8601(span.start),
                "endTime": iso8601(span.end),
                "durationInNanos": span.end - span.start,
                "status": {"code": span.status_code, "message": span.status_message},
                "traceGroup": root.name,
                "traceGroupFields": trace_group_fields,
                "events": [
                    {"name": event["name"], "time": iso8601(event["time_unix_nano"])}
                    for event in span.events
                ],
                "links": [],
                "droppedAttributesCount": 0,
                "droppedEventsCount": 0,
                "droppedLinksCount": 0,
                **resource_attrs,
                **flatten("span.attributes", span.attributes),
            })
        return docs

    def docs_for_bulk(self, target_docs):
        """Generate whole traces until at least target_docs documents exist.

        Returns the documents; the caller must report the actual length to
        OpenSearch Benchmark rather than the requested target, since one trace
        is an indivisible group of spans_per_trace documents.
        """
        docs = []
        while len(docs) < target_docs:
            docs.extend(self.render(self.generate_trace()))
        return docs
