# 4. Evaluation Engine V1 Architecture

- Status: Accepted
- Date: 2026-09-01

## Context

ADR 001 reserved `services/worker` and `services/evaluator` as separate, independently deployable
Python services but deferred their design entirely; both directories are still empty. ADR 002 and
ADR 003 settled the trace/span telemetry model and its ClickHouse storage, including two decisions
that turn out to bound what an evaluation engine can reliably do: no automatic PII/secret redaction
exists (ADR 002 §10, ADR 003 §11), and the only relationship between spans is the `parent_span_id`
tree (ADR 002 §2) — there is no field recording which retrieval span(s), if any, fed a given LLM
span's prompt.

Before writing an evaluator, a design review inspected the current repository end to end: the
`spans` ClickHouse table (ADR 003 §2), the ingestion API and service (`app/schemas/traces.py`,
`app/services/ingestion.py`, `app/api/v1/traces.py` — a fully synchronous FastAPI route with no
background-task or queue infrastructure anywhere in `apps/api`), the query API
(`app/schemas/query.py`), and the Python SDK (`vigil/client.py`, `vigil/span.py`, `vigil/types.py`,
`examples/python-sdk/basic.py`). That review found three facts that materially constrain V1 scope:
the SDK has no dedicated retrieval API — `Span` exposes only `set_attribute`, `set_input`,
`set_output`, `set_status`, `record_llm_usage`, so a `retrieval`-typed span's content lives wherever
a customer chose to put it; the shipped example stores bare document IDs
(`{"results": ["doc-1", "doc-42"]}`) in a retrieval span's `output`, not retrieved text; and there is
no link field anywhere connecting an LLM span to the retrieval span(s) whose results were actually
in its prompt — in the same example, the retrieval and LLM spans are siblings under a common
`agent` span, with no data field recording that relationship.

This ADR documents the approved V1 Evaluation Engine architecture: what evaluator ships, why two of
the three originally-considered evaluation concepts are explicitly excluded, the job/service/storage
architecture, the result schema, cost and safety controls, the privacy posture, the Phase 2
prerequisite for RAG-aware evaluation, and the validation-first implementation order. It does not
create code, install dependencies, create database migrations, modify the SDK or ingestion schema,
or modify the dashboard — all of that is separate, later work gated on this ADR and, per §10 below,
on an offline validation result.

## Decision

### 1. V1 scope

V1 ships exactly one evaluator: **Relevance** — does an `llm` span's `output` address its `input`.
It is computed with **embedding cosine similarity between the span's `input` and `output`, using a
local/self-hosted embedding model**. No third-party model API call is made by this evaluator, and
none is required for it to function.

**LLM-as-judge is not the default evaluation mechanism for anything in V1.** It is not implemented,
configured, or reachable behind a flag in this release. Any future LLM-judge mode is Phase 2+,
explicitly opt-in, and secondary to a deterministic/local primary signal — never the default path
for a new evaluator.

**Groundedness and faithfulness/hallucination are explicitly excluded from V1.** Not partially
implemented, not available disabled-by-default, not a stub — no code for either exists in this
release. §2 documents why.

### 2. Why groundedness and faithfulness are blocked from V1

These are data-availability problems, not algorithm problems, and no V1 implementation attempts to
work around them:

- **No reliable retrieval → LLM context linkage.** The only relationship between any two spans is
  `parent_span_id`. Nothing records which retrieval span(s) a given LLM span's prompt actually drew
  on. A tree-adjacency heuristic ("nearest retrieval-typed sibling") would be wrong often enough
  (parallel retrieval branches, multi-hop agents, reranked/filtered results) to be scientifically
  indefensible as the basis for a faithfulness score.
- **Retrieval span output is not guaranteed to contain text.** The SDK's `set_output` accepts any
  JSON-serializable value; the shipped example stores document IDs only. Without retrieved text, a
  groundedness evaluator has no evidence to check a claim against, regardless of algorithm.
- **No dedicated retrieval schema.** `span_type` is an open, unenforced string (ADR 002 §5); there is
  no structured "retrieved chunk" shape (id / text / score) anywhere in the SDK or the ingestion
  schema for a groundedness evaluator to depend on.
