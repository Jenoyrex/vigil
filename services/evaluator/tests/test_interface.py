"""Tests for the Evaluator protocol and EvaluationResult (app/interface.py,
app/types.py), independent of any specific evaluator implementation."""

from __future__ import annotations

import dataclasses

import pytest

from app.interface import Evaluator
from app.relevance import RelevanceEvaluator, RelevanceEvaluatorInput
from app.types import EvaluationResult, InvalidEvaluatorInputError


def test_relevance_evaluator_satisfies_the_evaluator_protocol() -> None:
    evaluator = RelevanceEvaluator()
    assert isinstance(evaluator, Evaluator)


def test_evaluator_protocol_requires_name_version_and_evaluate() -> None:
    class NotAnEvaluator:
        pass

    assert not isinstance(NotAnEvaluator(), Evaluator)


def test_something_with_the_right_shape_but_no_inheritance_still_satisfies_protocol() -> None:
    """The whole point of a structural Protocol: no shared base class is
    required, only the matching shape."""

    class DuckTypedEvaluator:
        name = "duck"
        version = "0.0.1"

        def evaluate(self, evaluator_input: object) -> EvaluationResult:
            return EvaluationResult(
                evaluator_name=self.name,
                evaluator_version=self.version,
                score=1.0,
                label="relevant",
                explanation="stub",
                evaluation_latency_ms=0.0,
            )

    assert isinstance(DuckTypedEvaluator(), Evaluator)


def test_evaluation_result_is_frozen() -> None:
    result = EvaluationResult(
        evaluator_name="relevance",
        evaluator_version="0.1.0",
        score=0.9,
        label="relevant",
        explanation="example",
        evaluation_latency_ms=1.2,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.score = 0.1  # type: ignore[misc]


def test_evaluation_result_defaults_cost_model_and_provider_to_none() -> None:
    result = EvaluationResult(
        evaluator_name="relevance",
        evaluator_version="0.1.0",
        score=0.9,
        label="relevant",
        explanation="example",
        evaluation_latency_ms=1.2,
    )
    assert result.evaluation_cost_usd is None
    assert result.evaluator_model is None
    assert result.evaluator_provider is None


def test_invalid_evaluator_input_error_is_a_value_error() -> None:
    assert issubclass(InvalidEvaluatorInputError, ValueError)


def test_relevance_evaluator_input_round_trips_via_dataclasses_fields() -> None:
    evaluator_input = RelevanceEvaluatorInput(input_text="a", output_text="b")
    field_names = {f.name for f in dataclasses.fields(evaluator_input)}
    assert field_names == {"input_text", "output_text"}
