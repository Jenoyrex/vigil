"""Tests for worker.registry.EvaluatorRegistry.

Uses one module-scoped real registry (constructing both production
evaluators, including `EmbeddingRelevanceEvaluator`'s ONNX session load) --
mirroring services/evaluator/tests/test_embedding_relevance.py's own
module-scoped fixture, so that load cost is paid at most once per test run.
"""

from __future__ import annotations

import pytest
from app.embedding_relevance import EVALUATOR_NAME as EMBEDDING_NAME
from app.embedding_relevance import EVALUATOR_VERSION as EMBEDDING_VERSION
from app.relevance import EVALUATOR_NAME as RELEVANCE_NAME
from app.relevance import EVALUATOR_VERSION as RELEVANCE_VERSION
from app.relevance import RelevanceEvaluatorInput

from worker.registry import EvaluatorRegistry, UnknownEvaluatorError


@pytest.fixture(scope="module")
def registry() -> EvaluatorRegistry:
    return EvaluatorRegistry()


def test_registers_exactly_the_two_production_relevance_evaluators(
    registry: EvaluatorRegistry,
) -> None:
    assert registry.registered_keys() == {
        (RELEVANCE_NAME, RELEVANCE_VERSION),
        (EMBEDDING_NAME, EMBEDDING_VERSION),
    }


def test_get_returns_the_tfidf_relevance_evaluator(registry: EvaluatorRegistry) -> None:
    evaluator = registry.get(RELEVANCE_NAME, RELEVANCE_VERSION)
    assert evaluator.name == RELEVANCE_NAME
    assert evaluator.version == RELEVANCE_VERSION


def test_get_returns_the_embedding_relevance_evaluator(registry: EvaluatorRegistry) -> None:
    evaluator = registry.get(EMBEDDING_NAME, EMBEDDING_VERSION)
    assert evaluator.name == EMBEDDING_NAME
    assert evaluator.version == EMBEDDING_VERSION


def test_get_raises_for_unknown_evaluator_name(registry: EvaluatorRegistry) -> None:
    with pytest.raises(UnknownEvaluatorError):
        registry.get("groundedness", "0.1.0")


def test_get_raises_for_unregistered_version_of_a_known_evaluator(
    registry: EvaluatorRegistry,
) -> None:
    """Version skew during a rolling deploy: a worker fleet that no longer
    ships (or never shipped) this exact version must not silently fall
    back to whatever version it does have -- see registry.py's
    `UnknownEvaluatorError` docstring."""
    with pytest.raises(UnknownEvaluatorError):
        registry.get(RELEVANCE_NAME, "99.0.0")


def test_get_returns_the_same_instance_across_repeated_lookups(registry: EvaluatorRegistry) -> None:
    """Instance reuse: the whole point of registering once at construction
    time is that repeated dispatch never re-constructs (and, for the
    embedding evaluator, never reloads the ONNX session)."""
    first = registry.get(EMBEDDING_NAME, EMBEDDING_VERSION)
    second = registry.get(EMBEDDING_NAME, EMBEDDING_VERSION)
    assert first is second


def test_threshold_is_passed_per_call_without_mutating_the_shared_instance(
    registry: EvaluatorRegistry,
) -> None:
    """The same registry-held instance, looked up twice, must honor a
    different per-call threshold each time with no leakage between calls --
    this is what actually lets one instance serve many projects'
    independently configured thresholds."""
    evaluator = registry.get(RELEVANCE_NAME, RELEVANCE_VERSION)
    evaluator_input = RelevanceEvaluatorInput(
        input_text="What is the capital of France?",
        output_text="The capital of France is Paris.",
    )

    high_threshold_result = evaluator.evaluate(evaluator_input, threshold=0.999999)
    assert high_threshold_result.label == "not_relevant"

    low_threshold_result = evaluator.evaluate(evaluator_input, threshold=0.0)
    assert low_threshold_result.label == "relevant"

    # No override -> back to this instance's own unmutated default
    # (DEFAULT_THRESHOLD = 0.5; this pair's TF-IDF cosine similarity is
    # already independently asserted >= 0.5 in
    # services/evaluator/tests/test_relevance.py).
    default_result = evaluator.evaluate(evaluator_input)
    assert default_result.label == "relevant"
    assert evaluator is registry.get(RELEVANCE_NAME, RELEVANCE_VERSION)