- **No redaction pipeline.** ADR 002 §10 and ADR 003 §11 both record this as an accepted, unresolved
  gap. Any evaluator design that would call a third-party model with raw customer `input`/`output`
  compounds that gap with a new, unreviewed exposure. V1 avoids this entirely by using only a local
  embedding model; a future groundedness/faithfulness evaluator that might want a third-party
  cross-check inherits this same blocker.
- **Non-RAG hallucination has no ground truth in the observed telemetry.** For an LLM span with no
  linked retrieval context, Vigil has only the one already-produced output. There is nothing in the
  telemetry to check a claim against, and Vigil cannot re-sample the model to do self-consistency
  checking without making its own additional LLM calls — a materially different technique, with
  different cost and semantics, than observing telemetry that already exists.

Consequently, this ADR does **not** attempt to distinguish "unsupported by retrieved context" from
"irrelevant" from "factually wrong" in V1. Relevance can identify "irrelevant" today. The other two
distinctions require the Phase 2 prerequisite in §9, and even then, "factually wrong" in the
real-world sense is not something telemetry-only observability can ever fully verify — at best, a
future NLI-based evaluator can detect "contradicted by the specific retrieved context," which is a
proxy for factual correctness, not proof of it.

### 3. Evaluation architecture

```
telemetry (spans in ClickHouse)
  -> evaluation selection   (services/worker poller: scans ClickHouse for newly-ingested spans
                              eligible per each project's evaluator_configs — enabled evaluators,
                              sampling rate — since the last checkpoint)
  -> evaluation job          (a row created in PostgreSQL evaluation_jobs, status=pending, via
                              apps/api's job-creation endpoint — see §4)
  -> worker                  (services/worker claims pending jobs directly against PostgreSQL,
                              enforces concurrency/timeout/rate limits, dispatches to an evaluator)
  -> evaluator                (services/evaluator computes score/label/explanation; no queue,
                              database, or HTTP awareness of its own)
  -> evaluation result       (services/worker writes the result directly to ClickHouse
                              evaluation_results and updates the PostgreSQL job to succeeded)
  -> ClickHouse               (evaluation_results: high-volume, append-only, analytical)
  -> Query API                (apps/api exposes evaluation results read-side, project-scoped
                              exactly like every existing trace/analytics endpoint)
  -> Dashboard                 (surfaces score/label/explanation on span detail and an aggregate
                              view, reusing the existing BFF/data-layer pattern — not built in V1,
                              see §12)
```

The ingestion path (`POST /v1/traces`) is untouched end to end. The worker's poller is the only new
consumer of `spans`, and it only reads from ClickHouse — it never writes to `spans`, and nothing in
this pipeline is reachable from, or adds latency to, the ingestion request.

### 4. Service boundaries

- **`services/worker`** owns the job lifecycle: the poller (decides *when* new telemetry becomes
  eligible jobs), the claim loop (`SELECT ... FOR UPDATE SKIP LOCKED` against PostgreSQL, connected
  to directly — not through apps/api — since a hot claim loop cannot afford an HTTP round trip per
  job), retry/backoff/dead-letter bookkeeping, concurrency/timeout/rate-limit enforcement, dispatch
  to `services/evaluator`, and writing the final result directly to ClickHouse (connected to
  directly, mirroring how `apps/api` itself writes `spans`). No evaluation algorithm lives here.
- **`services/evaluator`** owns evaluation algorithms only: given a span's relevant fields (and, once
  it exists, linked context) plus a resolved config, return a score/label/explanation/latency/cost.
  No queue, database, or HTTP awareness of its own — it is a plain library, independently
  unit-testable with in-memory inputs. Per ADR 001 decision 5, it is reserved as an independently
  deployable service; V1 deploys it as a library imported by `services/worker`'s single process,
  since nothing yet demands a second deployable, while keeping the code boundary clean for a future
  split (e.g. if an evaluator becomes GPU-bound and needs independent autoscaling).
