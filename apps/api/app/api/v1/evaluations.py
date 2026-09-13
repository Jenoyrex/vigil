"""The evaluations API surface.

`POST /v1/evaluations/jobs` -- the internal, worker-only evaluation-job
creation endpoint (ADR 005 sections 9/10, Phase 3H amendment) -- is
authenticated by `get_internal_service_auth` (`X-Vigil-Internal-Token`),
never a customer API key; structurally separate from every other route in
this file, which is customer-facing (Phase 3I, ADR 005 section 4's
still-owed "evaluator_configs CRUD endpoints" and "job-status and
evaluation-results read endpoints") and, as of Phase 4C, additionally
rate-limited via `app.api.rate_limit.require_default_rate_limit` (which
itself still resolves the same `AuthenticatedKey` `get_current_api_key`
always has -- this endpoint is not exempted from authentication, only from
customer-style rate limiting, which has no meaning for a single trusted
internal caller). Every route here is thin, matching `traces.py`'s own
layering: authentication (+ rate limiting, customer routes only) ->
validation (schema) -> `app.services.evaluations` -> status-code mapping.
No ClickHouse query or business logic lives in this module.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import AuthenticatedKey, get_internal_service_auth
from app.api.rate_limit import require_default_rate_limit
from app.api.v1.traces import get_traces_query_repository
from app.clickhouse.client import get_clickhouse_client
from app.clickhouse.evaluations_query_repository import EvaluationsQueryRepository
from app.clickhouse.query_common import ClickHouseQueryError
from app.clickhouse.query_repository import TracesQueryRepository
from app.clickhouse.repository import ClickHouseUnavailableError
from app.db.session import get_db
from app.schemas.evaluations import (
    EvaluationJobCreateRequest,
    EvaluationJobCreateResponse,
    EvaluationJobListResponse,
    EvaluationJobStatus,
    EvaluatorConfigListResponse,
    EvaluatorConfigOut,
    EvaluatorConfigUpsertRequest,
    EvaluatorName,
    SpanEvaluationsResponse,
)
from app.schemas.query import SpanId, TraceId
from app.services.evaluations import (
    create_evaluation_job,
    get_evaluator_config_response,
    list_evaluation_jobs_response,
    list_evaluator_configs_response,
    list_span_evaluations_response,
    upsert_evaluator_config_response,
)
from app.services.query import QueryValidationError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["evaluations"])


def get_evaluations_query_repository() -> EvaluationsQueryRepository:
    return EvaluationsQueryRepository(get_clickhouse_client())


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


# -- evaluator_configs CRUD (customer-facing) --------------------------------


@router.get(
    "/v1/evaluations/configs",
    response_model=EvaluatorConfigListResponse,
    summary="List this project's evaluator configurations",
    description=(
        "Lists every evaluator this project has ever configured -- empty "
        "if none (evaluation is opt-in and off by default, ADR 005 section "
        "10). An evaluator with no row here has never been configured, "
        "distinct from an explicit enabled=false row."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        429: {"description": "Rate limit exceeded for this API key. See the Retry-After header."},
    },
)
def list_evaluator_configs_endpoint(
    auth: AuthenticatedKey = Depends(require_default_rate_limit),
    db: Session = Depends(get_db),
) -> EvaluatorConfigListResponse:
    return list_evaluator_configs_response(db, project_id=auth.project_id)


@router.get(
    "/v1/evaluations/configs/{evaluator_name}",
    response_model=EvaluatorConfigOut,
    summary="Fetch one evaluator's configuration",
    description=(
        "Fetches this project's configuration for one evaluator. Returns "
        "404 if this evaluator has never been configured for this project "
        "-- no default configuration is synthesized; PUT this same path to "
        "create one."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        429: {"description": "Rate limit exceeded for this API key. See the Retry-After header."},
        404: {"description": "This evaluator has never been configured for this project."},
    },
)
def get_evaluator_config_endpoint(
    evaluator_name: EvaluatorName,
    auth: AuthenticatedKey = Depends(require_default_rate_limit),
    db: Session = Depends(get_db),
) -> EvaluatorConfigOut:
    result = get_evaluator_config_response(
        db, project_id=auth.project_id, evaluator_name=evaluator_name
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This evaluator has never been configured for this project.",
        )
    return result


@router.put(
    "/v1/evaluations/configs/{evaluator_name}",
    response_model=EvaluatorConfigOut,
    summary="Enable/configure (or update) one evaluator for this project",
    description=(
        "Creates or fully replaces this project's configuration for one "
        "evaluator -- the customer-facing enable/disable mechanism (ADR "
        "005 section 10). Full-replace (PUT) semantics: any field omitted "
        "from the request body resolves to its own documented default, "
        "never to whatever a prior PUT left it at -- so a repeated PUT with "
        "the same body is genuinely idempotent. `evaluator_name` is an "
        "open string (no catalog table, matching `span_type`'s precedent) "
        "-- configuring a name services/worker's registry doesn't "
        "recognize is accepted but has no effect until a matching "
        "evaluator is installed."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        429: {"description": "Rate limit exceeded for this API key. See the Retry-After header."},
        422: {
            "description": (
                "Invalid evaluator_name, sampling_rate outside [0, 1], or negative max_retries."
            )
        },
    },
)
def upsert_evaluator_config_endpoint(
    payload: EvaluatorConfigUpsertRequest,
    evaluator_name: EvaluatorName,
    auth: AuthenticatedKey = Depends(require_default_rate_limit),
    db: Session = Depends(get_db),
) -> EvaluatorConfigOut:
    return upsert_evaluator_config_response(
        db,
        project_id=auth.project_id,
        evaluator_name=evaluator_name,
        enabled=payload.enabled,
        sampling_rate=payload.sampling_rate,
        threshold=payload.threshold,
        max_retries=payload.max_retries,
    )


# -- evaluation_jobs status list (customer-facing) ---------------------------


@router.get(
    "/v1/evaluations/jobs",
    response_model=EvaluationJobListResponse,
    summary="List this project's evaluation jobs",
    description=(
        "Lists evaluation jobs for the authenticated project, most recent "
        "first -- primarily for observing pending/failed/dead_letter state "
        "(ADR 005 sections 1/8). Cursor-paginated, same opaque-cursor "
        "scheme as `GET /v1/traces`. Reuses the existing "
        "`(project_id, created_at)` index (app/db/models/evaluation_job.py) "
        "-- no new index was added for this endpoint."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        429: {"description": "Rate limit exceeded for this API key. See the Retry-After header."},
        422: {"description": "Malformed cursor."},
    },
)
def list_evaluation_jobs_endpoint(
    status_filter: EvaluationJobStatus | None = Query(
        default=None, alias="status", description="Exact match on job status."
    ),
    evaluator_name: EvaluatorName | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(
        default=None, description="Opaque next_cursor from a prior response."
    ),
    auth: AuthenticatedKey = Depends(require_default_rate_limit),
    db: Session = Depends(get_db),
) -> EvaluationJobListResponse:
    try:
        return list_evaluation_jobs_response(
            db,
            project_id=auth.project_id,
            status=status_filter,
            evaluator_name=evaluator_name,
            limit=limit,
            cursor=cursor,
        )
    except QueryValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


# -- evaluation_results, span-scoped (customer-facing) -----------------------


@router.get(
    "/v1/traces/{trace_id}/spans/{span_id}/evaluations",
    response_model=SpanEvaluationsResponse,
    summary="Fetch evaluation results for one span",
    description=(
        "Fetches every evaluation_results row for one span, one entry per "
        "evaluator that has run against it. Always 200 with a possibly-"
        "empty `results` list -- never 404 -- since a span with no "
        "evaluation history (evaluation disabled, not sampled in, or "
        "simply not yet evaluated) is a normal, common outcome, not an "
        "error. Does not verify the span itself exists; pair with "
        "`GET /v1/traces/{trace_id}/spans/{span_id}` for that. Kept as a "
        "separate endpoint deliberately -- never inlined into that "
        "response -- so the existing span-detail path pays no additional "
        "ClickHouse round trip for projects with evaluation disabled."
    ),
    responses={
        401: {"description": "Missing, malformed, unknown, or revoked API key."},
        429: {"description": "Rate limit exceeded for this API key. See the Retry-After header."},
        422: {"description": "Malformed trace_id or span_id."},
        503: {"description": "ClickHouse is temporarily unavailable; safe to retry."},
    },
)
def list_span_evaluations_endpoint(
    trace_id: TraceId,
    span_id: SpanId,
    auth: AuthenticatedKey = Depends(require_default_rate_limit),
    repository: EvaluationsQueryRepository = Depends(get_evaluations_query_repository),
) -> SpanEvaluationsResponse:
    try:
        return list_span_evaluations_response(
            repository, project_id=auth.project_id, trace_id=trace_id, span_id=span_id
        )
    except ClickHouseUnavailableError as exc:
        logger.error("clickhouse unavailable during span evaluations lookup: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry storage is temporarily unavailable. Please retry.",
        ) from exc
    except ClickHouseQueryError as exc:
        logger.error("clickhouse query failed during span evaluations lookup: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query evaluation results.",
        ) from exc
