"""The running service: transcripts from the bus, the gRPC API, and what happens when things go wrong."""

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path

import grpc
import jsonschema
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from likho.insights.v1 import insights_pb2 as pb

from likho_insights.bus import (
    COMPLETED_SUBJECT,
    FAILED_SUBJECT,
    RECORDING_DELETED_SUBJECT,
    TRANSCRIPTION_COMPLETED_SUBJECT,
)
from likho_insights.ids import new_id
from likho_insights.model import ModelError
from tests.conftest import GOOD_ANSWER, Platform, make_transcript

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

CONTRACTS = Path(__file__).parent / "contracts"
WORKSPACE = "wsp_01JB7Z5K3M9Q2W4X6Y8A0C1E3G"


def valid(event: dict, schema_file: str) -> None:
    checker = jsonschema.FormatChecker()
    jsonschema.Draft202012Validator(
        json.loads((CONTRACTS / "cloudevent.schema.json").read_text(encoding="utf-8")), format_checker=checker
    ).validate(event)
    jsonschema.Draft202012Validator(
        json.loads((CONTRACTS / schema_file).read_text(encoding="utf-8")), format_checker=checker
    ).validate(event["data"])


async def _http_status(port: int, path: str) -> int:
    def get() -> int:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return error.code

    return await asyncio.to_thread(get)


async def test_health_and_status(fresh: Platform) -> None:
    health = health_pb2_grpc.HealthStub(fresh.channel)
    reply = await health.Check(health_pb2.HealthCheckRequest(service="likho.insights.v1.InsightsService"))
    assert reply.status == health_pb2.HealthCheckResponse.SERVING
    assert await _http_status(fresh.settings.http_port, "/healthz") == 200
    assert await _http_status(fresh.settings.http_port, "/readyz") == 200
    assert await _http_status(fresh.settings.http_port, "/metrics") == 200
    status = await fresh.stub.GetStatus(pb.GetStatusRequest())
    assert (status.enabled, status.model, status.form_version) == (True, "fake/one", "example-1")


async def test_a_completed_transcript_gets_its_insights(fresh: Platform) -> None:
    transcript_id, recording_id = new_id("trn"), new_id("rec")
    fresh.transcripts.transcripts[transcript_id] = make_transcript(transcript_id, recording_id)
    fresh.model.answers.append(GOOD_ANSWER)
    await fresh.publish(
        TRANSCRIPTION_COMPLETED_SUBJECT,
        "likho.transcription.completed.v1",
        {
            "job_id": new_id("job"),
            "recording_id": recording_id,
            "transcript_id": transcript_id,
            "workspace_id": WORKSPACE,
            "version": 1,
        },
    )
    events = await fresh.events(transcript_id, until=COMPLETED_SUBJECT)
    completed = next(e for s, e in events if s == COMPLETED_SUBJECT)
    valid(completed, "likho.insights.completed.v1.schema.json")
    assert completed["data"]["sentiment"] == "positive" and completed["data"]["score_total"] == 16
    assert completed["data"]["model"] == "fake/one"

    # The model saw the lines in Hinglish, and the form's checks.
    system, user = fresh.model.asked[-1]
    assert "aapne Ashwagandha ke liye call kiya tha" in user and '"greeting"' in system

    # Fetched by recording or by transcript: the same insights, with every check and score.
    by_recording = (await fresh.stub.GetInsights(pb.GetInsightsRequest(recording_id=recording_id))).insights
    by_transcript = (await fresh.stub.GetInsights(pb.GetInsightsRequest(transcript_id=transcript_id))).insights
    assert by_recording.id == by_transcript.id == completed["data"]["insights_id"]
    assert by_recording.summary.startswith("A customer called about Ashwagandha")
    assert list(by_recording.products) == ["Ashwagandha"] and by_recording.intent == "order Ashwagandha"
    assert [c.answer for c in by_recording.checks] == ["yes", "yes", "na", "yes", "na", "yes", "yes", "yes"]
    assert [(s.key, s.score, s.max) for s in by_recording.scores] == [
        ("communication", 4, 5),
        ("knowledge", 3, 5),
        ("resolution", 9, 10),
    ]
    assert (by_recording.score_total, by_recording.score_max, by_recording.form_version) == (16, 20, "example-1")
    assert by_recording.input_tokens == 100 and by_recording.created_at.seconds > 0

    # Asked again without force: the stored insights, no second question to the model.
    asked_before = len(fresh.model.asked)
    again = (await fresh.stub.Analyse(pb.AnalyseRequest(transcript_id=transcript_id, workspace_id=WORKSPACE))).insights
    assert again.id == by_recording.id and len(fresh.model.asked) == asked_before

    # With force: the model is asked again and the newer answer replaces the older one.
    fresh.model.answers.append({**GOOD_ANSWER, "sentiment": "mixed"})
    redone = (
        await fresh.stub.Analyse(pb.AnalyseRequest(transcript_id=transcript_id, workspace_id=WORKSPACE, force=True))
    ).insights
    assert redone.sentiment == "mixed" and len(fresh.model.asked) == asked_before + 1
    assert (
        await fresh.stub.GetInsights(pb.GetInsightsRequest(recording_id=recording_id))
    ).insights.sentiment == "mixed"

    # The recording is deleted: its insights go.
    await fresh.publish(
        RECORDING_DELETED_SUBJECT,
        "likho.recording.deleted.v1",
        {"recording_id": recording_id, "workspace_id": WORKSPACE},
    )
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        try:
            await fresh.stub.GetInsights(pb.GetInsightsRequest(recording_id=recording_id))
        except grpc.aio.AioRpcError as error:
            assert error.code() == grpc.StatusCode.NOT_FOUND
            break
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the insights survived the deletion")
        await asyncio.sleep(0.2)


