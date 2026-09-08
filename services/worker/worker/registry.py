"""Evaluator registry: constructs every registered `Evaluator` implementation
exactly once and reuses those instances across every job dispatched to
them -- docs/decisions/005-evaluation-job-storage-worker.md section 6's
"construct once per worker process, never once per job" requirement,
because `EmbeddingRelevanceEvaluator`'s ONNX session load is expensive and
its (thread-safe) session should never be reloaded per job, per project, or
per configured threshold.

Registered for this milestone -- the two relevance evaluators ADR 005
section 12 names as production-selectable, explicitly:

- `("relevance", "0.1.0")` -- `RelevanceEvaluator`, the TF-IDF baseline.
- `("relevance_embedding", "0.1.0")` -- `EmbeddingRelevanceEvaluator`,
  `BAAI/bge-small-en-v1.5`, ADR 005 section 12's recommended first
  production evaluator.

No LLM-judge, groundedness, or faithfulness evaluator exists to register --
ADR 004 sections 1-2, unchanged by this phase.

Threshold is never baked into which instance gets registered or looked up:
every instance here is constructed with no threshold override (each
evaluator's own `DEFAULT_THRESHOLD` -- an unvalidated placeholder, not a
WikiQA-derived number, see each evaluator's own docstring), and the
per-project effective threshold is supplied at `evaluate()` call time
instead (see `app.interface.Evaluator.evaluate`'s `threshold` parameter and
this ADR's Phase 3 threshold-resolution amendment). Nothing in this module
mutates an evaluator instance's state, and evaluator instances are safe for
concurrent `evaluate()` calls (both registered evaluators document this
explicitly -- `RelevanceEvaluator` has no shared mutable state at all,
`EmbeddingRelevanceEvaluator`'s ONNX Runtime session is documented safe for
concurrent inference from multiple threads).
"""

from __future__ import annotations

from app.embedding_relevance import EmbeddingRelevanceEvaluator
from app.interface import Evaluator
from app.relevance import RelevanceEvaluator


class UnknownEvaluatorError(LookupError):
    """No registered evaluator matches the requested `(evaluator_name,
    evaluator_version)` pair -- e.g. a worker fleet mid rolling-deploy that
    no longer ships (or never shipped) that exact version. Not this
    registry's decision what to do about it; the caller (`worker/execution.py`)
    surfaces this as a typed failure for the not-yet-built retry/dead-letter
    layer to decide on.
    """


class EvaluatorRegistry:
    def __init__(self, evaluators: list[Evaluator] | None = None) -> None:
        """`evaluators` defaults to constructing this milestone's two
        production evaluators (see module docstring). Tests may pass a
        different list -- e.g. a lightweight test double -- so registry
        lookup/reuse behavior can be exercised without paying
        `EmbeddingRelevanceEvaluator`'s model-load cost.
        """
        if evaluators is None:
            evaluators = [RelevanceEvaluator(), EmbeddingRelevanceEvaluator()]
        self._evaluators: dict[tuple[str, str], Evaluator] = {
            (evaluator.name, evaluator.version): evaluator for evaluator in evaluators
        }

    def get(self, evaluator_name: str, evaluator_version: str) -> Evaluator:
        try:
            return self._evaluators[(evaluator_name, evaluator_version)]
        except KeyError:
            raise UnknownEvaluatorError(
                f"No registered evaluator for evaluator_name={evaluator_name!r}, "
                f"evaluator_version={evaluator_version!r}."
            ) from None

    def registered_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(self._evaluators)
