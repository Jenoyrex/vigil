"""Offline validation harness for `app.embedding_relevance.EmbeddingRelevanceEvaluator`, run
against the exact same locally cached WikiQA data and the exact same protocol as
`validation/wikiqa.py` (dataset loading, threshold-sweep-on-validation /
freeze-then-test-on-held-out methodology, metrics, failure sampling) -- see this project's
milestone instructions: "Do NOT change the WikiQA validation methodology."

Deliberately a separate script, not a change to `validation/wikiqa.py`: that module, its TF-IDF
baseline numbers, and `validation/reports/wikiqa_baseline.md` all stay untouched, so the baseline
remains reproducible and comparable exactly as validated. This module reuses
`validation.wikiqa`'s dataset loader and stats function unmodified (`load_split`,
`compute_dataset_stats`, `apply_threshold`, `WikiQAExample`, `EvaluatedExample`) and
`validation.metrics` / `validation.reporting` unmodified -- the only thing that differs from
`validation/wikiqa.py::main()` is which evaluator produces the scores and which files the report
is written to.

Two-step, network-separated usage, matching `validation/wikiqa.py`'s own pattern:

    uv run python scripts/download_wikiqa.py          # network, one-time (dataset)
    uv run python -m validation.wikiqa_embedding        # network on first run only (model
                                                          # weights, via
                                                          # EmbeddingRelevanceEvaluator's own
                                                          # lazy download), fully offline after

Requires the `embedding` extra (`fastembed`) -- see `README.md`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from app.embedding_relevance import EVALUATOR_VERSION, EmbeddingRelevanceEvaluator
from app.relevance import RelevanceEvaluatorInput
from validation.metrics import ThresholdCandidate, roc_auc, select_by_max_f1, sweep_thresholds
from validation.reporting import render_failures_json, render_markdown_report, sample_failures
from validation.wikiqa import (
    CACHE_DIR,
    REPORTS_DIR,
    DatasetStats,
    WikiQAExample,
    apply_threshold,
    compute_dataset_stats,
    load_split,
)

#: Same values as validation/wikiqa.py -- reusing the identical protocol constants is part of
#: "do not change the WikiQA validation methodology."
FAILURE_SAMPLE_SEED = 42
MAX_FAILURE_EXAMPLES_PER_KIND = 15
THRESHOLD_SWEEP_STEPS = 101


@dataclass(frozen=True)
class EvaluatedExample:
    example: WikiQAExample
    score: float
    predicted_label: int


def evaluate_examples(
    examples: list[WikiQAExample], *, evaluator: EmbeddingRelevanceEvaluator
) -> list[float]:
    """Identical in spirit to validation/wikiqa.py::evaluate_examples: runs the evaluator once
    per example over its raw, unthresholded score. See that function's docstring for why a
    `None` score (not_evaluable) is treated as the worst possible score (0.0) for threshold-sweep
    purposes -- the same reasoning applies unchanged here; real WikiQA rows are never empty."""
    scores: list[float] = []
    for example in examples:
        result = evaluator.evaluate(
            RelevanceEvaluatorInput(input_text=example.question, output_text=example.answer)
        )
        scores.append(result.score if result.score is not None else 0.0)
    return scores


def _print_stats(stats: DatasetStats) -> None:
    print(f"  [{stats.split}] {stats.total_examples} examples, "
          f"{stats.positive_examples} positive ({stats.positive_rate:.1%}), "
          f"{stats.negative_examples} negative")


def main() -> None:
    print("Loading cached WikiQA splits ...")
    validation_examples = load_split("validation")
    test_examples = load_split("test")

    validation_stats = compute_dataset_stats(validation_examples, split="validation")
    test_stats = compute_dataset_stats(test_examples, split="test")
    _print_stats(validation_stats)
    _print_stats(test_stats)

    print("\nLoading EmbeddingRelevanceEvaluator (model load / first-time download if not "
          "cached) ...")
    load_start = datetime.now(UTC)
    evaluator = EmbeddingRelevanceEvaluator()
    print(f"Model ready in {(datetime.now(UTC) - load_start).total_seconds():.1f}s.")

    print("\nRunning EmbeddingRelevanceEvaluator over the validation split ...")
    validation_scores = evaluate_examples(validation_examples, evaluator=evaluator)
    validation_labels = [ex.label for ex in validation_examples]

    print("Sweeping thresholds on the validation split ...")
    candidates = sweep_thresholds(
        validation_labels, validation_scores, num_steps=THRESHOLD_SWEEP_STEPS
    )
    chosen: ThresholdCandidate = select_by_max_f1(candidates)
    print(
        f"Selected threshold={chosen.threshold:.4f} (validation F1={chosen.f1:.4f}, "
        f"precision={chosen.precision:.4f}, recall={chosen.recall:.4f})"
    )

    print("\nRunning EmbeddingRelevanceEvaluator over the held-out test split ...")
    test_scores = evaluate_examples(test_examples, evaluator=evaluator)
    test_labels = [ex.label for ex in test_examples]
    test_predictions = apply_threshold(test_scores, chosen.threshold)

    test_candidates = sweep_thresholds(test_labels, test_scores, num_steps=THRESHOLD_SWEEP_STEPS)
    test_at_threshold = min(test_candidates, key=lambda c: abs(c.threshold - chosen.threshold))
    test_roc_auc = roc_auc(test_labels, test_scores)

    print(f"Test metrics at frozen threshold={chosen.threshold:.4f}: "
          f"precision={test_at_threshold.precision:.4f}, recall={test_at_threshold.recall:.4f}, "
          f"F1={test_at_threshold.f1:.4f}, ROC-AUC={test_roc_auc}")

    evaluated_test = [
        EvaluatedExample(example=ex, score=score, predicted_label=pred)
        for ex, score, pred in zip(test_examples, test_scores, test_predictions, strict=True)
    ]
    false_positives = sample_failures(
        evaluated_test, kind="false_positive",
        max_examples=MAX_FAILURE_EXAMPLES_PER_KIND, seed=FAILURE_SAMPLE_SEED,
    )
    false_negatives = sample_failures(
        evaluated_test, kind="false_negative",
        max_examples=MAX_FAILURE_EXAMPLES_PER_KIND, seed=FAILURE_SAMPLE_SEED,
    )

    metadata_path = CACHE_DIR / "_metadata.json"
    dataset_metadata = (
        json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    )

    run_info = {
        "evaluator_name": evaluator.name,
        "evaluator_version": EVALUATOR_VERSION,
        "threshold_selection_method": "select_by_max_f1 (validation.metrics)",
        "selected_threshold": chosen.threshold,
        "validation_split_size": len(validation_examples),
        "test_split_size": len(test_examples),
        "failure_sample_seed": FAILURE_SAMPLE_SEED,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "dataset_metadata": dataset_metadata,
        "failures_json_filename": "wikiqa_embedding_failures.json",
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    failures_path = REPORTS_DIR / "wikiqa_embedding_failures.json"
    failures_path.write_text(
        render_failures_json(
            false_positives=false_positives, false_negatives=false_negatives, run_info=run_info
        ),
        encoding="utf-8",
    )

    report_path = REPORTS_DIR / "wikiqa_embedding.md"
    report_path.write_text(
        render_markdown_report(
            run_info=run_info,
            validation_stats=validation_stats,
            test_stats=test_stats,
            validation_candidates=candidates,
            chosen=chosen,
            test_at_threshold=test_at_threshold,
            test_roc_auc=test_roc_auc,
            false_positives=false_positives,
            false_negatives=false_negatives,
        ),
        encoding="utf-8",
    )

    print(f"\nWrote {report_path}")
    print(f"Wrote {failures_path}")


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
