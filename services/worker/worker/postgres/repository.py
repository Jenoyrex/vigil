"""Data-access layer for `evaluation_jobs` lifecycle mechanics: atomic
claiming and the two terminal completion transitions.

Per docs/decisions/005-evaluation-job-storage-worker.md section 6 and the
approved Phase 3 plan section 2/3, this module owns exactly the
worker-owned execution mechanics of a job that already exists -- claiming
it, and recording that a claimed attempt finished (successfully or not). It
does not decide whether a job should exist (that is `apps/api`'s
job-creation endpoint, not yet built) and it does not compute retry
backoff or promote a job to `dead_letter` on its own initiative (both are
the not-yet-built dispatch layer's job); this module only executes the
state transition it is explicitly told to make, with an explicit
`last_error`/`next_attempt_at` supplied by that future caller.

Every method here is exactly one SQL statement, executed against a
connection the caller owns and passes in (mirroring
`worker.clickhouse.repository.EvaluationResultsRepository`'s
`__init__(self, client)` shape) -- this module never opens its own
connection, so a test can pass a connection with whatever transaction
semantics a given scenario needs (see tests/test_evaluation_jobs_postgres_integration.py's
held-lock concurrency test, which deliberately uses a non-autocommit
connection for one side of the race).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ClaimedJob:
    """One row `claim_jobs` claimed, per docs/decisions/005... section 1's
    job identity plus what the dispatch layer needs to run it. `created_at`
    is returned here because it becomes `evaluation_results.job_created_at`
    verbatim once a result is persisted (Phase 2/ADR 005 section 5) --
    immutable across retries, so it must be read at claim time, not
    recomputed later.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    trace_id: str
    span_id: str
    evaluator_name: str
    evaluator_version: str
    attempt_count: int
    max_retries: int
    created_at: datetime


# Column order the claim query's RETURNING clause produces -- kept as one
# named tuple so `claim_jobs` and its tests agree on it in exactly one
# place, the same discipline `RESULT_COLUMNS`/`SPAN_COLUMNS` already
# establish for the ClickHouse repositories.
_CLAIM_RETURNING_COLUMNS = (
    "id",
    "project_id",
    "trace_id",
    "span_id",
    "evaluator_name",
    "evaluator_version",
    "attempt_count",
    "max_retries",
    "created_at",
)

_CLAIM_JOBS_SQL = f"""
    UPDATE evaluation_jobs
    SET
        status = 'running',
        claimed_at = now(),
        claimed_by = %(worker_id)s,
        attempt_count = attempt_count + 1,
        updated_at = now()
    WHERE id IN (
        SELECT id
        FROM evaluation_jobs
        WHERE status IN ('pending', 'failed')
          AND (next_attempt_at IS NULL OR next_attempt_at <= now())
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT %(batch_size)s
    )
    RETURNING
        {", ".join(_CLAIM_RETURNING_COLUMNS)}
"""

# The attempt_count fencing invariant (docs/decisions/005... Phase 3 plan
# section 3): a completion transition only ever takes effect if the row is
# still owned by the exact attempt that is completing it. If a stuck-job
# reaper (not yet built) has since reset this row -- incrementing
# attempt_count and moving it back to 'failed' or on to 'dead_letter' --
# this WHERE clause matches zero rows, and the caller must treat that as
# "this attempt's result is stale, drop it" rather than raise or retry the
# statement.
_MARK_SUCCEEDED_SQL = """
    UPDATE evaluation_jobs
    SET status = 'succeeded',
        updated_at = now()
    WHERE id = %(job_id)s
      AND status = 'running'
      AND attempt_count = %(claimed_attempt_count)s
"""

_MARK_FAILED_SQL = """
    UPDATE evaluation_jobs
    SET status = 'failed',
        next_attempt_at = %(next_attempt_at)s,
        last_error = %(last_error)s,
        updated_at = now()
    WHERE id = %(job_id)s
      AND status = 'running'
      AND attempt_count = %(claimed_attempt_count)s
"""

_MARK_DEAD_LETTER_SQL = """
    UPDATE evaluation_jobs
    SET status = 'dead_letter',
        next_attempt_at = NULL,
        last_error = %(last_error)s,
        updated_at = now()
    WHERE id = %(job_id)s
      AND status = 'running'
      AND attempt_count = %(claimed_attempt_count)s
"""


class EvaluationJobsRepository:
    def __init__(self, connection) -> None:
        self._connection = connection

    def claim_jobs(self, *, worker_id: str, batch_size: int) -> list[ClaimedJob]:
        """Atomically claim up to `batch_size` due `pending`/`failed` jobs
        for `worker_id`, `SKIP LOCKED` so two concurrent claimers (this
        worker's next tick, or a different worker process entirely) never
        return overlapping rows. One statement: the row-selection lock
        acquisition and the `running` transition happen together, so there
        is no window between "observed as claimable" and "marked running"
        for a race to open in.
        """
        cursor = self._connection.execute(
            _CLAIM_JOBS_SQL, {"worker_id": worker_id, "batch_size": batch_size}
        )
        return [
            ClaimedJob(**dict(zip(_CLAIM_RETURNING_COLUMNS, row, strict=True)))
            for row in cursor.fetchall()
        ]

    def mark_succeeded(self, *, job_id: uuid.UUID, claimed_attempt_count: int) -> bool:
        """Transition a claimed job to its terminal `succeeded` state.
        Returns `False` (zero rows affected) if this attempt is stale --
        the caller must not treat that as an error, only as "drop this
        result, someone else now owns this job."
        """
        cursor = self._connection.execute(
            _MARK_SUCCEEDED_SQL,
            {"job_id": job_id, "claimed_attempt_count": claimed_attempt_count},
        )
        return cursor.rowcount == 1

    def mark_failed(
        self,
        *,
        job_id: uuid.UUID,
        claimed_attempt_count: int,
        next_attempt_at: datetime,
        last_error: str,
    ) -> bool:
        """Transition a claimed job back to `failed` (re-claimable once
        `next_attempt_at` elapses) -- the retryable outcome. Backoff
        calculation is the caller's responsibility (not yet built); this
        method only stores whatever `next_attempt_at` it is given. Returns
        `False` if this attempt is stale, per `mark_succeeded`'s docstring.
        """
        cursor = self._connection.execute(
            _MARK_FAILED_SQL,
            {
                "job_id": job_id,
                "claimed_attempt_count": claimed_attempt_count,
                "next_attempt_at": next_attempt_at,
                "last_error": last_error,
            },
        )
        return cursor.rowcount == 1

    def mark_dead_letter(
        self, *, job_id: uuid.UUID, claimed_attempt_count: int, last_error: str
    ) -> bool:
        """Transition a claimed job to the terminal `dead_letter` state --
        the caller has already determined retries are exhausted (not this
        method's decision). `next_attempt_at` is always cleared to `NULL`:
        a dead-lettered job is never re-claimed. Returns `False` if this
        attempt is stale, per `mark_succeeded`'s docstring.
        """
        cursor = self._connection.execute(
            _MARK_DEAD_LETTER_SQL,
            {
                "job_id": job_id,
                "claimed_attempt_count": claimed_attempt_count,
                "last_error": last_error,
            },
        )
        return cursor.rowcount == 1
