"""Tests for the offline validation harness (validation/wikiqa.py,
validation/metrics.py, validation/reporting.py).

Uses only tiny synthetic fixtures -- never the real WikiQA dataset, which
is not part of this repository (see scripts/download_wikiqa.py's module
docstring for why: its license does not permit redistribution).
"""

from __future__ import annotations

import json

import pytest
from validation.metrics import (
    compute_confusion_matrix,
    f1_score,
    precision,
    recall,
    roc_auc,
    select_by_max_f1,
    select_by_min_precision,
    sweep_thresholds,
)
from validation.reporting import sample_failures
from validation.wikiqa import (
    EvaluatedExample,
    WikiQAExample,
    apply_threshold,
    compute_dataset_stats,
    load_split,
)


def _example(question_id: str, label: int, question: str = "q", answer: str = "a") -> WikiQAExample:
    return WikiQAExample(
        question_id=question_id, question=question, document_title="doc", answer=answer, label=label
    )


# -- label parsing / split loading ---------------------------------------


def test_load_split_parses_valid_jsonl(tmp_path) -> None:
    path = tmp_path / "test.jsonl"
    path.write_text(
        json.dumps(
            {
                "question_id": "Q1",
                "question": "What is the capital of France?",
                "document_title": "France",
                "answer": "Paris.",
                "label": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    examples = load_split("test", cache_dir=tmp_path)
    assert len(examples) == 1
    assert examples[0].question_id == "Q1"
    assert examples[0].label == 1


def test_load_split_skips_blank_lines(tmp_path) -> None:
    path = tmp_path / "test.jsonl"
    row = json.dumps(
        {"question_id": "Q1", "question": "q", "document_title": "d", "answer": "a", "label": 0}
    )
    path.write_text(f"{row}\n\n   \n{row}\n", encoding="utf-8")
    examples = load_split("test", cache_dir=tmp_path)
    assert len(examples) == 2


def test_load_split_missing_file_raises_with_actionable_message(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="download_wikiqa.py"):
        load_split("test", cache_dir=tmp_path)


def test_load_split_empty_file_raises(tmp_path) -> None:
    (tmp_path / "test.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no examples"):
        load_split("test", cache_dir=tmp_path)


def test_load_split_missing_field_raises(tmp_path) -> None:
    path = tmp_path / "test.jsonl"
    path.write_text(json.dumps({"question_id": "Q1", "question": "q"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required field"):
        load_split("test", cache_dir=tmp_path)


@pytest.mark.parametrize("bad_label", [2, -1, "yes", None, 0.5])
def test_load_split_invalid_label_raises(tmp_path, bad_label) -> None:
    path = tmp_path / "test.jsonl"
    path.write_text(
        json.dumps(
            {
                "question_id": "Q1",
                "question": "q",
                "document_title": "d",
                "answer": "a",
                "label": bad_label,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_split("test", cache_dir=tmp_path)


# -- dataset stats ----------------------------------------------------------


def test_compute_dataset_stats_counts_class_balance_and_groups() -> None:
    examples = [
        _example("Q1", 1, answer="a long enough answer here"),
        _example("Q1", 0, answer="another candidate"),
        _example("Q2", 0, answer="no"),
        _example("Q2", 0, answer="hi"),  # Q2 is an all-negative group
    ]
    stats = compute_dataset_stats(examples, split="test", short_answer_word_threshold=3)
    assert stats.total_examples == 4
    assert stats.positive_examples == 1
    assert stats.negative_examples == 3
    assert stats.positive_rate == pytest.approx(0.25)
    assert stats.unique_questions == 2
    assert stats.all_negative_question_groups == 1
    # "no" (1 word), "hi" (1 word), and "another candidate" (2 words) are
    # all < 3 words; only "a long enough answer here" (5 words) is not.
    assert stats.short_answer_examples == 3


def test_compute_dataset_stats_handles_empty_list() -> None:
    stats = compute_dataset_stats([], split="test")
    assert stats.total_examples == 0
    assert stats.positive_rate == 0.0


# -- metric calculation -------------------------------------------------


def test_confusion_matrix_matches_hand_computed_counts() -> None:
    labels =      [1, 1, 0, 0, 1, 0]
    predictions = [1, 0, 0, 1, 1, 0]
    cm = compute_confusion_matrix(labels, predictions)
    assert cm.true_positive == 2   # indices 0, 4
    assert cm.false_negative == 1  # index 1
    assert cm.false_positive == 1  # index 3
    assert cm.true_negative == 2   # indices 2, 5
    assert cm.total == 6


def test_precision_recall_f1_hand_computed() -> None:
    labels =      [1, 1, 0, 0, 1, 0]
    predictions = [1, 0, 0, 1, 1, 0]
    cm = compute_confusion_matrix(labels, predictions)
    assert precision(cm) == pytest.approx(2 / 3)
    assert recall(cm) == pytest.approx(2 / 3)
    assert f1_score(cm) == pytest.approx(2 / 3)


def test_precision_recall_f1_are_zero_not_error_with_no_predicted_positives() -> None:
    cm = compute_confusion_matrix([1, 0, 1], [0, 0, 0])
    assert precision(cm) == 0.0
    assert recall(cm) == 0.0
    assert f1_score(cm) == 0.0


def test_confusion_matrix_requires_equal_length_inputs() -> None:
    with pytest.raises(ValueError):
        compute_confusion_matrix([1, 0], [1])


def test_roc_auc_matches_known_perfect_separation() -> None:
    labels = [0, 0, 1, 1]
    scores = [0.1, 0.2, 0.8, 0.9]
    assert roc_auc(labels, scores) == pytest.approx(1.0)


def test_roc_auc_is_none_when_only_one_class_present() -> None:
    assert roc_auc([1, 1, 1], [0.1, 0.5, 0.9]) is None


# -- threshold sweep / selection -----------------------------------------


def test_sweep_thresholds_produces_expected_number_of_candidates() -> None:
    candidates = sweep_thresholds([1, 0], [0.9, 0.1], num_steps=11)
    assert len(candidates) == 11
    assert candidates[0].threshold == 0.0
    assert candidates[-1].threshold == 1.0


def test_sweep_thresholds_requires_equal_length_inputs() -> None:
    with pytest.raises(ValueError):
        sweep_thresholds([1, 0, 1], [0.5, 0.5])


def test_sweep_thresholds_rejects_too_few_steps() -> None:
    with pytest.raises(ValueError):
        sweep_thresholds([1, 0], [0.5, 0.5], num_steps=1)


def test_select_by_max_f1_picks_the_best_scoring_candidate() -> None:
    # Perfectly separable: any threshold in (0.4, 0.6] gives F1 = 1.0.
    labels = [0, 0, 1, 1]
    scores = [0.1, 0.2, 0.8, 0.9]
    candidates = sweep_thresholds(labels, scores, num_steps=11)
    chosen = select_by_max_f1(candidates)
    assert chosen.f1 == pytest.approx(1.0)


def test_select_by_max_f1_breaks_ties_toward_higher_threshold() -> None:
    labels = [0, 1]
    scores = [0.5, 0.5]
    # Every threshold <= 0.5 gives the same (degenerate) confusion matrix
    # here; the tie-break rule must be deterministic, not incidental.
    candidates = sweep_thresholds(labels, scores, num_steps=11)
    chosen = select_by_max_f1(candidates)
    tied = [c for c in candidates if c.f1 == chosen.f1]
    assert chosen.threshold == max(c.threshold for c in tied)


def test_select_by_max_f1_rejects_empty_candidates() -> None:
    with pytest.raises(ValueError):
        select_by_max_f1([])


def test_select_by_min_precision_finds_eligible_threshold() -> None:
    labels = [0, 0, 1, 1]
    scores = [0.1, 0.4, 0.6, 0.9]
    candidates = sweep_thresholds(labels, scores, num_steps=11)
    chosen = select_by_min_precision(candidates, min_precision=0.9)
    assert chosen.precision >= 0.9


def test_select_by_min_precision_raises_when_unachievable() -> None:
    labels = [0, 1]
    scores = [0.9, 0.1]  # perfectly anti-correlated -- no threshold reaches high precision
    candidates = sweep_thresholds(labels, scores, num_steps=11)
    with pytest.raises(ValueError, match="No threshold"):
        select_by_min_precision(candidates, min_precision=0.99)


def test_apply_threshold_matches_evaluator_inclusive_comparison() -> None:
    assert apply_threshold([0.5, 0.4999, 0.5001], 0.5) == [1, 0, 1]


# -- determinism ----------------------------------------------------------


def test_sweep_thresholds_is_deterministic() -> None:
    labels = [1, 0, 1, 0, 1]
    scores = [0.9, 0.1, 0.6, 0.4, 0.55]
    first = sweep_thresholds(labels, scores)
    second = sweep_thresholds(labels, scores)
    assert first == second


def test_sample_failures_is_deterministic_given_the_same_seed() -> None:
    evaluated = [
        EvaluatedExample(example=_example(f"Q{i}", label=0), score=0.9, predicted_label=1)
        for i in range(50)
    ]
    first = sample_failures(evaluated, kind="false_positive", max_examples=5, seed=42)
    second = sample_failures(evaluated, kind="false_positive", max_examples=5, seed=42)
    assert first == second


def test_sample_failures_different_seeds_can_differ() -> None:
    evaluated = [
        EvaluatedExample(example=_example(f"Q{i}", label=0), score=0.9, predicted_label=1)
        for i in range(50)
    ]
    a = sample_failures(evaluated, kind="false_positive", max_examples=5, seed=1)
    b = sample_failures(evaluated, kind="false_positive", max_examples=5, seed=2)
    assert {e.question_id for e in a} != {e.question_id for e in b}


# -- failure-report generation -------------------------------------------


def test_sample_failures_selects_only_false_positives() -> None:
    evaluated = [
        EvaluatedExample(example=_example("Q1", label=0), score=0.9, predicted_label=1),  # FP
        EvaluatedExample(example=_example("Q2", label=1), score=0.9, predicted_label=1),  # TP
        EvaluatedExample(example=_example("Q3", label=0), score=0.1, predicted_label=0),  # TN
        EvaluatedExample(example=_example("Q4", label=1), score=0.1, predicted_label=0),  # FN
    ]
    false_positives = sample_failures(evaluated, kind="false_positive", max_examples=10, seed=1)
    assert [e.question_id for e in false_positives] == ["Q1"]


def test_sample_failures_selects_only_false_negatives() -> None:
    evaluated = [
        EvaluatedExample(example=_example("Q1", label=0), score=0.9, predicted_label=1),  # FP
        EvaluatedExample(example=_example("Q2", label=1), score=0.9, predicted_label=1),  # TP
        EvaluatedExample(example=_example("Q3", label=0), score=0.1, predicted_label=0),  # TN
        EvaluatedExample(example=_example("Q4", label=1), score=0.1, predicted_label=0),  # FN
    ]
    false_negatives = sample_failures(evaluated, kind="false_negative", max_examples=10, seed=1)
    assert [e.question_id for e in false_negatives] == ["Q4"]


def test_sample_failures_respects_max_examples_bound() -> None:
    evaluated = [
        EvaluatedExample(example=_example(f"Q{i}", label=0), score=0.9, predicted_label=1)
        for i in range(100)
    ]
    sampled = sample_failures(evaluated, kind="false_positive", max_examples=7, seed=1)
    assert len(sampled) == 7


def test_sample_failures_returns_full_pool_when_under_the_bound() -> None:
    evaluated = [
        EvaluatedExample(example=_example(f"Q{i}", label=0), score=0.9, predicted_label=1)
        for i in range(3)
    ]
    sampled = sample_failures(evaluated, kind="false_positive", max_examples=10, seed=1)
    assert len(sampled) == 3


def test_sample_failures_handles_no_matching_examples() -> None:
    evaluated = [
        EvaluatedExample(example=_example("Q1", label=1), score=0.9, predicted_label=1),  # TP only
    ]
    assert sample_failures(evaluated, kind="false_positive", max_examples=10, seed=1) == []
