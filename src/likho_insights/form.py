"""The auditor's form: the yes/no checks and the scored points a model fills in from a transcript.

The form is a JSON file (QA_FORM_FILE). The repository ships an example; the company's own form
lives in config/qa.local.json, which git ignores.
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class Check(BaseModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=300)


class ScorePoint(BaseModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=300)
    max: float = Field(gt=0, le=100)


class Form(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    description: str = ""
    checks: list[Check] = Field(max_length=40)
    scores: list[ScorePoint] = Field(max_length=40)

    @field_validator("checks", "scores")
    @classmethod
    def _unique_keys(cls, items: list[Check] | list[ScorePoint]) -> list[Check] | list[ScorePoint]:
        keys = [item.key for item in items]
        if len(keys) != len(set(keys)):
            raise ValueError("the keys must be unique")
        return items

    @property
    def score_max(self) -> float:
        return float(sum(point.max for point in self.scores))


def load_form(path: str | Path) -> Form:
    """Reads and checks the form; a bad file fails the start, not a call."""
    text = Path(path).read_text(encoding="utf-8")
    return Form.model_validate(json.loads(text))
