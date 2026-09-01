"""Request/response models for `GET /v1/analytics/spans` and
`GET /v1/analytics/llm-usage`. See app/services/analytics.py for the
business logic and app/clickhouse/analytics_repository.py for the queries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

SpanGroupBy = Literal["environment", "span_type", "release", "resource"]
SpanBucket = Literal["hour", "day"]
LlmGroupBy = Literal["llm_provider", "llm_model", "environment"]


class LatencyPercentiles(BaseModel):
    """Approximate percentiles, from ClickHouse `quantile()` (not
    `quantileExact()`) -- see app/clickhouse/analytics_repository.py."""

    p50: float
    p90: float
    p99: float


class SpanAnalyticsGroup(BaseModel):
    value: str
    span_count: int
    error_span_count: int
    error_rate: float
    latency_ms: LatencyPercentiles


class SpanAnalyticsBucket(BaseModel):
    bucket_start: datetime
    span_count: int
    error_span_count: int
    error_rate: float
    latency_ms: LatencyPercentiles


class SpanAnalyticsResponse(BaseModel):
    """Exactly one of the flat fields / `groups` / `buckets` is populated,
    matching whichever of `group_by`/`bucket` (mutually exclusive) the
    request used -- see app/services/analytics.py."""

    start_time_from: datetime
    start_time_to: datetime
    group_by: SpanGroupBy | None = None
    bucket: SpanBucket | None = None
    span_count: int | None = None
    error_span_count: int | None = None
    error_rate: float | None = None
    latency_ms: LatencyPercentiles | None = None
    groups: list[SpanAnalyticsGroup] | None = None
    buckets: list[SpanAnalyticsBucket] | None = None


class LlmUsageGroup(BaseModel):
    value: str
    llm_span_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost_usd: str


class LlmUsageResponse(BaseModel):
    """`total_cost_usd` is a string (not a JSON number) to preserve
    Decimal64(6) precision -- see
    docs/decisions/003-clickhouse-telemetry-storage.md section 2."""

    start_time_from: datetime
    start_time_to: datetime
    group_by: LlmGroupBy | None = None
    llm_span_count: int | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_tokens: int | None = None
    total_cost_usd: str | None = None
    groups: list[LlmUsageGroup] | None = None
