"""Experimental candidate relevance evaluator: dense semantic embedding cosine similarity.

Not the V1 production evaluator -- `app/relevance.py`'s TF-IDF lexical baseline keeps that role
unchanged. This module is the "next milestone" ADR 004 section 10 calls for: an embedding-based
implementation of the same relevance question, built to be validated head-to-head against the
TF-IDF baseline via the offline WikiQA harness (see `validation/wikiqa_embedding.py` and
`validation/reports/wikiqa_comparison.md` for the resulting evidence and decision). See
`README.md`'s "Embedding relevance evaluator (experimental)" section for the full model-selection
comparison that led to the choice below.

Algorithm: encode `input_text` and `output_text` independently with a local ONNX sentence-embedding
model (`BAAI/bge-small-en-v1.5`, run via the `fastembed` package's `TextEmbedding`, MIT-licensed
model and library), then score with cosine similarity between the two resulting dense vectors.
`fastembed` already L2-normalizes its output embeddings (see this module's own determinism test),
so cosine similarity reduces to a plain dot product -- computed directly rather than through a
general cosine-similarity routine, to avoid a redundant, already-known-to-be-1.0 norm computation.

Cosine similarity's natural range is `[-1.0, 1.0]`, unlike TF-IDF's non-negative-by-construction
`[0.0, 1.0]`. This module rescales via `score = (cosine + 1) / 2` -- a fixed, monotonic, fully
documented linear remap, not an arbitrary rescaling -- so this evaluator's score occupies the same
`[0.0, 1.0]` contract as `app/relevance.py`'s (directly comparable in the WikiQA report, and
directly compatible with `validation/metrics.sweep_thresholds`'s `[0.0, 1.0]` sweep range, which
this evaluator reuses unmodified). The remap is rank-preserving, so it changes no evaluator's
relative ordering of examples and therefore does not affect ROC-AUC.

Two distinct phases, deliberately separated -- see this module's tests and
`validation/wikiqa_embedding.py`'s module docstring for how each is exercised:

- **First-time model acquisition** (network): the first time a process constructs
  `EmbeddingRelevanceEvaluator` with no cached weights at `cache_dir`, `fastembed` downloads the
  ~67 MB quantized ONNX model + tokenizer from Hugging Face into `cache_dir` -- outside this
  repository (see `DEFAULT_CACHE_DIR` below), never committed to git.
- **Offline inference** (no network): every `evaluate()` call thereafter -- and every call in a
  process where the cache was already populated by a prior run -- is pure local ONNX Runtime
  computation. `tests/test_embedding_relevance.py::test_evaluate_makes_no_network_calls` blocks
  sockets around `evaluate()` only (after construction/model-load has already completed) and
  asserts this holds.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

from app.relevance import RelevanceEvaluatorInput
from app.types import EvaluationResult, InvalidEvaluatorInputError

EVALUATOR_NAME = "relevance_embedding"
EVALUATOR_VERSION = "0.1.0"
MODEL_NAME = "BAAI/bge-small-en-v1.5"

#: Where `fastembed` caches downloaded model weights. Deliberately outside this repository (a
#: fixed location under the invoking user's home directory, mirroring where `huggingface_hub`
#: caches models by default) so model weights are never a candidate for `git add` -- there is no
#: relative, in-repo path anywhere in this module for a weights file to land on. Overridable via
#: `VIGIL_EVALUATOR_EMBEDDING_CACHE_DIR` for environments (e.g. CI, containers) that want an
#: explicit, shared cache location instead of the invoking user's home directory.
DEFAULT_CACHE_DIR = str(Path.home() / ".cache" / "vigil-evaluator" / "fastembed")

#: UNVALIDATED placeholder, exactly like `app.relevance.DEFAULT_THRESHOLD` -- see that constant's
#: docstring. Not checked against labeled data until the WikiQA validation harness runs; see
#: `validation/reports/wikiqa_embedding.md` for the threshold that harness actually selects.
DEFAULT_THRESHOLD = 0.5

_LABEL_RELEVANT = "relevant"
_LABEL_NOT_RELEVANT = "not_relevant"
_LABEL_NOT_EVALUABLE = "not_evaluable"


class EmbeddingRelevanceEvaluator:
    """Implements the `Evaluator[RelevanceEvaluatorInput]` protocol (see `app/interface.py`).

    Reuses `app.relevance.RelevanceEvaluatorInput` as its input type rather than defining a
    duplicate `(input_text, output_text)` dataclass -- both evaluators answer the same relevance
    question over the same plain-text-pair shape, and `app/relevance.py` is only ever *imported
    from* here, never modified, so the TF-IDF baseline stays exactly as validated.

    Unlike `RelevanceEvaluator` (which is cheap to construct many times -- no state to load),
    constructing this class loads an ONNX inference session and is deliberately meant to happen
    once per process and be reused across many `evaluate()` calls -- the same lifecycle
    `services/worker` (not yet built) is expected to use in production: one evaluator instance,
    many spans.
    """

    name = EVALUATOR_NAME
    version = EVALUATOR_VERSION

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        model_name: str = MODEL_NAME,
        cache_dir: str | None = None,
        threads: int | None = None,
    ) -> None:
        """`threshold` (on the rescaled `[0.0, 1.0]` score) is the sole, configurable cutpoint
        between `relevant` and `not_relevant` -- see `DEFAULT_THRESHOLD`'s docstring for why the
        default is an explicitly unvalidated placeholder.

        `cache_dir` defaults to `DEFAULT_CACHE_DIR`, or the
        `VIGIL_EVALUATOR_EMBEDDING_CACHE_DIR` environment variable when set and `cache_dir` is not
        explicitly passed -- either way, always outside this repository. `threads` is exposed
        (default `None`, meaning "let ONNX Runtime choose") because pinning it to `1` is the
        strongest available lever for cross-environment determinism if a future caller needs it;
        this module's own tests observed bit-identical repeated output at the default setting on
        this development platform, but -- exactly like `app/relevance.py`'s README already notes
        for neural inference in general -- floating-point non-associativity across different
        CPU architectures/BLAS backends/thread counts is a known real source of tiny cross-platform
        discrepancy that this evaluator does not claim to have ruled out on every possible target
        platform.
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be within [0.0, 1.0], got {threshold!r}.")
        self._threshold = threshold
        self._model_name = model_name

        resolved_cache_dir = (
            cache_dir
            if cache_dir is not None
            else os.environ.get("VIGIL_EVALUATOR_EMBEDDING_CACHE_DIR", DEFAULT_CACHE_DIR)
        )
        self._model = TextEmbedding(
            model_name=model_name, cache_dir=resolved_cache_dir, threads=threads
        )

    def evaluate(self, evaluator_input: RelevanceEvaluatorInput) -> EvaluationResult:
        start = time.perf_counter()

        if not isinstance(evaluator_input, RelevanceEvaluatorInput):
            raise InvalidEvaluatorInputError(
                f"EmbeddingRelevanceEvaluator.evaluate requires a RelevanceEvaluatorInput, "
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

        input_vector, output_vector = (
            np.asarray(v) for v in self._model.embed([input_stripped, output_stripped])
        )
        score = _rescaled_cosine_similarity(input_vector, output_vector)

        label = _LABEL_RELEVANT if score >= self._threshold else _LABEL_NOT_RELEVANT
        explanation = (
            f"Embedding cosine similarity (rescaled to [0.0, 1.0]) between input and output was "
            f"{score:.4f}; threshold={self._threshold:.4f} -> label={label!r}."
        )

        return EvaluationResult(
            evaluator_name=self.name,
            evaluator_version=self.version,
            score=score,
            label=label,
            explanation=explanation,
            evaluation_latency_ms=_elapsed_ms(start),
            evaluation_cost_usd=None,
            evaluator_model=self._model_name,
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
            evaluator_model=self._model_name,
            evaluator_provider=None,
        )


def _rescaled_cosine_similarity(vector_a: np.ndarray, vector_b: np.ndarray) -> float:
    # `fastembed`'s output embeddings are already L2-normalized (norm == 1.0), so cosine
    # similarity is just the dot product -- no separate norm division needed. Still clip
    # defensively against floating-point overshoot before rescaling, the same discipline
    # `app/relevance.py` applies to its own similarity computation.
    raw_cosine = float(np.dot(vector_a, vector_b))
    raw_cosine = max(-1.0, min(1.0, raw_cosine))
    rescaled = (raw_cosine + 1.0) / 2.0
    return max(0.0, min(1.0, rescaled))


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000
