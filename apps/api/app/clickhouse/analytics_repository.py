"""Read-side ClickHouse queries for span/LLM-usage analytics
(`GET /v1/analytics/spans`, `GET /v1/analytics/llm-usage`). Never uses
`FINAL` -- see app/clickhouse/query_repository.py's docstrings and ADR 003
section 8 for why broad aggregates tolerate ReplacingMergeTree's eventual
deduplication.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from clickhouse_connect.driver.client import Client

from app.clickhouse.query_common import execute_query

MAX_ANALYTICS_GROUPS = 50

_SPAN_GROUP_BY_COLUMNS = {
    "environment": "environment",
    "span_type": "span_type",
    "release": "release",
    "resource": "resource",
}
_SPAN_BUCKET_EXPRESSIONS = {
    "hour": "toStartOfHour(start_time)",
    "day": "toStartOfDay(start_time)",
}
_LLM_GROUP_BY_COLUMNS = {
    "llm_provider": "llm_provider",
    "llm_model": "llm_model",
    "environment": "environment",
}

_SPAN_METRICS_SELECT = """count() AS span_count,
                countIf(status = 'error') AS error_span_count,
                quantile(0.50)(duration_ms) AS p50_latency_ms,
                quantile(0.90)(duration_ms) AS p90_latency_ms,
                quantile(0.99)(duration_ms) AS p99_latency_ms"""

_LLM_METRICS_SELECT = """count() AS llm_span_count,
                coalesce(sum(llm_input_tokens), 0) AS total_input_tokens,
                coalesce(sum(llm_output_tokens), 0) AS total_output_tokens,
                coalesce(sum(llm_total_tokens), 0) AS total_tokens,
                coalesce(sum(llm_cost_usd), toDecimal64(0, 6)) AS total_cost_usd"""


def _safe_identifier(mapping: dict[str, str], key: str) -> str:
    """Map a validated Literal value to a literal SQL fragment.

    ClickHouse's server-side parameter binding (`{name:Type}`) only binds
    *values*, never identifiers/expressions -- so `group_by`/`bucket`
    selection can't go through `execute_query`'s `parameters` dict like
    every other input here does. Safety instead comes from `key` always
    being constrained to one of a small Literal enum by FastAPI/Pydantic
    (app/schemas/analytics.py's `SpanGroupBy`/`SpanBucket`/`LlmGroupBy`)
    before this is ever called -- never arbitrary client text. The
    KeyError branch should be unreachable; it exists so a future bug can
    never fall through to interpolating unexpected text into a query.
    """
    try:
        return mapping[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported grouping/bucket key: {key!r}") from exc


class AnalyticsRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def span_analytics(
        self,
        *,
        project_id: UUID,
        start_time_from: datetime,
        start_time_to: datetime,
        environment: str | None,
        resource: str | None,
        span_type: str | None,
        group_by: str | None,
        bucket: str | None,
    ) -> list[dict[str, Any]]:
        conditions, parameters = _span_scope(
            project_id, start_time_from, start_time_to, environment, resource, span_type
        )
        where_sql = " AND ".join(conditions)

        if group_by is not None:
            column = _safe_identifier(_SPAN_GROUP_BY_COLUMNS, group_by)
            query = f"""
                SELECT
                    {column} AS group_value,
                    {_SPAN_METRICS_SELECT}
                FROM spans
                WHERE {where_sql}
                GROUP BY group_value
                ORDER BY span_count DESC
                LIMIT {MAX_ANALYTICS_GROUPS}
            """
        elif bucket is not None:
            bucket_expr = _safe_identifier(_SPAN_BUCKET_EXPRESSIONS, bucket)
            query = f"""
                SELECT
                    {bucket_expr} AS bucket_start,
                    {_SPAN_METRICS_SELECT}
                FROM spans
                WHERE {where_sql}
                GROUP BY bucket_start
                ORDER BY bucket_start ASC
            """
        else:
            query = f"""
                SELECT
                    {_SPAN_METRICS_SELECT}
                FROM spans
                WHERE {where_sql}
            """
        return execute_query(self._client, query, parameters)

    def llm_usage_analytics(
        self,
        *,
        project_id: UUID,
        start_time_from: datetime,
        start_time_to: datetime,
        environment: str | None,
        group_by: str | None,
    ) -> list[dict[str, Any]]:
        # `llm_provider IS NOT NULL` is the documented "is this an LLM
        # span" signal (docs/decisions/002 decision 2), independent of
        # span_type -- kept in WHERE (not countIf/sumIf) uniformly for both
        # the flat and grouped cases, so a non-LLM span never contributes
        # to any aggregate here regardless of mode.
        conditions = [
            "project_id = {project_id:UUID}",
            "start_time >= {start_time_from:DateTime64(3)}",
            "start_time < {start_time_to:DateTime64(3)}",
            "llm_provider IS NOT NULL",
        ]
        parameters: dict[str, Any] = {
            "project_id": project_id,
            "start_time_from": start_time_from,
            "start_time_to": start_time_to,
        }
        if environment is not None:
            conditions.append("environment = {environment:String}")
            parameters["environment"] = environment
        where_sql = " AND ".join(conditions)

        if group_by is not None:
            column = _safe_identifier(_LLM_GROUP_BY_COLUMNS, group_by)
            query = f"""
                SELECT
                    {column} AS group_value,
                    {_LLM_METRICS_SELECT}
                FROM spans
                WHERE {where_sql}
                GROUP BY group_value
                ORDER BY total_cost_usd DESC
                LIMIT {MAX_ANALYTICS_GROUPS}
            """
        else:
            query = f"""
                SELECT
                    {_LLM_METRICS_SELECT}
                FROM spans
                WHERE {where_sql}
            """
        return execute_query(self._client, query, parameters)


def _span_scope(
    project_id: UUID,
    start_time_from: datetime,
    start_time_to: datetime,
    environment: str | None,
    resource: str | None,
    span_type: str | None,
) -> tuple[list[str], dict[str, Any]]:
    conditions = [
        "project_id = {project_id:UUID}",
        "start_time >= {start_time_from:DateTime64(3)}",
        "start_time < {start_time_to:DateTime64(3)}",
    ]
    parameters: dict[str, Any] = {
        "project_id": project_id,
        "start_time_from": start_time_from,
        "start_time_to": start_time_to,
    }
    if environment is not None:
        conditions.append("environment = {environment:String}")
        parameters["environment"] = environment
    if resource is not None:
        conditions.append("resource = {resource:String}")
        parameters["resource"] = resource
    if span_type is not None:
        conditions.append("span_type = {span_type:String}")
        parameters["span_type"] = span_type
    return conditions, parameters
