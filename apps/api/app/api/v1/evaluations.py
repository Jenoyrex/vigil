"""`POST /v1/evaluations/jobs` -- the internal, worker-only evaluation-job
creation endpoint. ADR 005 sections 9/10, Phase 3H amendment.

Authenticated by `get_internal_service_auth` (`X-Vigil-Internal-Token`),
never `get_current_api_key` -- structurally separate from every
customer-facing route this router file's siblings (`traces.py`,
`analytics.py`) expose. Thin route, matching `traces.py`'s own layering:
authentication -> validation (schema) -> `app.services.evaluations` ->
status-code mapping. No ClickHouse query or business-eligibility logic
lives here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_internal_service_auth
from app.api.v1.traces import get_traces_query_repository
from app.clickhouse.query_common import ClickHouseQueryError
from app.clickhouse.query_repository import TracesQueryRepository
from app.clickhouse.repository import ClickHouseUnavailableError
from app.db.session import get_db
from app.schemas.evaluations import EvaluationJobCreateRequest, EvaluationJobCreateResponse
from app.services.evaluations import create_evaluation_job

logger = logging.getLogger(__name__)

router = APIRouter(tags=["evaluations"])


@router.post(
    "/v1/evaluations/jobs",
    response_model=EvaluationJobCreateResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(get_internal_service_auth)],
    summary="Create (or idempotently confirm) one evaluation job",
    description=(
        "Internal, worker-only endpoint -- authenticated via "
        "`X-Vigil-Internal-Token`, never a customer API key. Called once "
        "per `(span, evaluator_name)` pair the worker poller's installed "
        "evaluator registry is capable of running. Business eligibility "
        "(evaluator enabled? sampled in?) and ground-truth project "
        "ownership are decided here, exactly once, per ADR 005 section 10."
    ),
    responses={
        401: {"description": "Missing or invalid X-Vigil-Internal-Token."},
        404: {"description": "Span not found, or belongs to a different project than asserted."},
        422: {"description": "Malformed trace_id/span_id or missing/empty required fields."},
        503: {"description": "ClickHouse is temporarily unavailable; safe to retry."},
    },
)
def create_job(
    payload: EvaluationJobCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    traces_repository: TracesQueryRepository = Depends(get_traces_query_repository),
) -> EvaluationJobCreateResponse:
    try:
        result = create_evaluation_job(
            db,
            traces_repository,
            project_id=payload.project_id,
            trace_id=payload.trace_id,
            span_id=payload.span_id,
            evaluator_name=payload.evaluator_name,
            evaluator_version=payload.evaluator_version,
        )
    except ClickHouseUnavailableError as exc:
        logger.error("clickhouse unavailable during evaluation job creation: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry storage is temporarily unavailable. Please retry.",
        ) from exc
    except ClickHouseQueryError as exc:
        logger.error("clickhouse rejected query during evaluation job creation: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify span.",
        ) from exc

    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Span not found.")

    # The route's declared status_code (200) covers every outcome except a
    # genuine new insert -- FastAPI's status_code is fixed per-route, so a
    # 201-only-on-creation distinction needs the injected Response object.
    if result.created:
        response.status_code = status.HTTP_201_CREATED

    return EvaluationJobCreateResponse(
        job_id=result.job_id, created=result.created, reason=result.reason
    )
