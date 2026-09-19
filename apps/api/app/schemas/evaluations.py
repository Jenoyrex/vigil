"""Request/response schemas for the evaluations API surface.

`POST /v1/evaluations/jobs` (ADR 005 sections 9/10, Phase 3H amendment) is
internal-token-only -- every other schema in this module is customer-facing
(`get_current_api_key`), Phase 3I (ADR 005 section 4's still-owed
"evaluator_configs CRUD endpoints" and "job-status and evaluation-results
read endpoints").
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.db.models.evaluation_job import EVALUATION_JOB_STATUSES
from app.schemas.query import SpanId, TraceId

EvaluationJobCreateReason = Literal["created", "already_exists", "not_enabled", "not_sampled"]
EvaluationJobStatus = Literal[EVALUATION_JOB_STATUSES]  # type: ignore[valid-type]

# `evaluator_name` validation (Phase 4A) -- customer-facing only. Bounds
# length and character set; deliberately does NOT enumerate specific known
# names (`"relevance"`, `"relevance_embedding"`, or anything else) -- ADR
# 005 section 2's open-string, no-catalog design (matching `span_type`'s own
# precedent) is unchanged: any string matching this pattern is still
# accepted, known or not, so a new evaluator shipped in services/evaluator
# never requires an apps/api change to become configurable. This closes the
# gap `app/db/models/evaluation_job.py`'s own docstring already named
# ("format validation belongs to the API schema layer... Phase 4+") --
# before this, an unbounded string reached the `evaluator_configs` table's
# `uq_evaluator_configs_project_id_evaluator_name` unique index directly,
# risking an ugly, unhandled 500 (e.g. a Postgres "index row size exceeds
# maximum" error) instead of a clean 422.
#
# Deliberately NOT applied to `EvaluationJobCreateRequest.evaluator_name`
# below: that value is supplied by the trusted, internal-token-authenticated
# worker (never customer input), and is already constrained in practice to
# whatever `worker.registry.EvaluatorRegistry.registered_keys()` defines --
# a different trust boundary with a different (and already-adequate)
# validation story.
EVALUATOR_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _validate_evaluator_name(value: str) -> str:
    if not EVALUATOR_NAME_RE.fullmatch(value):
        raise ValueError(
            "evaluator_name must be 1-128 characters, using only letters, "
            "digits, underscore, hyphen, or period."
        )
    return value


EvaluatorName = Annotated[str, AfterValidator(_validate_evaluator_name)]


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


# -- evaluator_configs CRUD (customer-facing, ADR 005 section 2) --------------


class EvaluatorConfigOut(BaseModel):
    """One project's configuration for one evaluator. Absence of a row (not
    representable here -- see `GET /v1/evaluations/configs/{evaluator_name}`'s
    404 behavior) means "never configured," distinct from an explicit
    `enabled=false` row."""

    evaluator_name: str
    enabled: bool
    sampling_rate: float
    threshold: float | None
    max_retries: int
    created_at: datetime
    updated_at: datetime


class EvaluatorConfigListResponse(BaseModel):
    configs: list[EvaluatorConfigOut]


class EvaluatorConfigUpsertRequest(BaseModel):
    """Body for `PUT /v1/evaluations/configs/{evaluator_name}` -- full-replace
    (PUT, not PATCH) semantics: every field not supplied resolves to the
    exact same default `evaluator_configs`' own schema uses
    (`app/db/models/evaluator_config.py`), so a repeated PUT with the same
    body is genuinely idempotent and an omitted field is never silently
    left at whatever a prior PUT set it to. `enabled` has no default --
    this endpoint IS the enable/disable mechanism (ADR 005 section 10's
    "opt-in, off by default"), so every call must state it explicitly.
    """

    enabled: bool
    sampling_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    threshold: float | None = None
    max_retries: int = Field(default=3, ge=0)


# -- evaluation_jobs status list (customer-facing, ADR 005 sections 1/4) -----


class EvaluationJobOut(BaseModel):
    id: uuid.UUID
    trace_id: str
    span_id: str
    evaluator_name: str
    evaluator_version: str
    status: EvaluationJobStatus
    attempt_count: int
    max_retries: int
    next_attempt_at: datetime | None
    claimed_at: datetime | None
    claimed_by: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class EvaluationJobListResponse(BaseModel):
    """Ordered `created_at DESC, id DESC` -- `id` is the deterministic
    tie-breaker, mirroring `TraceListResponse`'s `start_time DESC, trace_id
    DESC` precedent exactly (app/schemas/query.py)."""

    jobs: list[EvaluationJobOut]
    next_cursor: str | None = None


# -- evaluation_results, span-scoped (customer-facing, ADR 005 section 5) ---


class EvaluationResultOut(BaseModel):
    """One `evaluation_results` row. Field set mirrors ADR 004 section 7's
    result schema exactly. `evaluation_cost_usd` is a string, not a float,
    for the same reason `SpanOut.llm_cost_usd` is (app/schemas/query.py):
    ClickHouse `Decimal64(6)` precision must survive JSON serialization
    without a binary-float rounding error."""

    evaluation_id: uuid.UUID
    trace_id: str
    span_id: str
    evaluator_name: str
    evaluator_version: str
    score: float | None
    label: str
    explanation: str
    evaluator_model: str | None
    evaluator_provider: str | None
    evaluation_latency_ms: float
    evaluation_cost_usd: str | None
    job_created_at: datetime
    written_at: datetime


class SpanEvaluationsResponse(BaseModel):
    """Response for `GET /v1/traces/{trace_id}/spans/{span_id}/evaluations`.
    `results` is `[]`, never 404, for a span with no evaluation history --
    ADR 005 section 5: evaluation_results is joined against spans only at
    the application layer, never a ClickHouse-level JOIN, so this endpoint
    does not itself verify the span exists (app/api/v1/traces.py's own
    span-detail endpoint already covers that check independently)."""

    results: list[EvaluationResultOut]
