"""V1 relevance evaluator: does an LLM span's output address its input?

Per docs/decisions/004-evaluation-engine.md section 1, this is the only
evaluator implemented in V1, and it is deliberately local-only. See
README.md's "Dependency and model choice" section for the full,
evidence-based comparison against fastembed and sentence-transformers
that led to the approach below, and its "Score and threshold semantics"
section for what `score`/`label` mean and why the threshold is not yet
validated.

Algorithm: TF-IDF vectorization of `input_text` and `output_text`, fit
per-pair -- on just those two texts, since there is no larger corpus to
fit against here -- scored by cosine similarity between the two
resulting vectors. TF-IDF vectors have only non-negative entries, so
their cosine similarity is always within [0.0, 1.0] already; no
artificial rescaling is applied, only defensive clipping against
floating-point overshoot. This is a *lexical* (word-overlap-weighted)
similarity, not a *semantic* one -- see README.md's "Known limitations."

No network call is made anywhere in this module, at import time,
construction time, or evaluate() time: `TfidfVectorizer` is pure local
computation with no pretrained weights to load or download.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.types import EvaluationResult, InvalidEvaluatorInputError

EVALUATOR_NAME = "relevance"
EVALUATOR_VERSION = "0.1.0"
MODEL_NAME = "tfidf-cosine"

#: UNVALIDATED placeholder. See README.md "Score and threshold
#: semantics" and docs/decisions/004-evaluation-engine.md section 10 --
#: this number has not been checked against any labeled data. It exists
#: so the evaluator has *a* default, not because 0.5 has been shown to
#: be a good cutpoint.
DEFAULT_THRESHOLD = 0.5

_LABEL_RELEVANT = "relevant"
_LABEL_NOT_RELEVANT = "not_relevant"
_LABEL_NOT_EVALUABLE = "not_evaluable"


@dataclass(frozen=True)
class RelevanceEvaluatorInput:
    """Plain text pair for relevance evaluation.

    Deliberately `str`, not the arbitrary JSON a span's `input`/`output`
    can hold (docs/decisions/002-trace-span-telemetry-model.md section
    2) -- converting a raw span's JSON input/output into evaluatable
    text is an adapter concern for whoever calls this evaluator
    (services/worker, not yet built), not this evaluator's job. Keeping
    this evaluator's own contract to plain strings is what makes it
    independently testable with no Vigil-internal types at all.

    Validated at construction time -- see `__post_init__` -- so
    `RelevanceEvaluator.evaluate` can trust it received a well-typed
    input and never needs to re-check.
    """

    input_text: str
    output_text: str

    def __post_init__(self) -> None:
        for field_name in ("input_text", "output_text"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise InvalidEvaluatorInputError(
                    f"RelevanceEvaluatorInput.{field_name} must be a str, "
                    f"got {type(value).__name__}."
                )


class _NoVocabularyError(Exception):
    """Internal: both texts had no vocabulary left after tokenization and
    stop-word removal (e.g. both were pure punctuation or numbers)."""


class RelevanceEvaluator:
    """Implements the `Evaluator[RelevanceEvaluatorInput]` protocol (see
    interface.py). See the module docstring for the algorithm and
    README.md for the full rationale, score semantics, and limitations.
    """

    name = EVALUATOR_NAME
    version = EVALUATOR_VERSION

    def __init__(self, *, threshold: float = DEFAULT_THRESHOLD) -> None:
        """`threshold` (on the [0.0, 1.0] score) is the sole,
        configurable cutpoint between `relevant` and `not_relevant`. It
        must be configurable, and must default to an explicitly-flagged
        placeholder, because it has not been scientifically validated
        yet -- see docs/decisions/004-evaluation-engine.md section 10:
        that validation is the next milestone, not this one.
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be within [0.0, 1.0], got {threshold!r}.")
        self._threshold = threshold

    def evaluate(self, evaluator_input: RelevanceEvaluatorInput) -> EvaluationResult:
        start = time.perf_counter()

        if not isinstance(evaluator_input, RelevanceEvaluatorInput):
            raise InvalidEvaluatorInputError(
                f"RelevanceEvaluator.evaluate requires a RelevanceEvaluatorInput, "
                f"got {type(evaluator_input).__name__}."
            )

        input_stripped = evaluator_input.input_text.strip()
        output_stripped = evaluator_input.output_text.strip()

        if not input_stripped or not output_stripped:
            missing = []
            if not input_stripped:
                missing.append("input_text")
            if not output_stripped:
                missing.append("output_text")
            return self._not_evaluable_result(
                start,
                "Relevance requires non-whitespace text on both sides; "
                f"{' and '.join(missing)} contained none.",
            )

        try:
            score = _tfidf_cosine_similarity(input_stripped, output_stripped)
        except _NoVocabularyError:
            return self._not_evaluable_result(
                start,
                "input_text and output_text contained no comparable vocabulary after "
                "removing English stop words (e.g. both consisted only of punctuation, "
                "numbers, or common stop words).",
            )

        label = _LABEL_RELEVANT if score >= self._threshold else _LABEL_NOT_RELEVANT
        explanation = (
            f"TF-IDF cosine similarity between input and output was {score:.4f} "
            f"(range [0.0, 1.0]); threshold={self._threshold:.4f} -> label={label!r}."
        )

        return EvaluationResult(
            evaluator_name=self.name,
            evaluator_version=self.version,
            score=score,
            label=label,
            explanation=explanation,
            evaluation_latency_ms=_elapsed_ms(start),
            evaluation_cost_usd=None,
            evaluator_model=MODEL_NAME,
            evaluator_provider=None,
        )

    def _not_evaluable_result(self, start: float, explanation: str) -> EvaluationResult:
        return EvaluationResult(
            evaluator_name=self.name,
            evaluator_version=self.version,
            score=None,
            label=_LABEL_NOT_EVALUABLE,
            explanation=explanation,
            evaluation_latency_ms=_elapsed_ms(start),
            evaluation_cost_usd=None,
            evaluator_model=MODEL_NAME,
            evaluator_provider=None,
        )


def _tfidf_cosine_similarity(text_a: str, text_b: str) -> float:
    vectorizer = TfidfVectorizer(stop_words="english")
    try:
        matrix = vectorizer.fit_transform([text_a, text_b])
    except ValueError as exc:
        # sklearn raises ValueError("empty vocabulary; ...") for this case.
        raise _NoVocabularyError from exc

    raw_similarity = float(cosine_similarity(matrix[0], matrix[1])[0, 0])
    # TF-IDF vectors are non-negative, so cosine similarity is
    # mathematically within [0.0, 1.0] already; clip defensively against
    # floating-point overshoot (e.g. 1.0000000000000002) rather than
    # assume no library/platform ever produces one.
    return max(0.0, min(1.0, raw_similarity))


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000
