#!/usr/bin/env bash
# Deterministic verification for the local ClickHouse evaluation_results
# table, mirroring verify.sh's approach for `spans`.
#
# Confirms, against a running `clickhouse` container started via
# infrastructure/docker-compose.yml:
#   1. ClickHouse is reachable.
#   2. The `evaluation_results` table exists.
#   3. Its schema matches docs/decisions/005-evaluation-job-storage-worker.md
#      section 5.
#   4. A representative test result can be inserted.
#   5. The test result can be queried back.
#   6. A duplicate insertion (same evaluation_id -- the row's full identity,
#      per ADR 005 section 5/9) demonstrates ReplacingMergeTree's eventual-dedup
#      behavior: visible as two rows immediately, one row under FINAL, and one
#      physical row after a merge.
#
# Run from anywhere in the repo:
#   infrastructure/clickhouse/verify_evaluation_results.sh

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
# not a real evaluation job, and never collides with real data by convention.
# Reuses verify.sh's fixed test project/trace/span identity so the two
# scripts' test rows are obviously related to the same eye, without either
# one depending on the other having run first.
TEST_EVALUATION_ID="00000000-0000-4000-8000-0000000000e1"
TEST_PROJECT_ID="00000000-0000-4000-8000-000000000001"
TEST_TRACE_ID="4bf92f3577b34da6a3ce929d0e0e4736"
TEST_SPAN_ID="00f067aa0ba902b7"

step "1. ClickHouse is reachable"
ch --query "SELECT 1"

step "2. evaluation_results table exists"
ch --query "EXISTS TABLE evaluation_results"

step "3. Schema (compare by eye against ADR 005 section 5)"
ch --query "SHOW CREATE TABLE evaluation_results FORMAT TabSeparatedRaw"

step "4. Insert a representative test result"
ch --query "
INSERT INTO evaluation_results
(
    evaluation_id, project_id, trace_id, span_id,
    evaluator_name, evaluator_version,
    score, label, explanation,
    evaluator_model, evaluator_provider,
    evaluation_latency_ms, evaluation_cost_usd,
    job_created_at
)
VALUES
(
    '${TEST_EVALUATION_ID}', '${TEST_PROJECT_ID}', '${TEST_TRACE_ID}', '${TEST_SPAN_ID}',
    'relevance_embedding', '0.1.0',
    0.87, 'relevant', 'cosine similarity 0.8700; threshold=0.5000 -> label=''relevant''.',
    'BAAI/bge-small-en-v1.5', NULL,
    345.2, NULL,
    toDateTime64('2026-09-08 12:00:00.000', 3)
)
"

step "5. Query the test result back"
ch --query "
SELECT toString(evaluation_id) AS evaluation_id, project_id, toString(trace_id) AS trace_id,
       toString(span_id) AS span_id, evaluator_name, evaluator_version, score, label,
       evaluator_model, evaluator_provider, evaluation_latency_ms
FROM evaluation_results
WHERE project_id = '${TEST_PROJECT_ID}' AND evaluation_id = '${TEST_EVALUATION_ID}'
FORMAT PrettyCompact
"

step "6a. Insert a duplicate (same evaluation_id, later written_at) -- simulates a retried write"
ch --query "
INSERT INTO evaluation_results
(
    evaluation_id, project_id, trace_id, span_id,
    evaluator_name, evaluator_version,
    score, label, explanation,
    evaluator_model, evaluator_provider,
    evaluation_latency_ms, evaluation_cost_usd,
    job_created_at, written_at
)
VALUES
(
    '${TEST_EVALUATION_ID}', '${TEST_PROJECT_ID}', '${TEST_TRACE_ID}', '${TEST_SPAN_ID}',
    'relevance_embedding', '0.1.0',
    0.87, 'relevant', 'cosine similarity 0.8700; threshold=0.5000 -> label=''relevant''.',
    'BAAI/bge-small-en-v1.5', NULL,
    345.2, NULL,
    toDateTime64('2026-09-08 12:00:00.000', 3), now64(3) + INTERVAL 5 SECOND
)
"

step "6b. Without FINAL, both rows are visible immediately (eventual dedup, not yet merged)"
ch --query "
SELECT count() AS row_count
FROM evaluation_results
WHERE project_id = '${TEST_PROJECT_ID}' AND evaluation_id = '${TEST_EVALUATION_ID}'
"

step "6c. With FINAL, ReplacingMergeTree collapses to one row (immediate-correctness read path)"
ch --query "
SELECT count() AS row_count
FROM evaluation_results FINAL
WHERE project_id = '${TEST_PROJECT_ID}' AND evaluation_id = '${TEST_EVALUATION_ID}'
"

step "6d. Force a merge; the physical row count collapses to one without needing FINAL"
ch --query "OPTIMIZE TABLE evaluation_results FINAL"
ch --query "
SELECT count() AS row_count
FROM evaluation_results
WHERE project_id = '${TEST_PROJECT_ID}' AND evaluation_id = '${TEST_EVALUATION_ID}'
"

step "Done"
echo "All checks ran. Review the output above against docs/decisions/005-evaluation-job-storage-worker.md."
