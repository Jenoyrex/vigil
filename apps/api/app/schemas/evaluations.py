"""Request/response schemas for `POST /v1/evaluations/jobs` -- ADR 005
sections 9/10, Phase 3H amendment.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.query import SpanId, TraceId

EvaluationJobCreateReason = Literal["created", "already_exists", "not_enabled", "not_sampled"]


class EvaluationJobCreateRequest(BaseModel):
    """Supplied entirely by the trusted, internal-token-authenticated worker
    poller -- never a customer request. `project_id` in a request body is
    the one legitimate exception to "project_id never in a request body"
    (ADR 005 section 9): safe only because `app.services.evaluations`
    independently re-verifies it against ClickHouse ground truth before
    ever trusting it, not because the caller is assumed honest.
    """

    project_id: uuid.UUID
    trace_id: TraceId
    span_id: SpanId
    evaluator_name: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)


class EvaluationJobCreateResponse(BaseModel):
    """`job_id` is `None` only when `reason` is `"not_enabled"` or
    `"not_sampled"` -- no row exists for either of those outcomes. For
    `"created"`/`"already_exists"`, `job_id` identifies the (new or
    pre-existing) row either way -- idempotent rediscovery is not an error.
    """

    job_id: uuid.UUID | None
    created: bool
    reason: EvaluationJobCreateReason
