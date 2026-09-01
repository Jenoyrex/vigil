"""`POST /v1/traces` -- the telemetry ingestion endpoint.

Route responsibilities are deliberately layered and kept thin here:

    authentication (app.api.deps)
        -> validation (app.schemas.traces, via FastAPI's request body)
        -> transformation (app.services.ingestion)
        -> repository (app.clickhouse.repository)
        -> ClickHouse

This module only wires those together, generates a request id for log
correlation, and maps repository failures to HTTP responses. It does not
contain ClickHouse queries, hashing logic, or payload-limit math itself.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import AuthenticatedKey, get_current_api_key
from app.clickhouse.client import get_clickhouse_client
from app.clickhouse.query_common import ClickHouseQueryError
from app.clickhouse.query_repository import TracesQueryRepository
from app.clickhouse.repository import (
    ClickHouseInsertError,
    ClickHouseUnavailableError,
    SpansRepository,
)
from app.config import settings
from app.schemas.query import (
    AwareDatetime,
    SpanId,
    SpanOut,
    TraceDetailResponse,
    TraceId,
    TraceListResponse,
)
from app.schemas.traces import TracesIngestResponse, TracesRequest
from app.services.ingestion import transform_request
from app.services.query import (
    QueryValidationError,
    get_span_response,
    get_trace_response,
    list_traces_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telemetry"])


def get_spans_repository() -> SpansRepository:
    return SpansRepository(get_clickhouse_client())


def get_traces_query_repository() -> TracesQueryRepository:
    return TracesQueryRepository(get_clickhouse_client())


@router.post(
    "/v1/traces",
    response_model=TracesIngestResponse,
    status_code=status.HTTP_200_OK,
    summary="Ingest a batch of spans",
    description=(
        "Accepts one or more spans, which may belong to several traces or to "
        "an incomplete trace -- a trace is a derived grouping of spans "
        "sharing a `trace_id`, not something ingested as its own entity. "
        "Authenticate with `Authorization: Bearer <api-key>`; the project is "
        "always resolved from that key, never from the request body. "
        "Insertion into ClickHouse happens synchronously before this "
        "endpoint responds, so a 200 response means the batch was accepted "
        "for storage -- see the response model and API docs for what that "
        "does and does not guarantee about deduplication."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        413: {"description": "Request body exceeds the maximum allowed size."},
        422: {
            "description": (
                "Structurally invalid request: bad trace_id/span_id, "
                "end_time before start_time, empty/too many spans, etc."
            )
        },
        503: {"description": "ClickHouse is temporarily unavailable; safe to retry."},
    },
)
def ingest_traces(
    payload: TracesRequest,
    auth: AuthenticatedKey = Depends(get_current_api_key),
    repository: SpansRepository = Depends(get_spans_repository),
) -> TracesIngestResponse:
    request_id = str(uuid.uuid4())
    rows = transform_request(payload, project_id=auth.project_id)

    try:
        repository.insert_spans(rows)
    except ClickHouseUnavailableError as exc:
        logger.error("clickhouse unavailable request_id=%s error=%s", request_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry storage is temporarily unavailable. Please retry.",
        ) from exc
    except ClickHouseInsertError as exc:
        logger.error("clickhouse insert failed request_id=%s error=%s", request_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store telemetry.",
        ) from exc

    logger.info(
        "ingested spans request_id=%s project_id=%s span_count=%d",
        request_id,
        auth.project_id,
        len(rows),
    )
    return TracesIngestResponse(accepted=len(rows), request_id=request_id)


@router.get(
    "/v1/traces",
    response_model=TraceListResponse,
    summary="List traces",
    description=(
        "Lists traces for the authenticated project, most recent first. A "
        "trace is a derived grouping of spans sharing a trace_id -- see "
        "docs/decisions/002-trace-span-telemetry-model.md -- not an "
        "independently stored entity. Always scoped to a bounded time "
        "window (defaults to the last 24h; capped at "
        "VIGIL_API_MAX_QUERY_WINDOW_DAYS) so a query can never "
        "accidentally scan the full retention window. Ordered by "
        "start_time DESC, with trace_id DESC as a deterministic "
        "tie-breaker for cursor pagination."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        422: {"description": "Invalid time range, cursor, or query parameters."},
        503: {"description": "ClickHouse is temporarily unavailable; safe to retry."},
    },
)
def list_traces_endpoint(
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
    has_error: bool | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(
        default=None, description="Opaque next_cursor from a prior response."
    ),
    auth: AuthenticatedKey = Depends(get_current_api_key),
    repository: TracesQueryRepository = Depends(get_traces_query_repository),
) -> TraceListResponse:
    try:
        return list_traces_response(
            repository,
            project_id=auth.project_id,
            start_time_from=start_time_from,
            start_time_to=start_time_to,
            environment=environment,
            resource=resource,
            has_error=has_error,
            limit=limit,
            cursor=cursor,
            default_window_hours=settings.default_query_window_hours,
            max_window_days=settings.max_query_window_days,
        )
    except QueryValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ClickHouseUnavailableError as exc:
        logger.error("clickhouse unavailable during trace list: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry storage is temporarily unavailable. Please retry.",
        ) from exc
    except ClickHouseQueryError as exc:
        logger.error("clickhouse query failed during trace list: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to query telemetry."
        ) from exc


@router.get(
    "/v1/traces/{trace_id}",
    response_model=TraceDetailResponse,
    summary="Fetch a complete trace",
    description=(
        "Fetches one trace and its spans, ordered by start_time ASC -- "
        "note this ordering does NOT imply parent-before-child (spans may "
        "arrive out of order per ADR 002); reconstruct the tree via "
        "parent_span_id. Uses ClickHouse FINAL for immediate "
        "deduplication, since this is the single-trace detail view ADR 003 "
        "section 8 identifies as needing it. Spans beyond "
        "VIGIL_API_MAX_SPANS_PER_TRACE_RESPONSE are omitted, flagged via "
        "truncated/total_span_count."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        404: {"description": "No spans found for this trace_id in the authenticated project."},
        422: {"description": "Malformed trace_id or start_date."},
        503: {"description": "ClickHouse is temporarily unavailable; safe to retry."},
    },
)
def get_trace_endpoint(
    trace_id: TraceId,
    start_date: date | None = Query(
        default=None,
        description=(
            "Optional YYYY-MM-DD hint for the trace's start_time, letting "
            "the query prune to a single ClickHouse partition instead of "
            "scanning every retained day."
        ),
    ),
    auth: AuthenticatedKey = Depends(get_current_api_key),
    repository: TracesQueryRepository = Depends(get_traces_query_repository),
) -> TraceDetailResponse:
    try:
        result = get_trace_response(
            repository,
            project_id=auth.project_id,
            trace_id=trace_id,
            start_date=start_date,
            max_spans=settings.max_spans_per_trace_response,
        )
    except ClickHouseUnavailableError as exc:
        logger.error("clickhouse unavailable during trace detail: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry storage is temporarily unavailable. Please retry.",
        ) from exc
    except ClickHouseQueryError as exc:
        logger.error("clickhouse query failed during trace detail: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to query telemetry."
        ) from exc

    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found.")
    return result


@router.get(
    "/v1/traces/{trace_id}/spans/{span_id}",
    response_model=SpanOut,
    summary="Fetch a single span",
    description=(
        "Fetches one span, identified by the (project_id, trace_id, "
        "span_id) triple that is its logical identity per "
        "docs/decisions/003-clickhouse-telemetry-storage.md section 8. "
        "Uses ClickHouse FINAL, same rationale as trace detail."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        404: {
            "description": "No span found for this trace_id/span_id in the authenticated project."
        },
        422: {"description": "Malformed trace_id, span_id, or start_date."},
        503: {"description": "ClickHouse is temporarily unavailable; safe to retry."},
    },
)
def get_span_endpoint(
    trace_id: TraceId,
    span_id: SpanId,
    start_date: date | None = Query(default=None),
    auth: AuthenticatedKey = Depends(get_current_api_key),
    repository: TracesQueryRepository = Depends(get_traces_query_repository),
) -> SpanOut:
    try:
        result = get_span_response(
            repository,
            project_id=auth.project_id,
            trace_id=trace_id,
            span_id=span_id,
            start_date=start_date,
        )
    except ClickHouseUnavailableError as exc:
        logger.error("clickhouse unavailable during span detail: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry storage is temporarily unavailable. Please retry.",
        ) from exc
    except ClickHouseQueryError as exc:
        logger.error("clickhouse query failed during span detail: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to query telemetry."
        ) from exc

    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Span not found.")
    return result
