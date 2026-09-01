# WikiQA Baseline Validation Report

**Evaluator under test:** `relevance` v`0.1.0` — the
existing deterministic **TF-IDF cosine-similarity lexical baseline** in
`services/evaluator/app/relevance.py`. This is explicitly **not** an embedding-based evaluator (see
`docs/decisions/004-evaluation-engine.md` section 1) — it is not described as one anywhere in this
report, and this validation does not change that categorization.

**Evaluated at:** 2026-09-01T15:40:19.157464+00:00

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
- **Selected threshold:** `0.1700`.
- **Validation metrics at selected threshold:** precision = 0.1213, recall =
  0.4357, F1 = 0.1897.
- This threshold was frozen before any test-split evaluation ran, and was not changed afterward.

## Held-out test metrics (frozen threshold, never tuned on test labels)

| metric | value |
|---|---|
| threshold used | 0.1700 |
| precision | 0.1061 |
| recall | 0.4403 |
| F1 | 0.1710 |
| ROC-AUC | 0.6798 |

**Confusion matrix (test split, at frozen threshold):**

| | predicted relevant | predicted not_relevant |
|---|---|---|
| **actually relevant** | TP = 129 | FN = 164 |
| **actually not_relevant** | FP = 1087 | TN = 4785 |

## Failure analysis

Bounded sample (seed=42, up to 15 false positives
and 15 false negatives shown; full machine-readable sample in
`wikiqa_baseline_failures.json`).

### False positives (evaluator said relevant; ground truth says not relevant)

| question | answer | true_label | score | predicted_label |
|---|---|---|---|---|
| what religion is church of christ | Church of Christ (Temple Lot) , a Latter Day Saint denomination based in Independence, Missouri | 0 | 0.2377 | 1 |
| when did spongebob first air | Hillenburg decided to use the name "SpongeBob". | 0 | 0.1708 | 1 |
| who made the matrix | Reviewers praised The Matrix for its innovative visual effects, cinematography and its entertainment. | 0 | 0.2597 | 1 |
| what are the root chords for first position? | Root position triads from C major scale . | 0 | 0.3563 | 1 |
| what is a law degree called? | The Juris Doctor (J.D.) is a professional doctorate and first professional graduate degree in law . | 0 | 0.2377 | 1 |
| what does google don't be evil mean? | Criticism of Google often includes a reference to "Don't be evil". | 0 | 0.3809 | 1 |
| How does Chronic Kidney Disease stage 4 effect Diabetics | Stage 5 CKD is often called End Stage Renal Disease (ESRD) and is synonymous with the now outdated terms chronic kidney failure (CKF) or chr… | 0 | 0.2885 | 1 |
| how did crater lake get its color | Crater Lake is known for the " Old Man of the Lake ", a full-sized tree which is now a stump that has been bobbing vertically in the lake fo… | 0 | 0.3111 | 1 |
| what is connecticut's intermediate appellate court | In some places, the appellate court has limited powers of review. | 0 | 0.2606 | 1 |
| what is an example of a chemical reaction that absorbs heat? | The substance (or substances) initially involved in a chemical reaction are called reactants or reagents . | 0 | 0.1786 | 1 |
| how much population is a us urban city | World map showing percent of population living in an urban environment. | 0 | 0.2695 | 1 |
| what division does uconn hockey play in? | UConn is one of only 15 universities in the nation that plays Division I FBS football and Division I men's ice hockey. | 0 | 0.3249 | 1 |
| how was the president involved in the gulf war | The war is also known under other names , such as the Persian Gulf War, First Gulf War, Gulf War I, or the First Iraq War, before the term "… | 0 | 0.4151 | 1 |
| What album is our song on taylor swift | "Our Song", a 2010 song by The Spill Canvas | 0 | 0.2412 | 1 |
| what blood type is universal donor | Blood type (or blood group) is determined, in part, by the ABO blood group antigens present on red blood cells. | 0 | 0.3381 | 1 |


