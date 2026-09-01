"""Read-side ClickHouse queries for the Trace Explorer: list traces, fetch
one trace's spans + summary, fetch one span.

Every method requires `project_id` and includes it in the query's WHERE
clause -- this module has no way to discover a project_id itself; callers
(app/services/query.py) must always supply the value resolved from
app.api.deps.get_current_api_key, never one read from request data. This
mirrors app/clickhouse/repository.py's SpansRepository for the write path.

`trace_id`/`span_id`/`parent_span_id` are `FixedString` columns in `spans`;
clickhouse_connect decodes `FixedString` output as raw `bytes`, not `str`
(verified against 1.7.2), so every SELECT that returns one of these columns
wraps it in `toString(...)` to get a plain string back. This only affects
SELECT *output* columns -- comparing a FixedString column against a
`{name:String}` bound parameter in WHERE/HAVING works correctly without
casting, since ClickHouse compares the two types directly.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from clickhouse_connect.driver.client import Client

from app.clickhouse.query_common import execute_query

# Span columns for trace-detail/span-detail responses. Deliberately excludes
# `project_id` (never exposed to the client) and `ingested_at` (an internal
# receipt timestamp, not client-facing). Keep in sync with
# app/schemas/query.py's `SpanOut`.
_SPAN_DETAIL_COLUMNS = (
    "toString(span_id) AS span_id",
    "toString(parent_span_id) AS parent_span_id",
    "name",
    "span_type",
    "resource",
    "start_time",
    "end_time",
    "duration_ms",
    "status",
    "status_message",
    "input",
    "input_size_bytes",
    "input_truncated",
    "output",
    "output_size_bytes",
    "output_truncated",
    "attributes",
    "attributes_truncated",
    "events.time",
    "events.name",
    "events.attributes",
    "events_truncated",
    "llm_provider",
    "llm_model",
    "llm_input_tokens",
    "llm_output_tokens",
    "llm_total_tokens",
    "llm_cost_usd",
    "environment",
    "release",
)
_SPAN_DETAIL_SELECT = ",\n                ".join(_SPAN_DETAIL_COLUMNS)


class TracesQueryRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def list_traces(
        self,
        *,
        project_id: UUID,
        start_time_from: datetime,
        start_time_to: datetime,
        environment: str | None,
        resource: str | None,
        has_error: bool | None,
        limit: int,
        cursor: tuple[datetime, str] | None,
    ) -> list[dict[str, Any]]:
        """Traces (grouped from `spans`) for one project in a time window,
        ordered `start_time DESC, trace_id DESC` (trace_id is the
        deterministic tie-breaker cursor pagination relies on). No `FINAL`
        -- see app/clickhouse/query_common.py's module docstring and ADR 003
        section 8 for why a list/aggregate endpoint tolerates
        ReplacingMergeTree's eventual deduplication.
        """
        conditions = [
            "project_id = {project_id:UUID}",
            "start_time >= {start_time_from:DateTime64(3)}",
            "start_time < {start_time_to:DateTime64(3)}",
        ]
        parameters: dict[str, Any] = {
            "project_id": project_id,
            "start_time_from": start_time_from,
            "start_time_to": start_time_to,
            "limit": limit,
        }
        if environment is not None:
            conditions.append("environment = {environment:String}")
            parameters["environment"] = environment
        if resource is not None:
            conditions.append("resource = {resource:String}")
            parameters["resource"] = resource

        having_clauses: list[str] = []
        if has_error is True:
            having_clauses.append("error_span_count > 0")
        elif has_error is False:
            having_clauses.append("error_span_count = 0")
        if cursor is not None:
            cursor_start_time, cursor_trace_id = cursor
            having_clauses.append(
                "(trace_start_time, trace_id) < "
                "({cursor_start_time:DateTime64(3)}, {cursor_trace_id:String})"
            )
            parameters["cursor_start_time"] = cursor_start_time
            parameters["cursor_trace_id"] = cursor_trace_id

        where_sql = " AND ".join(conditions)
        having_sql = f"HAVING {' AND '.join(having_clauses)}" if having_clauses else ""

        # `any(environment)`/`any(resource)` are deliberately aliased to
        # `trace_environment`/`trace_resource`, NOT `environment`/`resource`:
        # ClickHouse resolves a SELECT alias that shares a name with a real
        # column against every reference to that name in the same query,
        # including the WHERE clause -- so `any(environment) AS environment`
        # together with a `WHERE environment = ...` filter substitutes the
        # aggregate expression into WHERE and fails with "Aggregate function
        # ... is found in WHERE" (verified directly against ClickHouse
        # 24.8). Giving the aggregate a distinct alias avoids the collision
        # entirely. See app/services/query.py's `_build_trace_summary`,
        # which reads these same alias names back out of the row dict.
        query = f"""
            SELECT
                toString(trace_id) AS trace_id,
                min(start_time) AS trace_start_time,
                max(end_time) AS trace_end_time,
                count() AS span_count,
                countIf(status = 'error') AS error_span_count,
                countIf(parent_span_id IS NULL) AS root_span_count,
                anyIf(name, parent_span_id IS NULL) AS root_span_name,
                any(environment) AS trace_environment,
                any(resource) AS trace_resource
            FROM spans
            WHERE {where_sql}
            GROUP BY trace_id
            {having_sql}
            ORDER BY trace_start_time DESC, trace_id DESC
            LIMIT {{limit:UInt32}}
        """
        return execute_query(self._client, query, parameters)

    def summarize_trace(
        self, *, project_id: UUID, trace_id: str, start_date: date | None
    ) -> dict[str, Any] | None:
        """Trace-level status/count/time-range summary derived from ALL of
        the trace's spans -- not just the possibly-truncated page
        `get_trace_spans` returns. See
        app/services/query.py:get_trace_response for why these must come
        from a separate, untruncated aggregate. No `FINAL`: a summary count
        can tolerate the same eventual-consistency window any other
        aggregate does; only the span *content* below needs it.
        """
        conditions, parameters = _trace_scope(project_id, trace_id, start_date)
        where_sql = " AND ".join(conditions)
        query = f"""
            SELECT
                count() AS total_span_count,
                countIf(status = 'error') AS error_span_count,
                countIf(parent_span_id IS NULL) AS root_span_count,
                min(start_time) AS trace_start_time,
                max(end_time) AS trace_end_time
            FROM spans
            WHERE {where_sql}
        """
        rows = execute_query(self._client, query, parameters)
        if not rows or rows[0]["total_span_count"] == 0:
            return None
        return rows[0]

    def get_trace_spans(
        self, *, project_id: UUID, trace_id: str, start_date: date | None, limit: int
    ) -> list[dict[str, Any]]:
        """Up to `limit` spans of one trace, ordered oldest first. Uses
        `FINAL` -- this is the "single-trace detail view" ADR 003 section 8
        identifies as needing immediate deduplication; the cost is bounded
        because the query is already scoped to one project_id + trace_id.
        """
        conditions, parameters = _trace_scope(project_id, trace_id, start_date)
        parameters["limit"] = limit
        where_sql = " AND ".join(conditions)
        query = f"""
            SELECT
                {_SPAN_DETAIL_SELECT}
            FROM spans FINAL
            WHERE {where_sql}
            ORDER BY start_time ASC
            LIMIT {{limit:UInt32}}
        """
        return execute_query(self._client, query, parameters)

    def get_span(
        self, *, project_id: UUID, trace_id: str, span_id: str, start_date: date | None
    ) -> list[dict[str, Any]]:
        """The single span identified by (project_id, trace_id, span_id) --
        ADR 003 section 8's logical identity, exactly. Uses `FINAL` for the
        same reason as `get_trace_spans`, at an even narrower key range.
        """
        conditions, parameters = _trace_scope(project_id, trace_id, start_date)
        conditions.append("span_id = {span_id:String}")
        parameters["span_id"] = span_id
        where_sql = " AND ".join(conditions)
        query = f"""
            SELECT
                {_SPAN_DETAIL_SELECT}
            FROM spans FINAL
            WHERE {where_sql}
            LIMIT 1
        """
        return execute_query(self._client, query, parameters)


def _trace_scope(
    project_id: UUID, trace_id: str, start_date: date | None
) -> tuple[list[str], dict[str, Any]]:
    """Shared (project_id, trace_id[, start_date]) WHERE-condition builder
    for summarize_trace/get_trace_spans/get_span. `start_date` is an
    optional caller-supplied partition-pruning hint (see
    app/api/v1/traces.py) -- without it, a lookup by trace_id alone must
    scan every retained daily partition, since `spans`' ORDER BY is
    `(project_id, toDate(start_time), trace_id, span_id)` and the date sits
    before trace_id in that key.
    """
    conditions = ["project_id = {project_id:UUID}", "trace_id = {trace_id:String}"]
    parameters: dict[str, Any] = {"project_id": project_id, "trace_id": trace_id}
    if start_date is not None:
        conditions.append("toDate(start_time) = {start_date:Date}")
        parameters["start_date"] = start_date
    return conditions, parameters
