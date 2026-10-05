"""Starts the service:  python -m likho_insights   (or the `likho-insights` command)."""

import asyncio
import contextlib
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from likho.insights.v1 import insights_pb2, insights_pb2_grpc

from likho_insights import __version__
from likho_insights.analyser import Analyser, AnalysisError
from likho_insights.bus import RECORDING_DELETED_SUBJECT, TRANSCRIPTION_COMPLETED_SUBJECT, Bus
from likho_insights.form import load_form
from likho_insights.grpc_server import InsightsServicer
from likho_insights.health import start_health_server
from likho_insights.metrics import MetricsInterceptor, shared
from likho_insights.model import AnthropicModel, Model
from likho_insights.settings import Settings
from likho_insights.store import InsightsStore
from likho_insights.transcripts import TranscriptionGateway, Transcripts

log = logging.getLogger("likho_insights")

SERVICE_NAME = insights_pb2.DESCRIPTOR.services_by_name["InsightsService"].full_name


class JsonFormatter(logging.Formatter):
    """One JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("transcript", "recording", "model"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)


def make_model(settings: Settings) -> Model | None:
    """The configured model, or None: without a key nothing is analysed and no text leaves."""
    if settings.anthropic_api_key:
        return AnthropicModel(settings.anthropic_api_key, settings.anthropic_model, settings.model_timeout_seconds)
    return None


async def serve(
    settings: Settings,
    stop: asyncio.Event | None = None,
    *,
    model: Model | None = None,
    transcripts: Transcripts | None = None,
) -> None:
    """Run until `stop` is set (or the process is told to stop). Tests pass a model and transcripts of their own."""
    stop = stop or asyncio.Event()
    form = load_form(settings.qa_form_file)
    store = InsightsStore(settings.mongo_url, settings.mongo_database)
    await store.prepare()
    gateway: TranscriptionGateway | None = None
    if transcripts is None:
        gateway = TranscriptionGateway(settings.transcription_grpc_addr, settings.rpc_timeout_seconds)
        transcripts = gateway
    model = model if model is not None else make_model(settings)
    metrics = shared(__version__, settings.otel_exporter_otlp_endpoint)
    metrics.set_enabled(model is not None)

    bus = Bus(settings.nats_url)
    try:
        await bus.connect(settings.nats_connect_timeout_seconds)
        log.info("connected to the event bus at %s", settings.nats_url)
    except Exception:
        log.exception("event bus not reachable at %s; transcripts are not taken from it", settings.nats_url)

    analyser = Analyser(
        store,
        transcripts,
        model,
        form,
        bus,
        metrics,
        report_language=settings.report_language,
        max_transcript_chars=settings.max_transcript_chars,
        max_output_tokens=settings.model_max_output_tokens,
    )

    server = grpc.aio.server(
        options=[("grpc.max_receive_message_length", 8 * 1024 * 1024)], interceptors=[MetricsInterceptor(metrics)]
    )
    insights_pb2_grpc.add_InsightsServiceServicer_to_server(InsightsServicer(store, analyser), server)
    health_servicer = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    await health_servicer.set(SERVICE_NAME, health_pb2.HealthCheckResponse.SERVING)
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    server.add_insecure_port(f"[::]:{settings.grpc_port}")
    await server.start()

    async def ready() -> bool:
        return await store.ping()

    http = await start_health_server(settings.http_port, ready, metrics.scrape)

    tasks: list[asyncio.Task[None]] = []
    if settings.consumers_enabled and bus.connected:

        async def on_completed(event: dict[str, Any]) -> None:
            data = event["data"]
            transcript_id, recording_id = str(data["transcript_id"]), str(data["recording_id"])
            workspace_id = str(data["workspace_id"])
            try:
                await analyser.analyse(transcript_id, workspace_id)
            except AnalysisError as error:
                if error.retryable:
                    raise
                await analyser.report_failure(transcript_id, recording_id, workspace_id, error, attempt=1)
                log.warning("transcript %s not analysed (%s): %s", transcript_id, error.code, error.message)
            metrics.events_handled.add(1, {"subject": TRANSCRIPTION_COMPLETED_SUBJECT, "outcome": "ok"})

        async def on_deleted(event: dict[str, Any]) -> None:
            recording_id = str(event["data"]["recording_id"])
            gone = await store.delete_recording(recording_id)
            if gone:
                log.info("forgot the insights of a deleted recording", extra={"recording": recording_id})
            metrics.events_handled.add(1, {"subject": RECORDING_DELETED_SUBJECT, "outcome": "ok"})

        group = settings.consumer_group
        if analyser.enabled:
            tasks.append(
                asyncio.create_task(
                    bus.take(
                        TRANSCRIPTION_COMPLETED_SUBJECT,
                        f"{group}-completed",
                        settings.consumer_start,
                        on_completed,
                        stop,
                    )
                )
            )
        else:
            log.warning(
                "no model is configured (ANTHROPIC_API_KEY is empty): transcripts are not analysed, no text leaves"
            )
        tasks.append(
            asyncio.create_task(
                bus.take(RECORDING_DELETED_SUBJECT, f"{group}-deleted", settings.consumer_start, on_deleted, stop)
            )
        )

    log.info(
        "likho-insights %s: gRPC on %d, health on %d, model %s, form %s",
        __version__,
        settings.grpc_port,
        settings.http_port,
        analyser.model_name or "none (off)",
        form.version,
    )

    await stop.wait()

    log.info("stopping: finishing calls in flight")
    await health_servicer.set(SERVICE_NAME, health_pb2.HealthCheckResponse.NOT_SERVING)
    await server.stop(grace=10)
    http.close()
    await http.wait_closed()
    for task in tasks:
        await task
    await bus.close()
    if gateway is not None:
        await gateway.close()
    await store.close()
    await asyncio.to_thread(metrics.flush)


async def _run() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
            continue
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    await serve(settings, stop)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
