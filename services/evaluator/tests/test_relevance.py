"""Tests for the V1 relevance evaluator (app/relevance.py).

These tests are entirely self-contained: no apps/api, no PostgreSQL, no
ClickHouse, no Redis, no dashboard, and (see
test_relevance_evaluate_makes_no_network_calls) no network access at all.
"""

from __future__ import annotations

import socket

import pytest

from app.relevance import (
    DEFAULT_THRESHOLD,
    EVALUATOR_NAME,
    EVALUATOR_VERSION,
    RelevanceEvaluator,
    RelevanceEvaluatorInput,
)
from app.types import EvaluationResult, InvalidEvaluatorInputError

# Deliberately zero shared vocabulary after English-stop-word removal, so
# TF-IDF cosine similarity is mathematically guaranteed to be exactly
# 0.0 -- no dependency on the exact IDF weighting to make this example
# unambiguous.
_UNRELATED_INPUT = "What is the capital of France?"
_UNRELATED_OUTPUT = "Bananas contain potassium and dietary fiber."

_RELEVANT_INPUT = "What is the capital of France?"
_RELEVANT_OUTPUT = "The capital of France is Paris."


# -- construction / metadata --------------------------------------------


def test_evaluator_name_and_version_are_stable_identifiers() -> None:
    evaluator = RelevanceEvaluator()
    assert evaluator.name == EVALUATOR_NAME == "relevance"
    assert evaluator.version == EVALUATOR_VERSION
    assert isinstance(evaluator.version, str) and evaluator.version


def test_result_carries_the_evaluators_own_name_and_version() -> None:
    evaluator = RelevanceEvaluator()
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert result.evaluator_name == evaluator.name
    assert result.evaluator_version == evaluator.version


def test_result_reports_a_local_model_and_no_provider() -> None:
    """No third-party call is made -- evaluator_provider must stay None,
    per docs/decisions/004-evaluation-engine.md section 8."""
    evaluator = RelevanceEvaluator()
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert result.evaluator_model == "tfidf-cosine"
    assert result.evaluator_provider is None
    assert result.evaluation_cost_usd is None


def test_constructor_rejects_out_of_range_threshold() -> None:
    with pytest.raises(ValueError):
        RelevanceEvaluator(threshold=1.5)
    with pytest.raises(ValueError):
        RelevanceEvaluator(threshold=-0.1)


def test_constructor_accepts_boundary_thresholds() -> None:
    RelevanceEvaluator(threshold=0.0)
    RelevanceEvaluator(threshold=1.0)


# -- valid relevant / irrelevant examples --------------------------------


def test_valid_relevant_example_is_labeled_relevant() -> None:
    evaluator = RelevanceEvaluator()
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert result.score is not None
    assert result.score >= DEFAULT_THRESHOLD
    assert result.label == "relevant"


def test_clearly_unrelated_input_output_scores_zero_and_not_relevant() -> None:
    evaluator = RelevanceEvaluator()
    result = evaluator.evaluate(RelevanceEvaluatorInput(_UNRELATED_INPUT, _UNRELATED_OUTPUT))
    # Zero shared vocabulary after stop-word removal -> orthogonal TF-IDF
    # vectors -> cosine similarity is exactly 0.0, not just "low".
    assert result.score == pytest.approx(0.0)
    assert result.label == "not_relevant"


def test_identical_input_and_output_scores_one_and_relevant() -> None:
    evaluator = RelevanceEvaluator()
    text = "The quarterly revenue report shows a 12 percent increase."
    result = evaluator.evaluate(RelevanceEvaluatorInput(text, text))
    assert result.score == pytest.approx(1.0)
    assert result.label == "relevant"


# -- empty / whitespace-only handling ------------------------------------


@pytest.mark.parametrize(
    ("input_text", "output_text"),
    [
        ("", "The capital of France is Paris."),
        ("What is the capital of France?", ""),
        ("", ""),
        ("   \n\t  ", "The capital of France is Paris."),
        ("What is the capital of France?", "   \n\t  "),
        ("   ", "   "),
    ],
)
def test_empty_or_whitespace_only_text_is_not_evaluable(input_text: str, output_text: str) -> None:
    evaluator = RelevanceEvaluator()
    result = evaluator.evaluate(RelevanceEvaluatorInput(input_text, output_text))
    assert result.score is None
    assert result.label == "not_evaluable"
    assert result.explanation  # a real, non-empty reason is always given


