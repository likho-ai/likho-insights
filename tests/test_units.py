"""The form, the prompt and the reading of a model's answer: no database or network needed."""

import json
from pathlib import Path

import pytest

from likho_insights.analyser import build_prompt, interpret
from likho_insights.form import Form, load_form
from likho_insights.model import ModelError, parse_json
from tests.conftest import GOOD_ANSWER, make_transcript

FORM = load_form("config/qa.example.json")


class TestForm:
    def test_the_example_form_loads(self) -> None:
        assert FORM.version == "example-1"
        assert len(FORM.checks) == 8 and len(FORM.scores) == 3
        assert FORM.score_max == 20

    def test_keys_must_be_unique_and_well_formed(self, tmp_path: Path) -> None:
        bad = tmp_path / "qa.json"
        bad.write_text(
            json.dumps(
                {"version": "x", "checks": [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}], "scores": []}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unique"):
            load_form(bad)
        with pytest.raises(ValueError):
            Form.model_validate({"version": "x", "checks": [{"key": "Bad Key", "label": "A"}], "scores": []})


class TestPrompt:
    def test_the_prompt_names_every_check_and_score_and_carries_the_lines(self) -> None:
        prompt = build_prompt(FORM, make_transcript("trn_1", "rec_1"), "English", 24_000)
        for check in FORM.checks:
            assert f'"{check.key}": {check.label}' in prompt.system
        for point in FORM.scores:
            assert f'"{point.key}": {point.label} (0 to {point.max:g})' in prompt.system
        assert "[00:05] aapne Ashwagandha ke liye call kiya tha" in prompt.user
        assert "5 lines" in prompt.user and not prompt.cut
        assert "JSON" in prompt.system

    def test_a_long_transcript_is_cut_with_a_note(self) -> None:
        prompt = build_prompt(FORM, make_transcript("trn_1", "rec_1"), "English", 80)
        assert prompt.cut
        assert "left out for length" in prompt.user and "shortened" in prompt.user
        assert "[00:20]" not in prompt.user


class TestInterpret:
    def test_a_good_answer_is_read_against_the_form(self) -> None:
        read = interpret(GOOD_ANSWER, FORM)
        assert read["sentiment"] == "positive" and read["products"] == ["Ashwagandha"]
        assert [c["key"] for c in read["checks"]] == [c.key for c in FORM.checks]
        assert read["checks"][0] == {
            "key": "greeting",
            "label": FORM.checks[0].label,
            "answer": "yes",
            "evidence": "namaste, Herbal House se bol raha hoon",
        }
        assert read["checks"][2]["answer"] == "na"
        assert read["score_total"] == 16 and read["score_max"] == 20
        assert read["scores"][2] == {
            "key": "resolution",
            "label": FORM.scores[2].label,
            "score": 9,
            "max": 10,
            "reason": "The order was confirmed.",
        }

    def test_what_is_missing_or_out_of_bounds_is_tamed(self) -> None:
        read = interpret(
            {
                "summary": "  Short.  ",
                "sentiment": "ecstatic",
                "products": "not a list",
                "checks": [{"key": "greeting", "answer": "YES"}, {"key": "unknown", "answer": "yes"}],
                "scores": {"resolution": {"score": 42, "reason": "too much"}, "knowledge": "3"},
            },
            FORM,
        )
        assert read["summary"] == "Short." and read["sentiment"] == "neutral" and read["products"] == []
        by_key = {c["key"]: c for c in read["checks"]}
        assert by_key["greeting"]["answer"] == "yes" and by_key["closing"]["answer"] == "na"
        assert "unknown" not in by_key
        scores = {s["key"]: s for s in read["scores"]}
        assert scores["resolution"]["score"] == 10  # clamped to the maximum
        assert scores["knowledge"]["score"] == 3
        assert scores["communication"] == {
            "key": "communication",
            "label": FORM.scores[0].label,
            "score": 0,
            "max": 5,
            "reason": "The model gave no score.",
        }
        assert read["score_total"] == 13

    def test_no_summary_is_a_bad_answer(self) -> None:
        with pytest.raises(ModelError) as error:
            interpret({"sentiment": "positive"}, FORM)
        assert error.value.code == "bad_answer"


class TestParseJson:
    def test_json_with_or_without_a_fence_or_chatter(self) -> None:
        assert parse_json('{"a": 1}') == {"a": 1}
        assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert parse_json('Here you go:\n{"a": {"b": 2}}\nThanks.') == {"a": {"b": 2}}

    def test_not_json_is_a_bad_answer(self) -> None:
        for text in ("no json here", "[1, 2]", "{broken"):
            with pytest.raises(ModelError) as error:
                parse_json(text)
            assert error.value.code == "bad_answer" and not error.value.retryable
