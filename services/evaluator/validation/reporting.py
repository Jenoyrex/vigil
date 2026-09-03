"""Failure-sample selection and report rendering for the WikiQA offline
validation harness. No dataset loading or evaluator logic lives here --
see validation/wikiqa.py for orchestration and validation/metrics.py for
metric computation. Kept separate so report formatting can change without
touching either.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from validation.metrics import ThresholdCandidate

if TYPE_CHECKING:
    from validation.wikiqa import DatasetStats, EvaluatedExample


@dataclass(frozen=True)
class FailureExample:
    question_id: str
    question: str
    answer: str
    true_label: int
    score: float
    predicted_label: int


def sample_failures(
    evaluated: list[EvaluatedExample],
    *,
    kind: Literal["false_positive", "false_negative"],
    max_examples: int,
    seed: int,
) -> list[FailureExample]:
    """`false_positive`: true_label=0 but predicted_label=1 (evaluator
    over-called relevance). `false_negative`: true_label=1 but
    predicted_label=0 (evaluator under-called relevance -- the case most
    likely to be TF-IDF's known paraphrase weak spot; see
    validation/reports/wikiqa_baseline.md).

    Sampling is seeded and deterministic (`random.Random(seed)`, not
    global module state) so the same report is produced on every run
    given the same data and threshold. If the full pool of matching
    examples is at or under `max_examples`, every one is included (no
    sampling needed) -- only an oversized pool is subsampled.
    """
    if kind == "false_positive":
        pool = [e for e in evaluated if e.example.label == 0 and e.predicted_label == 1]
    else:
        pool = [e for e in evaluated if e.example.label == 1 and e.predicted_label == 0]

    if len(pool) > max_examples:
        pool = random.Random(seed).sample(pool, max_examples)

    # Deterministic output ordering regardless of whether sampling ran.
    pool = sorted(pool, key=lambda e: e.example.question_id)

    return [
        FailureExample(
            question_id=e.example.question_id,
            question=e.example.question,
            answer=e.example.answer,
            true_label=e.example.label,
            score=e.score,
            predicted_label=e.predicted_label,
        )
        for e in pool
    ]


def render_failures_json(
    *,
    false_positives: list[FailureExample],
    false_negatives: list[FailureExample],
    run_info: dict,
) -> str:
    """Machine-readable failure report. Deliberately just the bounded
    failure samples plus enough run metadata to interpret them
    standalone -- not the full test set."""
    payload = {
        "run_info": run_info,
        "false_positives": [asdict(f) for f in false_positives],
        "false_negatives": [asdict(f) for f in false_negatives],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _fmt_pct(value: float) -> str:
    return f"{value:.1%}"


def _fmt4(value: float) -> str:
    return f"{value:.4f}"


def _class_distribution_table(validation_stats: DatasetStats, test_stats: DatasetStats) -> str:
    header = (
        "| split | total | positive | negative | positive rate | unique questions "
        f"| all-negative question groups | answers < {3} words |"
    )
    separator = "|---|---|---|---|---|---|---|---|"
    rows = []
    for stats in (validation_stats, test_stats):
        rows.append(
            f"| {stats.split} | {stats.total_examples} | {stats.positive_examples} "
            f"| {stats.negative_examples} | {_fmt_pct(stats.positive_rate)} "
            f"| {stats.unique_questions} | {stats.all_negative_question_groups} "
            f"| {stats.short_answer_examples} |"
        )
    return "\n".join([header, separator, *rows])


def _failure_table(examples: list[FailureExample]) -> str:
    if not examples:
        return "_None found in the sampled test evaluation._\n"
    lines = ["| question | answer | true_label | score | predicted_label |",
             "|---|---|---|---|---|"]
    for ex in examples:
        question = ex.question.replace("|", "\\|")
        answer = ex.answer.replace("|", "\\|")
        if len(answer) > 140:
            answer = answer[:140] + "…"
        lines.append(
            f"| {question} | {answer} | {ex.true_label} | {ex.score:.4f} | {ex.predicted_label} |"
        )
    return "\n".join(lines) + "\n"


#: Per-evaluator description line used by `render_markdown_report`'s header, keyed by
#: `run_info["evaluator_name"]`. Added so this same rendering function can honestly describe
#: either evaluator run through this harness (`validation/wikiqa.py` for the TF-IDF baseline,
#: `validation/wikiqa_embedding.py` for the embedding candidate) without hardcoding one
#: evaluator's identity into shared report-rendering code -- a caller with a `evaluator_name` not
#: listed here still gets a generic, still-accurate fallback description rather than a wrong one.
_EVALUATOR_DESCRIPTIONS = {
    "relevance": (
        "the existing deterministic **TF-IDF cosine-similarity lexical baseline** in "
        "`services/evaluator/app/relevance.py`. This is explicitly **not** an embedding-based "
        "evaluator (see `docs/decisions/004-evaluation-engine.md` section 1) — it is not "
        "described as one anywhere in this report, and this validation does not change that "
        "categorization."
    ),
    "relevance_embedding": (
        "the experimental **dense semantic embedding cosine-similarity candidate** in "
        "`services/evaluator/app/embedding_relevance.py` (`BAAI/bge-small-en-v1.5` via "
        "`fastembed`/ONNX Runtime, local inference only). This is explicitly **not** the V1 "
        "production evaluator — see `services/evaluator/README.md`'s \"Embedding relevance "
        "evaluator (experimental)\" section and `validation/reports/wikiqa_comparison.md` for "
        "the head-to-head comparison this report feeds into."
    ),
}


def render_markdown_report(
    *,
    run_info: dict,
    validation_stats: DatasetStats,
    test_stats: DatasetStats,
    validation_candidates: list[ThresholdCandidate],
    chosen: ThresholdCandidate,
    test_at_threshold: ThresholdCandidate,
    test_roc_auc: float | None,
    false_positives: list[FailureExample],
    false_negatives: list[FailureExample],
) -> str:
    cm = test_at_threshold.confusion_matrix
    dataset_meta = run_info.get("dataset_metadata", {})
    evaluator_description = _EVALUATOR_DESCRIPTIONS.get(
        run_info["evaluator_name"],
        f"the evaluator named {run_info['evaluator_name']!r} in `services/evaluator/app/`.",
    )

    return f"""# WikiQA Validation Report — `{run_info["evaluator_name"]}`

