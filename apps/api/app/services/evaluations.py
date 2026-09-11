"""Business logic for `POST /v1/evaluations/jobs` -- ADR 005 sections 9/10,
Phase 3H amendment.

This is where the actual business decisions about evaluation-job existence
are made and checked exactly once, in one place (ADR 005 section 10): is
`evaluator_name` enabled for `project_id` (`evaluator_configs` lookup)?
does this specific span pass the configured `sampling_rate` (deterministic,
never a per-poll-tick random draw)? does the asserted `project_id` actually
match ClickHouse ground truth? Only if all three hold does the row get
created (or, if it already exists, the existing row is returned unchanged
-- idempotent, per the existing `(project_id, trace_id, span_id,
evaluator_name, evaluator_version)` unique constraint on `evaluation_jobs`).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.clickhouse.query_repository import TracesQueryRepository
from app.db.models import EvaluationJob, EvaluatorConfig

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