### False negatives (evaluator said not_relevant; ground truth says relevant)

| question | answer | true_label | score | predicted_label |
|---|---|---|---|---|
| how does interlibrary loan work | Interlibrary loan (abbreviated ILL, and sometimes called interloan, document delivery, or document supply) is a service whereby a user of on… | 1 | 0.1216 | 0 |
| how does interlibrary loan work | The user makes a request with their local library, which, acting as an intermediary, identifies owners of the desired item, places the reque… | 1 | 0.0000 | 0 |
| what percentage of water in in the body | In a newborn infant, this may be as high as 75 percent of the body weight, but it progressively decreases from birth to old age, most of the… | 1 | 0.0787 | 0 |
| how did harmon killebrew get strong | With quick hands and exceptional upper-body strength, Killebrew was known not just for the frequency of his home runs but also for their dis… | 1 | 0.0764 | 0 |
| what year did isaac newton die | Sir Isaac Newton (25 December 164220 March 1727) was an English physicist and mathematician who is widely regarded as one of the most influe… | 1 | 0.1159 | 0 |
| when to use semicolon | The Italian printer Aldus Manutius the Elder established the practice of using the semicolon to separate words of opposed meaning and to ind… | 1 | 0.1048 | 0 |
| what is a neuro tract | A neural pathway, neural tract, or neural face, connects one part of the nervous system with another and usually consists of bundles of elon… | 1 | 0.0833 | 0 |
| what is stent surgery | In the technical vocabulary of medicine , a stent is a mesh 'tube' inserted into a natural passage/conduit in the body to prevent or counter… | 1 | 0.0986 | 0 |
| where was martin luther born | Martin Luther (; 10 November 1483 – 18 February 1546) was a German monk , former Catholic priest , professor of theology and seminal figure … | 1 | 0.1456 | 0 |
| who is mary matalin married to | She is married to Democratic political consultant James Carville . | 1 | 0.1363 | 0 |
| what became of rich on price is right | Fields can also be heard on the radio on K-EARTH 101 KRTH , KNX-AM and KFWB in Los Angeles. | 1 | 0.0000 | 0 |
| What does Human sperm consist of? | In humans, seminal fluid contains several components besides spermatozoa: proteolytic and other enzymes as well as fructose are elements of … | 1 | 0.0000 | 0 |
| how often does ham station need to ID? | Station identification used to be done regularly by an announcer at the halfway point during the presentation of a television program, or in… | 1 | 0.0736 | 0 |
| how many numbers on a credit card | An ISO/IEC 7812 card number is typically 16 digits in length, and consists of: | 1 | 0.1037 | 0 |
| how did david carradine die | He died on June 3, 2009, apparently of auto-erotic asphyxiation . | 1 | 0.0000 | 0 |


### Dominant failure patterns (human-readable summary)

Based on manual inspection of the sampled examples above (not auto-generated — this is a read of
the actual text):

- **Keyword-overlap false positives, exactly as predicted before running this harness.** The
  dominant false-positive pattern: an answer about the same general topic as the question, sharing
  its most distinctive vocabulary, without actually answering it. "who made the matrix" scored 0.26
  against "Reviewers praised The Matrix for its innovative visual effects..." (shares "Matrix"
  heavily, never says who made it). "what religion is church of christ" scored 0.24 against an
  answer about a specific splinter denomination also named "Church of Christ" — topically adjacent,
  not actually responsive. "how was the president involved in the gulf war" scored 0.42 against a
  sentence about the war's alternate names — high overlap on "gulf war," zero content about the
  president's involvement. This is TF-IDF's core weakness surfacing directly: it measures shared
  vocabulary, which correlates with but is not the same as answering the question.