- **`apps/api`** owns the PostgreSQL schema (source of truth via its existing Alembic setup) for
  `evaluator_configs` and `evaluation_jobs`, the configuration CRUD API, the job-creation endpoint
  that `services/worker`'s poller calls to enqueue a newly-eligible span, and the read API for
  evaluation results that the dashboard will eventually consume — mirroring exactly how the existing
  trace/analytics query layer is project-scoped from the authenticated API key, never from a
  client-supplied `project_id`. `apps/api` does not run the poller and does not claim or execute
  jobs.
- **PostgreSQL** owns `evaluator_configs` and `evaluation_jobs`: low-volume, mutable,
  relational-integrity-critical state needing locking and transactional status transitions — the
  same category of data ADR 002 §8 already assigns to PostgreSQL (`projects`, `api_keys`), applied to
  evaluation.
- **ClickHouse** owns `evaluation_results`: high-volume, append-only, analytical, joined against
  `spans` on `(project_id, trace_id, span_id)` for the dashboard — the same category ADR 002 §8
  already assigns to ClickHouse (`spans`, `traces`), applied to evaluation. A result row is written
  once, only after a job succeeds; a failed or dead-lettered job produces no ClickHouse row.

Per ADR 001 decision 6 (no shared core package; duplicate rather than centralize), `services/worker`
does not import `apps/api`'s SQLAlchemy models. It calls `apps/api`'s job-creation endpoint to
enqueue work, and it connects to PostgreSQL and ClickHouse directly (with its own minimal,
independently-implemented data access) for claiming, status updates, and writing results — the same
duplication-over-coupling posture ADR 001 already established for the API/worker/evaluator boundary.

### 5. Job lifecycle

