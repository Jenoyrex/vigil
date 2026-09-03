# TF-IDF vs. Embedding Relevance Evaluator — Comparison Report

Head-to-head comparison of `services/evaluator/app/relevance.py` (TF-IDF, V1 production evaluator)
against `services/evaluator/app/embedding_relevance.py` (`BAAI/bge-small-en-v1.5` via `fastembed`,
experimental candidate), both run through the identical WikiQA offline validation protocol
(`validation/reports/wikiqa_baseline.md` and `validation/reports/wikiqa_embedding.md` respectively
— see those reports for full detail; this report is the synthesis, not a replacement for either).

**Same dataset, same splits, same protocol, same metric code, different evaluator.** Both reports
used the identical cached WikiQA validation (2733 rows) and test (6165 rows) splits, the identical
`validation.metrics.sweep_thresholds`/`select_by_max_f1` threshold-selection policy (101-step sweep
on validation, frozen and applied to test), and the identical `validation.metrics`/
`validation.reporting` code. Nothing about the methodology changed between the two runs — only
which evaluator produced the scores.

## Metrics (held-out test split, frozen threshold selected on validation)

| | TF-IDF (`relevance` v0.1.0) | Embedding (`relevance_embedding` v0.1.0) | Change |
|---|---|---|---|
| Selected threshold | 0.1700 | 0.8900 | — (different score distributions; not directly comparable as raw numbers) |
| Precision | 0.1061 | **0.3342** | **+215%** (3.15x) |
| Recall | **0.4403** | 0.4232 | -3.9% |
| F1 | 0.1710 | **0.3735** | **+118%** (2.18x) |
| ROC-AUC | 0.6798 | **0.8220** | +0.142 absolute |
| TP / FP / FN / TN | 129 / 1087 / 164 / 4785 | 124 / 247 / 169 / 5625 | FP down 77%, FN roughly flat |

**Reading the confusion matrix change:** the embedding evaluator did not "trade recall for
precision" in the usual sense of a stricter threshold catching fewer true positives — true
positives are almost unchanged (129 → 124) and false negatives are almost unchanged (164 → 169).
What actually changed is false positives collapsing from 1087 to 247, a 77% reduction, at
essentially the same recall. That is a strictly better operating point, not a different tradeoff
along the same curve.

## Inference cost (measured directly in this environment, not estimated)

Measured with `EvaluationResult.evaluation_latency_ms` (each evaluator's own self-reported timing,
around its own computation only) over 300 real WikiQA test-split `(question, answer)` pairs, after
model warm-up:

| | TF-IDF | Embedding |
|---|---|---|
| Mean latency / call | 3.35 ms | 345.39 ms |
| Median (p50) | 3.11 ms | 286.87 ms |
| p95 | 4.75 ms | 498.58 ms |
| Ratio | 1x (baseline) | **~103x slower** |

This ~100x gap is expected and structural, not a tuning artifact: TF-IDF is closed-form sparse
linear algebra on a 2-document vocabulary; the embedding evaluator runs two forward passes through
a 12-layer transformer via ONNX Runtime per call (this evaluator's interface evaluates one
`(input, output)` pair at a time — see `app/embedding_relevance.py` — so it cannot benefit from the
~35ms/text batched throughput measured separately at `batch_size=256` during initial model
evaluation; batching is a `services/worker`-level opportunity, not something this evaluator's
per-call interface exposes). Absolute latency here reflects this specific sandboxed development
machine's CPU allocation, not a claim about production hardware — the ~100x ratio between the two
evaluators is the more portable number.

Full end-to-end validation-harness run time (2733 + 6165 = 8898 evaluate() calls, no batching, per
the harness's one-call-per-example design matching production's per-span usage): TF-IDF completes
in well under a minute; the embedding run took approximately 20 minutes wall-clock in this
environment, consistent with the per-call ratio above.

