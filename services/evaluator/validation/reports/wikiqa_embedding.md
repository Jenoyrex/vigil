# WikiQA Validation Report — `relevance_embedding`

**Evaluator under test:** `relevance_embedding` v`0.1.0` —
the experimental **dense semantic embedding cosine-similarity candidate** in `services/evaluator/app/embedding_relevance.py` (`BAAI/bge-small-en-v1.5` via `fastembed`/ONNX Runtime, local inference only). This is explicitly **not** the V1 production evaluator — see `services/evaluator/README.md`'s "Embedding relevance evaluator (experimental)" section and `validation/reports/wikiqa_comparison.md` for the head-to-head comparison this report feeds into.

**Evaluated at:** 2026-09-03T04:02:47.104148+00:00

## Dataset information

- **Dataset:** WikiQA (`microsoft/wiki_qa`), config
  `default`.
- **Source:** Hugging Face datasets-server API mirror of the official Microsoft Research WikiQA
  Corpus, downloaded directly by `scripts/download_wikiqa.py` — never redistributed by Vigil (see
  "Licensing" below).
- **Dataset revision (git commit SHA on the Hugging Face repo at download time):**
  `3f104672b5de699878fe7907afc486f0de325eb5`.
- **Downloaded at:** 2026-09-01T15:39:10.127956+00:00.
- **Split sizes used by this harness:** validation = 2733, test =
  6165. **The train split is not used** — this evaluator has no trainable
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
3. Sweep 101 evenly spaced candidate thresholds across [0.0, 1.0] on the
   validation split; select the threshold maximizing F1 (`validation.metrics.select_by_max_f1` —
   the V1 default policy; `select_by_min_precision` exists as an example of a different, pluggable
   policy, not used here).
4. **Freeze that threshold.** Run the evaluator once per pair on the **held-out test split**,
   independently, and apply the frozen threshold — the threshold is never re-tuned against test
   labels, and no evaluator change was made in response to any test-set result.
5. Compute precision/recall/F1/ROC-AUC/confusion-matrix on the test split at the frozen
   threshold, and sample a bounded set of false positives/negatives for failure analysis.

## Class distribution

| split | total | positive | negative | positive rate | unique questions | all-negative question groups | answers < 3 words |
|---|---|---|---|---|---|---|---|
| validation | 2733 | 140 | 2593 | 5.1% | 296 | 170 | 30 |
| test | 6165 | 293 | 5872 | 4.8% | 633 | 390 | 42 |

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
  101 evenly spaced thresholds in [0.0, 1.0].
- **Selected threshold:** `0.8900`.
- **Validation metrics at selected threshold:** precision = 0.3182, recall =
  0.4000, F1 = 0.3544.
- This threshold was frozen before any test-split evaluation ran, and was not changed afterward.

## Held-out test metrics (frozen threshold, never tuned on test labels)

| metric | value |
|---|---|
| threshold used | 0.8900 |
| precision | 0.3342 |
| recall | 0.4232 |
| F1 | 0.3735 |
| ROC-AUC | 0.8220 |

**Confusion matrix (test split, at frozen threshold):**

| | predicted relevant | predicted not_relevant |
|---|---|---|
| **actually relevant** | TP = 124 | FN = 169 |
| **actually not_relevant** | FP = 247 | TN = 5625 |

## Failure analysis

Bounded sample (seed=42, up to 15 false positives
and 15 false negatives shown; full machine-readable sample in
`wikiqa_embedding_failures.json`).

### False positives (evaluator said relevant; ground truth says not relevant)

| question | answer | true_label | score | predicted_label |
|---|---|---|---|---|
| what bacteria grow on macconkey agar | A MacConkey agar plate with an active bacterial culture. | 0 | 0.9050 | 1 |
| what percentage of water in in the body | In physiology , body water is the water content of the human body . | 0 | 0.9292 | 1 |
| what is accounts payable and receivable | Accounts receivable is money owed to a business by its clients (customers or debtors) and shown on its balance sheet as an asset. | 0 | 0.9075 | 1 |
| when do midlife crises happen | A midlife crisis is experienced by many people during the midlife transition when they realize that life may be more than halfway over. | 0 | 0.9122 | 1 |
| what is a tapered wheel bearing | Cutaway view of a tapered roller bearing | 0 | 0.9061 | 1 |
| what was michigan's first capital | The first state capitol was located in Detroit , the original capital of Michigan, and was relocated to Lansing in 1847, due to the need to … | 0 | 0.9206 | 1 |
| what is the input and what is the output of a function | The output of a function f corresponding to an input x is denoted by f(x) (read "f of x"). | 0 | 0.8985 | 1 |
| who are the field dimension for lacrosse | Field lacrosse is played professionally in North America by the Major League Lacrosse . | 0 | 0.9030 | 1 |
| what is psi in pressure | Pressure (the symbol: p) is the ratio of force to the area over which that force is distributed. | 0 | 0.9083 | 1 |
| when was the last national football championship for auburn | Auburn has claimed three national championships (1913, 1957, 2010) and produced three Heisman Trophy winners: quarterback Pat Sullivan in 19… | 0 | 0.8992 | 1 |
| how does a heat pump work | A heat pump uses some amount of external high-grade energy to accomplish the desired transfer of thermal energy from heat source to heat sin… | 0 | 0.9029 | 1 |
| what does gloria in excelsis deo mean | The name is often abbreviated to Gloria in Excelsis or simply Gloria. | 0 | 0.9155 | 1 |
| what happened to the 6th army | Sixth Army may refer to: | 0 | 0.9121 | 1 |
| how many members are in the house of representatives | The United States House of Representatives is one of the two houses of the United States Congress (a bicameral legislature ). | 0 | 0.9090 | 1 |
| what food is in afghan | A table setting of Afghan food in Kabul . | 0 | 0.9149 | 1 |


