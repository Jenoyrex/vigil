# 5. Evaluation Job/Storage/Worker Integration

- Status: Accepted
- Date: 2026-09-03

## Context

ADR 004 approved the V1 evaluation engine's architecture and, per its section 10, required an
evaluator to be validated offline before any production infrastructure is built around it. That
validation is now complete: `services/evaluator` ships two independently selectable relevance
evaluators (`relevance`, TF-IDF, and `relevance_embedding`, `BAAI/bge-small-en-v1.5` via
`fastembed`/ONNX Runtime), both conforming to the same `Evaluator` protocol, both benchmarked
against WikiQA with a published, honest comparison
(`services/evaluator/validation/reports/wikiqa_comparison.md`). `services/worker` remains an empty
directory — this ADR is the design for filling it in, plus the PostgreSQL/ClickHouse schema and the
`apps/api` surface it depends on.

Before writing this ADR, a full architecture review inspected the current repository end to end:
ADR 001-004; the entire `apps/api` implementation (`app/db/models/*`, `app/db/base.py`,
`app/db/session.py`, `app/security/api_keys.py`, `app/api/deps.py`, `app/api/v1/traces.py`,
`app/clickhouse/{client,repository,query_common,query_repository}.py`,
`app/services/{ingestion,query}.py`, `app/config.py`, `app/main.py`, the initial Alembic migration);
`infrastructure/docker-compose.yml`, `infrastructure/clickhouse/init/001_create_spans_table.sql`,
`infrastructure/clickhouse/verify.sh`; `apps/api/tests/conftest.py`'s test-database and
fake-repository patterns; and `services/evaluator`'s interface, both evaluators, and their
validation reports. That review found: no Redis service exists anywhere in this repository's
infrastructure; no CI exists yet; no `traces` materialized view has actually been built despite
being described in ADR 002/003 (list-traces still does a live `GROUP BY` over `spans`) — an accepted
precedent for deferring an ADR-described piece until something actually depends on it; every
existing write and read path resolves `project_id` exclusively from `AuthenticatedKey` (never a
request body); and no `context_span_ids` or retrieval-linkage field exists anywhere in the SDK,
confirming ADR 004 section 9's prerequisite is still unbuilt and this milestone correctly stays
scoped to the `relevance` question only.

This ADR resolves three decisions an earlier review draft of this milestone left open — the
worker-to-API authentication mechanism, whether job creation goes through the API or a direct
PostgreSQL insert, and what V1's operational default values should be and how they relate to the
WikiQA validation numbers — and, having resolved them, documents the complete design. It does not
create code, migrations, or ClickHouse DDL; those are separate, later work gated on this ADR, per
the same validation/design-before-implementation discipline ADR 004 section 10 established.

## Decision

### 1. Job model