async def test_what_goes_wrong_is_said(fresh: Platform) -> None:
    # No such transcript: a failed event, and NOT_FOUND over gRPC.
    missing = new_id("trn")
    recording_id = new_id("rec")
    await fresh.publish(
        TRANSCRIPTION_COMPLETED_SUBJECT,
        "likho.transcription.completed.v1",
        {
            "job_id": new_id("job"),
            "recording_id": recording_id,
            "transcript_id": missing,
            "workspace_id": WORKSPACE,
            "version": 1,
        },
    )
    events = await fresh.events(missing, until=FAILED_SUBJECT)
    failed = next(e for s, e in events if s == FAILED_SUBJECT)
    valid(failed, "likho.insights.failed.v1.schema.json")
    assert failed["data"]["code"] == "transcript_not_found"
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await fresh.stub.Analyse(pb.AnalyseRequest(transcript_id=missing, workspace_id=WORKSPACE))
    assert error.value.code() == grpc.StatusCode.NOT_FOUND

    # The model answers nonsense: a bad answer, nothing stored.
    transcript_id = new_id("trn")
    fresh.transcripts.transcripts[transcript_id] = make_transcript(transcript_id, recording_id)
    fresh.model.answers.append({"sentiment": "positive"})
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await fresh.stub.Analyse(pb.AnalyseRequest(transcript_id=transcript_id, workspace_id=WORKSPACE))
    assert error.value.code() == grpc.StatusCode.INTERNAL and "summary" in (error.value.details() or "")
    with pytest.raises(grpc.aio.AioRpcError):
        await fresh.stub.GetInsights(pb.GetInsightsRequest(transcript_id=transcript_id))

    # The model is down: UNAVAILABLE, worth a retry.
    fresh.model.error = ModelError("model_error", "The model could not be reached", retryable=True)
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await fresh.stub.Analyse(pb.AnalyseRequest(transcript_id=transcript_id, workspace_id=WORKSPACE))
    assert error.value.code() == grpc.StatusCode.UNAVAILABLE
    fresh.model.error = None

    # An empty transcript has nothing to say.
    empty = new_id("trn")
    fresh.transcripts.transcripts[empty] = make_transcript(empty, recording_id, lines=(("", ""),))
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await fresh.stub.Analyse(pb.AnalyseRequest(transcript_id=empty, workspace_id=WORKSPACE))
    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION

    # Bad requests.
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await fresh.stub.GetInsights(pb.GetInsightsRequest())
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await fresh.stub.Analyse(pb.AnalyseRequest(transcript_id=transcript_id))
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT
