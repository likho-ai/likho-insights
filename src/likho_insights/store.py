"""Insights in MongoDB: one document per transcript analysed."""

from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient

from likho_insights.ids import new_id

Document = dict[str, Any]


class InsightsStore:
    def __init__(self, url: str, database: str) -> None:
        self._client: AsyncMongoClient[Document] = AsyncMongoClient(url, tz_aware=True, serverSelectionTimeoutMS=5000)
        self._insights = self._client[database]["insights"]

    async def prepare(self) -> None:
        """Create the indexes. Fails when the database is not reachable."""
        await self._insights.create_index("transcript_id", unique=True)
        await self._insights.create_index([("recording_id", ASCENDING), ("created_at", DESCENDING)])
        await self._insights.create_index([("workspace_id", ASCENDING), ("created_at", DESCENDING)])

    async def ping(self) -> bool:
        await self._client.admin.command("ping")
        return True

    async def close(self) -> None:
        await self._client.close()

    async def put(self, document: Document) -> Document:
        """Store the insights of a transcript, replacing earlier ones for the same transcript (the id stays)."""
        earlier = await self._insights.find_one({"transcript_id": document["transcript_id"]}, {"_id": 1})
        insights_id = earlier["_id"] if earlier is not None else new_id("ins")
        document = {**document, "_id": insights_id, "created_at": datetime.now(UTC)}
        await self._insights.replace_one({"transcript_id": document["transcript_id"]}, document, upsert=True)
        stored = await self._insights.find_one({"transcript_id": document["transcript_id"]})
        assert stored is not None
        return stored

    async def get(self, insights_id: str) -> Document | None:
        return await self._insights.find_one({"_id": insights_id})

    async def for_transcript(self, transcript_id: str) -> Document | None:
        return await self._insights.find_one({"transcript_id": transcript_id})

    async def latest_for_recording(self, recording_id: str) -> Document | None:
        return await self._insights.find_one({"recording_id": recording_id}, sort=[("created_at", DESCENDING)])

    async def delete_recording(self, recording_id: str) -> int:
        result = await self._insights.delete_many({"recording_id": recording_id})
        return result.deleted_count
