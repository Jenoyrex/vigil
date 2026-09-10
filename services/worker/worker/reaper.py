"""Stuck-job reaper: reclaims `evaluation_jobs` rows left `status='running'`
after the worker that claimed them crashes (or is killed, or loses
connectivity) mid-evaluation and never calls back into
`worker.postgres.repository.EvaluationJobsRepository.mark_succeeded`/
`mark_failed`/`mark_dead_letter`. Phase 3E's `worker/failure_handling.py`
already closed this gap for every *catchable* Python exception raised inside
`execute_job`; this module closes it for the one case that can't raise -- the
worker process itself dying mid-evaluation.

Deliberately a thin wrapper, not a new policy: `reap_stuck_jobs` applies the
identical attempt-budget boundary check and the identical
`worker.failure_handling.compute_next_attempt_at` backoff formula Phase 3E
already established for an ordinary caught failure, then calls the existing,
unmodified `mark_failed`/`mark_dead_letter` methods -- no second retry
policy, and no new SQL write statement anywhere in this module. The only new
SQL is `EvaluationJobsRepository.select_stuck_jobs`'s read.

**`attempt_count` is never incremented here.** `attempt_count` means "number
of execution attempts `claim_jobs` has started" -- `claim_jobs` is the ONLY
operation that increments it, and it does so *before* execution begins, not
after completion (`worker/postgres/repository.py`'s `_CLAIM_JOBS_SQL`). A job
stuck `running` has therefore already had its current attempt counted: the
claim that got it stuck already incremented `attempt_count`, whether or not
the worker that claimed it ever finishes. Reclaiming it here must not
increment `attempt_count` a second time -- doing so would double-charge the
retry budget against a single real attempt once this job is legitimately
re-claimed and `attempt_count` is incremented again by `claim_jobs`. This
mirrors `mark_failed`/`mark_dead_letter`, which also never touch
`attempt_count`. See docs/decisions/005-evaluation-job-storage-worker.md's
Phase 3F amendment for the full rationale.

Fencing against the stale worker this reaper exists to protect against relies
entirely on `mark_failed`/`mark_dead_letter`'s pre-existing
`WHERE status = 'running' AND attempt_count = claimed_attempt_count`
predicate (unmodified by this module): the reclaim's status transition away
from `'running'` alone is sufficient to make any later stale completion from
the crashed worker match zero rows, regardless of whether `attempt_count`
changed.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from worker.failure_handling import compute_next_attempt_at
from worker.postgres.repository import EvaluationJobsRepository, StuckJob

logger = logging.getLogger(__name__)

#: `last_error` must never be unbounded (ADR 004 section 6/8's existing
#: precedent, reaffirmed by `worker/failure_handling.py`) -- this module's
#: own diagnostic message is short and fixed-shape, so this cap is a
#: defensive match to that precedent, not something normally reached.
_MAX_LAST_ERROR_LENGTH = 2000


@dataclass(frozen=True)
class ReapedJobOutcome:
    """What `reap_stuck_jobs` did to one stuck job's row. `new_status` is
    the status this code *attempted* to transition to (`'failed'` or
    `'dead_letter'`), regardless of whether the fenced update actually took
    effect. `recorded` is `False` when `mark_failed`/`mark_dead_letter`
    matched zero rows -- meaning the original worker's own completion (a
    genuinely-alive worker that was merely slow, or finished at the exact
    moment the reaper ran) already resolved this job first. Per
    `EvaluationJobsRepository.mark_succeeded`'s documented convention, this
    is not an error and must never be raised or retried.
    """

    job_id: uuid.UUID
    new_status: str
    next_attempt_at: datetime | None
    recorded: bool


def _bounded_last_error(stuck_job: StuckJob, *, stuck_threshold_seconds: float) -> str:
    text = (
        f"stuck-job reaper: job claimed by {stuck_job.claimed_by!r} at "
        f"{stuck_job.claimed_at.isoformat()} exceeded "
        f"stuck_job_threshold_seconds={stuck_threshold_seconds!r} without a completion report"
    )
    if len(text) <= _MAX_LAST_ERROR_LENGTH:
        return text
    return text[:_MAX_LAST_ERROR_LENGTH]


def _reap_one(
    stuck_job: StuckJob,
    *,
    jobs_repository: EvaluationJobsRepository,
    stuck_threshold_seconds: float,
) -> ReapedJobOutcome:
    """Apply the exact same attempt-budget boundary check
    `worker.failure_handling.handle_execution_failure` applies to an
    ordinary caught failure, using `stuck_job.attempt_count` exactly as
    `select_stuck_jobs` returned it -- never recomputed or incremented.
    """
    last_error = _bounded_last_error(stuck_job, stuck_threshold_seconds=stuck_threshold_seconds)

    if stuck_job.attempt_count >= stuck_job.max_retries:
        recorded = jobs_repository.mark_dead_letter(
            job_id=stuck_job.id,
            claimed_attempt_count=stuck_job.attempt_count,
            last_error=last_error,
        )
        return ReapedJobOutcome(
            job_id=stuck_job.id, new_status="dead_letter", next_attempt_at=None, recorded=recorded
        )

    next_attempt_at = compute_next_attempt_at(stuck_job.attempt_count)
    recorded = jobs_repository.mark_failed(
        job_id=stuck_job.id,
        claimed_attempt_count=stuck_job.attempt_count,
        next_attempt_at=next_attempt_at,
        last_error=last_error,
    )
    return ReapedJobOutcome(
        job_id=stuck_job.id,
        new_status="failed",
        next_attempt_at=next_attempt_at,
        recorded=recorded,
    )


def reap_stuck_jobs(
    *,
    jobs_repository: EvaluationJobsRepository,
    stuck_threshold_seconds: float,
    batch_size: int,
) -> list[ReapedJobOutcome]:
    """Find up to `batch_size` jobs stuck `running` past
    `stuck_threshold_seconds` and reclaim each one: `dead_letter` if its
    already-claimed `attempt_count` has reached `max_retries` (this
    abandoned attempt was the last one allowed), otherwise `failed` with
    `next_attempt_at` computed by the same backoff formula an ordinary
    caught failure uses. One job's repository call raising is caught and
    logged, never allowed to abort the rest of the batch -- the same
    per-job isolation `worker.dispatcher.Dispatcher` already applies to
    `execute_job` failures.
    """
    stuck_jobs = jobs_repository.select_stuck_jobs(
        stuck_threshold_seconds=stuck_threshold_seconds, batch_size=batch_size
    )

    outcomes: list[ReapedJobOutcome] = []
    for stuck_job in stuck_jobs:
        try:
            outcomes.append(
                _reap_one(
                    stuck_job,
                    jobs_repository=jobs_repository,
                    stuck_threshold_seconds=stuck_threshold_seconds,
                )
            )
        except Exception:  # noqa: BLE001 -- one job's reclaim failing (e.g. a
            # transient PostgreSQL error on this specific UPDATE) must never
            # abort the rest of the batch; the row is simply left `running`
            # and will be picked up again on the reaper's next tick.
            logger.exception("Failed to reap stuck job %s", stuck_job.id)
    return outcomes
