"""Pure metric functions for the offline validation harness.

No dependency on the WikiQA dataset shape, the evaluator, or any file I/O
-- these functions operate only on plain lists of labels/scores/
predictions, so they are testable with tiny synthetic fixtures (see
tests/test_validation.py) independent of the real dataset.

Uses `sklearn.metrics` for the confusion matrix and ROC-AUC computation --
no new dependency, since `scikit-learn` is already a direct dependency of
`app/relevance.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sklearn.metrics import confusion_matrix as _sk_confusion_matrix
from sklearn.metrics import roc_auc_score as _sk_roc_auc_score


@dataclass(frozen=True)
class ConfusionMatrix:
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int

    @property
    def total(self) -> int:
        return self.true_positive + self.false_positive + self.true_negative + self.false_negative


def compute_confusion_matrix(labels: list[int], predictions: list[int]) -> ConfusionMatrix:
    """`labels`/`predictions` are 0/1 lists of equal length. `labels=[0, 1]`
    is passed explicitly to `sklearn`'s function so the matrix has a fixed
    2x2 shape even if one class is entirely absent from this particular
    batch (e.g. a tiny test fixture with no positives)."""
    if len(labels) != len(predictions):
        raise ValueError(
            f"labels and predictions must be the same length, got {len(labels)} and "
            f"{len(predictions)}."
        )
    tn, fp, fn, tp = _sk_confusion_matrix(labels, predictions, labels=[0, 1]).ravel()
    return ConfusionMatrix(
        true_positive=int(tp), false_positive=int(fp), true_negative=int(tn), false_negative=int(fn)
    )


def precision(cm: ConfusionMatrix) -> float:
    denominator = cm.true_positive + cm.false_positive
    return cm.true_positive / denominator if denominator else 0.0


def recall(cm: ConfusionMatrix) -> float:
    denominator = cm.true_positive + cm.false_negative
    return cm.true_positive / denominator if denominator else 0.0


def f1_score(cm: ConfusionMatrix) -> float:
    p, r = precision(cm), recall(cm)
    return 2 * p * r / (p + r) if (p + r) else 0.0


def roc_auc(labels: list[int], scores: list[float]) -> float | None:
    """`None` if ROC-AUC is undefined for this data (only one class
    present -- there is no meaningful ranking metric with no negatives or
    no positives to rank against each other)."""
    if len(set(labels)) < 2:
        return None
    return float(_sk_roc_auc_score(labels, scores))


@dataclass(frozen=True)
class ThresholdCandidate:
    threshold: float
    confusion_matrix: ConfusionMatrix
    precision: float
    recall: float
    f1: float


def sweep_thresholds(
    labels: list[int], scores: list[float], *, num_steps: int = 101
) -> list[ThresholdCandidate]:
    """Evaluate precision/recall/F1 at `num_steps` evenly spaced
    thresholds across [0.0, 1.0] (inclusive of both ends). A prediction is
    `1` (relevant) when `score >= threshold`, matching
    `RelevanceEvaluator`'s own `>=` comparison exactly (app/relevance.py)."""
    if len(labels) != len(scores):
        raise ValueError(
            f"labels and scores must be the same length, got {len(labels)} and {len(scores)}."
        )
    if num_steps < 2:
        raise ValueError(f"num_steps must be >= 2, got {num_steps}.")

    candidates = []
    for step in range(num_steps):
        threshold = step / (num_steps - 1)
        predictions = [1 if score >= threshold else 0 for score in scores]
        cm = compute_confusion_matrix(labels, predictions)
        candidates.append(
            ThresholdCandidate(
                threshold=threshold,
                confusion_matrix=cm,
                precision=precision(cm),
                recall=recall(cm),
                f1=f1_score(cm),
            )
        )
    return candidates


#: A threshold-selection policy: given the full sweep, pick one candidate.
#: `select_by_max_f1` is the V1 default (see
#: docs/decisions/004-evaluation-engine.md section 10); this type exists so
#: a different policy (e.g. `select_by_min_precision`) can be substituted
#: without changing anything else in the harness.
ThresholdSelectionStrategy = Callable[[list[ThresholdCandidate]], ThresholdCandidate]


def select_by_max_f1(candidates: list[ThresholdCandidate]) -> ThresholdCandidate:
    """Default V1 policy: the threshold maximizing F1 on the validation
    split. Ties are broken by preferring the HIGHER threshold -- a more
    conservative (harder to satisfy) bar for "relevant" under a tie,
    an arbitrary but deterministic and documented choice."""
    if not candidates:
        raise ValueError("candidates must not be empty.")
    return max(candidates, key=lambda c: (c.f1, c.threshold))


def select_by_min_precision(
    candidates: list[ThresholdCandidate], *, min_precision: float
) -> ThresholdCandidate:
    """Alternate policy, not used by default in V1: among thresholds
    achieving at least `min_precision`, pick the one maximizing recall.
    Demonstrates that the threshold-selection policy is pluggable -- see
    `ThresholdSelectionStrategy` above -- not a claim that this is the
    right policy for production; that remains an open, unvalidated
    product decision (docs/decisions/004-evaluation-engine.md section 10)."""
    eligible = [c for c in candidates if c.precision >= min_precision]
    if not eligible:
        raise ValueError(f"No threshold in the sweep achieves precision >= {min_precision}.")
    return max(eligible, key=lambda c: (c.recall, c.threshold))
