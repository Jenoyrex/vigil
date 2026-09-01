"""Loads the locally cached WikiQA dataset and orchestrates the offline
validation harness for `app.relevance.RelevanceEvaluator`.

Two-step, network-separated design (see scripts/download_wikiqa.py's
module docstring for the licensing reason this is a separate step, not
folded into this module):

    uv run python scripts/download_wikiqa.py     # network, one-time
    uv run python -m validation.wikiqa            # offline, deterministic

This module makes no network calls of its own -- it only reads the local
JSONL cache `scripts/download_wikiqa.py` writes to
`services/evaluator/.cache/wikiqa/`. Running it without having downloaded
the cache first fails immediately and explicitly (see `load_split`), not
with a confusing downstream error.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.relevance import EVALUATOR_VERSION, RelevanceEvaluator, RelevanceEvaluatorInput
from validation.metrics import (
    ThresholdCandidate,
    roc_auc,
    select_by_max_f1,
    sweep_thresholds,
)
from validation.reporting import render_failures_json, render_markdown_report, sample_failures

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "wikiqa"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"

#: Answers with fewer than this many whitespace-separated tokens are
#: flagged (not filtered -- see module docstring on "do not silently
#: remove") as short, a known weak spot for TF-IDF's lexical overlap.
SHORT_ANSWER_WORD_THRESHOLD = 3

#: How many false-positive / false-negative examples to include in the
#: machine-readable failure report -- bounded, per this milestone's
#: "do not include more examples than necessary" requirement.
MAX_FAILURE_EXAMPLES_PER_KIND = 15

#: Fixed seed for failure-example sampling, so the same failure report is
#: produced on every run given the same data and threshold.
FAILURE_SAMPLE_SEED = 42

#: Number of evenly spaced thresholds swept across [0.0, 1.0].
THRESHOLD_SWEEP_STEPS = 101


@dataclass(frozen=True)
class WikiQAExample:
    """One row of the WikiQA dataset. Field names and types match the
    dataset's actual schema, verified directly against
    https://datasets-server.huggingface.co (not just the dataset card) --
    see validation/reports/wikiqa_baseline.md's "Dataset information"
    section."""

    question_id: str
    question: str
    document_title: str
    answer: str
    label: int  # 0 = not relevant, 1 = relevant, per the verified schema.


@dataclass(frozen=True)
class EvaluatedExample:
    example: WikiQAExample
    score: float
    predicted_label: int


@dataclass(frozen=True)
class DatasetStats:
    """Descriptive statistics, reported (never used to silently filter
    examples -- see this milestone's "Dataset concerns" requirement)."""

    split: str
    total_examples: int
    positive_examples: int
    negative_examples: int
    positive_rate: float
    unique_questions: int
    all_negative_question_groups: int
    short_answer_examples: int


def load_split(split: str, *, cache_dir: Path = CACHE_DIR) -> list[WikiQAExample]:
    path = cache_dir / f"{split}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"No cached WikiQA data at {path}. Run "
            f"`uv run python scripts/download_wikiqa.py` first -- this module never "
            f"downloads data itself."
        )

    examples: list[WikiQAExample] = []
    with path.open(encoding="utf-8") as fh:
        for line_number, raw_line in enumerate(fh, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            examples.append(_parse_example(json.loads(stripped), source=path, line=line_number))
    if not examples:
        raise ValueError(f"{path} exists but contains no examples.")
    return examples


def _parse_example(raw: dict, *, source: Path, line: int) -> WikiQAExample:
    missing = [key for key in ("question_id", "question", "document_title", "answer", "label")
               if key not in raw]
    if missing:
        raise ValueError(f"{source}:{line}: missing required field(s) {missing}.")

    label = raw["label"]
    # Strict on purpose: `int(0.5)` truncates to `0` without raising, which
    # would silently accept a malformed label rather than reject it. `bool`
    # is a subclass of `int` in Python (`isinstance(True, int)` is `True`)
    # but is never a valid label value here either.
    if isinstance(label, bool) or not isinstance(label, int) or label not in (0, 1):
        raise ValueError(f"{source}:{line}: 'label' must be the int 0 or 1, got {label!r}.")

    return WikiQAExample(
        question_id=str(raw["question_id"]),
        question=str(raw["question"]),
        document_title=str(raw["document_title"]),
        answer=str(raw["answer"]),
        label=label,
    )


def compute_dataset_stats(
    examples: list[WikiQAExample],
    *,
    split: str,
    short_answer_word_threshold: int = SHORT_ANSWER_WORD_THRESHOLD,
) -> DatasetStats:
    positive = sum(1 for ex in examples if ex.label == 1)
    negative = len(examples) - positive

    by_question: dict[str, list[WikiQAExample]] = {}
    for ex in examples:
        by_question.setdefault(ex.question_id, []).append(ex)
    all_negative_groups = sum(
        1 for group in by_question.values() if all(ex.label == 0 for ex in group)
    )

    short_answers = sum(
        1 for ex in examples if len(ex.answer.split()) < short_answer_word_threshold
    )

    return DatasetStats(
        split=split,
        total_examples=len(examples),
        positive_examples=positive,
        negative_examples=negative,
        positive_rate=positive / len(examples) if examples else 0.0,
        unique_questions=len(by_question),
        all_negative_question_groups=all_negative_groups,
        short_answer_examples=short_answers,
    )


def evaluate_examples(
    examples: list[WikiQAExample], *, evaluator: RelevanceEvaluator
) -> list[float]:
    """Runs the evaluator once per example and returns the raw,
    unthresholded score for each. The evaluator's own internal threshold
    (and the `label` it derives from it) is deliberately ignored here --
    the harness sweeps and selects its own threshold independently (see
    validation.metrics), so which threshold this `evaluator` instance
    happens to be constructed with does not affect anything computed by
    this function."""
    scores: list[float] = []
    for example in examples:
        result = evaluator.evaluate(
            RelevanceEvaluatorInput(input_text=example.question, output_text=example.answer)
        )
        # `score` is None only for not-evaluable input (empty/whitespace
        # text or no comparable vocabulary -- see app/relevance.py). Real
        # WikiQA rows are never empty, but this is handled explicitly
        # rather than silently coerced, consistent with "do not silently
        # remove examples": a None score is treated as the worst possible
        # score (0.0) for threshold-sweep purposes, since "could not be
        # evaluated" is not evidence of relevance.
        scores.append(result.score if result.score is not None else 0.0)
    return scores


def apply_threshold(scores: list[float], threshold: float) -> list[int]:
    return [1 if score >= threshold else 0 for score in scores]


def _print_stats(stats: DatasetStats) -> None:
    print(f"  [{stats.split}] {stats.total_examples} examples, "
          f"{stats.positive_examples} positive ({stats.positive_rate:.1%}), "
          f"{stats.negative_examples} negative")
    print(f"  [{stats.split}] {stats.unique_questions} unique questions, "
          f"{stats.all_negative_question_groups} with no positive candidate at all")
    print(f"  [{stats.split}] {stats.short_answer_examples} answers under "
          f"{SHORT_ANSWER_WORD_THRESHOLD} words")


def main() -> None:
    print("Loading cached WikiQA splits ...")
    validation_examples = load_split("validation")
    test_examples = load_split("test")

    validation_stats = compute_dataset_stats(validation_examples, split="validation")
    test_stats = compute_dataset_stats(test_examples, split="test")
    _print_stats(validation_stats)
    _print_stats(test_stats)

    # threshold= here is irrelevant to score computation; see evaluate_examples's docstring.
    evaluator = RelevanceEvaluator()
    print("\nRunning RelevanceEvaluator over the validation split ...")
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

    print("\nRunning RelevanceEvaluator over the held-out test split ...")
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
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    failures_path = REPORTS_DIR / "wikiqa_baseline_failures.json"
    failures_path.write_text(
        render_failures_json(
            false_positives=false_positives, false_negatives=false_negatives, run_info=run_info
        ),
        encoding="utf-8",
    )

    report_path = REPORTS_DIR / "wikiqa_baseline.md"
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
