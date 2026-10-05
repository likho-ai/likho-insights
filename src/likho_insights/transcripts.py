"""Where transcripts come from: likho-transcription's gRPC service."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import grpc
from likho.transcription.v1 import transcription_pb2, transcription_pb2_grpc


@dataclass(frozen=True)
class Line:
    index: int
    start_seconds: float
    end_seconds: float
    text_roman: str
    text_script: str


@dataclass(frozen=True)
class Transcript:
    id: str
    recording_id: str
    version: int
    language: str
    created_at: datetime
    lines: tuple[Line, ...]


class TranscriptNotFoundError(LookupError):
    pass


class TranscriptsDownError(ConnectionError):
    pass


class Transcripts(Protocol):
    async def get(self, transcript_id: str) -> Transcript: ...


class TranscriptionGateway:
    def __init__(self, address: str, timeout: float) -> None:
        self._channel = grpc.aio.insecure_channel(address)
        self._stub = transcription_pb2_grpc.TranscriptionServiceStub(self._channel)
        self._timeout = timeout

    async def close(self) -> None:
        await self._channel.close()

    async def get(self, transcript_id: str) -> Transcript:
        try:
            reply = await self._stub.GetTranscript(
                transcription_pb2.GetTranscriptRequest(id=transcript_id), timeout=self._timeout
            )
        except grpc.aio.AioRpcError as error:
            if error.code() == grpc.StatusCode.NOT_FOUND:
                raise TranscriptNotFoundError(transcript_id) from error
            raise TranscriptsDownError(f"likho-transcription did not answer ({error.code().name})") from error
        t = reply.transcript
        created = t.created_at.ToDatetime().replace(tzinfo=UTC) if t.HasField("created_at") else datetime.now(UTC)
        return Transcript(
            id=t.id,
            recording_id=t.recording_id,
            version=int(t.version),
            language=t.language.detected if t.HasField("language") else "",
            created_at=created,
            lines=tuple(
                Line(int(s.index), s.start_seconds, s.end_seconds, s.text_roman, s.text_script) for s in t.segments
            ),
        )
