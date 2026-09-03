# vigil-evaluator

Evaluation algorithms for Vigil, per `docs/decisions/004-evaluation-engine.md` ("ADR 004"). This
package is the `services/evaluator` half of ADR 004's service boundary: it owns evaluation
**algorithms only**. It has no knowledge of a job queue, PostgreSQL, ClickHouse, HTTP, or any
Vigil-internal model — it does not import from, or get imported by, `apps/api`, `services/worker`,
or the dashboard. Given plain evaluator input, it returns a plain evaluator result. Nothing else.

**This milestone builds the evaluator interface and the V1 relevance evaluator only.** No job
infrastructure, no storage, no worker, no API changes, no dashboard changes — see ADR 004 section
10 for why: this evaluator has to be validated against benchmark data *before* anything is built to
depend on it, which is the next milestone, not this one.

## What this package is not (yet)

- **Not connected to anything.** No caller exists yet. `services/worker` — the thing that will
  eventually call `RelevanceEvaluator.evaluate()` in production — has not been built.
- **Not benchmark-validated.** The small labeled examples in `tests/test_relevance.py` verify the
  evaluator *behaves as designed* (correct score bounds, deterministic, handles edge cases). They
  are not a benchmark and must not be read as evidence of real-world accuracy — see "Known
  limitations" below and ADR 004 section 10/11.
- **Not groundedness or faithfulness.** ADR 004 section 2 explains why those are blocked from V1;
  this package contains no code for either, not even disabled.
- **Not LLM-as-judge.** No third-party model call exists anywhere in this package.

## Evaluator interface (`app/interface.py`, `app/types.py`)

```python
class Evaluator(Protocol[TInput]):
    name: str
    version: str
    def evaluate(self, evaluator_input: TInput) -> EvaluationResult: ...
```

`Evaluator` is a `typing.Protocol`, not an abstract base class — an evaluator only needs to
structurally match this shape (`name`, `version`, `evaluate`), not inherit from anything. This keeps
every evaluator, present and future, decoupled from a shared base class that `services/worker` (or a
test) would otherwise need to import.

`EvaluationResult` is the single output type every evaluator returns, matching ADR 004 section 7:

| Field | Type | Notes |
|---|---|---|
| `evaluator_name` | `str` | e.g. `"relevance"` |
| `evaluator_version` | `str` | e.g. `"0.1.0"` — bumped whenever the algorithm/model/thresholds change |
| `score` | `float \| None` | `None` means "not meaningfully computable for this input", not zero |
| `label` | `str` | always populated, including for a not-evaluable outcome |
| `explanation` | `str` | bounded, human-readable; never a raw payload dump |
| `evaluation_latency_ms` | `float` | measured by the evaluator itself, around its own computation |
| `evaluation_cost_usd` | `float \| None` | `None` for a zero-marginal-cost local evaluator |
| `evaluator_model` | `str \| None` | populated even for local models — attribution, not just disclosure |
| `evaluator_provider` | `str \| None` | `None` for local models; set only if an external API were called |

Deliberately **excluded**: `evaluation_id`, `project_id`, `trace_id`, `span_id`, `created_at`. Those
identify *where* a result belongs once persisted against a specific span — a storage/job concern for
whatever calls an evaluator (`services/worker`, not yet built), not something a pure text-scoring
function has any business knowing. This is the "do not unnecessarily couple the interface to
database models" requirement, applied concretely.

## Relevance evaluator (`app/relevance.py`)

**Question it answers:** does an LLM span's `output` address its `input`? Nothing more — it does not
check factual correctness, and it does not require or use any retrieval context (per ADR 004 section
1, V1 does not assume RAG at all).

**Input contract** (`RelevanceEvaluatorInput`):

```python
@dataclass(frozen=True)
class RelevanceEvaluatorInput:
    input_text: str
    output_text: str
```

Deliberately plain strings, not the arbitrary JSON a span's `input`/`output` column can hold.
Converting a raw span's JSON `input`/`output` into evaluatable text (e.g. picking a message out of a
chat-messages array) is an adapter's job — `services/worker`'s, once it exists — not this
evaluator's. Keeping the contract to plain strings is exactly what makes this evaluator testable with
zero Vigil-internal types.

### Dependency and model choice

ADR 004 calls for "embedding cosine similarity ... using a local/self-hosted embedding model." Before
picking a library, two real dense-embedding options were installed and their actual resolved
dependency trees inspected — not assumed from memory:

