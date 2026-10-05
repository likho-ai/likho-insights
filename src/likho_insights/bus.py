"""The event bus (NATS JetStream): the events this service publishes and the ones it takes.

Contracts: likho-contracts/events/likho.insights.{completed,failed}.v1.schema.json and
streams.yaml. Event ids are derived from the transcript, so publishing again (a retry) is
stored once.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import nats
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

log = logging.getLogger(__name__)

SOURCE = "likho-insights"
STREAM = "LIKHO"
COMPLETED_SUBJECT = "likho.insights.completed"
FAILED_SUBJECT = "likho.insights.failed"
TRANSCRIPTION_COMPLETED_SUBJECT = "likho.transcription.completed"
RECORDING_DELETED_SUBJECT = "likho.recording.deleted"

Event = dict[str, Any]
Handler = Callable[[Event], Awaitable[None]]


def envelope(event_id: str, event_type: str, subject: str, data: dict[str, Any]) -> Event:
    return {
        "specversion": "1.0",
        "id": event_id,
        "source": SOURCE,
        "type": event_type,
        "time": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "subject": subject,
        "datacontenttype": "application/json",
        "data": data,
    }


def completed_event(document: dict[str, Any]) -> Event:
    # One id per analysis: a forced re-analysis is a new event; a retry of the same publish is not.
    made = int(document["created_at"].timestamp() * 1000)
    return envelope(
        f"evt_{document['transcript_id']}_insights_{made}",
        "likho.insights.completed.v1",
        document["recording_id"],
        {
            "insights_id": document["_id"],
            "transcript_id": document["transcript_id"],
            "recording_id": document["recording_id"],
            "workspace_id": document["workspace_id"],
            "model": document["model"],
            "sentiment": document["sentiment"],
            "score_total": document["score_total"],
            "score_max": document["score_max"],
            "input_tokens": document["input_tokens"],
            "output_tokens": document["output_tokens"],
        },
    )


def failed_event(
    transcript_id: str, recording_id: str, workspace_id: str, code: str, message: str, attempt: int
) -> Event:
    return envelope(
        f"evt_{transcript_id}_insights_failed_{attempt}",
        "likho.insights.failed.v1",
        recording_id,
        {
            "transcript_id": transcript_id,
            "recording_id": recording_id,
            "workspace_id": workspace_id,
            "code": code,
            "message": message,
            "attempt": attempt,
        },
    )


class Bus:
    def __init__(self, url: str) -> None:
        self._url = url
        self._nc: Any = None
        self._js: Any = None
        self._subscriptions: list[Any] = []

    async def connect(self, timeout_seconds: float = 0.0) -> None:
        """Connects, trying again while NATS is not there yet, for `timeout_seconds` (0 = one try)."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        wait = 1.0
        while True:
            try:
                self._nc = await nats.connect(self._url, name=SOURCE, max_reconnect_attempts=-1, connect_timeout=5)
                self._js = self._nc.jetstream()
                return
            except Exception as error:
                if asyncio.get_running_loop().time() + wait > deadline:
                    raise
                log.warning("event bus not ready (%s); trying again in %.0f s", error, wait)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 10.0)

    @property
    def connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    @property
    def js(self) -> Any:
        return self._js

    async def close(self) -> None:
        for subscription in self._subscriptions:
            with contextlib.suppress(Exception):
                await subscription.unsubscribe()
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None

    async def publish(self, subject: str, event: Event) -> None:
        if self._js is None:
            log.warning("event bus not connected; %s not published", event["type"])
            return
        await self._js.publish(
            subject, json.dumps(event, ensure_ascii=False).encode(), headers={"Nats-Msg-Id": event["id"]}, timeout=5
        )

    async def take(self, subject: str, durable: str, start: str, handle: Handler, stop: asyncio.Event) -> None:
        """Takes every event on the subject with a durable pull consumer, one at a time, until stop."""
        config = ConsumerConfig(
            durable_name=durable,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=180,
            max_deliver=5,
            deliver_policy=DeliverPolicy.NEW if start == "new" else DeliverPolicy.ALL,
            filter_subject=subject,
        )
        subscription = await self._js.pull_subscribe(subject, durable=durable, stream=STREAM, config=config)
        self._subscriptions.append(subscription)
        log.info("taking %s as %s", subject, durable)
        while not stop.is_set():
            try:
                messages = await subscription.fetch(1, timeout=1)
            except (NatsTimeoutError, TimeoutError):
                continue
            except Exception:
                log.exception("could not fetch from %s; trying again", subject)
                await asyncio.sleep(1)
                continue
            for message in messages:
                try:
                    event = json.loads(message.data)
                    if not isinstance(event, dict) or "data" not in event:
                        raise ValueError("not a CloudEvent")
                except ValueError as error:
                    log.error("dropping a message on %s that is not an event: %s", subject, error)
                    await message.term()
                    continue
                try:
                    await handle(event)
                except Exception:
                    log.exception("event %s on %s not handled; will retry", event.get("id"), subject)
                    await message.nak(delay=10)
                    continue
                await message.ack()

    async def forget(self, durables: list[str]) -> None:
        """Deletes durable consumers (tests)."""
        for durable in durables:
            with contextlib.suppress(Exception):
                await self._js.delete_consumer(STREAM, durable)
