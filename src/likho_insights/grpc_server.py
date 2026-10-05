"""likho.insights.v1.InsightsService over gRPC."""

import logging
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from likho.insights.v1 import insights_pb2, insights_pb2_grpc

from likho_insights.analyser import Analyser, AnalysisError
from likho_insights.store import InsightsStore

log = logging.getLogger(__name__)


def _stamp(value: Any) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


def to_pb(document: dict[str, Any]) -> insights_pb2.Insights:
    return insights_pb2.Insights(
        id=document["_id"],
        transcript_id=document["transcript_id"],
        recording_id=document["recording_id"],
        workspace_id=document["workspace_id"],
        transcript_version=int(document.get("transcript_version", 0)),
        summary=document.get("summary", ""),
        products=list(document.get("products", [])),
        sentiment=document.get("sentiment", ""),
        intent=document.get("intent", ""),
        checks=[
            insights_pb2.Check(key=c["key"], label=c["label"], answer=c["answer"], evidence=c.get("evidence", ""))
            for c in document.get("checks", [])
        ],
        scores=[
            insights_pb2.Score(
                key=s["key"], label=s["label"], score=float(s["score"]), max=float(s["max"]), reason=s.get("reason", "")
            )
            for s in document.get("scores", [])
        ],
        score_total=float(document.get("score_total", 0)),
        score_max=float(document.get("score_max", 0)),
        model=document.get("model", ""),
        input_tokens=int(document.get("input_tokens", 0)),
        output_tokens=int(document.get("output_tokens", 0)),
        form_version=document.get("form_version", ""),
        created_at=_stamp(document.get("created_at")),
    )


class InsightsServicer(insights_pb2_grpc.InsightsServiceServicer):
    def __init__(self, store: InsightsStore, analyser: Analyser) -> None:
        self._store = store
        self._analyser = analyser

    async def GetInsights(
        self, request: insights_pb2.GetInsightsRequest, context: grpc.aio.ServicerContext
    ) -> insights_pb2.GetInsightsResponse:
        if request.transcript_id:
            document = await self._store.for_transcript(request.transcript_id)
        elif request.recording_id:
            document = await self._store.latest_for_recording(request.recording_id)
        else:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "a recording_id or a transcript_id is required")
            raise AssertionError  # pragma: no cover
        if document is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "no insights for it yet")
            raise AssertionError  # pragma: no cover
        return insights_pb2.GetInsightsResponse(insights=to_pb(document))

    async def Analyse(
        self, request: insights_pb2.AnalyseRequest, context: grpc.aio.ServicerContext
    ) -> insights_pb2.AnalyseResponse:
        if not request.transcript_id or not request.workspace_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "transcript_id and workspace_id are required")
        try:
            document = await self._analyser.analyse(request.transcript_id, request.workspace_id, force=request.force)
        except AnalysisError as error:
            codes = {
                "no_model": grpc.StatusCode.FAILED_PRECONDITION,
                "transcript_not_found": grpc.StatusCode.NOT_FOUND,
                "empty_transcript": grpc.StatusCode.FAILED_PRECONDITION,
                "model_error": grpc.StatusCode.UNAVAILABLE,
                "bad_answer": grpc.StatusCode.INTERNAL,
                "internal": grpc.StatusCode.UNAVAILABLE,
            }
            await context.abort(codes.get(error.code, grpc.StatusCode.INTERNAL), error.message)
            raise AssertionError from error  # pragma: no cover
        return insights_pb2.AnalyseResponse(insights=to_pb(document))

    async def GetStatus(
        self, request: insights_pb2.GetStatusRequest, context: grpc.aio.ServicerContext
    ) -> insights_pb2.GetStatusResponse:
        return insights_pb2.GetStatusResponse(
            enabled=self._analyser.enabled,
            model=self._analyser.model_name,
            form_version=self._analyser.form.version,
        )