A `evaluation_jobs` row moves through: **pending** (created by apps/api's job-creation endpoint,
called by the worker's poller) → **running** (claimed by a worker via `SKIP LOCKED`) → **succeeded**
(evaluator returned a result, written to ClickHouse, job row updated) or **failed** (evaluator raised
or timed out on this attempt).

- **Retry behavior**: a `failed` attempt is retried with exponential backoff and jitter, up to a
  configured `max_retries`, incrementing `attempt_count` and setting `next_attempt_at` each time.
- **Dead-letter behavior**: once `max_retries` is exhausted, the job moves to **dead_letter** —
  preserved (with the last, bounded, non-payload-containing error message) for operator inspection,
  excluded from dashboard aggregates, never silently deleted.
- **Idempotency**: the logical identity of a job is `(project_id, trace_id, span_id, evaluator_name,
  evaluator_version)`, enforced as a unique constraint — the same idempotency-key philosophy ADR 002
  §7 established for spans, applied to evaluation jobs. Re-enqueuing the same evaluator version
  against the same span is a no-op against an existing row, not a duplicate.

### 6. Cost and safety controls

- **Opt-in, off by default.** No evaluator runs for a project until that project's
  `evaluator_configs` row explicitly enables it. This is the default specifically because of the
  privacy posture in §8, not merely a UX preference.
- **Sampling.** `evaluator_configs` carries a per-project, per-evaluator sampling rate — not every
  eligible span is evaluated by default; this is the primary cost-control lever as evaluators beyond
  V1's zero-cost relevance evaluator are added.
- **Bounded concurrency.** `services/worker` enforces a configurable maximum number of concurrent
  evaluations, sized to respect both local resource limits and any future third-party provider's
  rate limits.
- **Payload limits.** Evaluators must apply the same truncation discipline the ingestion path already
  uses (ADR 003 §10's 64 KiB per-field caps) to what they read from `input`/`output` — no evaluator
  processes an unbounded payload.
- **Evaluator timeout.** Every evaluator call has a hard, configurable per-call timeout; a stuck call
  fails that attempt (subject to retry, §5) rather than holding a worker slot indefinitely.
- **Retry limits.** Bounded by `max_retries` (§5) — retries are not unbounded, and a permanently
  failing job reaches `dead_letter` rather than retrying forever.

### 7. Evaluation result schema

| Field | Purpose |
|---|---|
| `evaluation_id` | Unique identifier for this result row. |
| `project_id` | Tenant scope, always server-derived — never client-supplied, matching every other read/write path in this system. |
| `trace_id` | Matches `spans.trace_id` exactly, for joining a result back to its trace. |
| `span_id` | Matches `spans.span_id` exactly, for joining a result back to its span. |
| `evaluator_name` | Which evaluator produced this result (e.g. `relevance`). |
| `evaluator_version` | The evaluator's version at the time of this result, so historical results stay interpretable after the evaluator's algorithm, model, or thresholds change later. |
| `score` | Continuous, evaluator-defined range (documented per evaluator — e.g. relevance's cosine-similarity-derived score). |
| `label` | Evaluator-defined categorical outcome (e.g. `relevant` / `partially_relevant` / `irrelevant`), for the cases a single number is less legible than a bucket. |
| `explanation` | Bounded-length, human-readable reason for the score/label. Never a raw payload dump — see §8. |
| `evaluator_model` / `evaluator_provider` | Which model computed this result, and which provider hosts it. Populated even for local models (attribution, not just third-party disclosure); `evaluator_provider` is null for local models and set only when an external API was actually called. |
| `evaluation_latency_ms` | How long this evaluation took — needed to reason about worker throughput and timeout tuning. |
| `evaluation_cost_usd` | Marginal cost of this evaluation. Null (not zero) for a local, zero-marginal-cost evaluator like V1's relevance evaluator — the same "null means not applicable, zero means a real zero" distinction ADR 003 §2 already uses for `llm_cost_usd`. |
| `created_at` | When this result was produced. |

This ADR documents the field list and its justification, not the ClickHouse column types or a
migration — following the same split ADR 002 (concept) and ADR 003 (concrete schema) already used
for `spans`, the concrete `evaluation_results` DDL is deferred to the implementation step in §10,
once the relevance evaluator has been validated.

### 8. Privacy and security

- **V1's evaluator makes no third-party model calls.** Relevance is computed with a local embedding
  model. No customer `input` or `output` leaves Vigil's own infrastructure as part of evaluation.
- **Customer prompt/output stays local.** This is a direct consequence of the above, stated
  explicitly because it is the load-bearing justification for defaulting evaluation to opt-in (§6)
  rather than a stronger, unimplemented redaction guarantee.
- **API keys never enter evaluator payloads.** An evaluator receives only the span fields it needs
  (`input`, `output`, and later, linked context) — never request headers, credentials, or anything
  from the authentication path.
- **Redaction remains an explicit future requirement, not solved here.** ADR 002 §10 and ADR 003 §11
  already record no redaction as an accepted V1 gap for storage; this ADR does not invent one for
  evaluation either. It is called out specifically as a **blocking prerequisite for any future
  evaluator that would call a third-party model** on real customer data — V1's local-only relevance
  evaluator has no such dependency and is unaffected, but any Phase 2+ evaluator that wants a
  third-party cross-check inherits this blocker until a redaction pipeline exists and gets its own
  ADR.

### 9. Phase 2 prerequisite: `context_span_ids`

Reliable RAG groundedness/faithfulness evaluation requires an explicit, customer-set link from an
LLM span to the retrieval span(s) whose results were actually included in its prompt — proposed as a
`context_span_ids` field (span IDs of the retrieval spans used), set by the instrumented application
at the point it builds the prompt, since only that application actually knows which retrieved chunks
it used. This is necessary, not merely convenient, because:

- The only structural relationship spans have today is `parent_span_id`, which encodes *execution
  nesting*, not *data flow into a prompt* — a retrieval span and the LLM span it informs are commonly
  siblings, not parent/child, and a trace can contain multiple retrieval spans (parallel branches,
  multi-hop agents, reranking) where a tree-adjacency heuristic cannot reliably pick the right one(s).
- Without an explicit link, any groundedness/faithfulness evaluator would be validated against a
  *guess* at which context was used, not the actual context — undermining the "scientifically
  defensible" goal this whole engine is being built around.

`context_span_ids` alone is not sufficient by itself — Phase 2 additionally requires the retrieval
span's `output` to documentedly contain retrievable text (not bare IDs, per §2), which is an SDK/
documentation change, not just a schema field. Both are out of scope for this ADR: implementing
either requires modifying the SDK and/or ingestion schema, which this ADR explicitly does not do
(see §12).

### 10. Validation strategy and implementation order

**Validation must happen before production integration.** This revises the implementation order
from the design proposal that preceded this ADR, which had built job/storage/worker infrastructure
before validating the evaluator. The approved order is:

```
ADR
  -> evaluator interface        (services/evaluator's plain function/library shape: span fields in,
                                  score/label/explanation/latency/cost out — no queue, database, or
                                  HTTP dependency)
  -> relevance evaluator         (the embedding-similarity implementation, against the interface
                                  above, callable with in-memory inputs only)
  -> offline validation harness  (runs the relevance evaluator against held-out labeled data,
                                  entirely outside any production path)
  -> benchmark/failure analysis  (precision/recall/F1 — and ROC-AUC if the evaluator's score is used
                                  as a continuous threshold, per §11's honesty about available data —
                                  published, with failure cases examined, not just an aggregate
                                  number)
  -> production job/storage/worker integration   (only after the above; §3-§7)
  -> API
  -> dashboard
  -> end-to-end verification
```

This order exists so that an evaluator's validity is established before any infrastructure is built
around it — if the relevance evaluator's approach turns out to be inadequate against benchmark data,
that is discovered at the cheapest possible point (a library function and an offline script), not
after `evaluation_jobs`, a worker, a ClickHouse table, an API, and a dashboard already depend on it.
"Scientifically defensible" means the evaluator earns its place in the production pipeline by
demonstrated performance, not the reverse.

### 11. Dataset strategy

Distinguishing what has actually been verified from what has not, per this review's own instruction
not to invent dataset structure: this ADR's earlier design discussion described HaluEval and
RAGTruth from their published papers/documentation and general knowledge of their structure — **the
literal dataset files have not been downloaded or inspected by this project**, and no dataset
compatibility has been confirmed against real data.

**What is asserted (from published descriptions, not verified against the files):**
- RAGTruth is human-annotated at word-span granularity within a response, with hallucination-type
  labels, built on retrieved-passage-grounded generation.
- HaluEval's QA subset provides `(question, knowledge, right_answer, hallucinated_answer)`-shaped
  examples, with hallucinated answers generated and filtered via an LLM-based process rather than
  organically human-authored.

**What remains to be verified during implementation**, before either dataset is relied on for any
published precision/recall/F1/ROC-AUC number:
- The actual column names, file format, license terms, and download mechanism for each dataset.
- Whether the described shapes match closely enough to be used directly, or need adaptation, once the
  files are actually inspected.
- Whether label quality/coverage in the real files supports the specific metrics this ADR's V1
  evaluator would be validated against (relevance has no equivalent canonical benchmark at all — see
  below — so this concern applies to the Phase 2 groundedness/faithfulness evaluators, not V1).
- Any licensing or redistribution constraints relevant to using either dataset in an open-source
  project's CI-style validation harness.

For V1's relevance evaluator specifically: **no canonical, widely-cited labeled benchmark for
"answer relevance" as a standalone metric was identified**, and this ADR does not claim one. The
offline validation harness in §10 will need to either adapt an existing QA-pair dataset (treating
gold-answer presence as a relevance proxy) or use a small internally hand-labeled sample — the
specific choice is implementation work for the validation-harness step, not settled here.

### 12. V1 / Phase 2 / Phase 3 boundaries — what is explicitly not being built

**V1 builds:** the relevance evaluator (local embeddings only); the evaluator interface; the offline
validation harness and its published benchmark/failure analysis; `evaluator_configs` and
`evaluation_jobs` (PostgreSQL); `evaluation_results` (ClickHouse); `services/worker`'s poller, claim
loop, retry/backoff/dead-letter, concurrency/timeout/rate-limit enforcement; `apps/api`'s
configuration, job-creation, and read endpoints; opt-in/off-by-default wiring; sampling and payload
limits.

**V1 explicitly does not build:**
- Groundedness or faithfulness/hallucination evaluators, in any form, behind any flag (§1, §2).
- LLM-as-judge as a default or available evaluation mode (§1).
- Any change to the Python or TypeScript SDK, or to the ingestion schema — including
  `context_span_ids` (§9) and any retrieval-content-shape requirement. Both are Phase 2 prerequisites,
  not V1 work.
- Any redaction pipeline (§8) — remains an explicit, tracked future requirement.
- Dashboard changes of any kind — the pipeline in §3 ends at the Query API in V1; dashboard
  integration is a later, separate step per §10's implementation order.
- Redis, or any queue technology beyond PostgreSQL's `SKIP LOCKED` — nothing in V1's scope
  demonstrates a need for it (consistent with ADR 001 decision 7).
- Non-RAG hallucination detection in any form — no ground truth exists in observed telemetry for it
  (§2); not attempted at any phase boundary defined by this ADR.

**Phase 2** (contingent on §9's SDK/schema prerequisite shipping first, itself contingent on its own
follow-up ADR): groundedness and faithfulness evaluators (claim extraction → evidence matching →
NLI entailment, validated offline against RAGTruth and HaluEval once §11's open verification items
are resolved); an optional, explicitly opt-in LLM-judge secondary cross-check, gated on the
redaction/data-handling review §8 requires; revisiting Redis if real throughput numbers justify it.

**Phase 3** (speculative, not committed): non-RAG hallucination detection, if a viable approach is
ever found despite §2's ground-truth gap; a redaction pipeline (its own ADR, cross-cutting, not
evaluator-specific); evaluator models fine-tuned/calibrated on Vigil's own accumulated,
human-reviewed evaluation feedback, if a feedback loop is ever built; per-plan retention tiers
interacting with evaluation data.

## Reasoning

Each decision's reasoning is inlined under it above. At a high level: this ADR ships the one
evaluator the current telemetry can actually support reliably, refuses to build the other two on top
of heuristics the review found to be unreliable, and inverts the original implementation order so
that the evaluator's validity is proven against public labeled data before any production
infrastructure is built to depend on it — directly serving the "scientifically defensible" goal this
milestone was framed around.

## Tradeoffs

- Shipping only relevance in V1 means groundedness and faithfulness — arguably the more
  differentiated, higher-value evaluation concepts for an LLM observability product — are not
  available until Phase 2, and Phase 2 itself is gated on an SDK/schema change with its own ADR and
  rollout, not a short timeline.
- Local-only embedding-based relevance avoids third-party exposure and cost, but is a weaker signal
  than an LLM-judge would be for nuanced cases (multi-part questions, topically-similar non-answers) —
  an accepted tradeoff in exchange for zero cost, zero third-party exposure, and full determinism.
- Requiring offline validation before any production integration (§10) delays the first
  end-to-end-visible result (e.g. in the dashboard) relative to the original proposal's order, in
  exchange for never building job/storage/worker infrastructure around an evaluator that hasn't been
  shown to work.
- `services/worker` connecting to PostgreSQL and ClickHouse directly (rather than exclusively through
  `apps/api`) means two independently deployed services touch the same datastores directly, which
  ADR 001 already accepts as the cost of not sharing a core package (decision 6) — the same tradeoff
  ADR 001 already named, applied here.
- No canonical relevance benchmark existing (§11) means V1's validation will rely on an adapted or
  internally-labeled dataset rather than a widely-cited academic benchmark, which is a weaker
  external-validity claim than Phase 2's evaluators will be able to make against RAGTruth/HaluEval.

## Consequences

- `services/evaluator`'s first implementation must be the relevance evaluator, built and validated
  per §10's order, before any of §3-§7's production infrastructure is implemented.
- `apps/api` must gain a job-creation and configuration API surface and PostgreSQL migrations for
  `evaluator_configs`/`evaluation_jobs`; `services/worker` must gain the poller/claim/dispatch loop —
  both deferred until after the validation step in §10 produces a passing result.
- The concrete `evaluation_results` ClickHouse DDL is deferred to implementation time, following the
  same ADR-002-then-ADR-003 split already used for `spans`.
- No SDK, ingestion schema, or dashboard change is authorized by this ADR. `context_span_ids` (§9)
  and any redaction pipeline (§8) each require their own follow-up ADR before implementation.
- Groundedness, faithfulness, LLM-as-judge-as-default, and non-RAG hallucination detection remain
  explicitly out of scope until the conditions in §9, §8, and §2 (respectively) are met, and should
  each be revisited via a new ADR rather than added incrementally to this one.