### False negatives (evaluator said not_relevant; ground truth says relevant)

| question | answer | true_label | score | predicted_label |
|---|---|---|---|---|
| what percentage of water in in the body | In a newborn infant, this may be as high as 75 percent of the body weight, but it progressively decreases from birth to old age, most of the… | 1 | 0.8376 | 0 |
| how did harmon killebrew get strong | With quick hands and exceptional upper-body strength, Killebrew was known not just for the frequency of his home runs but also for their dis… | 1 | 0.8383 | 0 |
| How much did Waterboy grossed | The movie was extremely profitable, earning $161.5 million in North America alone. | 1 | 0.7618 | 0 |
| what school did Zach Thomas play for before making it in to the NFL | He played college football for Texas Tech University , and was recognized as an All-American . | 1 | 0.8479 | 0 |
| how is whooping cough distinguished from similar diseases | Symptoms are initially mild, and then develop into severe coughing fits, which produce the namesake high-pitched "whoop" sound in infected b… | 1 | 0.8533 | 0 |
| what is a google in math | A googol is the large number 10100; that is, the digit 1 followed by 100 zeroes : | 1 | 0.8369 | 0 |
| what was the first year of kentucky derby | Unlike the Preakness and Belmont Stakes, which took hiatuses in 1891-1893 and 1911-1912 respectively, the Kentucky Derby has been run every … | 1 | 0.8553 | 0 |
| what is the actresses name that played in walk that line? | The film was nominated for five Academy Awards including Best Actor (Joaquin Phoenix), Best Actress (Reese Witherspoon, which she won), and … | 1 | 0.7396 | 0 |
| what is atherosclerotic heart disease | It is caused by the formation of multiple plaques within the arteries. | 1 | 0.8491 | 0 |
| what became of rich on price is right | Fields is currently a meteorologist for the CBS owned and operated television stations KCBS-TV and KCAL-TV in Los Angeles, California. | 1 | 0.7351 | 0 |
| what became of rich on price is right | Fields can also be heard on the radio on K-EARTH 101 KRTH , KNX-AM and KFWB in Los Angeles. | 1 | 0.7367 | 0 |
| what does a liquid oxygen plant look like | The blue color of liquid oxygen in a dewar flask | 1 | 0.8815 | 0 |
| how jameson irish whiskey is made | Jameson is similar in its adherence to the single distillery principle to the single malt tradition, but Jameson blends column still spirit … | 1 | 0.8502 | 0 |
| what does hair testing show | A drug test is a technical analysis of a biological specimen – for example urine, hair, blood, sweat, or oral fluid / saliva – to determine … | 1 | 0.8588 | 0 |
| what food is in afghan | Accompanying these staples are dairy products ( yogurt and whey ), various nuts , and native vegetables, as well as fresh and dried fruits; … | 1 | 0.8659 | 0 |


### Dominant failure patterns (human-readable summary)

Based on manual inspection of the sampled examples above, cross-checked directly against the exact
pairs `validation/reports/wikiqa_baseline.md` flagged as TF-IDF's dominant failures (re-run through
`EmbeddingRelevanceEvaluator` individually to confirm, not inferred):

- **TF-IDF's keyword-overlap false positives are substantially resolved, not just reduced.** Both
  named examples from the TF-IDF failure analysis were re-checked directly: "who made the matrix"
  vs. "Reviewers praised The Matrix for its innovative visual effects..." (label 0) now scores
  0.8366 — below this report's 0.8900 threshold, correctly `not_relevant`. "what religion is
  church of christ" vs. the Temple Lot denomination answer (label 0) scores 0.8554 — also correctly
  below threshold. Neither survives as a false positive at the operating point this harness
  selected. Test-set FP count fell from TF-IDF's 1087 to 247 (a 4.4x reduction) even though the
  test set and protocol are identical, corroborating this at the aggregate level, not just for
  these two spot-checks.
