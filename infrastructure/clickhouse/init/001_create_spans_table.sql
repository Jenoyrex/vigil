-- V1 spans table, per docs/decisions/003-clickhouse-telemetry-storage.md.
-- Executed automatically on first container start (mounted at
-- /docker-entrypoint-initdb.d/). The table name is fully qualified with the
-- `vigil` database because the official image's entrypoint runs mounted .sql
-- files via `clickhouse-client` without a `--database` flag (it only uses
-- CLICKHOUSE_DB to issue the initial `CREATE DATABASE`), so an unqualified
-- `CREATE TABLE spans` would silently land in the `default` database instead.
-- If CLICKHOUSE_DB is ever changed from `vigil`, this qualifier must change
-- with it (see infrastructure/clickhouse/README.md).
-- Do not add columns here without a follow-up ADR revising 003.

CREATE TABLE IF NOT EXISTS vigil.spans
(
    project_id            UUID,
    trace_id              FixedString(32),
    span_id                FixedString(16),
    parent_span_id        Nullable(FixedString(16)),

    name                  String,
    span_type             LowCardinality(String),
    resource              LowCardinality(String),

    start_time            DateTime64(3),
    end_time              DateTime64(3),
    duration_ms           UInt32 MATERIALIZED dateDiff('millisecond', start_time, end_time),

    status                Enum8('unset' = 0, 'ok' = 1, 'error' = 2) DEFAULT 'unset',
    status_message        Nullable(String),

    input                 Nullable(String),
    input_size_bytes      UInt32 DEFAULT 0,
    input_truncated       Bool DEFAULT false,

    output                Nullable(String),
    output_size_bytes     UInt32 DEFAULT 0,
    output_truncated      Bool DEFAULT false,

    attributes            Map(LowCardinality(String), String),
    attributes_truncated  Bool DEFAULT false,

    events Nested
    (
        time              DateTime64(3),
        name              LowCardinality(String),
        attributes        Map(LowCardinality(String), String)
    ),
    events_truncated      Bool DEFAULT false,

    llm_provider          LowCardinality(Nullable(String)),
    llm_model             LowCardinality(Nullable(String)),
    llm_input_tokens      Nullable(UInt32),
    llm_output_tokens     Nullable(UInt32),
    llm_total_tokens      Nullable(UInt32),
    llm_cost_usd          Nullable(Decimal64(6)),

    environment           LowCardinality(String),
    release               LowCardinality(Nullable(String)),

    ingested_at           DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toDate(start_time)
ORDER BY (project_id, toDate(start_time), trace_id, span_id)
TTL toDate(start_time) + INTERVAL 30 DAY DELETE;
