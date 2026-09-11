"""Read-side ClickHouse queries for evaluation results -- Phase 3I,
docs/decisions/005-evaluation-job-storage-worker.md sections 4/5/10.

Mirrors app/clickhouse/query_repository.py's conventions exactly: every
method requires `project_id` and includes it in the WHERE clause (this
module has no way to discover a project_id itself; callers --
app/services/evaluations.py -- must always supply the value resolved from
app.api.deps.get_current_api_key, never one read from request data); native
ClickHouse parameter binding for every value; `FixedString` output columns
(`trace_id`, `span_id`) wrapped in `toString(...)` since clickhouse_connect
decodes `FixedString` as raw bytes, not str.

Deliberately a fresh module, never importing services/worker's
EvaluationResultsRepository (services/worker/worker/clickhouse/repository.py)
-- ADR 001 decision 6 ("duplicate rather than centralize"): apps/api and
services/worker are independently deployed services that must not share a
data-access layer, the same boundary already applied to `evaluation_jobs`
(apps/api's SQLAlchemy model vs. worker/postgres/repository.py's raw
psycopg) and to the sampling algorithm (ADR 005's Phase 3H amendment).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from clickhouse_connect.driver.client import Client

from app.clickhouse.query_common import execute_query

# Deliberately excludes `project_id` (never exposed to the client, implicit
# from auth, matching SpanOut's own precedent) and no column here needs a
# JOIN back to `spans` -- ADR 005 section 5: "Raw input/output is never
# duplicated into evaluation_results; a consumer needing both issues two
# independent, already-existing-pattern queries," applied here as "this
# repository reads evaluation_results only, never spans."
_RESULT_SELECT_COLUMNS = (
    "evaluation_id",
    "toString(trace_id) AS trace_id",
    "toString(span_id) AS span_id",
    "evaluator_name",
    "evaluator_version",
    "score",
    "label",
    "explanation",
    "evaluator_model",
    "evaluator_provider",
    "evaluation_latency_ms",
    "evaluation_cost_usd",
    "job_created_at",
    "written_at",
)
_RESULT_SELECT = ",\n                ".join(_RESULT_SELECT_COLUMNS)


class EvaluationsQueryRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get_span_evaluations(
        self, *, project_id: UUID, trace_id: str, span_id: str
    ) -> list[dict[str, Any]]:
        """Every evaluation_results row for one span -- typically 0-2 rows
        (one per evaluator that has run against this span), never an
        unbounded set: `evaluator_name`/`evaluator_version` pairs that have
        ever executed against one fixed (project_id, trace_id, span_id) are
        bounded by how many evaluators/versions this deployment has ever
        run, not by telemetry volume. Uses `FINAL` for the same reason
        app/clickhouse/query_repository.py's `get_span` does: this is a
        single-identity-range detail lookup (narrowed by project_id,
        trace_id, AND span_id, not a broad scan), so immediate
        deduplication is cheap and correct here where it would be
        prohibitively expensive over an unbounded range. A duplicate
        physical row for one evaluation_id can occur if a worker's
        ClickHouse insert succeeded but its confirmation was lost before
        the corresponding PostgreSQL succeeded transition -- see
        services/worker/worker/execution.py's ordering -- ReplacingMergeTree
        eventually merges these, but FINAL guarantees this read never
        surfaces the stale duplicate first.

        Ordered by (evaluator_name, evaluator_version) for a stable,
        deterministic response -- this endpoint returns the full set in one
        response, never paginated (see this repository's module docstring
        on why the result set is inherently small).
        """
        query = f"""
            SELECT
                {_RESULT_SELECT}
            FROM evaluation_results FINAL
            WHERE project_id = {{project_id:UUID}}
              AND trace_id = {{trace_id:String}}
              AND span_id = {{span_id:String}}
            ORDER BY evaluator_name, evaluator_version
        """
        parameters: dict[str, Any] = {
            "project_id": project_id,
            "trace_id": trace_id,
            "span_id": span_id,
        }
        return execute_query(self._client, query, parameters)