Model load / first construction cost (paid once per process, not per `evaluate()` call): TF-IDF's
`RelevanceEvaluator()` constructor is near-instant (no state to load). `EmbeddingRelevanceEvaluator()`
took several seconds on first construction in this environment (ONNX session initialization); a
`services/worker` process that constructs one evaluator instance and reuses it across many spans
(the intended lifecycle — see `app/embedding_relevance.py`'s class docstring) pays this once, not
per span.

## Model size / dependencies / CPU considerations

| | TF-IDF | Embedding |
|---|---|---|
| Model weights | None (fit per-pair at call time) | ~67 MB (quantized ONNX, `BAAI/bge-small-en-v1.5`) |
| Resolved dependency count (`pip install --dry-run`, this repo's venv) | 15 packages (`scikit-learn`, `scipy`, `numpy`, ...) | 27 packages (adds `fastembed`, `onnxruntime`, `huggingface-hub`, `tokenizers`, ...) |
| `httpx`/`requests` in the tree | No | Yes — unavoidable for any real pretrained-model option, verified directly (see `README.md`'s model-comparison table: `sentence-transformers` and `spaCy` both also pull it in) |
| Runtime backend | Pure NumPy/SciPy | ONNX Runtime (no PyTorch) |
| Network required | Never, at any point | Once per cache location (model download); never during `evaluate()` — enforced by a socket-blocking test in both evaluators' test suites |
| Determinism | Closed-form arithmetic, deterministic by construction | Bit-identical repeated output confirmed on this development platform (module-level and cross-instance); cross-platform floating-point non-associativity not independently ruled out on every target platform |
| Install footprint | ~140 MB on disk (per original README measurement) | Model (~67 MB) + ONNX Runtime + supporting packages — heavier than TF-IDF's footprint, lighter than a PyTorch-based alternative would be (`sentence-transformers` dry-run resolved 34 packages including `torch`) |

## Qualitative failure-mode comparison

| Failure mode | TF-IDF | Embedding |
|---|---|---|
| Keyword-overlap false positives (shares vocabulary, wrong answer) | **Dominant failure pattern** — e.g. "who made the matrix" vs. a sentence about reviews of the Matrix, score 0.26, flagged relevant | **Substantially resolved.** The exact same TF-IDF false-positive examples were re-scored directly: both now fall below the embedding evaluator's (higher) threshold and are correctly classified `not_relevant`. Aggregate FP count fell 77% (1087 → 247). |
| Coreference/pronoun blindness (correct answer via "he"/"she", no named-entity overlap) | **Severe** — mathematically guaranteed `0.0000` score (zero lexical overlap), e.g. "how did david carradine die" → "He died..." | **Signal recovered, threshold not always cleared.** The same example scores 0.8235 with embeddings (up from 0.0000) — real evidence the model resolves the pronoun — but still falls just under this dataset's 0.8900 operating threshold, so it remains a false negative at that specific operating point even though the underlying signal improved dramatically. |
| Synonym/paraphrase blindness (correct answer, different vocabulary) | Severe — e.g. "sperm" vs. "seminal fluid, spermatozoa" scored `0.0000` | Not exhaustively re-tested pair-by-pair in this report, but expected to improve for the same structural reason as coreference (contextual embeddings, not surface tokens) — a candidate item for a future, more targeted follow-up experiment. |
| New/residual pattern | N/A | **Semantic topical false positives** — answers that share the question's topic/entity without answering it now score nearly as high as genuine correct answers (e.g. 0.90–0.93 for both), compressing the usable score range and directly explaining why precision (0.3342) stays well under 1.0 despite strong ROC-AUC (0.8220). |
| Threshold sensitivity | Some near-miss false negatives noted, not the dominant pattern | **More pronounced** — most sampled false negatives cluster in a narrow 0.74–0.88 band just under the 0.89 threshold, a direct consequence of BGE compressing same-domain English Q&A pairs into a narrow high-similarity range regardless of correctness. |

## Conclusion

**Outcome: B — the embedding evaluator improves substantially over the TF-IDF baseline, but does
not yet clear the bar for unconditional adoption as V1's production evaluator. Another,
more targeted experiment is warranted before that decision.**

Weighed directly against the three possible outcomes this milestone was scoped against:

- **Not C (does not justify its cost).** The improvement is large, real, and mechanistically
  explained, not noise: F1 more than doubled (0.1710 → 0.3735), precision more than tripled
  (0.1061 → 0.3342) at essentially unchanged recall, ROC-AUC improved by 0.14 absolute, and —
  critically — the exact failure examples the TF-IDF report predicted a semantic model would fix
  (keyword-overlap false positives, coreference-driven false negatives) were individually
  re-verified to behave exactly as hypothesized when re-run through the embedding evaluator. A
  ~100x per-call latency cost and a heavier dependency footprint are real costs, but they are not
  disproportionate to a 2-3x accuracy improvement for an evaluation pipeline that (per ADR 004 §6)
  is already designed around sampling and bounded concurrency rather than per-request latency.
- **Not an unqualified A (clearly ready for production as-is).** At its validation-optimal
  threshold, test precision is still 0.3342 — roughly 2 out of every 3 pairs this evaluator calls
  "relevant" are still wrong per WikiQA's ground truth. That is a substantial improvement over
  TF-IDF's roughly 9-out-of-10 wrong, but it is not the bar a product-facing trust signal should
  clear without qualification. The failure analysis identifies a specific, addressable reason —
  semantic topical false positives compressing the usable score range — that a follow-up
  experiment could plausibly improve on (candidates: a larger BGE variant trading latency for
  accuracy, a query-side instruction prefix per the model card's retrieval guidance — not tested in
  this first experiment to keep the comparison symmetric with TF-IDF's treatment of both texts —
  or a non-single-global-threshold policy, since `validation.metrics.select_by_min_precision`
  already exists and is unused by default).
- **B is the supported conclusion.** Substantial, mechanistically-verified improvement over the
  baseline, combined with a concrete, identified residual failure mode (topical-but-wrong false
  positives) and at least one untested lever likely to address it (instruction-prefixed query
  embeddings), is a direct, evidence-based argument for **one more targeted embedding experiment**
  before a production-adoption decision — not a demand to abandon this candidate, and not
  justification to ship it into production as a trusted score today.

## What this report does not do

Per this milestone's explicit scope: no evaluator was integrated into `services/worker`,
`apps/api`, ClickHouse, PostgreSQL, Redis, the dashboard, or the SDK. `app/relevance.py` was not
modified — its validated numbers in `validation/reports/wikiqa_baseline.md` are unchanged and were
not re-run. No threshold, model, or algorithm decision made here is final; this report is evidence
to inform the next milestone's decision, per the same validation-before-integration discipline
`docs/decisions/004-evaluation-engine.md` section 10 established for the TF-IDF baseline.