- **Pronoun/coreference blindness is the single most consistent false-negative pattern.** Several
  correct answers refer to the question's subject via a pronoun rather than repeating the named
  entity: "who is mary matalin married to" → "**She** is married to ... James Carville" (score
  0.14); "how did david carradine die" → "**He** died on June 3, 2009 ..." (score **0.00** — zero
  lexical overlap at all, since neither "david" nor "carradine" appears). TF-IDF has no mechanism
  to resolve "she"/"he" back to the question's subject; a model with any notion of coreference or
  discourse context would not make this class of error.
- **Synonym/paraphrase false negatives**, the other predicted pattern: "What does Human sperm
  consist of?" scored **0.00** against an answer built entirely from "seminal fluid,"
  "spermatozoa" — correct, but sharing almost no surface tokens with "sperm." "what percentage of
  water in in the body" (0.08) and "what year did isaac newton die" (0.12) show the same effect at
  smaller scale: the correct answer is long and only briefly touches the exact phrasing of the
  question, diluting the shared-term signal in the normalized TF-IDF vector.
- **Several false negatives are near-misses of the frozen threshold** (0.1216, 0.1159, 0.1456 vs.
  threshold 0.1700), not dramatic failures — meaning some of this loss is an artifact of the single
  global threshold rather than the score being wildly wrong. This does not change the overall
  conclusion: F1 is low at every threshold on the sweep (0.19 at its validation-optimal point), so
  no single cutpoint recovers strong performance from this scoring function.
- **Short answers (<3 words) are not disproportionately represented** in either sampled failure
  list — most false positives and false negatives above are full sentences. The "Dataset concerns"
  hypothesis that short answers would be a major driver is not supported by this sample.

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

**Outcome: B — TF-IDF is useful as a baseline, but not strong enough for production. Evaluate a
semantic embedding approach next.**

Reasoning, weighed directly against the three possible outcomes:

- **Not A (acceptable as-is).** At its validation-optimal threshold, test-set precision is 0.1061 —
  roughly 9 out of every 10 pairs the evaluator calls "relevant" are not, per WikiQA's ground truth.
  That is too weak to drive a product-facing signal without heavy caveats, regardless of how the
  threshold is tuned: F1 tops out at 0.19 across the full 101-point sweep on the validation split
  (see "Threshold selection"), so this is a ceiling on the scoring function itself, not a threshold
  miscalibration.
- **Not C (benchmark mismatch too significant to decide).** The results are not noise, and they are
  not uninterpretable. ROC-AUC = 0.6798 is meaningfully above the 0.5 random baseline, and F1 =
  0.1710 is about **1.9x** the trivial "always predict relevant" baseline for this class balance
  (precision ≈ positive rate = 0.0475, recall = 1.0, F1 ≈ 0.0908) — so the evaluator carries real,
  above-chance signal. More importantly, the failure analysis above did not surface confusing or
  contradictory behavior; it surfaced *exactly* the two failure modes predicted from TF-IDF's known
  theoretical limitations before this harness ever ran (keyword-overlap false positives, and
  paraphrase/coreference-driven false negatives). A benchmark that produces theory-consistent,
  mechanistically explainable results is doing its job — the honest reading is "the baseline is
  weak," not "the benchmark can't tell us anything."
- **B is the supported conclusion.** Weak-but-real, above-chance discrimination, combined with a
  concrete, reproducible failure pattern (pronoun/coreference blindness, synonym blindness) that a
  dense semantic embedding is specifically designed to address, is a direct, evidence-based argument
  for evaluating a semantic embedding approach next — not a demand to abandon the baseline (it
  remains useful as the zero-cost, zero-network-dependency comparison point any future embedding
  approach should be measured against), and not license to ship the current evaluator into
  production as a trusted score.

**This does not change anything today.** Per this milestone's explicit scope, the evaluator
algorithm in `services/evaluator/app/relevance.py` has not been modified — this report is evidence
to inform the *next* decision, not a change made in response to it. Any move toward a semantic
embedding approach is a separate, future milestone, subject to the same dependency-discipline and
validation-before-integration discipline this one followed (see `docs/decisions/004-evaluation-engine.md`
section 10).
