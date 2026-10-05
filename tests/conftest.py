"""Fixtures for the integration tests: the real service on free ports against the likho-infra stack
(MongoDB and NATS), with a fake model and a stand-in likho-transcription.

Start the stack first:  likho-infra> bash scripts/up.sh   (or .\\stack.ps1 up)
Without it these tests are skipped locally; with LIKHO_REQUIRE_STACK=1 (set in CI) they fail instead.
"""

import asyncio
import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import grpc
import nats
import pytest
import pytest_asyncio
from likho.insights.v1 import insights_pb2_grpc
from nats.js.api import ConsumerConfig, DeliverPolicy

from likho_insights.__main__ import serve
from likho_insights.bus import COMPLETED_SUBJECT, FAILED_SUBJECT, STREAM
from likho_insights.ids import new_id
from likho_insights.model import FakeModel
from likho_insights.settings import Settings
from likho_insights.store import InsightsStore
from likho_insights.transcripts import Line, Transcript, TranscriptNotFoundError

LINES = (
    ("namaste, Herbal House se bol raha hoon", "नमस्ते, हर्बल हाउस से बोल रहा हूँ"),
    ("aapne Ashwagandha ke liye call kiya tha", "आपने अश्वगंधा के लिए कॉल किया था"),
    ("price 499 hai, delivery kal tak ho jayegi", "प्राइस 499 है, डिलीवरी कल तक हो जाएगी"),
    ("theek hai, order confirm kar dijiye", "ठीक है, ऑर्डर कन्फर्म कर दीजिए"),
    ("dhanyavaad, aapka din shubh ho", "धन्यवाद, आपका दिन शुभ हो"),
)


def make_transcript(transcript_id: str, recording_id: str, lines: tuple[tuple[str, str], ...] = LINES) -> Transcript:
    return Transcript(
        id=transcript_id,
        recording_id=recording_id,
        version=1,
        language="hi",
        created_at=datetime.now(UTC),
        lines=tuple(Line(i, i * 5.0, i * 5.0 + 4, roman, script) for i, (roman, script) in enumerate(lines)),
    )


@dataclass
class FakeTranscripts:
    """Stands in for likho-transcription: transcripts by id."""

    transcripts: dict[str, Transcript] = field(default_factory=dict)

    async def get(self, transcript_id: str) -> Transcript:
        if transcript_id not in self.transcripts:
            raise TranscriptNotFoundError(transcript_id)
        return self.transcripts[transcript_id]


