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

## Running this package

```
cd services/evaluator
uv sync
uv run pytest
uv run ruff check .
```

No environment variables, no network access, and no other Vigil service need be running for any of
the above.