**Evaluator under test:** `{run_info["evaluator_name"]}` v`{run_info["evaluator_version"]}` —
{evaluator_description}

**Evaluated at:** {run_info["evaluated_at"]}

## Dataset information

- **Dataset:** WikiQA (`{dataset_meta.get("dataset_id", "microsoft/wiki_qa")}`), config
  `{dataset_meta.get("dataset_config", "default")}`.
- **Source:** Hugging Face datasets-server API mirror of the official Microsoft Research WikiQA
  Corpus, downloaded directly by `scripts/download_wikiqa.py` — never redistributed by Vigil (see
  "Licensing" below).
- **Dataset revision (git commit SHA on the Hugging Face repo at download time):**
  `{dataset_meta.get("dataset_revision_sha", "unknown")}`.
- **Downloaded at:** {dataset_meta.get("downloaded_at", "unknown")}.
- **Split sizes used by this harness:** validation = {run_info["validation_split_size"]}, test =
  {run_info["test_split_size"]}. **The train split is not used** — this evaluator has no trainable
  parameters beyond its single threshold, which is selected on the validation split and confirmed on
  the held-out test split, so there is nothing for a training split to fit.
- **Verified schema** (fetched directly from the dataset's row-level API, not only its dataset
  card): `question_id` (str), `question` (str), `document_title` (str), `answer` (str), `label`
  (0 or 1; 1 = this `answer` is a relevant/correct answer to `question`).

## Licensing

WikiQA is distributed under the **Microsoft Research Data License Agreement** — permits use for
"research and technology development purposes" (including this kind of internal validation and
publishing results), explicitly **prohibits** "renting, leasing, [or] transferring rights to third
parties," and requires derivative works to carry the same terms. Consequently: **the raw dataset
is not committed to this repository.** `scripts/download_wikiqa.py` downloads a fresh copy
directly from the original source into a git-ignored local cache
(`services/evaluator/.cache/wikiqa/`, see `services/evaluator/.gitignore`) for each
user/environment that runs it — each download is that user's own act under the license's own
allowance, not Vigil redistributing a copy it holds.

## Evaluation protocol

1. Load the validation and test splits from the local cache (`validation/wikiqa.py::load_split`)
   — fails immediately and explicitly if the cache is missing, rather than downloading implicitly.
2. Run `RelevanceEvaluator.evaluate()` once per `(question, answer)` pair on the **validation**
   split, using only the raw `score` (the evaluator's own internally-applied threshold/label is
   ignored — the harness selects and applies its own threshold independently).
3. Sweep {len(validation_candidates)} evenly spaced candidate thresholds across [0.0, 1.0] on the
   validation split; select the threshold maximizing F1 (`validation.metrics.select_by_max_f1` —
   the V1 default policy; `select_by_min_precision` exists as an example of a different, pluggable
   policy, not used here).
4. **Freeze that threshold.** Run the evaluator once per pair on the **held-out test split**,
   independently, and apply the frozen threshold — the threshold is never re-tuned against test
   labels, and no evaluator change was made in response to any test-set result.
5. Compute precision/recall/F1/ROC-AUC/confusion-matrix on the test split at the frozen
   threshold, and sample a bounded set of false positives/negatives for failure analysis.

## Class distribution

{_class_distribution_table(validation_stats, test_stats)}

### Dataset concerns (reported, not silently filtered — no example was removed from either split)

