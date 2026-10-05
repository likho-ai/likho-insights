"""One transcript in, its insights out: the prompt, the model's answer read strictly, the
document stored, the event published. Nothing is sent anywhere unless a model is configured."""

import logging
import time
from dataclasses import dataclass
from typing import Any

from likho_insights.bus import COMPLETED_SUBJECT, FAILED_SUBJECT, Bus, completed_event, failed_event
from likho_insights.form import Form
from likho_insights.metrics import Metrics
from likho_insights.model import Model, ModelError
from likho_insights.store import Document, InsightsStore
from likho_insights.transcripts import Transcript, TranscriptNotFoundError, Transcripts, TranscriptsDownError

log = logging.getLogger(__name__)

SENTIMENTS = ("positive", "neutral", "negative", "mixed")
ANSWERS = ("yes", "no", "na")


class AnalysisError(Exception):
    """Why a transcript got no insights; the code is the failed event's and the metric's."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    cut: bool  # the transcript was longer than allowed and was shortened


def clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def build_prompt(form: Form, transcript: Transcript, report_language: str, max_chars: int) -> Prompt:
    """What the model is told and what it is given. The lines go in their Hinglish layer."""
    checks = "\n".join(f'  - "{c.key}": {c.label}' for c in form.checks)
    scores = "\n".join(f'  - "{s.key}": {s.label} (0 to {s.max:g})' for s in form.scores)
    system = (
        "You read the transcript of one phone call between a call-centre agent and a customer. The call is in "
        "Hindi or Urdu, written in Hinglish (Roman letters, as people type in chat); some words are English. "
        "The automatic transcription has errors; read for meaning, not spelling.\n\n"
        f"Answer with one JSON object and nothing else, with these keys:\n"
        f'- "summary": 2 to 4 sentences in {report_language}: who called about what, what was said, how it ended.\n'
        f'- "intent": what the customer wanted, in a few words in {report_language}.\n'
        '- "products": the product or service names mentioned, as they were said (an array of strings; '
        "empty if none).\n"
        '- "sentiment": the customer\'s mood by the end: "positive", "neutral", "negative" or "mixed".\n'
        '- "checks": an object with one key per check below; each value is an object {"answer": "yes" | "no" | "na", '
        '"evidence": the line that shows it, quoted from the transcript, or an empty string}. '
        '"na" means the call gave no way to tell.\n'
        '- "scores": an object with one key per scored point below; each value is an object {"score": a number from 0 '
        f'to the maximum, "reason": one sentence in {report_language}}}.\n\n'
        f"The checks:\n{checks}\n\nThe scored points:\n{scores}\n\n"
        'Be strict and fair: a check is "yes" only when the transcript shows it. Never invent names, prices or '
        "promises that are not in the transcript. Do not include any personal data beyond what the transcript says."
    )
    lines: list[str] = []
    used = 0
    cut = False
    for line in transcript.lines:
        text = f"[{clock(line.start_seconds)}] {line.text_roman.strip() or line.text_script.strip()}"
        if used + len(text) + 1 > max_chars:
            cut = True
            break
        lines.append(text)
        used += len(text) + 1
    body = "\n".join(lines)
    if cut:
        body += "\n[… the call goes on; the rest was left out for length]"
    user = (
        f"Transcript (language detected: {transcript.language or 'unknown'}; {len(transcript.lines)} lines"
        f"{', shortened' if cut else ''}):\n\n{body}"
    )
    return Prompt(system=system, user=user, cut=cut)


def _text(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if value is not None else ""


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entries(value: Any) -> dict[str, Any]:
    """checks/scores as an object keyed by key, or as a list of objects with a "key"."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        out: dict[str, Any] = {}
        for item in value:
            if isinstance(item, dict) and "key" in item:
                out[str(item["key"])] = item
        return out
    return {}


