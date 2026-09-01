"""`GET /v1/analytics/spans` and `GET /v1/analytics/llm-usage` -- read-side
telemetry analytics. Mirrors app/api/v1/traces.py's authentication/
tenant-resolution/error-mapping pattern exactly.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import AuthenticatedKey, get_current_api_key
from app.clickhouse.analytics_repository import AnalyticsRepository
from app.clickhouse.client import get_clickhouse_client
from app.clickhouse.query_common import ClickHouseQueryError
from app.clickhouse.repository import ClickHouseUnavailableError
from app.config import settings
from app.schemas.analytics import (
    LlmGroupBy,
    LlmUsageResponse,
    SpanAnalyticsResponse,
    SpanBucket,
    SpanGroupBy,
)
from app.schemas.query import AwareDatetime
from app.services.analytics import llm_usage_analytics_response, span_analytics_response
from app.services.query import QueryValidationError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analytics"])


def get_analytics_repository() -> AnalyticsRepository:
    return AnalyticsRepository(get_clickhouse_client())


@router.get(
    "/v1/analytics/spans",
    response_model=SpanAnalyticsResponse,
    summary="Span count/error-rate/latency analytics",
    description=(
        "Aggregate span counts, error rate, and latency percentiles "
        "(approximate, via ClickHouse quantile()) for the authenticated "
        "project over a bounded time window (defaults to the last 24h; "
        f"capped at {settings.max_query_window_days} days). `group_by` and "
        "`bucket` are mutually exclusive. Does not use FINAL -- broad "
        "aggregates tolerate ReplacingMergeTree's eventual deduplication "
        "(see docs/decisions/003-clickhouse-telemetry-storage.md section 8)."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        422: {"description": "Invalid time range or query parameters."},
        503: {"description": "ClickHouse is temporarily unavailable; safe to retry."},
    },
)
def span_analytics_endpoint(
    start_time_from: AwareDatetime | None = Query(
        default=None, description="Inclusive lower bound. Defaults to 24h before start_time_to."
    ),
    start_time_to: AwareDatetime | None = Query(
        default=None, description="Exclusive upper bound. Defaults to now."
    ),
    environment: str | None = Query(default=None),
    resource: str | None = Query(
        default=None, description="Exact match on the SDK's service.name."
    ),
    span_type: str | None = Query(default=None),
    group_by: SpanGroupBy | None = Query(default=None),
    bucket: SpanBucket | None = Query(default=None),
    auth: AuthenticatedKey = Depends(get_current_api_key),
    repository: AnalyticsRepository = Depends(get_analytics_repository),
) -> SpanAnalyticsResponse:
    try:
        return span_analytics_response(
            repository,
            project_id=auth.project_id,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
            environment=environment,
            resource=resource,
            span_type=span_type,
            group_by=group_by,
            bucket=bucket,
            default_window_hours=settings.default_query_window_hours,
            max_window_days=settings.max_query_window_days,
        )
    except QueryValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ClickHouseUnavailableError as exc:
        logger.error("clickhouse unavailable during span analytics: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry storage is temporarily unavailable. Please retry.",
        ) from exc
    except ClickHouseQueryError as exc:
        logger.error("clickhouse query failed during span analytics: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to query telemetry."
        ) from exc


@router.get(
    "/v1/analytics/llm-usage",
    response_model=LlmUsageResponse,
    summary="LLM token usage and cost analytics",
    description=(
        "Aggregate LLM token usage and cost for the authenticated project "
        "over a bounded time window. Only spans with a non-null "
        "llm_provider are counted -- the documented signal for 'this is an "
        "LLM span' (docs/decisions/002-trace-span-telemetry-model.md), "
        "independent of span_type. total_cost_usd is a string to preserve "
        "Decimal64(6) precision. Does not use FINAL, same rationale as "
        "span analytics."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        422: {"description": "Invalid time range or query parameters."},
        503: {"description": "ClickHouse is temporarily unavailable; safe to retry."},
    },
)
def llm_usage_analytics_endpoint(
    start_time_from: AwareDatetime | None = Query(default=None),
    start_time_to: AwareDatetime | None = Query(default=None),
    environment: str | None = Query(default=None),
    group_by: LlmGroupBy | None = Query(default=None),
    auth: AuthenticatedKey = Depends(get_current_api_key),
    repository: AnalyticsRepository = Depends(get_analytics_repository),
) -> LlmUsageResponse:
    try:
        return llm_usage_analytics_response(
            repository,
            project_id=auth.project_id,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
            environment=environment,
            group_by=group_by,
            default_window_hours=settings.default_query_window_hours,
            max_window_days=settings.max_query_window_days,
        )
    except QueryValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ClickHouseUnavailableError as exc:
        logger.error("clickhouse unavailable during llm usage analytics: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry storage is temporarily unavailable. Please retry.",
        ) from exc
    except ClickHouseQueryError as exc:
        logger.error("clickhouse query failed during llm usage analytics: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to query telemetry."
        ) from exc