- **Class imbalance:** WikiQA is heavily skewed toward `label=0` — most candidate answer sentences
  for a given question are wrong. Positive rate above is well under 50% on both splits, which is why
  this report leads with precision/recall/F1/ROC-AUC rather than accuracy (a trivial "always predict
  not_relevant" baseline would already score high on raw accuracy here).
- **Duplicate questions:** WikiQA's own structure is one question mapped to many candidate-answer
  rows — the "unique questions" column above being far smaller than "total" reflects this by design,
  not a data-quality defect. No de-duplication was performed: this evaluator scores each
  `(question, answer)` pair independently, so a repeated question string across rows is not a
  methodological problem for this harness.
- **All-negative question groups:** the count above is how many questions have zero `label=1`
  candidate among their rows in this split. These rows were kept, not excluded — this evaluator has
  no notion of "the correct answer among a candidate set" (it is not a ranker; it scores one pair in
  isolation), so an all-negative group is not a data quality issue for this evaluation methodology,
  only a descriptive fact about the source dataset.
- **Short answers:** flagged, not filtered — see "Failure analysis" below for whether they correlate
  with errors in practice.
- **Paraphrases / keyword-overlap false positives:** not detectable from dataset structure alone;
  addressed directly in "Failure analysis" below, by inspecting actual false negatives/positives.

## Threshold selection

- **Method:** maximize F1 on the validation split (`select_by_max_f1`), swept across
  {len(validation_candidates)} evenly spaced thresholds in [0.0, 1.0].
- **Selected threshold:** `{_fmt4(chosen.threshold)}`.
- **Validation metrics at selected threshold:** precision = {_fmt4(chosen.precision)}, recall =
  {_fmt4(chosen.recall)}, F1 = {_fmt4(chosen.f1)}.
- This threshold was frozen before any test-split evaluation ran, and was not changed afterward.

## Held-out test metrics (frozen threshold, never tuned on test labels)

| metric | value |
|---|---|
| threshold used | {_fmt4(test_at_threshold.threshold)} |
| precision | {_fmt4(test_at_threshold.precision)} |
| recall | {_fmt4(test_at_threshold.recall)} |
| F1 | {_fmt4(test_at_threshold.f1)} |
| ROC-AUC | {_fmt4(test_roc_auc) if test_roc_auc is not None else "undefined (single class)"} |

**Confusion matrix (test split, at frozen threshold):**

| | predicted relevant | predicted not_relevant |
|---|---|---|
| **actually relevant** | TP = {cm.true_positive} | FN = {cm.false_negative} |
| **actually not_relevant** | FP = {cm.false_positive} | TN = {cm.true_negative} |

## Failure analysis

Bounded sample (seed={run_info["failure_sample_seed"]}, up to {len(false_positives)} false positives
and {len(false_negatives)} false negatives shown; full machine-readable sample in
`{run_info.get("failures_json_filename", "wikiqa_baseline_failures.json")}`).

### False positives (evaluator said relevant; ground truth says not relevant)

{_failure_table(false_positives)}

### False negatives (evaluator said not_relevant; ground truth says relevant)

{_failure_table(false_negatives)}

### Dominant failure patterns (human-readable summary)

_Fill in after inspecting the sampled examples above — this section intentionally is not
auto-generated prose, to avoid asserting a pattern the actual sample doesn't support. Look
specifically for: paraphrase-driven false negatives (correct answer, low lexical overlap with the
question), keyword-overlap-driven false positives (shared vocabulary, wrong answer), and whether
short answers (per "Dataset concerns" above) are disproportionately represented in either failure
category._

## Limitations

- **WikiQA measures candidate-answer relevance to a question — it is not identical to evaluating
  generated LLM responses.** `answer` in this dataset is an *extractive* Wikipedia sentence from a
  retrieval/sentence-selection task, not a *generated* completion. This report is evidence about
  whether the evaluator can detect question–candidate-sentence relevance in that setting; it is not
  direct evidence about its behavior on production Vigil LLM-span traces.
- **Good WikiQA performance does not prove semantic relevance on production LLM traces.** The
  evaluator being TF-IDF-based (lexical, not semantic) is unchanged by this validation — see
  `services/evaluator/README.md`'s "Known limitations" for that discussion, which this report does
  not supersede.
- The threshold selected here is specific to WikiQA's score distribution and label balance; it is
  not claimed to be the right operating point for Vigil's actual production traffic, which will
  have a different distribution of question/answer pairs entirely.
- No comparison against a dense embedding baseline was performed in this milestone — see
  "Recommendation" below for whether one is warranted next.

## Conclusion / Recommendation

_Filled in after reviewing the metrics and failure analysis above against the three possible
outcomes documented for this milestone (TF-IDF acceptable as-is / useful baseline but insufficient,
recommend evaluating a semantic embedding approach next / benchmark mismatch too significant to
decide) — see the accompanying report turn for the actual decision and its reasoning against these
numbers._
"""
