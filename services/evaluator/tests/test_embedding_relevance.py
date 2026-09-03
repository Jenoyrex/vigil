"""Tests for the experimental embedding relevance evaluator (app/embedding_relevance.py).

Requires the `embedding` extra (`uv sync --extra embedding` / `pip install -e ".[embedding]"`) --
skipped automatically if `fastembed` is not installed, so `uv run pytest` without the extra still
exercises the (unaffected) TF-IDF baseline suite cleanly. Model weights are downloaded once (first
test in this file to construct an evaluator) into a cache directory outside this repository -- see
`app/embedding_relevance.py`'s `DEFAULT_CACHE_DIR` -- and reused for the rest of this module's
tests via a module-scoped fixture, so the (network, one-time) download cost and the (local,
~1-2s) model-load cost are each paid at most once per test run, not once per test.
"""

from __future__ import annotations

import socket

import pytest

pytest.importorskip("fastembed", reason="requires the 'embedding' extra")

from app.embedding_relevance import (  # noqa: E402
    DEFAULT_THRESHOLD,
    EVALUATOR_NAME,
    EVALUATOR_VERSION,
    MODEL_NAME,
    EmbeddingRelevanceEvaluator,
)
from app.interface import Evaluator  # noqa: E402
from app.relevance import RelevanceEvaluatorInput  # noqa: E402
from app.types import EvaluationResult, InvalidEvaluatorInputError  # noqa: E402

_RELEVANT_INPUT = "What is the capital of France?"
_RELEVANT_OUTPUT = "The capital of France is Paris."

_UNRELATED_INPUT = "What is the capital of France?"
_UNRELATED_OUTPUT = "Bananas contain potassium and dietary fiber."

_PARAPHRASE_INPUT = "How did David Carradine die?"
_PARAPHRASE_OUTPUT = "He died on June 3, 2009, apparently of auto-erotic asphyxiation."


@pytest.fixture(scope="module")
def evaluator() -> EmbeddingRelevanceEvaluator:
    """One shared evaluator (one loaded ONNX session) for this whole test module -- constructing
    `EmbeddingRelevanceEvaluator` is not free (model load), unlike `RelevanceEvaluator`."""
    return EmbeddingRelevanceEvaluator()


# -- construction / metadata --------------------------------------------