def interpret(data: dict[str, Any], form: Form) -> dict[str, Any]:
    """The model's JSON, read against the form: every check and score present, values within bounds."""
    summary = _text(data.get("summary"), 4000)
    if not summary:
        raise ModelError("bad_answer", "The model gave no summary", retryable=False)
    sentiment = _text(data.get("sentiment"), 20).lower()
    if sentiment not in SENTIMENTS:
        sentiment = "neutral"
    products_raw = data.get("products")
    products = [_text(p, 200) for p in products_raw if _text(p, 200)] if isinstance(products_raw, list) else []

    given_checks = _entries(data.get("checks"))
    checks = []
    for check in form.checks:
        given = given_checks.get(check.key)
        answer, evidence = "na", ""
        if isinstance(given, dict):
            answer = _text(given.get("answer"), 10).lower()
            evidence = _text(given.get("evidence"), 1000)
        elif isinstance(given, str):
            answer = given.strip().lower()
        if answer not in ANSWERS:
            answer = "na"
        checks.append({"key": check.key, "label": check.label, "answer": answer, "evidence": evidence})

    given_scores = _entries(data.get("scores"))
    scores = []
    total = 0.0
    for point in form.scores:
        given = given_scores.get(point.key)
        score, reason = None, ""
        if isinstance(given, dict):
            score = _number(given.get("score"))
            reason = _text(given.get("reason"), 1000)
        else:
            score = _number(given)
        if score is None:
            score, reason = 0.0, reason or "The model gave no score."
        score = min(max(score, 0.0), point.max)
        total += score
        scores.append({"key": point.key, "label": point.label, "score": score, "max": point.max, "reason": reason})

    return {
        "summary": summary,
        "intent": _text(data.get("intent"), 500),
        "products": products[:50],
        "sentiment": sentiment,
        "checks": checks,
        "scores": scores,
        "score_total": round(total, 2),
        "score_max": form.score_max,
    }


class Analyser:
    def __init__(
        self,
        store: InsightsStore,
        transcripts: Transcripts,
        model: Model | None,
        form: Form,
        bus: Bus,
        metrics: Metrics | None,
        *,
        report_language: str,
        max_transcript_chars: int,
        max_output_tokens: int,
    ) -> None:
        self._store = store
        self._transcripts = transcripts
        self._model = model
        self._form = form
        self._bus = bus
        self._metrics = metrics
        self._report_language = report_language
        self._max_chars = max_transcript_chars
        self._max_output_tokens = max_output_tokens

    @property
    def enabled(self) -> bool:
        """A model is configured: transcripts are analysed, and their text leaves this process."""
        return self._model is not None

    @property
    def model_name(self) -> str:
        return self._model.name if self._model is not None else ""

    @property
    def form(self) -> Form:
        return self._form

    def _count(self, outcome: str) -> None:
        if self._metrics is not None:
            self._metrics.analyses.add(1, {"outcome": outcome})

    async def analyse(self, transcript_id: str, workspace_id: str, *, force: bool = False) -> Document:
        """The insights of a transcript: the stored ones, or new ones from the model."""
        if not force:
            existing = await self._store.for_transcript(transcript_id)
            if existing is not None:
                return existing
        try:
            return await self._analyse(transcript_id, workspace_id)
        except AnalysisError as error:
            self._count(error.code)
            raise

    async def _analyse(self, transcript_id: str, workspace_id: str) -> Document:
        if self._model is None:
            raise AnalysisError(
                "no_model",
                "No model is configured (ANTHROPIC_API_KEY is empty): nothing is analysed and no text leaves.",
            )
        try:
            transcript = await self._transcripts.get(transcript_id)
        except TranscriptNotFoundError as error:
            raise AnalysisError("transcript_not_found", f"Transcript {transcript_id} was not found") from error
        except TranscriptsDownError as error:
            raise AnalysisError("internal", str(error), retryable=True) from error
        if not any(line.text_roman.strip() or line.text_script.strip() for line in transcript.lines):
            raise AnalysisError("empty_transcript", "The transcript has no words")

        prompt = build_prompt(self._form, transcript, self._report_language, self._max_chars)
        started = time.perf_counter()
        try:
            answer = await self._model.ask(prompt.system, prompt.user, self._max_output_tokens)
            read = interpret(answer.data, self._form)
        except ModelError as error:
            raise AnalysisError(error.code, error.message, retryable=error.retryable) from error
        finally:
            if self._metrics is not None:
                self._metrics.model_seconds.record(time.perf_counter() - started)
        if self._metrics is not None:
            self._metrics.tokens.add(answer.input_tokens, {"direction": "input"})
            self._metrics.tokens.add(answer.output_tokens, {"direction": "output"})

        document = await self._store.put(
            {
                "transcript_id": transcript.id,
                "recording_id": transcript.recording_id,
                "workspace_id": workspace_id,
                "transcript_version": transcript.version,
                **read,
                "model": self._model.name,
                "input_tokens": answer.input_tokens,
                "output_tokens": answer.output_tokens,
                "form_version": self._form.version,
                "transcript_cut": prompt.cut,
            }
        )
        self._count("done")
        await self._bus.publish(COMPLETED_SUBJECT, completed_event(document))
        log.info(
            "insights made",
            extra={"transcript": transcript.id, "recording": transcript.recording_id, "model": self._model.name},
        )
        return document

    async def report_failure(
        self, transcript_id: str, recording_id: str, workspace_id: str, error: AnalysisError, attempt: int
    ) -> None:
        await self._bus.publish(
            FAILED_SUBJECT, failed_event(transcript_id, recording_id, workspace_id, error.code, error.message, attempt)
        )
