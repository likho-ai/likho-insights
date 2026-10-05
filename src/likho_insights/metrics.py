"""What the service tells about itself (OpenTelemetry): always as Prometheus text at GET /metrics,
and pushed over OTLP/HTTP as well when OTEL_EXPORTER_OTLP_ENDPOINT is set."""

from __future__ import annotations

import atexit
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import grpc
from opentelemetry import metrics as otel
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

log = logging.getLogger(__name__)

SERVICE = "likho-insights"

_shared: Metrics | None = None


def shared(version: str, otlp_endpoint: str = "") -> Metrics:
    """The process's one set of instruments (Prometheus has one registry per process)."""
    global _shared
    if _shared is None:
        _shared = Metrics(version, otlp_endpoint)
        otel.set_meter_provider(_shared._provider)
        atexit.register(_shared._provider.shutdown)
    return _shared


class Metrics:
    def __init__(self, version: str, otlp_endpoint: str = "") -> None:
        readers: list[MetricReader] = [PrometheusMetricReader()]
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            url = otlp_endpoint.rstrip("/") + "/v1/metrics"
            readers.append(
                PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=url), export_interval_millis=15_000)
            )
            log.info("metrics go to %s every 15 s, and are at /metrics", url)
        self._provider = MeterProvider(
            resource=Resource.create({SERVICE_NAME: SERVICE, SERVICE_VERSION: version}),
            metric_readers=readers,
            views=[
                View(
                    instrument_name="likho_insights_request_seconds",
                    aggregation=ExplicitBucketHistogramAggregation((0.005, 0.025, 0.1, 0.5, 1, 2.5, 5, 10, 30, 60)),
                ),
                View(
                    instrument_name="likho_insights_model_seconds",
                    aggregation=ExplicitBucketHistogramAggregation((1, 2.5, 5, 10, 20, 30, 60, 120)),
                ),
            ],
        )
        meter = self._provider.get_meter(SERVICE, version)
        #: gRPC calls answered, by method and outcome.
        self.requests = meter.create_counter("likho_insights_requests", description="gRPC calls answered")
        self.request_seconds = meter.create_histogram(
            "likho_insights_request_seconds", description="How long a call took"
        )
        #: Transcripts analysed, by outcome: done, no_model, transcript_not_found, model_error, bad_answer, internal.
        self.analyses = meter.create_counter("likho_insights_analyses", description="Transcripts analysed, by outcome")
        self.model_seconds = meter.create_histogram(
            "likho_insights_model_seconds", description="How long the model took"
        )
        #: Tokens sent to and received from the model, by direction.
        self.tokens = meter.create_counter("likho_insights_tokens", description="Model tokens, by direction")
        self.events_handled = meter.create_counter("likho_insights_events_handled", description="Events handled")
        # Known label sets start at 0, so the first event shows as an increase.
        for outcome in (
            "done",
            "no_model",
            "transcript_not_found",
            "empty_transcript",
            "model_error",
            "bad_answer",
            "internal",
        ):
            self.analyses.add(0, {"outcome": outcome})
        for direction in ("input", "output"):
            self.tokens.add(0, {"direction": direction})
        #: Whether a model is configured (1) or not (0).
        self._enabled = 0
        meter.create_observable_gauge(
            "likho_insights_model_enabled",
            callbacks=[lambda options: [otel.Observation(self._enabled)]],
            description="1 when a model is configured and text may leave",
        )

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = 1 if enabled else 0

    def scrape(self) -> tuple[str, bytes]:
        return CONTENT_TYPE_LATEST, generate_latest(REGISTRY)

    def flush(self) -> None:
        self._provider.force_flush()


class MetricsInterceptor(grpc.aio.ServerInterceptor):
    """Counts and times every unary gRPC call."""

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler | None]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler | None:
        handler = await continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler
        method = str(handler_call_details.method).rsplit("/", 1)[-1]
        inner = handler.unary_unary
        metrics = self._metrics

        async def timed(request: Any, context: Any) -> Any:
            started = time.perf_counter()
            outcome = "ok"
            try:
                return await inner(request, context)
            except BaseException:
                outcome = "error"
                raise
            finally:
                metrics.requests.add(1, {"method": method, "outcome": outcome})
                metrics.request_seconds.record(time.perf_counter() - started, {"method": method})

        return grpc.unary_unary_rpc_method_handler(
            timed, request_deserializer=handler.request_deserializer, response_serializer=handler.response_serializer
        )
