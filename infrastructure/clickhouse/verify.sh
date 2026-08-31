#!/usr/bin/env bash
# Deterministic verification for the local ClickHouse telemetry store.
#
# Confirms, against a running `clickhouse` container started via
# infrastructure/docker-compose.yml:
#   1. ClickHouse is reachable.
#   2. The `spans` table exists.
#   3. Its schema matches docs/decisions/003-clickhouse-telemetry-storage.md.
#   4. A representative test span can be inserted.
#   5. The test span can be queried back.
#   6. A duplicate insertion (same project_id/trace_id/span_id) demonstrates
#      ReplacingMergeTree's eventual-dedup behavior: visible as two rows
#      immediately, one row under FINAL, and one physical row after a merge.
#
# Run from anywhere in the repo:
#   infrastructure/clickhouse/verify.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CLICKHOUSE_USER="${CLICKHOUSE_USER:-vigil}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-vigil}"
CLICKHOUSE_DB="${CLICKHOUSE_DB:-vigil}"

compose() {
    docker compose --project-directory "$INFRA_DIR" -f "$INFRA_DIR/docker-compose.yml" "$@"
}

ch() {
    compose exec -T clickhouse clickhouse-client \
        --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
        --database "$CLICKHOUSE_DB" --multiquery "$@"
}

step() { printf '\n=== %s ===\n' "$1"; }

# Fixed test identity so re-running this script is safe and recognizable;
# not a real trace, and never collides with real ingested data by convention.
TEST_PROJECT_ID="00000000-0000-4000-8000-000000000001"
TEST_TRACE_ID="4bf92f3577b34da6a3ce929d0e0e4736"
TEST_SPAN_ID="00f067aa0ba902b7"

step "1. ClickHouse is reachable"
ch --query "SELECT 1"

step "2. spans table exists"
ch --query "EXISTS TABLE spans"

step "3. Schema (compare by eye against ADR 003)"
ch --query "SHOW CREATE TABLE spans FORMAT TabSeparatedRaw"

step "4. Insert a representative test span"
ch --query "
INSERT INTO spans
(
    project_id, trace_id, span_id, parent_span_id,
    name, span_type, resource,
    start_time, end_time,
    status, status_message,
    input, input_size_bytes, input_truncated,
    output, output_size_bytes, output_truncated,
    attributes, attributes_truncated,
    events.time, events.name, events.attributes, events_truncated,
    llm_provider, llm_model, llm_input_tokens, llm_output_tokens, llm_total_tokens, llm_cost_usd,
    environment, release,
    ingested_at
)
VALUES
(
    '${TEST_PROJECT_ID}', '${TEST_TRACE_ID}', '${TEST_SPAN_ID}', NULL,
    'openai.chat.completion', 'llm', 'vigil-example-service',
    toDateTime64('2026-08-31 12:00:00.000', 3), toDateTime64('2026-08-31 12:00:01.250', 3),
    'ok', NULL,
    '{\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}', 49, false,
    '{\"role\":\"assistant\",\"content\":\"Hi there!\"}', 42, false,
    map('llm.request.temperature', '0.7'), false,
    [toDateTime64('2026-08-31 12:00:00.500', 3)], ['first_token'], [map()], false,
    'openai', 'gpt-4o-mini', 12, 8, 20, toDecimal64('0.000123', 6),
    'development', 'v0.1.0',
    now64(3)
)
"

step "5. Query the test span back"
ch --query "
SELECT project_id, trace_id, span_id, name, span_type, status,
       llm_provider, llm_model, llm_total_tokens, llm_cost_usd, duration_ms
FROM spans
WHERE project_id = '${TEST_PROJECT_ID}' AND trace_id = '${TEST_TRACE_ID}' AND span_id = '${TEST_SPAN_ID}'
FORMAT PrettyCompact
"

step "6a. Insert a duplicate (same identity, later ingested_at) — simulates a retried request"
ch --query "
INSERT INTO spans
(
    project_id, trace_id, span_id, parent_span_id,
    name, span_type, resource,
    start_time, end_time,
    status, status_message,
    input, input_size_bytes, input_truncated,
    output, output_size_bytes, output_truncated,
    attributes, attributes_truncated,
    events.time, events.name, events.attributes, events_truncated,
    llm_provider, llm_model, llm_input_tokens, llm_output_tokens, llm_total_tokens, llm_cost_usd,
    environment, release,
    ingested_at
)
VALUES
(
    '${TEST_PROJECT_ID}', '${TEST_TRACE_ID}', '${TEST_SPAN_ID}', NULL,
    'openai.chat.completion', 'llm', 'vigil-example-service',
    toDateTime64('2026-08-31 12:00:00.000', 3), toDateTime64('2026-08-31 12:00:01.250', 3),
    'ok', NULL,
    '{\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}', 49, false,
    '{\"role\":\"assistant\",\"content\":\"Hi there!\"}', 42, false,
    map('llm.request.temperature', '0.7'), false,
    [toDateTime64('2026-08-31 12:00:00.500', 3)], ['first_token'], [map()], false,
    'openai', 'gpt-4o-mini', 12, 8, 20, toDecimal64('0.000123', 6),
    'development', 'v0.1.0',
    now64(3) + INTERVAL 5 SECOND
)
"

step "6b. Without FINAL, both rows are visible immediately (eventual dedup, not yet merged)"
ch --query "
SELECT count() AS row_count
FROM spans
WHERE project_id = '${TEST_PROJECT_ID}' AND trace_id = '${TEST_TRACE_ID}' AND span_id = '${TEST_SPAN_ID}'
"

step "6c. With FINAL, ReplacingMergeTree collapses to one row (immediate-correctness read path)"
ch --query "
SELECT count() AS row_count
FROM spans FINAL
WHERE project_id = '${TEST_PROJECT_ID}' AND trace_id = '${TEST_TRACE_ID}' AND span_id = '${TEST_SPAN_ID}'
"

step "6d. Force a merge; the physical row count collapses to one without needing FINAL"
ch --query "OPTIMIZE TABLE spans FINAL"
ch --query "
SELECT count() AS row_count
FROM spans
WHERE project_id = '${TEST_PROJECT_ID}' AND trace_id = '${TEST_TRACE_ID}' AND span_id = '${TEST_SPAN_ID}'
"

step "Done"
echo "All checks ran. Review the output above against docs/decisions/003-clickhouse-telemetry-storage.md."