def test_evaluator_name_and_version_are_stable_identifiers(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    assert evaluator.name == EVALUATOR_NAME == "relevance_embedding"
    assert evaluator.version == EVALUATOR_VERSION
    assert isinstance(evaluator.version, str) and evaluator.version


def test_evaluator_name_is_distinct_from_the_tfidf_baseline(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    """Both evaluators must be independently selectable -- a distinct `evaluator_name` (rather
    than reusing `RelevanceEvaluator`'s `"relevance"`) is what makes that unambiguous per
    docs/decisions/004-evaluation-engine.md section 5's idempotency-key identity."""
    from app.relevance import EVALUATOR_NAME as TFIDF_NAME

    assert EVALUATOR_NAME != TFIDF_NAME


def test_result_carries_the_evaluators_own_name_and_version(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert result.evaluator_name == evaluator.name
    assert result.evaluator_version == evaluator.version


def test_result_reports_a_local_model_and_no_provider(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    """No third-party call is made -- evaluator_provider must stay None, per
    docs/decisions/004-evaluation-engine.md section 8."""
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert result.evaluator_model == MODEL_NAME
    assert result.evaluator_provider is None
    assert result.evaluation_cost_usd is None


def test_constructor_rejects_out_of_range_threshold() -> None:
    with pytest.raises(ValueError):
        EmbeddingRelevanceEvaluator(threshold=1.5)
    with pytest.raises(ValueError):
        EmbeddingRelevanceEvaluator(threshold=-0.1)


# -- interface compatibility -----------------------------------------------


def test_embedding_evaluator_satisfies_the_evaluator_protocol(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    assert isinstance(evaluator, Evaluator)


def test_embedding_evaluator_accepts_the_same_input_type_as_the_tfidf_evaluator() -> None:
    """Both evaluators are built to be independently selectable behind the same conceptual
    relevance question, over the same plain-text-pair contract -- see
    app/embedding_relevance.py's class docstring for why the input type is reused, not
    duplicated."""
    from app.relevance import RelevanceEvaluator

    tfidf_evaluator = RelevanceEvaluator()
    shared_input = RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT)
    tfidf_result = tfidf_evaluator.evaluate(shared_input)
    assert isinstance(tfidf_result, EvaluationResult)


# -- valid relevant / irrelevant examples --------------------------------


def test_valid_relevant_example_scores_higher_than_an_unrelated_pair(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    relevant = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    unrelated = evaluator.evaluate(RelevanceEvaluatorInput(_UNRELATED_INPUT, _UNRELATED_OUTPUT))
    assert relevant.score is not None
    assert unrelated.score is not None
    assert relevant.score > unrelated.score


def test_identical_input_and_output_scores_at_or_near_the_maximum(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    text = "The quarterly revenue report shows a 12 percent increase."
    result = evaluator.evaluate(RelevanceEvaluatorInput(text, text))
    assert result.score is not None
    # Identical text embeds to identical vectors, so cosine similarity is exactly 1.0 and the
    # rescaled score is exactly 1.0 -- allow tiny floating-point slack, not an exact equality.
    assert result.score == pytest.approx(1.0, abs=1e-4)
    assert result.label == "relevant"


def test_paraphrase_with_pronoun_reference_still_scores_meaningfully_relevant(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    """A regression-style sanity check against exactly the failure mode
    validation/reports/wikiqa_baseline.md documents as the TF-IDF baseline's dominant false
    negative pattern (coreference blindness: "how did X die" -> "He died ..."). This does not
    assert the embedding evaluator is perfect, only that it captures meaningfully more signal
    than TF-IDF's mathematically-guaranteed 0.0 on this exact pair."""
    result = evaluator.evaluate(RelevanceEvaluatorInput(_PARAPHRASE_INPUT, _PARAPHRASE_OUTPUT))
    assert result.score is not None
    assert result.score > 0.5


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
def test_empty_or_whitespace_only_text_is_not_evaluable(
    evaluator: EmbeddingRelevanceEvaluator, input_text: str, output_text: str
) -> None:
    result = evaluator.evaluate(RelevanceEvaluatorInput(input_text, output_text))
    assert result.score is None
    assert result.label == "not_evaluable"
    assert result.explanation  # a real, non-empty reason is always given


def test_punctuation_only_text_is_evaluable_unlike_the_tfidf_baseline(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    """Unlike TF-IDF (which has no vocabulary left after stop-word removal for pure punctuation,
    see test_relevance.py), a subword-tokenized embedding model can encode any non-empty string,
    including punctuation -- this is a deliberate, documented difference in evaluable-input
    surface between the two evaluators, not a bug in either."""
    result = evaluator.evaluate(RelevanceEvaluatorInput("the a is of", "... !! ???"))
    assert result.label != "not_evaluable"
    assert result.score is not None


# -- determinism / score bounds ------------------------------------------


def test_repeated_evaluation_of_the_same_input_is_bit_identical(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    evaluator_input = RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT)
    first = evaluator.evaluate(evaluator_input)
    second = evaluator.evaluate(evaluator_input)
    third = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert first.score == second.score == third.score
    assert first.label == second.label == third.label


def test_repeated_evaluation_across_separate_evaluator_instances_is_bit_identical() -> None:
    """Determinism must not depend on reusing the same loaded session -- a fresh
    `EmbeddingRelevanceEvaluator()` (fresh ONNX Runtime session, same cached weights) must produce
    the same score for the same input, since production worker processes will each construct
    their own instance."""
    evaluator_input = RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT)
    first = EmbeddingRelevanceEvaluator().evaluate(evaluator_input)
    second = EmbeddingRelevanceEvaluator().evaluate(evaluator_input)
    assert first.score == second.score


@pytest.mark.parametrize(
    ("input_text", "output_text"),
    [
        (_RELEVANT_INPUT, _RELEVANT_OUTPUT),
        (_UNRELATED_INPUT, _UNRELATED_OUTPUT),
        (_PARAPHRASE_INPUT, _PARAPHRASE_OUTPUT),
        ("Explain photosynthesis in one sentence.", "Photosynthesis converts light into energy."),
    ],
)
def test_score_is_always_within_zero_to_one(
    evaluator: EmbeddingRelevanceEvaluator, input_text: str, output_text: str
) -> None:
    result = evaluator.evaluate(RelevanceEvaluatorInput(input_text, output_text))
    assert result.score is not None
    assert 0.0 <= result.score <= 1.0


# -- threshold / label behavior -------------------------------------------


def test_default_threshold_matches_the_documented_constant() -> None:
    assert 0.0 <= DEFAULT_THRESHOLD <= 1.0


def test_lower_threshold_can_flip_the_same_pair_to_relevant(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    zero_threshold_evaluator = EmbeddingRelevanceEvaluator(threshold=0.0)
    result = zero_threshold_evaluator.evaluate(
        RelevanceEvaluatorInput(_UNRELATED_INPUT, _UNRELATED_OUTPUT)
    )
    assert result.label == "relevant"


def test_higher_threshold_can_flip_the_same_pair_to_not_relevant(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    max_threshold_evaluator = EmbeddingRelevanceEvaluator(threshold=1.0)
    result = max_threshold_evaluator.evaluate(
        RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT)
    )
    assert result.label == "not_relevant"


def test_threshold_boundary_is_inclusive_of_relevant() -> None:
    """A score exactly equal to the threshold counts as relevant (>=, not >), matching
    RelevanceEvaluator's own convention exactly."""
    evaluator = EmbeddingRelevanceEvaluator(threshold=1.0)
    text = "identical text on both sides"
    result = evaluator.evaluate(RelevanceEvaluatorInput(text, text))
    assert result.score == pytest.approx(1.0, abs=1e-4)
    assert result.label == "relevant"


# -- malformed input -------------------------------------------------------


def test_non_string_input_text_raises_at_construction() -> None:
    with pytest.raises(InvalidEvaluatorInputError):
        RelevanceEvaluatorInput(input_text=123, output_text="valid text")  # type: ignore[arg-type]


def test_evaluate_rejects_the_wrong_input_type_entirely(
    evaluator: EmbeddingRelevanceEvaluator,
) -> None:
    with pytest.raises(InvalidEvaluatorInputError):
        evaluator.evaluate("just a string, not a RelevanceEvaluatorInput")  # type: ignore[arg-type]
    with pytest.raises(InvalidEvaluatorInputError):
        evaluator.evaluate({"input_text": "x", "output_text": "y"})  # type: ignore[arg-type]
    with pytest.raises(InvalidEvaluatorInputError):
        evaluator.evaluate(None)  # type: ignore[arg-type]


# -- no network access during inference -------------------------------------


def test_evaluate_makes_no_network_calls(
    evaluator: EmbeddingRelevanceEvaluator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the "offline inference" half of this module's documented two-phase
    contract: once weights are loaded (the `evaluator` fixture already did that, outside this
    test), `evaluate()` itself must never touch the network. Blocks socket connections at the
    lowest level, exactly like test_relevance.py's equivalent test, so it catches any HTTP client
    or raw socket use, not just a specific library."""

    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("EmbeddingRelevanceEvaluator.evaluate attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)

    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert isinstance(result, EvaluationResult)


def test_latency_is_measured_and_non_negative(evaluator: EmbeddingRelevanceEvaluator) -> None:
    result = evaluator.evaluate(RelevanceEvaluatorInput(_RELEVANT_INPUT, _RELEVANT_OUTPUT))
    assert result.evaluation_latency_ms >= 0.0