- **`fastembed`** (ONNX-based, no PyTorch): `uv sync` resolved **36 packages**, including
  **`httpx` and `requests`** (both pulled in transitively by `huggingface-hub`, which `fastembed`
  needs to download model weights), plus `pillow`, `protobuf`, and `mmh3` — none of which this
  evaluator needs. `httpx` is on this milestone's explicit forbidden-dependency list. Rejected.
- **`sentence-transformers`** (PyTorch-based): not installed for comparison, since it is strictly
  heavier than `fastembed` (adds PyTorch and the `transformers` library on top of everything
  `fastembed` already pulled in) and would fail the same `httpx`/`requests` objection for the same
  reason (model downloading via `huggingface-hub`).

Both real dense-embedding options require downloading pretrained weights from a model hub at
first use, which — independent of the dependency-size objection — means the evaluator's first run in
any given environment is not fully network-independent, only its *subsequent* runs are (once weights
are cached).

**Chosen instead: TF-IDF vectorization + cosine similarity, via `scikit-learn`.** Installed and
verified: **15 packages** total (`scikit-learn`, `scipy`, `numpy`, `joblib`, `cloudpickle`,
`threadpoolctl`, `narwhals`, plus `pytest`/`ruff` dev tooling) — **zero** HTTP client anywhere in the
tree (confirmed via `uv tree`), zero `pillow`, zero `protobuf`. Runtime install footprint (scikit-learn
+ scipy + numpy) is ~140 MB on disk — not nothing, but standard, well-audited, CPU-only scientific
Python, not an ML framework.

This is a genuine tradeoff, stated plainly:

- TF-IDF vectors are a *sparse, lexical* (word-overlap-weighted) representation, not a *dense,
  semantic* one. Cosine similarity over TF-IDF vectors is a classical, well-established
  text-similarity technique — it predates neural embeddings and is still a standard baseline — but it
  will miss a correct answer that paraphrases the question with different words, and it can be fooled
  by keyword-stuffed but non-responsive text.
- No pretrained weights means: **zero network dependency at any point** — not just at evaluation
  time, but at install time and first-run time too. There is no model registry to be unreachable, no
  weights file to version or go stale, and nothing to download that isn't already a declared,
  pinned Python dependency.