def test_text_with_only_stopwords_and_punctuation_is_not_evaluable() -> None:
    """Non-empty, non-whitespace text that still has no comparable
    vocabulary after stop-word removal (sklearn raises "empty
    vocabulary" internally) must be handled the same way as empty text,
    not raised as an unhandled exception."""
    evaluator = RelevanceEvaluator()
    result = evaluator.evaluate(RelevanceEvaluatorInput("the a is of", "... !! ???"))
    assert result.score is None
    assert result.label == "not_evaluable"


# -- determinism / score bounds ------------------------------------------


def test_repeated_evaluation_of_the_same_input_is_deterministic() -> None:
    evaluator = RelevanceEvaluator()
    evaluator_input = RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT)
    first = evaluator.evaluate(evaluator_input)
    second = evaluator.evaluate(evaluator_input)
    third = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert first.score == second.score == third.score
    assert first.label == second.label == third.label


@pytest.mark.parametrize(
    ("input_text", "output_text"),
    [
        (_RELEVANT_INPUT, _RELEVANT_OUTPUT),
        (_UNRELATED_INPUT, _UNRELATED_OUTPUT),
        ("Explain photosynthesis in one sentence.", "Photosynthesis converts light into energy."),
        ("Is the store open on Sundays?", "Yes, we are open every day from 9am to 6pm."),
    ],
)
def test_score_is_always_within_zero_to_one(input_text: str, output_text: str) -> None:
    evaluator = RelevanceEvaluator()
    result = evaluator.evaluate(RelevanceEvaluatorInput(input_text, output_text))
    assert result.score is not None
    assert 0.0 <= result.score <= 1.0


# -- threshold / label behavior -------------------------------------------


def test_lower_threshold_can_flip_the_same_pair_to_relevant() -> None:
    evaluator = RelevanceEvaluator(threshold=0.0)
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert result.label == "relevant"


def test_higher_threshold_can_flip_the_same_pair_to_not_relevant() -> None:
    evaluator = RelevanceEvaluator(threshold=0.999999)
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert result.label == "not_relevant"


def test_threshold_boundary_is_inclusive_of_relevant() -> None:
    """A score exactly equal to the threshold counts as relevant (>=, not >)."""
    evaluator = RelevanceEvaluator(threshold=1.0)
    text = "identical text on both sides"
    result = evaluator.evaluate(RelevanceEvaluatorInput(text, text))
    assert result.score == pytest.approx(1.0)
    assert result.label == "relevant"


# -- malformed input -------------------------------------------------------


def test_non_string_input_text_raises_at_construction() -> None:
    with pytest.raises(InvalidEvaluatorInputError):
        RelevanceEvaluatorInput(input_text=123, output_text="valid text")  # type: ignore[arg-type]


def test_non_string_output_text_raises_at_construction() -> None:
    with pytest.raises(InvalidEvaluatorInputError):
        RelevanceEvaluatorInput(input_text="valid text", output_text=None)  # type: ignore[arg-type]


def test_evaluate_rejects_the_wrong_input_type_entirely() -> None:
    evaluator = RelevanceEvaluator()
    with pytest.raises(InvalidEvaluatorInputError):
        evaluator.evaluate("just a string, not a RelevanceEvaluatorInput")  # type: ignore[arg-type]
    with pytest.raises(InvalidEvaluatorInputError):
        evaluator.evaluate({"input_text": "x", "output_text": "y"})  # type: ignore[arg-type]
    with pytest.raises(InvalidEvaluatorInputError):
        evaluator.evaluate(None)  # type: ignore[arg-type]


# -- no network access ------------------------------------------------------


def test_relevance_evaluate_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: if a future change swapped in a networked
    embedding provider without updating this evaluator's contract, this
    test fails immediately. Blocks socket connections at the lowest
    level so it catches any HTTP client, not just a specific library."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("RelevanceEvaluator.evaluate attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)

    evaluator = RelevanceEvaluator()
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert isinstance(result, EvaluationResult)


def test_latency_is_measured_and_non_negative() -> None:
    evaluator = RelevanceEvaluator()
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert result.evaluation_latency_ms >= 0.0
