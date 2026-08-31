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

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AuthenticatedKey, get_current_api_key
from app.clickhouse.client import get_clickhouse_client
from app.clickhouse.repository import (
    ClickHouseInsertError,
    ClickHouseUnavailableError,
    SpansRepository,
)
from app.schemas.traces import TracesIngestResponse, TracesRequest
from app.services.ingestion import transform_request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telemetry"])


def get_spans_repository() -> SpansRepository:
    return SpansRepository(get_clickhouse_client())


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
