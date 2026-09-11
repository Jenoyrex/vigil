"""Business logic for the evaluations API surface.

`create_evaluation_job` (ADR 005 sections 9/10, Phase 3H amendment) is where
the actual business decisions about evaluation-job existence are made and
checked exactly once, in one place (ADR 005 section 10): is
`evaluator_name` enabled for `project_id` (`evaluator_configs` lookup)?
does this specific span pass the configured `sampling_rate` (deterministic,
never a per-poll-tick random draw)? does the asserted `project_id` actually
match ClickHouse ground truth? Only if all three hold does the row get
created (or, if it already exists, the existing row is returned unchanged
-- idempotent, per the existing `(project_id, trace_id, span_id,
evaluator_name, evaluator_version)` unique constraint on `evaluation_jobs`).

Everything below that function is Phase 3I (ADR 005 section 4's still-owed
"evaluator_configs CRUD endpoints" and "job-status and evaluation-results
read endpoints"): plain read/CRUD business logic over storage the pipeline
above already produces, following app/services/query.py's exact layering
(this module owns no SQL/ClickHouse query text itself; routes call into it,
it calls into app/clickhouse/evaluations_query_repository.py or the ORM).
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.clickhouse.evaluations_query_repository import EvaluationsQueryRepository
from app.clickhouse.query_repository import TracesQueryRepository
from app.db.models import EvaluationJob, EvaluatorConfig
from app.schemas.evaluations import (
    EvaluationJobListResponse,
    EvaluationJobOut,
    EvaluationResultOut,
    EvaluatorConfigListResponse,
    EvaluatorConfigOut,
    SpanEvaluationsResponse,
)
from app.services.query import QueryValidationError

EvaluationJobCreateReason = Literal["created", "already_exists", "not_enabled", "not_sampled"]


@dataclass(frozen=True)
class EvaluationJobCreationResult:
    job_id: uuid.UUID | None
    created: bool
    reason: EvaluationJobCreateReason


def _is_sampled_in(
    *,
    project_id: uuid.UUID,
    trace_id: str,
    span_id: str,
    evaluator_name: str,
    sampling_rate: float,
) -> bool:
    """Deterministic sampling decision: the same
    `(project_id, trace_id, span_id, evaluator_name)` always produces the
    same decision for a given `sampling_rate`, regardless of which process
    or how many times it is evaluated.

    This is an independently-implemented duplicate of
    `services/worker/worker/sampling.py`'s identical algorithm --
    `apps/api` must never import `services/worker` (ADR 001 decision 6's
    "duplicate rather than centralize" cross-service boundary, the same
    reason `apps/api` never imports `services/evaluator`). Keep both
    copies' behavior identical; `services/worker/tests/test_sampling.py` is
    the canonical unit test of the algorithm itself -- this endpoint's own
    tests (`not_enabled`/`not_sampled` cases) exercise this copy of it
    indirectly, through the API surface that actually enforces it.

    `hashlib.sha256`, not Python's built-in `hash()`: `hash()` is salted
    per-process (`PYTHONHASHSEED`) specifically to resist hash-flooding, so
    the *same* string hashes to *different* values across process restarts
    and across concurrently-running `apps/api` instances -- exactly the
    guarantee this function exists to provide. `sha256` is stable across
    processes, machines, and Python versions by construction.

    `evaluator_version` is deliberately excluded from the hashed key: a
    later evaluator version bump re-evaluates the identical population of
    spans already sampled in under the prior version, rather than
    re-randomizing the sample -- matching ADR 005 section 3's stated intent
    that a version bump should produce new jobs for already-evaluated
    spans, not a freshly-randomized subset of them.

    Boundary behavior: `sampling_rate <= 0.0` and `sampling_rate >= 1.0`
    are special-cased (never/always sampled in) both to skip the hash
    entirely and to avoid relying on a floating-point edge case at either
    boundary of the `[0, 1)`-normalized bucket.
    """
    if sampling_rate <= 0.0:
        return False
    if sampling_rate >= 1.0:
        return True
    key = f"{project_id}:{trace_id}:{span_id}:{evaluator_name}".encode()
    digest = hashlib.sha256(key).digest()
    bucket = int.from_bytes(digest[:8], "big") / (2**64)
    return bucket < sampling_rate


def create_evaluation_job(
    db: Session,
    traces_repository: TracesQueryRepository,
    *,
    project_id: uuid.UUID,
    trace_id: str,
    span_id: str,
    evaluator_name: str,
    evaluator_version: str,
) -> EvaluationJobCreationResult | None:
    """Returns `None` to signal "reject with 404" (span missing, or exists
    under a different project than asserted) -- mirrors
    `app/services/query.py`'s `get_span_response`'s identical
    None-means-404 convention. Every other outcome, including "not
    eligible," returns a populated result; those are not errors.
    """
    config = (
        db.query(EvaluatorConfig)
        .filter(
            EvaluatorConfig.project_id == project_id,
            EvaluatorConfig.evaluator_name == evaluator_name,
        )
        .first()
    )
    if config is None or not config.enabled:
        return EvaluationJobCreationResult(job_id=None, created=False, reason="not_enabled")

    if not _is_sampled_in(
        project_id=project_id,
        trace_id=trace_id,
        span_id=span_id,
        evaluator_name=evaluator_name,
        sampling_rate=config.sampling_rate,
    ):
        return EvaluationJobCreationResult(job_id=None, created=False, reason="not_sampled")

    # Ground-truth verification (ADR 005 section 9): the asserted project_id
    # is never trusted on its own. get_span already scopes its own WHERE
    # clause by project_id (app/clickhouse/query_repository.py), so an empty
    # result means either "no such span" or "this span belongs to a
    # different project" -- both cases are, correctly, indistinguishable
    # from the caller's perspective and both reject with 404. Reusing the
    # existing method verbatim; no new ClickHouse query is introduced here.
    rows = traces_repository.get_span(
        project_id=project_id, trace_id=trace_id, span_id=span_id, start_date=None
    )
    if not rows:
        return None

    # Idempotent, atomic insert -- never check-then-insert, which would race
    # under concurrent/overlapping pollers. Snapshots evaluator_name/
    # evaluator_version (the job's identity, asserted by the authenticated
    # worker -- ADR 005 section 2) and max_retries (ADR 005 section 1: so a
    # later evaluator_configs edit never retroactively changes an in-flight
    # job's retry budget). threshold is deliberately NOT snapshotted here --
    # it continues to resolve at execution time via
    # worker.postgres.evaluator_config_repository.EvaluatorConfigRepository,
    # unchanged from the existing Phase 3 threshold-resolution amendment.
    stmt = (
        pg_insert(EvaluationJob)
        .values(
            project_id=project_id,
            trace_id=trace_id,
            span_id=span_id,
            evaluator_name=evaluator_name,
            evaluator_version=evaluator_version,
            max_retries=config.max_retries,
        )
        .on_conflict_do_nothing(
            index_elements=[
                "project_id",
                "trace_id",
                "span_id",
                "evaluator_name",
                "evaluator_version",
            ]
        )
        .returning(EvaluationJob.id)
    )
    inserted_id = db.execute(stmt).scalars().first()
    if inserted_id is not None:
        db.commit()
        return EvaluationJobCreationResult(job_id=inserted_id, created=True, reason="created")

    # Conflict: the row already exists -- idempotent rediscovery, expected
    # and routine under the overlap-window poller strategy (ADR 005's Phase
    # 3H amendment), not a special case to avoid. evaluation_jobs rows are
    # never deleted anywhere in this system, so this SELECT cannot race with
    # a concurrent removal of the row the conflict just proved exists.
    existing_id = db.execute(
        select(EvaluationJob.id).where(
            EvaluationJob.project_id == project_id,
            EvaluationJob.trace_id == trace_id,
            EvaluationJob.span_id == span_id,
            EvaluationJob.evaluator_name == evaluator_name,
            EvaluationJob.evaluator_version == evaluator_version,
        )
    ).scalar_one()
    db.commit()
    return EvaluationJobCreationResult(job_id=existing_id, created=False, reason="already_exists")


# -- evaluator_configs CRUD (customer-facing, ADR 005 section 2) --------------


def _build_evaluator_config_out(config: EvaluatorConfig) -> EvaluatorConfigOut:
    return EvaluatorConfigOut(
        evaluator_name=config.evaluator_name,
        enabled=config.enabled,
        sampling_rate=config.sampling_rate,
        threshold=config.threshold,
        max_retries=config.max_retries,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


def list_evaluator_configs_response(
    db: Session, *, project_id: uuid.UUID
) -> EvaluatorConfigListResponse:
    configs = (
        db.query(EvaluatorConfig)
        .filter(EvaluatorConfig.project_id == project_id)
        .order_by(EvaluatorConfig.evaluator_name)
        .all()
    )
    return EvaluatorConfigListResponse(
        configs=[_build_evaluator_config_out(config) for config in configs]
    )


def get_evaluator_config_response(
    db: Session, *, project_id: uuid.UUID, evaluator_name: str
) -> EvaluatorConfigOut | None:
    """`None` means "never configured" (-> route maps to 404, per the
    approved plan's finalized decision NOT to synthesize a default config
    for an unconfigured evaluator) -- mirrors `get_span_response`'s
    identical None-means-404 convention."""
    config = (
        db.query(EvaluatorConfig)
        .filter(
            EvaluatorConfig.project_id == project_id,
            EvaluatorConfig.evaluator_name == evaluator_name,
        )
        .first()
    )
    if config is None:
        return None
    return _build_evaluator_config_out(config)


def upsert_evaluator_config_response(
    db: Session,
    *,
    project_id: uuid.UUID,
    evaluator_name: str,
    enabled: bool,
    sampling_rate: float,
    threshold: float | None,
    max_retries: int,
) -> EvaluatorConfigOut:
    """Idempotent full-replace upsert -- PUT semantics, not PATCH: every
    field is always written, matching `EvaluatorConfigUpsertRequest`'s own
    full-replace contract (app/schemas/evaluations.py). This is the
    customer-facing enable/disable mechanism (ADR 005 section 10's
    "opt-in, off by default").

    Uses the existing `uq_evaluator_configs_project_id_evaluator_name`
    unique constraint (app/db/models/evaluator_config.py) as the ON
    CONFLICT target -- the same atomic-upsert idiom `create_evaluation_job`
    above already uses for `evaluation_jobs`, adapted from DO NOTHING to DO
    UPDATE since a PUT must actually apply a changed value to an existing
    row, not no-op against it. Never check-then-insert/update, which would
    race under concurrent requests for the same (project_id, evaluator_name).

    `updated_at` is set explicitly in the SET clause: `TimestampMixin`'s
    `onupdate=func.now()` (app/db/base.py) only fires for an ORM-tracked
    attribute mutation, never for a Core-level `INSERT ... ON CONFLICT DO
    UPDATE` statement. `created_at` is deliberately omitted from SET -- an
    existing row's creation time must never change on update.

    `RETURNING` supplies every field the response needs directly from this
    one statement -- no second SELECT round trip, and no ambiguity about
    whether a just-written row could ever come back `None`.
    """
    stmt = (
        pg_insert(EvaluatorConfig)
        .values(
            project_id=project_id,
            evaluator_name=evaluator_name,
            enabled=enabled,
            sampling_rate=sampling_rate,
            threshold=threshold,
            max_retries=max_retries,
        )
        .on_conflict_do_update(
            index_elements=["project_id", "evaluator_name"],
            set_={
                "enabled": enabled,
                "sampling_rate": sampling_rate,
                "threshold": threshold,
                "max_retries": max_retries,
                "updated_at": func.now(),
            },
        )
        .returning(
            EvaluatorConfig.evaluator_name,
            EvaluatorConfig.enabled,
            EvaluatorConfig.sampling_rate,
            EvaluatorConfig.threshold,
            EvaluatorConfig.max_retries,
            EvaluatorConfig.created_at,
            EvaluatorConfig.updated_at,
        )
    )
    row = db.execute(stmt).one()
    db.commit()
    return EvaluatorConfigOut(
        evaluator_name=row.evaluator_name,
        enabled=row.enabled,
        sampling_rate=row.sampling_rate,
        threshold=row.threshold,
        max_retries=row.max_retries,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# -- evaluation_jobs status list (customer-facing, ADR 005 sections 1/4) -----


def encode_job_cursor(created_at: datetime, job_id: uuid.UUID) -> str:
    """Opaque pagination cursor for `GET /v1/evaluations/jobs`, encoding the
    previous page's last `(created_at, id)` -- the same tuple the list is
    sorted and filtered by (`created_at DESC, id DESC`). Reuses
    `app/services/query.py`'s exact `encode_trace_cursor`/`decode_trace_cursor`
    scheme (unsigned base64(json) -- a forged/corrupted cursor can only ever
    produce a `QueryValidationError` (422) or a wrong-but-still-project-scoped
    page, since the `project_id` WHERE clause is independent of cursor
    content), adapted for a UUID job id instead of a hex trace_id.
    """
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(job_id)}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_job_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data = json.loads(payload)
        created_at = datetime.fromisoformat(data["created_at"])
        job_id = uuid.UUID(str(data["id"]))
    except Exception as exc:
        raise QueryValidationError("Malformed pagination cursor.") from exc
    if created_at.tzinfo is None:
        raise QueryValidationError("Malformed pagination cursor.")
    return created_at, job_id


def _build_job_out(job: EvaluationJob) -> EvaluationJobOut:
    return EvaluationJobOut(
        id=job.id,
        trace_id=job.trace_id,
        span_id=job.span_id,
        evaluator_name=job.evaluator_name,
        evaluator_version=job.evaluator_version,
        status=job.status,
        attempt_count=job.attempt_count,
        max_retries=job.max_retries,
        next_attempt_at=job.next_attempt_at,
        claimed_at=job.claimed_at,
        claimed_by=job.claimed_by,
        last_error=job.last_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def list_evaluation_jobs_response(
    db: Session,
    *,
    project_id: uuid.UUID,
    status: str | None,
    evaluator_name: str | None,
    limit: int,
    cursor: str | None,
) -> EvaluationJobListResponse:
    """No new PostgreSQL index for this phase (per the approved plan) --
    reuses the existing `ix_evaluation_jobs_project_id_created_at` index
    (app/db/models/evaluation_job.py), already built for exactly this
    query shape. `status`/`evaluator_name` filters narrow within that
    project-scoped, created_at-ordered scan; at V1's expected job volume
    (bounded by each project's configured `sampling_rate`), this is not
    expected to need a composite index -- revisit only if real volume
    demonstrates otherwise.
    """
    decoded_cursor = decode_job_cursor(cursor) if cursor else None

    query = db.query(EvaluationJob).filter(EvaluationJob.project_id == project_id)
    if status is not None:
        query = query.filter(EvaluationJob.status == status)
    if evaluator_name is not None:
        query = query.filter(EvaluationJob.evaluator_name == evaluator_name)
    if decoded_cursor is not None:
        cursor_created_at, cursor_id = decoded_cursor
        query = query.filter(
            tuple_(EvaluationJob.created_at, EvaluationJob.id) < (cursor_created_at, cursor_id)
        )

    # Fetch one extra row so we can tell whether there's a next page without
    # a second round trip -- same convention as list_traces_response.
    rows = (
        query.order_by(EvaluationJob.created_at.desc(), EvaluationJob.id.desc())
        .limit(limit + 1)
        .all()
    )

    has_more = len(rows) > limit
    jobs = [_build_job_out(row) for row in rows[:limit]]

    next_cursor = None
    if has_more and jobs:
        last = jobs[-1]
        next_cursor = encode_job_cursor(last.created_at, last.id)

    return EvaluationJobListResponse(jobs=jobs, next_cursor=next_cursor)


# -- evaluation_results, span-scoped (customer-facing, ADR 005 section 5) ---


def _as_utc(value: datetime) -> datetime:
    """ClickHouse returns DateTime64 values as naive Python datetimes -- see
    app/services/query.py's identical helper for the full rationale
    (independently duplicated here: this one-line helper is private to each
    module, not meant for cross-module import)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _decimal_to_str(value: Decimal | None) -> str | None:
    """Same precision-preserving rationale as app/services/query.py's
    identical private helper for `SpanOut.llm_cost_usd` -- ClickHouse
    `Decimal64(6)` must survive JSON serialization without a binary-float
    rounding error."""
    return str(value) if value is not None else None


def _build_evaluation_result_out(row: dict[str, Any]) -> EvaluationResultOut:
    return EvaluationResultOut(
        evaluation_id=row["evaluation_id"],
        trace_id=row["trace_id"],
        span_id=row["span_id"],
        evaluator_name=row["evaluator_name"],
        evaluator_version=row["evaluator_version"],
        score=row["score"],
        label=row["label"],
        explanation=row["explanation"],
        evaluator_model=row["evaluator_model"],
        evaluator_provider=row["evaluator_provider"],
        evaluation_latency_ms=row["evaluation_latency_ms"],
        evaluation_cost_usd=_decimal_to_str(row["evaluation_cost_usd"]),
        job_created_at=_as_utc(row["job_created_at"]),
        written_at=_as_utc(row["written_at"]),
    )


def list_span_evaluations_response(
    repository: EvaluationsQueryRepository,
    *,
    project_id: uuid.UUID,
    trace_id: str,
    span_id: str,
) -> SpanEvaluationsResponse:
    """Always 200 with a possibly-empty `results` list, never 404 -- ADR 005
    section 5: `evaluation_results` is joined against `spans` only at the
    application layer, never a ClickHouse-level JOIN, so this does not
    itself verify the span exists (`GET /v1/traces/{trace_id}/spans/{span_id}`
    already covers that check independently, and per the approved plan this
    endpoint stays separate from -- never inlined into -- that response)."""
    rows = repository.get_span_evaluations(
        project_id=project_id, trace_id=trace_id, span_id=span_id
    )
    return SpanEvaluationsResponse(results=[_build_evaluation_result_out(row) for row in rows])
