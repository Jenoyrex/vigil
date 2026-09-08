-- V1 evaluation_results table, per docs/decisions/005-evaluation-job-storage-worker.md
-- ("ADR 005") section 5. Executed automatically on first container start
-- (mounted at /docker-entrypoint-initdb.d/), immediately after
-- 001_create_spans_table.sql runs (the official image executes mounted
-- init scripts in lexicographic filename order). See that file's header
-- comment for why the table name must stay fully qualified with the
-- `vigil` database -- the same reasoning applies unchanged here.
-- Do not add columns here without a follow-up ADR revising 005.

CREATE TABLE IF NOT EXISTS vigil.evaluation_results
(
    evaluation_id         UUID,
    project_id            UUID,
    trace_id              FixedString(32),
    span_id               FixedString(16),

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