GOOD_ANSWER: dict[str, Any] = {
    "summary": "A customer called about Ashwagandha; the agent gave the price and delivery, and the order was placed.",
    "intent": "order Ashwagandha",
    "products": ["Ashwagandha"],
    "sentiment": "positive",
    "checks": {
        "greeting": {"answer": "yes", "evidence": "namaste, Herbal House se bol raha hoon"},
        "need_understood": {"answer": "yes", "evidence": "aapne Ashwagandha ke liye call kiya tha"},
        "product_explained": {"answer": "na", "evidence": ""},
        "price_stated": {"answer": "yes", "evidence": "price 499 hai, delivery kal tak ho jayegi"},
        "objection_handled": {"answer": "na", "evidence": ""},
        "polite": {"answer": "yes", "evidence": ""},
        "next_step": {"answer": "yes", "evidence": "delivery kal tak ho jayegi"},
        "closing": {"answer": "yes", "evidence": "dhanyavaad, aapka din shubh ho"},
    },
    "scores": {
        "communication": {"score": 4, "reason": "Clear and polite."},
        "knowledge": {"score": 3, "reason": "Price and delivery given; nothing more."},
        "resolution": {"score": 9, "reason": "The order was confirmed."},
    },
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@dataclass
class Platform:
    settings: Settings
    stub: insights_pb2_grpc.InsightsServiceStub
    channel: grpc.aio.Channel
    model: FakeModel
    transcripts: FakeTranscripts
    store: InsightsStore
    nc: Any
    subscriptions: dict[str, Any]
    seen: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def publish(self, subject: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        event = {
            "specversion": "1.0",
            "id": new_id("evt"),
            "source": "test",
            "type": event_type,
            "time": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "subject": data.get("recording_id", ""),
            "datacontenttype": "application/json",
            "data": data,
        }
        await self.nc.jetstream().publish(subject, json.dumps(event).encode(), headers={"Nats-Msg-Id": event["id"]})
        return event

    async def events(self, transcript_id: str, until: str, within: float = 20.0) -> list[tuple[str, dict[str, Any]]]:
        """Everything this service published about a transcript, up to the first `until` event."""
        deadline = asyncio.get_running_loop().time() + within
        while True:
            mine = [(s, e) for s, e in self.seen if e["data"].get("transcript_id") == transcript_id]
            if any(subject == until for subject, _ in mine):
                return mine
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"no {until} event for {transcript_id} within {within}s; saw {[s for s, _ in mine]}"
                )
            await self._read()

    async def _read(self) -> None:
        for subject, subscription in self.subscriptions.items():
            try:
                messages = await subscription.fetch(10, timeout=0.3)
            except (TimeoutError, nats.errors.TimeoutError):
                continue
            for message in messages:
                self.seen.append((subject, json.loads(message.data)))
                await message.ack()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def platform() -> AsyncIterator[Platform]:
    group = "test-" + new_id("ins")[-12:].lower()
    settings = Settings(
        grpc_port=_free_port(),
        http_port=_free_port(),
        mongo_database="likho_insights_test",
        consumer_group=group,
        consumer_start="new",
        qa_form_file="config/qa.example.json",
        anthropic_api_key="",  # the model is the fake, passed in below
        log_level="WARNING",
    )
    mongo_host, mongo_port = settings.mongo_url.split("//", 1)[1].split("/")[0].split(":")
    nats_host, nats_port = settings.nats_url.split("//", 1)[1].split(":")
    missing = [
        name
        for name, host, port in (("MongoDB", mongo_host, mongo_port), ("NATS", nats_host, nats_port))
        if not _reachable(host, int(port))
    ]
    if missing:
        message = f"{' and '.join(missing)} not reachable; start the likho-infra stack"
        if os.environ.get("LIKHO_REQUIRE_STACK") == "1":
            pytest.fail(message)
        pytest.skip(message)

    model = FakeModel()
    transcripts = FakeTranscripts()
    nc = await nats.connect(settings.nats_url)
    js = nc.jetstream()
    subscriptions = {}
    for subject in (COMPLETED_SUBJECT, FAILED_SUBJECT):
        subscriptions[subject] = await js.pull_subscribe(
            subject,
            durable=f"{group}-watch-{subject.rsplit('.', 1)[-1]}",
            stream=STREAM,
            config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW, filter_subject=subject),
        )

    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, stop, model=model, transcripts=transcripts))
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{settings.grpc_port}")
    try:
        await asyncio.wait_for(channel.channel_ready(), timeout=30)
    except TimeoutError:
        stop.set()
        await task
        raise
    store = InsightsStore(settings.mongo_url, settings.mongo_database)
    yield Platform(
        settings, insights_pb2_grpc.InsightsServiceStub(channel), channel, model, transcripts, store, nc, subscriptions
    )

    await channel.close()
    stop.set()
    await asyncio.wait_for(task, timeout=30)
    for name in (
        f"{group}-completed",
        f"{group}-deleted",
        *(f"{group}-watch-{s.rsplit('.', 1)[-1]}" for s in subscriptions),
    ):
        with contextlib.suppress(Exception):  # best effort
            await js.delete_consumer(STREAM, name)
    await nc.close()
    await store._insights.database.client.drop_database(settings.mongo_database)
    await store.close()


@pytest_asyncio.fixture(loop_scope="session")
async def fresh(platform: Platform) -> Platform:
    """The platform with the fake model's answers and the transcripts cleared."""
    platform.model.answers.clear()
    platform.model.asked.clear()
    platform.model.error = None
    platform.transcripts.transcripts.clear()
    return platform
