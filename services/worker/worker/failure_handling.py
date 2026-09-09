"""Failure classification, retry/backoff calculation, and the state
transition a claimed job takes when `worker.execution.execute_job` raises.

Deliberately its own module -- neither `execute_job` nor `Dispatcher` decide
retry/backoff/dead-letter policy (see each module's own docstring); this is
the "not-yet-built failure-management layer" both of them already reference.
See docs/decisions/005-evaluation-job-storage-worker.md's Phase 3E amendment
for the full design rationale this module implements.

Classification (`is_retryable`): exactly two exception types are permanent
(dead-lettered immediately, regardless of remaining retry budget) --
`SourceSpanNotFoundError` and `InvalidEvaluatorInputError` -- because an
identical retry of the same job is guaranteed to observe the identical
failure (the same span identity will still not exist; the same span content
will still produce the same malformed evaluator input). Every other
exception this module has ever seen raised from `execute_job` -- ClickHouse
failures, PostgreSQL failures, `UnknownEvaluatorError`, a generic evaluator
exception, or anything unclassified -- is retryable: each could plausibly
observe different external state on a later attempt (a transient outage
recovering, a rolling deploy completing), and retrying is bounded by
`max_retries` regardless, so treating the unknown case as retryable rather
than inventing a larger taxonomy is the deliberately simple default.

`attempt_count` semantics (docs/decisions/005... Phase 3 plan section 3,
reaffirmed by this module): `max_retries` is a **total-attempt cap**, not a
count of retries after the first attempt -- `evaluation_job.py`'s own
docstring already establishes this ("only once attempt_count >= max_retries
does the next failure move to the terminal dead_letter state"). This module
does not reinterpret that; `handle_execution_failure` below applies it
directly to whatever `job.attempt_count` and `job.max_retries` already are,
never recomputing or re-reading either.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.types import InvalidEvaluatorInputError

from worker.config import settings
from worker.execution import SourceSpanNotFoundError
from worker.postgres.repository import ClaimedJob, EvaluationJobsRepository

#: Exception types where an identical retry is guaranteed to observe the
#: identical failure -- see module docstring. Order doesn't matter; this is
#: a membership test via `isinstance`.
_PERMANENT_EXCEPTION_TYPES: tuple[type[Exception], ...] = (
    SourceSpanNotFoundError,
    InvalidEvaluatorInputError,
)

#: `last_error` must never be a raw payload dump (ADR 004 section 6/8's
#: existing precedent for this column, unchanged by this module) -- bounded
#: to a fixed cap regardless of how long the underlying exception's message
#: happens to be.
_MAX_LAST_ERROR_LENGTH = 2000


def is_retryable(exc: Exception) -> bool:
    """`False` for the two permanent exception types this module knows
    about; `True` for everything else, including exception types this
    module has never seen before -- see module docstring for why "retryable
    by default" is the deliberately simple choice here.
    """
    return not isinstance(exc, _PERMANENT_EXCEPTION_TYPES)


def compute_next_attempt_at(attempt_count: int, *, now: datetime | None = None) -> datetime:
    """Exponential backoff with jitter, per
    docs/decisions/005-evaluation-job-storage-worker.md's Phase 3E amendment:

        delay_seconds = min(retry_max_delay_seconds,
                             retry_base_seconds * (2 ** (attempt_count - 1))
                            ) + random.uniform(0, retry_jitter_seconds)

    `attempt_count` is already 1-indexed at the moment of failure (this
    attempt's own claim already incremented it) -- `attempt_count - 1` is
    what makes the first failure use exactly `retry_base_seconds` (not
    double it), doubling on each subsequent failure: attempt 1 -> base,
    attempt 2 -> 2x base, attempt 3 -> 4x base, ... capped at
    `retry_max_delay_seconds` before jitter is added. `now` is injectable
    for deterministic tests; defaults to the real current UTC time.
    """
    if now is None:
        now = datetime.now(UTC)

    exponential_delay = settings.retry_base_seconds * (2 ** (attempt_count - 1))
    capped_delay = min(settings.retry_max_delay_seconds, exponential_delay)
    jitter = random.uniform(0, settings.retry_jitter_seconds)  # noqa: S311 -- not
    # cryptographic use; this only spreads out reclaim timing to avoid a
    # thundering herd, per the Phase 3E amendment's own rationale.
    delay_seconds = capped_delay + jitter

    return now + timedelta(seconds=delay_seconds)


def _bounded_last_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if len(text) <= _MAX_LAST_ERROR_LENGTH:
        return text
    return text[:_MAX_LAST_ERROR_LENGTH]


@dataclass(frozen=True)
class FailureHandlingOutcome:
    """What `handle_execution_failure` actually did to the job's PostgreSQL
    row. `new_status` is `'failed'` or `'dead_letter'` -- the status this
    code *attempted* to transition to, regardless of whether the fenced
    update actually took effect. `recorded` is `False` when the fenced
    update affected zero rows (this attempt was stale -- see
    `EvaluationJobsRepository.mark_succeeded`'s docstring for the identical
    convention on the success path) -- a stale result is not an error and
    must never be raised or retried; it means some other, newer attempt
    already owns this job.
    """

    new_status: str
    next_attempt_at: datetime | None
    recorded: bool


def handle_execution_failure(
    job: ClaimedJob, exc: Exception, *, jobs_repository: EvaluationJobsRepository
) -> FailureHandlingOutcome:
    """Classify `exc` and apply the resulting state transition to `job`'s
    PostgreSQL row, using exactly the `attempt_count` `job` already carries
    (captured once at claim time, never re-read or recomputed here -- the
    same fencing token `execute_job`'s own `mark_succeeded` call already
    relies on).
    """
    last_error = _bounded_last_error(exc)

    if not is_retryable(exc) or job.attempt_count >= job.max_retries:
        recorded = jobs_repository.mark_dead_letter(
            job_id=job.id, claimed_attempt_count=job.attempt_count, last_error=last_error
        )
        return FailureHandlingOutcome(
            new_status="dead_letter", next_attempt_at=None, recorded=recorded
        )

    next_attempt_at = compute_next_attempt_at(job.attempt_count)
    recorded = jobs_repository.mark_failed(
        job_id=job.id,
        claimed_attempt_count=job.attempt_count,
        next_attempt_at=next_attempt_at,
        last_error=last_error,
    )
    return FailureHandlingOutcome(
        new_status="failed", next_attempt_at=next_attempt_at, recorded=recorded
    )