- **Fully deterministic by construction, not just "deterministic in practice."** TF-IDF + cosine
  similarity is closed-form arithmetic on sparse vectors with no stochastic component. Dense neural
  inference is *typically* deterministic on a fixed platform, but floating-point non-associativity
  across different CPU architectures/BLAS backends is a known, real source of tiny cross-platform
  discrepancies for neural models — a risk this approach avoids entirely, which matters directly for
  "deterministic/reproducible enough for offline validation" (this milestone's own requirement).

This is the honest reason this is a *foundation*: whether TF-IDF's lexical similarity is good enough,
or whether the dependency cost of a real dense embedding model is worth paying for better accuracy, is
exactly the question the offline validation harness (next milestone, ADR 004 section 10) is for. This
package does not claim to have answered it.

### Algorithm

For each `evaluate()` call: strip whitespace from `input_text`/`output_text`; if either is empty
after stripping, return a `not_evaluable` result (see below). Otherwise, fit a per-pair
`TfidfVectorizer` (English stop words removed) on exactly the two texts — there is no larger corpus
to fit against — and compute cosine similarity between the resulting two vectors. TF-IDF vectors have
only non-negative entries, so this similarity is mathematically guaranteed to already fall within
`[0.0, 1.0]`; the code clips defensively against floating-point overshoot but applies no artificial
rescaling.

### Score and threshold semantics

- **Score range:** `[0.0, 1.0]`, or `None` if the input was not evaluable (see below). `0.0` means no
  shared vocabulary at all between input and output (orthogonal TF-IDF vectors); `1.0` means
  identical text.
- **Label:** `"relevant"` if `score >= threshold`, else `"not_relevant"`; `"not_evaluable"` when no
  score could be computed at all.
- **Threshold is a required constructor argument with an explicitly unvalidated default
  (`DEFAULT_THRESHOLD = 0.5`).** This number has not been checked against any labeled data. It is
  configurable specifically *because* no offline validation has happened yet (ADR 004 section 10) —
  the validation harness milestone is expected to determine a real threshold, or determine that a
  single global threshold isn't the right mechanism at all. **This evaluator's output must not be
  treated as accurate or calibrated until that validation exists.**
- **Empty/missing input or output:** if either text is empty or whitespace-only after stripping, the
  result is `score=None, label="not_evaluable"`, with an explanation naming which side was empty.
  This is a defined, non-error outcome, not a crash and not a misleading `0.0` score (a `0.0` score
  means "compared, and no overlap found" — a materially different claim from "could not compare
  at all").
  The same `not_evaluable` outcome (with a different explanation) covers text that is non-empty but
  has no comparable vocabulary after stop-word removal (e.g. text consisting only of punctuation or
  numbers) — `scikit-learn` raises internally for this case, and it is caught and handled the same
  way, not left to propagate as an unhandled exception.
- **Malformed input:** `RelevanceEvaluatorInput` validates its own fields are `str` at construction
  time (`__post_init__`), raising `InvalidEvaluatorInputError` (a `ValueError` subclass) immediately
  — it never silently accepts a non-string value. `RelevanceEvaluator.evaluate()` separately rejects
  being called with anything that isn't a `RelevanceEvaluatorInput` at all, with the same exception
  type.

## Tests (`tests/`)

35 tests, all passing, none touching the network, PostgreSQL, ClickHouse, Redis, or the dashboard —
run with `uv run pytest`. Coverage includes: a valid relevant example, a clearly unrelated example
(zero shared vocabulary — mathematically guaranteed `score == 0.0`), identical input/output
(mathematically guaranteed `score == 1.0`), every empty/whitespace-only input/output combination,
punctuation/stop-word-only text, deterministic repeated evaluation, score-bounds checks across several
example pairs, threshold boundary behavior (inclusive `>=`) and threshold-driven label flips,
malformed-construction and malformed-call-type rejection, evaluator name/version/model/provider
metadata, and a dedicated test that blocks all socket connections at the Python `socket` module level
and asserts `evaluate()` still completes successfully.

## Embedding relevance evaluator (experimental) (`app/embedding_relevance.py`)

**Not the V1 production evaluator.** `app/relevance.py`'s TF-IDF evaluator keeps that role
unchanged — this section documents a second, independently selectable candidate built to answer
the question the TF-IDF validation report (`validation/reports/wikiqa_baseline.md`) leaves open:
"is a local semantic embedding worth its dependency cost?" See
`validation/reports/wikiqa_comparison.md` for the evidence-based answer.

### Why an embedding evaluator at all

`validation/reports/wikiqa_baseline.md`'s conclusion was **B**: TF-IDF carries real, above-chance
signal (test ROC-AUC 0.6798) but tops out at F1 0.1710 on WikiQA, with a failure analysis showing
two theory-predicted, mechanistically explainable weaknesses — **keyword-overlap false positives**
(shares vocabulary, doesn't answer the question) and **coreference/synonym false negatives**
(correct answer, near-zero lexical overlap — e.g. "how did X die" → "**He** died ..." scored
`0.0000`). Both are exactly the class of error a dense, contextual sentence embedding is designed
to address — pronouns and paraphrases can land close in embedding space despite sharing no surface
tokens. That is the hypothesis this evaluator exists to test, not an assumption this section treats
as already confirmed.

### Model candidates considered

Before picking a library or model, three real local options were installed (or dry-run resolved)
and their actual dependency trees inspected — not assumed from memory, and not from documentation
alone:

| | **sentence-transformers** (`all-MiniLM-L6-v2`) | **fastembed** (`BAAI/bge-small-en-v1.5`) | **spaCy** (`en_core_web_md`) |
|---|---|---|---|
| Embedding dim | 384 | 384 | 300 |
| Model size | ~90 MB (fp32 PyTorch weights) | ~67 MB (quantized ONNX, `model_optimized.onnx`) | ~40 MB |
| Architecture | 6-layer MiniLM transformer, mean-pooled, contextual | 12-layer BERT-base-derived transformer (BGE), contextual | Static GloVe-style word vectors, averaged — **not contextual** |
| Runtime backend | PyTorch (`torch`, CPU wheel) | ONNX Runtime (no PyTorch) | spaCy's own `thinc`/`blis` C-extension stack |
| Resolved dependency count (`pip install --dry-run`, this repo's venv) | **34 packages**, including `torch` (largest single wheel by far), `transformers`, `safetensors`, `sympy`, `networkx`, `jinja2` | **27 packages** — no PyTorch, no `transformers`; adds `onnxruntime`, `pillow`, `protobuf`, `mmh3` (CLIP-related, unused here) | **34 packages** — no PyTorch, but its own custom C-extension stack (`blis`, `cymem`, `murmurhash`, `preshed`, `thinc`) plus `pydantic` |
| `httpx`/`requests` in the tree? | **Yes** — via `huggingface-hub` (model download) | **Yes** — via `huggingface-hub` (model download) | **Yes** — via `weasel`/`smart_open` (model download) |
| CPU inference | Slower cold start (larger runtime init), mature and heavily battle-tested op coverage | Faster cold start, quantized int8 weights specifically tuned for CPU throughput | Fastest (no transformer forward pass at all) but weakest signal — see below |
| License (library / model) | Apache-2.0 / Apache-2.0 | Apache-2.0 / **MIT** | MIT / **CC BY-SA 4.0** (model) |
| Downloadable by users? | Yes, from Hugging Face, no gated access | Yes, from Hugging Face, no gated access | Yes, via `spacy download`, no gated access |
| Network required after install? | Only for the first download; cached thereafter | Only for the first download; cached thereafter | Only for the first download; cached thereafter |
| Strengths | Most mainstream choice, largest community track record, broad model zoo | Lightest runtime footprint of the two transformer options, no PyTorch, quantized-by-default, MIT model license, competitive-or-better MTEB scores than MiniLM-L6-v2 | Lightest model file, fastest inference, deterministic-by-construction (no float non-associativity risk from a transformer forward pass) |
| Weaknesses | Heaviest install (PyTorch dominates disk/memory footprint), slowest cold start | Slightly less mainstream than sentence-transformers as a library (though the model itself, BGE, is very widely used) | **Not contextual** — averaged static word vectors have no mechanism to resolve pronouns/coreference either, which is the *exact* failure mode this evaluator is meant to fix; dependency count is not actually lighter than fastembed once its own C-extension stack and `httpx`/`requests` (same as the other two) are counted |
| Suitability for Vigil | Good, but not the lightest available option for a backend service that only needs embeddings, not the full `transformers` ecosystem | **Best fit** — see "Model chosen" below | Rejected — see below |

**A dependency-discipline note carried over from the TF-IDF milestone's README, updated with new
evidence:** that milestone rejected `fastembed` specifically because it was not needed at all (TF-IDF
achieved zero-dependency, zero-network local computation) and `httpx`/`requests` were an avoidable
cost. That framing no longer applies once an actual pretrained tokenizer + model is a hard
requirement: **every realistic path checked above — `sentence-transformers`, `fastembed`, and even
`spaCy` — pulls in `httpx`/`requests` transitively via `huggingface-hub` or `weasel`**, because
downloading model weights from a hub is unavoidable for any of them. This was verified directly
(`pip install --dry-run`), not assumed. The dependency-discipline question this milestone actually
faces is therefore not "can `httpx` be avoided" (it cannot, for any transformer-quality option) but
"which runtime is lightest once that cost is accepted, and is it confined to first-time model
acquisition rather than leaking into every inference call" — see the next section for how that is
verified and enforced.

### Model chosen: `BAAI/bge-small-en-v1.5` via `fastembed`

- **No PyTorch.** ONNX Runtime is a mature, widely audited, CPU-optimized inference runtime;
  avoiding `torch` measurably reduces install size and process memory footprint, which matters for
  a backend evaluation service that may run many worker processes.
- **MIT-licensed model weights**, explicitly downloadable by any user directly from Hugging Face,
  no gated access, no acceptable-use restriction beyond MIT's own terms.
- **Quantized by default** (`model_optimized.onnx`, int8) — smaller on disk (~67 MB) and measurably
  faster on CPU than an unquantized fp32 forward pass, with the accuracy cost of int8 quantization
  widely reported as small for retrieval/similarity tasks (not independently re-verified by this
  project beyond the WikiQA numbers in `validation/reports/wikiqa_embedding.md` themselves, which
  are this evaluator's real accuracy evidence regardless of quantization).
- **Deterministic in practice, with the same honest caveat TF-IDF's README already states for any
  neural model:** this project's own tests
  (`tests/test_embedding_relevance.py::test_repeated_evaluation_of_the_same_input_is_bit_identical`
  and its cross-instance variant) confirmed bit-identical repeated output on this development
  platform. Floating-point non-associativity across different CPU architectures/BLAS
  backends/thread counts remains a known, real source of tiny cross-platform discrepancy that this
  project has not verified is absent on every possible deployment target — `threads` is exposed as
  a constructor parameter specifically so a caller can pin it to `1` if stronger determinism
  guarantees are ever needed.
- **`sentence-transformers/all-MiniLM-L6-v2` was not chosen** despite being the more mainstream
  library, because it requires PyTorch for no accuracy benefit this project has evidence for — the
  ONNX/`fastembed` path reaches a comparable-or-better model (BGE small generally reports
  competitive-to-better MTEB scores than MiniLM-L6-v2) with a measurably lighter runtime.
- **spaCy's static word vectors were rejected outright**, not merely deprioritized: they are not
  contextual, so they inherit TF-IDF's coreference blindness (the dominant failure pattern this
  evaluator exists to address) by construction, while offering no dependency-count advantage over
  `fastembed` once spaCy's own download-time `httpx`/`requests` pull-in and C-extension stack are
  counted.

### Two-phase network contract

Per this milestone's requirement to distinguish first-time model acquisition from offline
inference:

- **First-time model acquisition (network, one-time per cache location):** the first time a
  process constructs `EmbeddingRelevanceEvaluator()` and no cached weights exist at `cache_dir`
  (default: `~/.cache/vigil-evaluator/fastembed`, always outside this repository — overridable via
  the `VIGIL_EVALUATOR_EMBEDDING_CACHE_DIR` environment variable or the `cache_dir` constructor
  argument), `fastembed` downloads the ~67 MB quantized ONNX model and tokenizer from Hugging Face.
- **Offline inference (no network, every call thereafter):** `evaluate()` never touches the
  network. Enforced directly, not just asserted:
  `tests/test_embedding_relevance.py::test_evaluate_makes_no_network_calls` blocks socket
  connections at the Python `socket` module level around `evaluate()` (after model load has
  already completed) and asserts it still succeeds — the same technique
  `tests/test_relevance.py::test_relevance_evaluate_makes_no_network_calls` uses for the TF-IDF
  evaluator.
- **Model weights are never committed to git.** There is no relative, in-repo path anywhere this
  evaluator's code could write a weights file to — `DEFAULT_CACHE_DIR` is always resolved under the
  invoking user's home directory (or an operator-chosen absolute path via the environment
  variable), the same "cache lives outside the repo" discipline `scripts/download_wikiqa.py`
  already established for the WikiQA dataset cache.

### Input contract, score, and threshold semantics

Reuses `app.relevance.RelevanceEvaluatorInput` (`input_text`, `output_text`) rather than defining a
duplicate type — both evaluators answer the same relevance question over the same plain-text-pair
shape. `app/relevance.py` is only ever imported from here, never modified.

- **Algorithm:** independently embed `input_text` and `output_text` with `BAAI/bge-small-en-v1.5`,
  then cosine similarity between the two vectors. `fastembed`'s output is already L2-normalized, so
  this reduces to a dot product.
- **Score range:** cosine similarity's natural range is `[-1.0, 1.0]`, unlike TF-IDF's
  non-negative-by-construction `[0.0, 1.0]`. This evaluator rescales via `score = (cosine + 1) /
  2` — a fixed, monotonic, fully documented linear remap, not an arbitrary one — so its score
  occupies the same `[0.0, 1.0]` contract as the TF-IDF evaluator's (directly comparable in
  `validation/reports/wikiqa_comparison.md`, and directly compatible with
  `validation.metrics.sweep_thresholds`'s `[0.0, 1.0]` sweep range, reused unmodified). The remap
  is rank-preserving, so it does not change ROC-AUC or any evaluator's relative example ordering.
- **`evaluator_name` is `"relevance_embedding"`, distinct from TF-IDF's `"relevance"`** — this is
  what makes the two independently selectable per this milestone's requirement, and avoids any
  ambiguity in the `(project_id, trace_id, span_id, evaluator_name, evaluator_version)` idempotency
  key `docs/decisions/004-evaluation-engine.md` section 5 defines for evaluation jobs, should both
  ever run against the same span in a future production integration.
- **Threshold is a required constructor argument with an explicitly unvalidated default
  (`DEFAULT_THRESHOLD = 0.5`)** — same posture as TF-IDF's threshold: not checked against labeled
  data until the WikiQA validation harness runs; see `validation/reports/wikiqa_embedding.md` for
  the threshold that harness actually selects.
- **Empty/whitespace input or output:** identical `not_evaluable` handling to TF-IDF, same
  explanation convention.
- **Punctuation-only / stop-word-only text is evaluable here, unlike TF-IDF.** A subword-tokenized
  transformer can encode any non-empty string, including pure punctuation — there is no "empty
  vocabulary" failure mode for an embedding model the way there is for `TfidfVectorizer`. This is a
  deliberate, documented difference in evaluable-input surface between the two evaluators, not a
  bug in either (see `tests/test_embedding_relevance.py::test_punctuation_only_text_is_evaluable_unlike_the_tfidf_baseline`).
- **Malformed input:** identical validation and exception type to TF-IDF
  (`InvalidEvaluatorInputError`), since both reuse `RelevanceEvaluatorInput`.

### Installing the `embedding` extra

This evaluator's dependencies (`fastembed`, `numpy`) are an **optional extra**, not a base
dependency of this package — `app/relevance.py` never imports `app/embedding_relevance.py`, and
installing `vigil-evaluator` for the TF-IDF baseline alone does not pull in ONNX Runtime,
`huggingface-hub`, or any of their transitive dependencies:

```
uv sync --extra embedding
uv run pytest                              # embedding tests skip automatically if the extra
                                            # isn't installed (pytest.importorskip)
uv run python -m validation.wikiqa_embedding
```

## Known limitations

- **Lexical, not semantic.** See "Dependency and model choice" above — a correct paraphrase can score
  low; keyword-stuffed irrelevant text can score misleadingly high.
- **No benchmark validation.** The examples in this package's tests are illustrative, not a validation
  suite. No precision/recall/F1 has been measured against any labeled dataset.
- **Threshold is an unvalidated placeholder**, not a product decision.
- **No groundedness or faithfulness/hallucination detection.** Per ADR 004 section 2, this is a
  data-availability gap in Vigil's telemetry today (no reliable link between an LLM span and the
  retrieval context that fed it; retrieval span output isn't guaranteed to contain text at all; no
  redaction pipeline exists to safely involve a third-party model even if the linkage existed), not
  something this evaluator is missing by oversight. See ADR 004 section 9 for the specific SDK/schema
  prerequisite (`context_span_ids`) that would need to ship before either is attempted.
- **No LLM-as-judge, anywhere, by design.** Not a fallback, not an option — see ADR 004 section 1.

### Known limitations — embedding relevance evaluator specifically

- **Not the V1 production evaluator.** See `validation/reports/wikiqa_comparison.md` for whether
  and how that should change.
- **Answer-relevance only, same scope as TF-IDF.** Still does not check factual correctness, still
  does not use or require retrieval context — this evaluator changes *how* relevance is measured,
  not *what* question it answers. Everything in "Known limitations" above about groundedness,
  faithfulness, and LLM-as-judge being out of scope applies identically here.
- **Threshold is an unvalidated-until-the-harness-runs placeholder**, same posture as TF-IDF's.
- **Heavier runtime than TF-IDF.** Model load has real latency (dominated by the first `evaluate()`
  call's ONNX session initialization) and per-call inference is measurably slower than TF-IDF's
  closed-form arithmetic — see `validation/reports/wikiqa_comparison.md`'s "Inference cost" row for
  measured numbers, not an estimate.
- **English-oriented model.** `BAAI/bge-small-en-v1.5` is an English sentence-embedding model; this
  evaluator has not been validated (and is not expected to perform well) on non-English `input`/
  `output` pairs. TF-IDF has the same practical limitation via its English stop-word list, so this
  is not a regression, but it is not solved either.
- **Determinism verified on one platform.** See "Model chosen" above — bit-identical repeated
  output was confirmed on this development machine; cross-platform floating-point
  non-associativity for neural inference is a known class of risk this project has not independently
  ruled out on every possible deployment target.

## Running this package

```
cd services/evaluator
uv sync                      # TF-IDF baseline only
uv run pytest
uv run ruff check .

uv sync --extra embedding    # adds the experimental embedding evaluator
uv run pytest                # now also runs tests/test_embedding_relevance.py
```

No environment variables and no other Vigil service need be running for any of the above. Network
access is needed only for the embedding evaluator's first-ever model download per cache location
(see "Two-phase network contract" above) — the TF-IDF evaluator never needs network access at all,
at any point.
