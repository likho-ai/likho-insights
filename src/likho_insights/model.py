"""The language model behind one interface: Anthropic's Claude today, a local model another day,
a fake in tests. Nothing here decides *whether* text may leave; that is the key's presence
(settings) and the company's yes."""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Answer:
    """What a model said, as JSON already parsed, with what it cost."""

    data: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0


class ModelError(Exception):
    """The model did not answer, or not with JSON. retryable: worth another try later."""

    def __init__(self, code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class Model(Protocol):
    @property
    def name(self) -> str:
        """The registry name, e.g. anthropic/claude-sonnet-5-5."""
        ...

    async def ask(self, system: str, user: str, max_output_tokens: int) -> Answer: ...


class AnthropicModel:
    """Claude, through the Anthropic API, asked for a JSON object and nothing else."""

    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_seconds, max_retries=2)
        self._model = model

    @property
    def name(self) -> str:
        return f"anthropic/{self._model}"

    async def ask(self, system: str, user: str, max_output_tokens: int) -> Answer:
        from anthropic import APIConnectionError, APIStatusError, RateLimitError

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except RateLimitError as error:
            raise ModelError("model_error", f"The model is rate limited: {error}", retryable=True) from error
        except APIConnectionError as error:
            raise ModelError("model_error", f"The model could not be reached: {error}", retryable=True) from error
        except APIStatusError as error:
            retryable = error.status_code >= 500
            raise ModelError(
                "model_error", f"The model answered {error.status_code}: {error.message}", retryable
            ) from error
        text = "".join(str(getattr(block, "text", "")) for block in response.content if block.type == "text")
        return Answer(
            data=parse_json(text),
            input_tokens=int(response.usage.input_tokens),
            output_tokens=int(response.usage.output_tokens),
        )


@dataclass
class FakeModel:
    """Answers what a test puts in, and remembers what it was asked."""

    answers: list[dict[str, Any]] = field(default_factory=list)
    asked: list[tuple[str, str]] = field(default_factory=list)
    error: ModelError | None = None
    model: str = "fake/one"

    @property
    def name(self) -> str:
        return self.model

    async def ask(self, system: str, user: str, max_output_tokens: int) -> Answer:
        self.asked.append((system, user))
        if self.error is not None:
            raise self.error
        if not self.answers:
            raise ModelError("bad_answer", "The fake model has nothing to say", retryable=False)
        data = self.answers.pop(0)
        return Answer(data=data, input_tokens=100, output_tokens=50)


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json(text: str) -> dict[str, Any]:
    """The JSON object in a model's answer, with or without a code fence around it."""
    candidate = text.strip()
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1)
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise ModelError("bad_answer", "The model did not answer with a JSON object", retryable=False)
        candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise ModelError("bad_answer", f"The model's JSON could not be read: {error}", retryable=False) from error
    if not isinstance(data, dict):
        raise ModelError("bad_answer", "The model did not answer with a JSON object", retryable=False)
    return data
