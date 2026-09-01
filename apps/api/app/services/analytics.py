"""Business logic for `GET /v1/analytics/spans` and
`GET /v1/analytics/llm-usage`: group_by/bucket validation, time-window
resolution (shared with app.services.query), and ClickHouse row ->
response transformation.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.clickhouse.analytics_repository import AnalyticsRepository
from app.schemas.analytics import (
    LatencyPercentiles,
    LlmUsageGroup,
    LlmUsageResponse,
    SpanAnalyticsBucket,
    SpanAnalyticsGroup,
    SpanAnalyticsResponse,
)
from app.services.query import QueryValidationError, resolve_time_window

_EMPTY_SPAN_METRICS: dict[str, Any] = {
    "span_count": 0,
    "error_span_count": 0,
    "p50_latency_ms": 0.0,
    "p90_latency_ms": 0.0,
    "p99_latency_ms": 0.0,
}
_EMPTY_LLM_METRICS: dict[str, Any] = {
    "llm_span_count": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "total_tokens": 0,
    "total_cost_usd": Decimal("0"),
}


def _as_utc(value: datetime) -> datetime:
    """See app.services.query._as_utc -- ClickHouse DateTime64 reads (bucket
    boundaries here) come back naive; every value in `spans` is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _safe_float(value: Any) -> float:
    """`quantile()` over zero matching rows returns NaN, which is not valid
    JSON -- treat "no data" as a 0.0 floor rather than a special case the
    client has to know about."""
    result = float(value)
    return 0.0 if math.isnan(result) else result


def _error_rate(span_count: int, error_span_count: int) -> float:
    return (error_span_count / span_count) if span_count else 0.0


def _latency(row: dict[str, Any]) -> LatencyPercentiles:
    return LatencyPercentiles(
        p50=_safe_float(row["p50_latency_ms"]),
        p90=_safe_float(row["p90_latency_ms"]),
        p99=_safe_float(row["p99_latency_ms"]),
    )


def span_analytics_response(
    repository: AnalyticsRepository,
    *,
    project_id: UUID,
    start_time_from: datetime | None,
    start_time_to: datetime | None,
    environment: str | None,
    resource: str | None,
    span_type: str | None,
    group_by: str | None,
    bucket: str | None,
    default_window_hours: int,
    max_window_days: int,
) -> SpanAnalyticsResponse:
    if group_by is not None and bucket is not None:
        raise QueryValidationError("group_by and bucket are mutually exclusive.")

    window = resolve_time_window(
        start_time_from,
        start_time_to,
        default_window_hours=default_window_hours,
        max_window_days=max_window_days,
    )

    rows = repository.span_analytics(
        project_id=project_id,
        start_time_from=window.start_time_from,
        start_time_to=window.start_time_to,
        environment=environment,
        resource=resource,
        span_type=span_type,
        group_by=group_by,
        bucket=bucket,
    )

    response = SpanAnalyticsResponse(
        start_time_from=window.start_time_from,
        start_time_to=window.start_time_to,
        group_by=group_by,
        bucket=bucket,
    )

    if group_by is not None:
        response.groups = [
            SpanAnalyticsGroup(
                value=row["group_value"],
                span_count=row["span_count"],
                error_span_count=row["error_span_count"],
                error_rate=_error_rate(row["span_count"], row["error_span_count"]),
                latency_ms=_latency(row),
            )
            for row in rows
        ]
    elif bucket is not None:
        response.buckets = [
            SpanAnalyticsBucket(
                bucket_start=_as_utc(row["bucket_start"]),
                span_count=row["span_count"],
                error_span_count=row["error_span_count"],
                error_rate=_error_rate(row["span_count"], row["error_span_count"]),
                latency_ms=_latency(row),
            )
            for row in rows
        ]
    else:
        row = rows[0] if rows else _EMPTY_SPAN_METRICS
        response.span_count = row["span_count"]
        response.error_span_count = row["error_span_count"]
        response.error_rate = _error_rate(row["span_count"], row["error_span_count"])
        response.latency_ms = _latency(row)

    return response


def llm_usage_analytics_response(
    repository: AnalyticsRepository,
    *,
    project_id: UUID,
    start_time_from: datetime | None,
    start_time_to: datetime | None,
    environment: str | None,
    group_by: str | None,
    default_window_hours: int,
    max_window_days: int,
) -> LlmUsageResponse:
    window = resolve_time_window(
        start_time_from,
        start_time_to,
        default_window_hours=default_window_hours,
        max_window_days=max_window_days,
    )

    rows = repository.llm_usage_analytics(
        project_id=project_id,
        start_time_from=window.start_time_from,
        start_time_to=window.start_time_to,
        environment=environment,
        group_by=group_by,
    )

    response = LlmUsageResponse(
        start_time_from=window.start_time_from,
        start_time_to=window.start_time_to,
        group_by=group_by,
    )

    if group_by is not None:
        response.groups = [
            LlmUsageGroup(
                value=row["group_value"],
                llm_span_count=row["llm_span_count"],
                total_input_tokens=row["total_input_tokens"],
                total_output_tokens=row["total_output_tokens"],
                total_tokens=row["total_tokens"],
                total_cost_usd=str(row["total_cost_usd"]),
            )
            for row in rows
        ]
    else:
        row = rows[0] if rows else _EMPTY_LLM_METRICS
        response.llm_span_count = row["llm_span_count"]
        response.total_input_tokens = row["total_input_tokens"]
        response.total_output_tokens = row["total_output_tokens"]
        response.total_tokens = row["total_tokens"]
        response.total_cost_usd = str(row["total_cost_usd"])

    return response