An evaluation job is one row in PostgreSQL `evaluation_jobs`, representing exactly one instruction:
"run evaluator `evaluator_name`/`evaluator_version` against span `(project_id, trace_id,
span_id)`." Fields: `id` (uuid, pk), `project_id` (uuid, fk → `projects`, `RESTRICT`), `trace_id`
(`char(32)`, OTel hex format — not a PostgreSQL `uuid`, matching ADR 002 section 4's ID format),
`span_id` (`char(16)`), `evaluator_name` (string — open, not a DB enum, matching `span_type`'s
precedent in ADR 002 section 5, since a new evaluator must never require a migration to become
runnable), `evaluator_version` (string), `status` (string, `CHECK IN ('pending', 'running',
'succeeded', 'failed', 'dead_letter')`, mirroring `api_keys.status`'s existing `CheckConstraint`
pattern), `attempt_count` (int, default 0), `max_retries` (int — snapshotted from
`evaluator_configs` at job-creation time, so a later config edit never retroactively changes an
in-flight job's retry policy), `next_attempt_at` (timestamptz, nullable), `claimed_at` (timestamptz,
nullable), `claimed_by` (string, nullable — a worker hostname/pid for operator debugging, not a
foreign key to any workers table), `last_error` (string, bounded length, nullable — never a payload
dump, per ADR 004 section 5), `created_at`/`updated_at` (the existing `TimestampMixin`).

**Idempotency**: a unique constraint on `(project_id, trace_id, span_id, evaluator_name,
evaluator_version)` — this tuple, per ADR 004 section 5, *is* the job's logical identity.
Re-enqueuing the same evaluator version against the same span is `INSERT ... ON CONFLICT (...) DO
NOTHING`, a no-op against the existing row.

**Lifecycle**: `pending` → `running` (claimed) → `succeeded` (evaluator returned a result, written
to ClickHouse, row updated) | `failed` (this attempt raised, timed out, or was reaped from a stuck
`running` state — see section 8). A `failed` row with `attempt_count < max_retries` and
`next_attempt_at <= now()` is re-claimable exactly like a `pending` row (the claim query's `WHERE
status IN ('pending', 'failed')` treats them identically); once `attempt_count >= max_retries`, the
next failure transitions directly to `dead_letter` instead of back to `failed`, and a
`dead_letter` row is never re-claimed. This is a 5-state model, not 6: `failed` is not a dead end,
it is "waiting to retry."

**Where mutable job state belongs**: PostgreSQL exclusively, matching ADR 004 section 4's
storage-boundary assignment — ClickHouse only ever receives one immutable, append-only write per
*successful* evaluation attempt; a failed or dead-lettered attempt produces no ClickHouse row.

### 2. Evaluator configuration

`evaluator_configs`, one row per `(project_id, evaluator_name)` (unique constraint), owned by
`apps/api`: `id`, `project_id` (fk → `projects`, `RESTRICT`), `evaluator_name` (open string, same
rationale as above), `enabled` (bool, default `false` — opt-in, per ADR 004 section 6),
`sampling_rate` (float, `CHECK (sampling_rate BETWEEN 0 AND 1)`, default **0.1** — see section 12),
`threshold` (float, nullable — `NULL` means "use this evaluator's own code-level default," never a
number this ADR invents), `max_retries` (int, default 3), `created_at`/`updated_at`.

`evaluator_version` is deliberately **not** a column on `evaluator_configs`. It is supplied by the
worker at job-creation time, read directly from whichever `Evaluator` implementation the worker has
installed (`RelevanceEvaluator.version` / `EmbeddingRelevanceEvaluator.version`), because only the
worker's process actually imports `services/evaluator` — `apps/api` must not gain that dependency
just to read a version string; doing so would pull `scikit-learn`/`fastembed`/ONNX Runtime into a
service that has no other reason to need them, contradicting `services/evaluator`'s decoupling from
API/database internals. The API records whatever `evaluator_version` the authenticated worker
asserts, the same trust posture `EvaluationResult.evaluator_model`/`evaluator_provider` already use
for "which exact algorithm produced this" attribution — not a security-relevant field, so trusting
the (already-authenticated, see section 9) worker's assertion of it does not weaken tenant
isolation.

**How the validated `relevance_embedding` evaluator fits in**: it is simply one more selectable
`evaluator_name` a project can independently enable via its own `evaluator_configs` row — nothing
about this ADR's mechanism special-cases it over `relevance` (TF-IDF). Per section 12, it is the
evaluator this ADR *recommends* operators actually enable first in production, but the job/worker/
API design treats every `evaluator_name` uniformly.

### 3. Input selection

The worker's poller scans ClickHouse `spans WHERE span_type = 'llm' AND ingested_at >
:checkpoint`, ordered by `ingested_at`. `span_type = 'llm'` is a hard filter: `relevance` answers
"does an LLM span's output address its input" (ADR 004 section 1) and has no defined meaning for a
non-LLM span type. No query language is introduced — eligibility is exactly one ClickHouse `WHERE`
clause plus, per section 6, a small fixed local registry the worker already has in Python.

**Avoiding repeated evaluation of the same span**: the unique constraint in section 1 is the primary
mechanism — safe under concurrent/overlapping pollers, safe under a full poller restart-and-rescan.
The checkpoint (section 7) exists purely to bound how much already-processed history a healthy
poller re-scans on every tick, not for correctness. Enabling a *new* `evaluator_version` (e.g.
bumping `RelevanceEvaluator` to `0.2.0`) intentionally produces new, distinct jobs for
already-evaluated spans — re-evaluating existing telemetry under a new algorithm version is the
desired behavior when an evaluator changes, not a bug. A deliberate historical backfill mechanism
(re-running an old evaluator version's spans under a new version on demand) is explicitly out of
scope for V1.

**`project_id` isolation**: every span row's `project_id` column was itself only ever set
server-side by the ingestion path from an `AuthenticatedKey` (ADR 002/003) — never client-supplied,
never a worker invention. The poller reads it verbatim off the scanned row and asserts it, verbatim,
in the job-creation request (section 9 explains why this is safe and how the API independently
verifies it rather than merely trusting the assertion).

### 4. PostgreSQL — minimum required tables

Exactly three new tables, all in `apps/api`'s existing Alembic setup, using the existing
`Base`/`TimestampMixin`/naming-convention (`app/db/base.py`) unchanged:

1. **`evaluator_configs`** — section 2.
2. **`evaluation_jobs`** — section 1. Indexes: the idempotency unique index (section 1); `(status,
   next_attempt_at)` for the claim query; `(project_id, created_at)` for the job-status list API.
3. **`evaluation_poller_checkpoint`** — a single-row watermark (`id`, `last_ingested_at`,
   `updated_at`). Justified as required for correctness, not "might be useful later": without a
   durable checkpoint, a worker restart must either silently skip any span ingested during its
   downtime (spans would simply never be considered for evaluation) or re-scan the full 30-day
   retention window on every restart. Worker-owned lifecycle state — see section 9's boundary.

**Explicitly not added**: an evaluators catalog table (`evaluator_name` is an open string, matching
`span_type`'s precedent — a catalog table would force a migration on every new evaluator, exactly
what ADR 002 section 5 rejected for span types); a per-attempt audit-history table (`attempt_count` +
`last_error` on the job row is sufficient for V1; full per-attempt history is a speculative future
feature); a workers registry table (`claimed_by` is a plain debugging string, not a referential-
integrity target).

### 5. ClickHouse — `evaluation_results` schema

```sql
CREATE TABLE evaluation_results
(
    evaluation_id         UUID,
    project_id            UUID,
    trace_id              FixedString(32),
    span_id                FixedString(16),

    evaluator_name        LowCardinality(String),
    evaluator_version     LowCardinality(String),

    score                 Nullable(Float64),
    label                 LowCardinality(String),
    explanation           String,

    evaluator_model       LowCardinality(Nullable(String)),
    evaluator_provider    LowCardinality(Nullable(String)),

    evaluation_latency_ms Float64,
    evaluation_cost_usd   Nullable(Decimal64(6)),

    job_created_at        DateTime64(3),
    written_at            DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(written_at)
PARTITION BY toDate(job_created_at)
ORDER BY (project_id, toDate(job_created_at), evaluator_name, evaluator_version, trace_id, span_id)
TTL toDate(job_created_at) + INTERVAL 30 DAY DELETE;
```

This is ADR 004 section 7's field list, given concrete ClickHouse types using the exact idioms ADR
003 already established for `spans` (`LowCardinality` for closed/repetitive strings, `Decimal64(6)`
for money, tenant-first `ORDER BY`).

**Why `job_created_at`, not the moment of writing, drives partitioning/ordering**: this is the one
point in this ADR that required deriving a new consequence from an existing principle rather than
copying a precedent directly. If partitioning instead used the moment a result is actually written,
a retried job (attempted again after `next_attempt_at`, possibly hours later) would write its
result row into a *different* daily partition than a first, failed attempt's — and ClickHouse's
`ReplacingMergeTree` never merges across partitions, so two rows for what is logically the same
evaluation could survive as permanent duplicates, not just eventually-consistent ones. `spans`
avoids this because a retried span insert always carries the same `start_time`. This schema applies
the identical fix: `job_created_at` (the job's own `created_at`, immutable across retries) drives
partitioning/ordering, exactly mirroring how `spans` partitions on `start_time` (immutable) rather
than `ingested_at` (receipt time); `written_at` is purely the `ReplacingMergeTree` version
tie-breaker, exactly mirroring `spans.ingested_at`'s role.

**Relation to `spans`**: joined only at the application layer on `(project_id, trace_id, span_id)`,
never a ClickHouse-level `JOIN` — the same boundary ADR 002 section 8 already established between
PostgreSQL and ClickHouse, applied here between two ClickHouse tables that happen to share a key.
Raw `input`/`output` is never duplicated into `evaluation_results`; a consumer needing both issues
two independent, already-existing-pattern queries.

### 6. Worker responsibilities

`services/worker` owns, and is the only thing that owns, the job lifecycle after creation:

- **Poller**: section 3's scan; for each scanned span, iterates the worker's own fixed local
  registry of installed `Evaluator` implementations (a Python-level fact — which evaluator classes
  this worker process has imported — not a database fact) and calls the job-creation endpoint once
  per `(span, evaluator_name)` pair it is capable of running. See section 9 for why eligibility
  (enabled? sampled in?) is *not* decided here.
- **Claim loop**: direct `psycopg` access to PostgreSQL (no ORM, no dependency on `apps/api`'s
  SQLAlchemy models, per ADR 001 decision 6's "duplicate rather than centralize") —
  `UPDATE evaluation_jobs SET status='running', claimed_at=now(), claimed_by=:worker_id,
  attempt_count=attempt_count+1 WHERE id IN (SELECT id FROM evaluation_jobs WHERE status IN
  ('pending','failed') AND (next_attempt_at IS NULL OR next_attempt_at <= now()) ORDER BY
  created_at FOR UPDATE SKIP LOCKED LIMIT :batch_size) RETURNING *` — exactly the mechanism ADR 004
  section 4 already specified, direct because "a hot claim loop cannot afford an HTTP round trip per
  job."
- **Dispatch**: each `Evaluator` implementation is constructed **once per worker process**, not once
  per job (`EmbeddingRelevanceEvaluator`'s constructor loads an ONNX session — expensive to repeat).
  Per claimed job, the worker fetches the span's `input`/`output` from ClickHouse with `FINAL`
  (single-span point lookup, the same immediate-correctness case `get_span` already handles),
  adapts it into `RelevanceEvaluatorInput` — this adapter lives in the worker, never in
  `services/evaluator`, per `app/relevance.py`'s own docstring ("converting a raw span's JSON
  input/output into evaluatable text is an adapter concern for whoever calls this evaluator... not
  this evaluator's job") — and calls `evaluate()` under a hard per-call timeout (ADR 004 section 6).
- **Concurrency**: a bounded thread pool per worker process, size from worker config (section 12).
  ONNX Runtime sessions are safe for concurrent inference from multiple threads, so this does not
  require per-thread evaluator instances.
- **Persistence**: a batched ClickHouse insert (mirrors `SpansRepository.insert_spans` — never one
  `INSERT` per result), then a PostgreSQL status update to `succeeded`, in that order. Each claimed
  job in a batch is evaluated and persisted independently — one job's exception must never affect
  its batch-mates (own `try`/`except` per job, not one around the whole batch).
- **Decoupling from API/database internals**: the worker depends on `services/evaluator` as an
  ordinary pinned dependency (including its `embedding` extra) and calls only the plain
  `Evaluator.evaluate(input) -> EvaluationResult` contract. `services/evaluator` imports nothing
  from the worker, the API, or either database — this ADR does not touch that boundary, only builds
  a caller on the correct side of it.

### 7. Poller/checkpoint design

`evaluation_poller_checkpoint` holds one row, `last_ingested_at`. Each poll tick: read the
checkpoint, scan `spans` with `ingested_at > last_ingested_at` (bounded batch size), attempt
job-creation dispatch for every scanned span, and advance the checkpoint to the batch's maximum
`ingested_at` **only if every span in the batch was either successfully dispatched or confirmed
ineligible** — a partial-batch failure (e.g. the API was briefly unreachable partway through) leaves
the checkpoint where it was, so the next tick retries the same batch, relying on job-creation's own
idempotency (section 1) to make re-dispatching already-succeeded spans a safe no-op.

### 8. Job claiming, retry/backoff/dead-letter, stuck-job reaper

Claiming is section 6's `SKIP LOCKED` query — safe under any number of concurrent worker processes
by construction; two workers can never claim the same row. On an evaluator exception or timeout:
compute `backoff = base_seconds * 2^attempt_count + jitter`, set `next_attempt_at = now() +
backoff`; if `attempt_count < max_retries`, `status = 'failed'` (re-claimable, section 1); if
`attempt_count >= max_retries`, `status = 'dead_letter'` (terminal, `last_error` preserved, bounded,
never deleted, excluded from any future aggregate view, per ADR 004 section 5 verbatim).

**Stuck-job reaper**: a job can be claimed (`running`) and then never resolved if its worker process
crashes mid-evaluation. A periodic query — run by any live worker, or the poller's own loop —
resets `evaluation_jobs SET status = 'failed', attempt_count = attempt_count + 1 WHERE status =
'running' AND claimed_at < now() - :stuck_threshold`, feeding the same backoff/dead-letter logic
above. `stuck_threshold` must be meaningfully larger than the per-call evaluator timeout (section 6)
so the reaper only catches genuine process death, never a call that is merely slow but alive.

### 9. Worker → API authentication (Decision 1)

**Mechanism**: a single shared-secret internal service token, structurally and procedurally
separate from customer API keys at every level — not merely a different value, a different code
path:

- Presented in a **dedicated header, `X-Vigil-Internal-Token`**, never `Authorization: Bearer` —
  deliberately not the same scheme customer keys use, so the two cannot be confused by a client, a
  proxy, or a future contributor skimming route code.
- Compared with `hmac.compare_digest` (constant-time) against `VIGIL_API_INTERNAL_SERVICE_TOKEN`, a
  new `apps/api` setting following the exact existing `Settings`/env-var pattern
  (`app/config.py`) — **the value itself is never in source code**, has no default baked into
  Python, and must be supplied via environment/`.env` in every environment, local development
  included (matching how ClickHouse/PostgreSQL credentials are already handled).
- This token authenticates **worker identity only** — "this caller is the trusted internal
  worker process" — and carries **zero project scope**. It is checked by a new dependency
  (`app/api/deps.py`, sibling to `get_current_api_key`, not a modification of it) that never touches
  the `api_keys` table at all. **The worker must never hold, read, or present a customer's `vgl_*`
  key** — this is enforced by construction: nothing in the worker's code path has any reason to
  ever look up an `APIKey` row, and this ADR does not give it one.

**How `project_id` is supplied safely, and how impersonation of an arbitrary customer request is
prevented**: the job-creation request body is the one legitimate exception to "project_id never in
a request body" — `{project_id, trace_id, span_id, evaluator_name, evaluator_version}`, all
supplied by the worker. This is safe, not merely convenient, because of two independent facts,
both required:

1. The internal token proves the caller is the trusted worker process, not an arbitrary customer —
   a customer's `vgl_*` key cannot satisfy this check (different header, different secret,
   different comparison, no `api_keys` lookup at all).
2. **The API does not trust the asserted `project_id` on its own.** Before creating a job row, the
   service layer performs a server-side ClickHouse lookup of `(trace_id, span_id)` and confirms the
   *actual* `project_id` column on that stored span matches the request's asserted `project_id`
   exactly. If they don't match, or the span doesn't exist at all, the request is rejected (`404`)
   and no job is created. This is the concrete mechanism that prevents impersonation: possessing a
   valid internal token is proof of *being the worker*, not license to assert any `project_id` —
   the assertion is only ever accepted after being checked against the same ground truth (the
   `spans` table) the worker itself read it from in the first place. A worker bug that scrambled
   `project_id` on the way to this call would produce a `404`, not a cross-tenant write.

**Why not OAuth/JWT**: there is exactly one internal caller identity in V1 (the worker fleet, as a
single class), calling exactly one endpoint, requiring no claims, scopes, or third-party
verification beyond "is this the worker." A shared secret with constant-time comparison is
proportionate to that; a token-issuance/claims/rotation framework would solve a problem — multiple
distinct internal caller identities needing independently revocable, differently-scoped credentials
— that does not exist yet. Rotating the shared secret means updating the environment variable and
restarting `apps/api` and `services/worker`; this ADR does not build database-backed revocation,
and names the trigger for revisiting that as "more than one internal caller identity needs
independent revocation," not a V1 requirement.

**Operational defense-in-depth, beyond the token**: this endpoint should not be reachable on
whatever public ingress exposes `POST /v1/traces` to customers — it is deployment-internal traffic
only. This ADR records that intent; the concrete network topology is deployment configuration, not
an application-layer decision, and is not specified further here.

### 10. API vs. direct database responsibilities — job creation path (Decision 2)

**Job creation always goes through `apps/api`, never a direct PostgreSQL insert from the worker.**
The pipeline is exactly:

```
worker poller
  → POST /v1/evaluations/jobs   (authenticated via X-Vigil-Internal-Token, section 9)
  → apps/api service layer      (business logic — see below)
  → PostgreSQL evaluation_jobs
```

The API's service layer, on receiving a job-creation request, is where the actual *business*
decisions live and are checked exactly once, in one place: is `evaluator_name` enabled for
`project_id` (`evaluator_configs` lookup)? does this specific span pass the configured
`sampling_rate` (a deterministic hash of `span_id`, evaluated here — not in the worker — so the
sampling decision is identical regardless of which worker process's poller happens to observe a
given span, and never needs to be duplicated into worker code)? does the asserted `project_id`
actually match ground truth (section 9)? Only if all three hold does the row get created (or, if it
already exists, the existing row is returned unchanged — idempotent).

This is a deliberate refinement over this milestone's earlier architecture-review draft, which had
tentatively described the worker's poller itself reading `evaluator_configs` from PostgreSQL. That
would duplicate business logic — "is this evaluator enabled and sampled-in" — into two places
(worker and API) for no benefit, and would make the sampling decision depend on which worker
process's local config cache happened to be current. Centralizing it behind the API, which already
canonically owns `evaluator_configs` per ADR 004 section 4, removes that duplication entirely: the
worker's poller needs to know only its own installed `Evaluator` classes (a static, code-level
fact), never a database-driven notion of "what's enabled."

**What the worker *does* access directly**, per this ADR's explicit boundary: `evaluation_jobs`
claim/status-transition writes (section 6, 8) and `evaluation_poller_checkpoint` reads/writes
(section 7) — both are **worker-owned execution/lifecycle state**: mechanical facts about a job or a
scan's progress that the worker itself produces and consumes, not business decisions about whether
work should exist in the first place. `evaluation_results` writes to ClickHouse are likewise direct
(ADR 004 section 4 already established this). **The distinguishing line is not "which database" —
it is "does this write represent a business decision (`evaluator_configs`-gated: should a job for
this span/evaluator even exist) or execution mechanics (a job that already exists is being
claimed/retried/completed)."** The former always goes through the API; the latter never does.

### 11. Redis/queue exclusion — re-affirmed

Re-checked against the actual current architecture, not merely re-asserted: `infrastructure/
docker-compose.yml` defines exactly `postgres` and `clickhouse`, no Redis service exists anywhere in
this repository's infrastructure today; the ingestion path remains fully synchronous with no
background-task infrastructure of any kind; PostgreSQL `SKIP LOCKED` is a proven pattern at
throughput levels far above what an opt-in, sampled (10% default, section 12), single-evaluator-at-
a-time V1 will see. Nothing this review found — including the two decisions resolved in sections 9
and 10 — introduces a pub/sub, sub-millisecond-latency, or fan-out requirement PostgreSQL does not
already satisfy. **Redis, Celery, Kafka, and RabbitMQ remain explicitly out of V1.** A future,
rate-limited, opt-in third-party evaluator (itself excluded from V1 by ADR 004 section 1) is the
only plausible future trigger for revisiting this, and is not a basis for action now.

### 12. Explicit V1 defaults (Decision 3)

**WikiQA's selected thresholds are not production defaults, and this ADR does not treat them as
such.** `validation/reports/wikiqa_baseline.md` selected `0.17` for TF-IDF; `wikiqa_embedding.md`
selected `0.89` for the embedding evaluator — both explicitly, in their own text, scoped to
"WikiQA's score distribution and label balance," not claimed as the right operating point for
Vigil's production traffic, which has a materially different distribution (generated LLM
completions, not extractive Wikipedia sentence selection). Neither number appears anywhere in this
schema as a default.

- **`evaluator_configs.threshold` defaults to `NULL`.** `NULL` means "fall back to this evaluator's
  own code-level `DEFAULT_THRESHOLD`" — `0.5` for both `relevance` and `relevance_embedding` today,
  each already explicitly documented in `services/evaluator` as an **unvalidated operational
  placeholder**, not a product decision (`app/relevance.py`'s and `app/embedding_relevance.py`'s own
  docstrings say so verbatim). This ADR changes nothing about that documented status and does not
  promote any WikiQA-selected number into it. A project may override `threshold` per evaluator via
  `evaluator_configs`, at any time, without a migration or deployment.
- **`evaluator_configs.sampling_rate` defaults to `0.1` (10%), not `1.0`.** This is a deliberately
  conservative choice, not an oversight: `validation/reports/wikiqa_comparison.md` measured the
  embedding evaluator at roughly **100x** TF-IDF's per-call latency (~345ms vs. ~3.35ms in that
  measurement). A project operator who enables an evaluator (already opt-in, per ADR 004 section 6)
  should not silently get full-volume evaluation of every eligible LLM span on their first attempt —
  a low default sampling rate bounds worst-case worker load and cost surprise, and forces an
  operator to make a deliberate, informed choice to raise it once they've observed V1's actual
  behavior on their own traffic. `sampling_rate` remains fully configurable per project per
  evaluator, with no floor or ceiling beyond `[0, 1]`.
- **Worker operational defaults** (`services/worker`'s own `Settings`, same `pydantic-settings`/
  env-var pattern as `apps/api/app/config.py`): `max_concurrent_evaluations` defaults small (e.g.
  `4`) and `evaluator_call_timeout_seconds` defaults conservative (e.g. `10`) — both changeable via
  environment variable with no code change, per ADR 004 section 6's "configurable per-call timeout"
  and "bounded concurrency" requirements.
- **Recommended first production evaluator**: `relevance_embedding` (`BAAI/bge-small-en-v1.5`) is
  the evaluator this ADR recommends operators actually enable first, given
  `wikiqa_comparison.md`'s outcome-B finding (a real, substantial improvement over the TF-IDF
  baseline at roughly 100x the per-call cost). This is a rollout recommendation, not a mechanism
  distinction — `relevance` (TF-IDF) remains independently selectable and fully supported by the
  identical job/worker/API design. **No LLM-as-judge, groundedness, or faithfulness evaluator is
  introduced by this ADR** — per ADR 004 sections 1 and 2, none exist in this codebase, and this ADR
  does not change that.
- **Ease of change**: every default above lives in exactly one of two places — a `DEFAULT` column
  value in a straightforward, revisable migration, or a `pydantic-settings` environment variable —
  both are the codebase's existing, established configuration mechanisms; this ADR invents neither
  a new default-management system nor a new place operators must know to look.

### 13. Security / project isolation summary

Two independent trust boundaries, not one: (a) the existing customer-facing boundary
(`Authorization: Bearer <vgl_*>` → `AuthenticatedKey.project_id`, unchanged, reused as-is for every
config/job-status/result read endpoint a project's own credentials may call); (b) the new
internal-only boundary (`X-Vigil-Internal-Token` → worker identity, section 9), used solely by
`POST /v1/evaluations/jobs`, which never accepts a customer's `vgl_*` key and independently
re-verifies the one piece of tenant-scoping data it accepts from that caller (`project_id`) against
ClickHouse ground truth before ever writing a PostgreSQL row. No third boundary is introduced.

### 14. Failure modes

| Failure | Handling |
|---|---|
| ClickHouse unavailable (poller scan) | Log, retry next tick; checkpoint (section 7) does not advance; no data loss |
| ClickHouse unavailable (span fetch / result write) | Treated as an attempt failure → normal retry/backoff (section 8) |
| PostgreSQL unavailable | Claim loop and job-creation calls back off and retry; checkpoint only advances past confirmed-dispatched batches |
| Evaluator/model unavailable at startup | Worker fails fast: every configured `Evaluator` is constructed eagerly before the process accepts claims, not lazily on first job |
| Malformed telemetry (unadaptable `input`/`output`) | Adapter produces empty text → the evaluator's existing, already-designed `not_evaluable` outcome — a successful evaluation, not a job failure |
| Duplicate job submission | Unique constraint + `ON CONFLICT DO NOTHING` (section 1); safe under concurrent/overlapping pollers |
| Worker crash mid-claim | No effect — uncommitted transaction |
| Worker crash mid-evaluation | Job stuck `running` → stuck-job reaper (section 8) |
| Partial batch failure | Each claimed job processed/persisted independently (section 6) |
| Retry exhaustion | → `dead_letter`, `last_error` preserved bounded, never deleted, queryable |

### 15. Migration/deployment order

1. **PostgreSQL** — Alembic migration for `evaluator_configs`, `evaluation_jobs`,
   `evaluation_poller_checkpoint` in `apps/api`. Independently deployable: unused new tables have no
   effect on any running system.
2. **ClickHouse** — `evaluation_results` DDL (new `infrastructure/clickhouse/init/
   002_create_evaluation_results_table.sql`, mirroring `001_create_spans_table.sql`'s style).
   Independently deployable, same reasoning.
3. **`services/worker` core** (claim loop, dispatch, ClickHouse persistence, retry/backoff/
   dead-letter, reaper) — buildable and testable against manually-inserted job rows before its
   poller half exists, since the poller's only dependency is step 4.
4. **`apps/api`** (config CRUD, job-creation endpoint + internal auth, job-status/result-read
   endpoints) — independently testable via the same fake-repository pattern
   `apps/api/tests/conftest.py` already establishes; developable in parallel with step 3, not
   strictly after it.
5. **Wire poller → job-creation endpoint** — the one genuine cross-service integration point; only
   meaningful once steps 3 and 4 both exist.
6. **Integration tests** — real PostgreSQL + real ClickHouse, mirroring
   `test_traces_clickhouse_integration.py`'s existing pattern.
7. **E2E** — ingest a real span, trigger the poller, confirm a job is created/claimed/completed and
   its result is queryable.

## Reasoning

Each decision's reasoning is inlined under it above. At a high level: this ADR resolves the three
open questions by consistently applying one principle — a decision either changes *whether work
should exist* (business logic: evaluator enabled? sampled in? does the asserted tenant match
reality?) or *how already-agreed-to-exist work executes* (lifecycle mechanics: claim, retry,
persist) — and routes the former through `apps/api` and the latter through direct database access,
exactly matching ADR 004 section 4's original service-boundary intent rather than reinterpreting it.
The same principle resolves the authentication question (a shared secret proves *which kind of
caller this is*; ground-truth re-verification, not trust, is what actually enforces tenant
isolation) and the defaults question (nothing this codebase has not actually validated for
production traffic — WikiQA's selected thresholds included — is promoted into a default value).

## Tradeoffs

- A shared internal-service secret is a weaker credential model than customer API keys (no
  per-caller hashing, no revocation list, no last-used tracking) — accepted because V1 has exactly
  one internal caller identity; revisiting this is explicitly named as contingent on that no longer
  being true.
- Job creation always paying an HTTP round trip (worker → API) rather than a direct insert adds
  latency to the *creation* path — accepted because creation is low-frequency (one call per
  eligible span, not a hot claim loop) and keeping business logic in exactly one place is worth that
  cost; the hot claim loop itself remains direct, per ADR 004 section 4.
- A conservative `sampling_rate` default (0.1) means a project that enables an evaluator without
  further configuration sees only 10% coverage, not full visibility — an explicit, documented
  tradeoff of safety-by-default over out-of-the-box completeness; raising it is a one-field config
  change.
- `evaluation_results` partitioning by `job_created_at` rather than `written_at` ties this table's
  retention window to job-creation time, not to when a (possibly retried, possibly delayed) result
  was actually produced — an accepted consequence of prioritizing correct deduplication over
  partition-time precision, matching `spans`' own established precedent exactly.
- The stuck-job reaper's `stuck_threshold` is a single global value in V1, not per-evaluator — a
  slow evaluator (the embedding evaluator, ~100x TF-IDF's latency) and a fast one share the same
  reaper window; this is acceptable while V1 has only two evaluators with a bounded, already-measured
  latency gap, and is named as a candidate refinement if a future evaluator's latency profile makes
  a single global threshold too coarse.

## Consequences

- `apps/api` must gain: three new SQLAlchemy models + one Alembic migration; `evaluator_configs`
  CRUD endpoints; the internal-auth-protected job-creation endpoint; job-status and
  evaluation-results read endpoints; a new `VIGIL_API_INTERNAL_SERVICE_TOKEN` setting; a new
  `get_internal_service_auth` dependency (additive, not a modification of `get_current_api_key`).
- `services/worker` must be built from scratch: `pyproject.toml` (depending on `services/evaluator`
  including its `embedding` extra, `psycopg`, `clickhouse-connect`), the poller, the claim loop, the
  dispatch/adapter layer, retry/backoff/dead-letter logic, the stuck-job reaper, and its own
  `Settings` for operational defaults (section 12).
- `infrastructure/clickhouse/init/` gains `002_create_evaluation_results_table.sql`;
  `infrastructure/clickhouse/verify.sh` should gain an equivalent verification section for the new
  table, mirroring its existing `spans` checks.
- No SDK, ingestion schema, or dashboard change is authorized by this ADR. `context_span_ids` (ADR
  004 section 9) and any redaction pipeline (ADR 004 section 8) remain their own future ADRs.
  Groundedness, faithfulness, and LLM-as-judge remain out of scope, unchanged from ADR 004 sections
  1-2.
- The concrete PostgreSQL model code, Alembic migration, ClickHouse DDL, and `services/worker`
  implementation are separate, later work, gated on this ADR — per the same order this ADR's own
  section 15 specifies.

## Amendment (Phase 3 planning): per-project threshold resolution

Section 12 already requires `evaluator_configs.threshold` to be configurable "at any time, without a
migration or deployment," but this ADR did not originally specify *how* a resolved, per-project
threshold reaches an evaluator at evaluation time — section 6 separately requires each evaluator to
be constructed once per worker process (never once per job, since `EmbeddingRelevanceEvaluator`'s
ONNX session load is expensive) and reused across every job it processes, regardless of which
project that job belongs to. Phase 3 implementation planning surfaced the resulting gap directly: a
single long-lived evaluator instance cannot, on its own, apply two different projects' two different
configured thresholds if `threshold` is fixed at construction time — the two requirements as
originally written were in tension, not merely under-specified.

**Resolution**: `services/evaluator`'s `Evaluator.evaluate()` contract (`app/interface.py`) gained an
optional, keyword-only `threshold: float | None = None` parameter, alongside its existing
constructor-time `threshold`. `services/worker` resolves the effective threshold for a claimed job —
reading that project's `evaluator_configs.threshold` (`NULL` resolves to `None`) — and passes it into
that one call: `evaluator.evaluate(evaluator_input, threshold=resolved_threshold)`. `threshold=None`
(every call site before this parameter existed, and any call that still omits it) preserves the
evaluator's own constructor-configured default exactly, unchanged. A per-call override affects only
that one invocation and never mutates the instance (`self._threshold` is never written to), so
concurrent or subsequent calls against the same shared instance — from the same project or a
different one — are unaffected by any other call's override.

This keeps every allocation this ADR already committed to unchanged: **evaluator instances remain
keyed by `(evaluator_name, evaluator_version)` and constructed exactly once at worker startup
(section 6)**; **the model/ONNX session is loaded exactly once per instance**; and **the evaluator,
not `services/worker`, continues to own threshold-to-label semantics** (`services/worker` never
duplicates the `score >= threshold` comparison or any evaluator's labeling logic — it only resolves
and forwards a number). Resolving `evaluator_configs.threshold` is worker-owned execution mechanics
(a parameter needed to run a job already known to exist), not the job-creation eligibility gate
section 10 reserves for `apps/api` (`enabled`/`sampling_rate`/ground-truth `project_id` verification
remain exactly as section 9/10 already specify, untouched by this amendment) — it does not reopen or
narrow that boundary.

No WikiQA-derived number is introduced or promoted as a result of this amendment — `DEFAULT_THRESHOLD
= 0.5` in both evaluators remains the same unvalidated placeholder section 12 already named, now
reachable as the per-call fallback (`threshold=None`) as well as the constructor-time default.