- **TF-IDF's coreference/pronoun-blindness false negatives improved in raw signal but did not
  fully cross the operating threshold.** The two named TF-IDF examples were re-checked directly:
  "how did david carradine die" → "**He** died on June 3, 2009..." went from TF-IDF's
  mathematically-guaranteed `0.0000` to **0.8235** — a large recovery of real signal, direct
  evidence the model resolves the pronoun back to the question's subject where TF-IDF structurally
  cannot. "who is mary matalin married to" → "**She** is married to... James Carville" went from
  `0.1363` to **0.8035**, the same pattern. Both remain just under this report's 0.8900 threshold,
  so both are still counted as false negatives here — the failure mode is not eliminated, but its
  *character* changed: from "no signal at all" to "correct signal that a single, high, precision-
  protecting threshold still doesn't clear."
- **A new dominant failure pattern replaces keyword overlap: semantic topical false positives.**
  The sampled false positives above are answers that share the question's *topic/entity* but not
  its actual content — "what bacteria grow on macconkey agar" scores 0.9050 against a sentence
  that only shows a photo caption of a MacConkey agar plate, never naming a bacterium; "what
  percentage of water in in the body" scores 0.9292 against a sentence that only defines "body
  water" as a concept, without giving a percentage. These score *higher* than several genuine true
  positives (e.g. the Carradine/Matalin false negatives above, at 0.82 and 0.80) — meaning the
  score range for "same topic, doesn't answer" and "different topic, does answer" overlap heavily
  in this model's output for this task, which is the direct cause of precision (0.3342) staying
  well under 1.0 despite ROC-AUC indicating strong overall ranking ability (0.8220).
- **Most false negatives are near-misses of the frozen threshold, more visibly than in the TF-IDF
  report.** Nearly every sampled false-negative score falls in a narrow 0.74–0.88 band, just below
  0.8900 — e.g. 0.8815, 0.8659, 0.8588. This reflects the same structural issue as the point above:
  BGE compresses same-domain English Q&A text into a narrow high-similarity band regardless of
  whether it actually answers the question, so the single global threshold this harness selects
  (to hold precision as high as it can on the validation split) inevitably clips a meaningful slice
  of true positives whose raw scores land just inside that same compressed band.
- **Short answers are not disproportionately represented** in either sampled failure list, the
  same finding as the TF-IDF report — most false positives and false negatives above are full
  sentences, not the `< 3`-word short-answer rows flagged in "Dataset concerns."

## Limitations

- **WikiQA measures candidate-answer relevance to a question — it is not identical to evaluating
  generated LLM responses.** `answer` in this dataset is an *extractive* Wikipedia sentence from a
  retrieval/sentence-selection task, not a *generated* completion. This report is evidence about
  whether the evaluator can detect question–candidate-sentence relevance in that setting; it is not
  direct evidence about its behavior on production Vigil LLM-span traces. This limitation applies
  identically to `validation/reports/wikiqa_baseline.md`'s TF-IDF numbers, so it does not bias the
  comparison between the two reports — both are measured on the same benchmark under the same
  limitation.
- **This evaluator's score range is narrower and higher than TF-IDF's for same-domain English
  text**, per the failure analysis above — the practical consequence is that a single global
  threshold trades recall for precision more sharply here than for TF-IDF's wider, lower score
  distribution. See `validation/reports/wikiqa_comparison.md` for whether a different
  threshold-selection policy (e.g. `select_by_min_precision`, already implemented and unused by
  default in `validation.metrics`) should be revisited before any production adoption.
- The threshold selected here (0.8900) is specific to WikiQA's score distribution and label
  balance; it is not claimed to be the right operating point for Vigil's actual production traffic,
  which will have a different distribution of question/answer pairs entirely — the same caveat
  `validation/reports/wikiqa_baseline.md` states for TF-IDF's threshold.
- **No query/document instruction prefix was used.** `BAAI/bge-small-en-v1.5`'s model card notes
  prefixes are "not so necessary" for this version (unlike the original `bge-small-en`), but this
  experiment did not test whether a query-side instruction prefix measurably improves separation
  between "same topic" and "actually answers" — a candidate follow-up, not attempted here to keep
  this first experiment a fair, symmetric-treatment comparison against TF-IDF (which also treats
  `input_text`/`output_text` symmetrically).

## Conclusion / Recommendation

See `validation/reports/wikiqa_comparison.md` for the full head-to-head comparison against
`validation/reports/wikiqa_baseline.md` and the resulting evidence-based decision (this milestone's
outcome **B**: a real, substantial improvement over TF-IDF that does not yet clear the bar for
unconditional V1 production adoption as-is).
