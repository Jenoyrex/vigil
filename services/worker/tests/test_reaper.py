"""Unit tests for worker.reaper.reap_stuck_jobs.

Most cases use the real `EvaluationJobsRepository` wrapped around
`fake_postgres_connection` (see tests/conftest.py) -- the same pattern
test_failure_handling.py already uses for `handle_execution_failure` -- so
these tests assert the exact SQL/parameters `mark_failed`/`mark_dead_letter`
actually receive, not just that *some* method was called. The one exception
is `test_one_jobs_repository_failure_does_not_abort_the_batch`, which needs a
repository double that fails selectively per job -- something
`fake_postgres_connection`'s single-queue design can't express.

Real-PostgreSQL concurrency/fencing behavior (SKIP LOCKED batch partitioning,
the fencing invariant actually rejecting a stale worker's completion) is
proven separately in test_reaper_postgres_integration.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from worker.postgres.repository import EvaluationJobsRepository, StuckJob
from worker.reaper import ReapedJobOutcome, reap_stuck_jobs

JOB_ID = uuid.uuid4()
JOB_ID_2 = uuid.uuid4()
CLAIMED_AT = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


def _queue_stuck_job(
    fake_postgres_connection,
    *,
    job_id: uuid.UUID = JOB_ID,
    attempt_count: int,
    max_retries: int = 3,
    claimed_by: str = "worker-a",
    claimed_at: datetime = CLAIMED_AT,
) -> None:
    fake_postgres_connection.queue_result(
        rows=[(job_id, attempt_count, max_retries, claimed_by, claimed_at)],
    )


# -- no stuck jobs -----------------------------------------------------------


def test_no_stuck_jobs_returns_empty_list_and_writes_nothing(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rows=[])
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=50
    )

    assert outcomes == []
    assert len(fake_postgres_connection.calls) == 1  # only the SELECT, no writes


# -- retryable stuck job: budget remains --------------------------------------


def test_retryable_stuck_job_calls_mark_failed_with_backoff(fake_postgres_connection) -> None:
    _queue_stuck_job(fake_postgres_connection, attempt_count=1, max_retries=3)
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=50
    )

    query = fake_postgres_connection.last_query
    assert "status = 'failed'" in query
    params = fake_postgres_connection.last_params
    assert params["job_id"] == JOB_ID
    assert params["claimed_attempt_count"] == 1
    assert params["next_attempt_at"] > datetime.now(UTC)
    assert "worker-a" in params["last_error"]
    assert "900.0" in params["last_error"]

    assert outcomes == [
        ReapedJobOutcome(
            job_id=JOB_ID,
            new_status="failed",
            next_attempt_at=params["next_attempt_at"],
            recorded=True,
        )
    ]


def test_retryable_stuck_job_never_calls_mark_dead_letter_while_budget_remains(
    fake_postgres_connection,
) -> None:
    _queue_stuck_job(fake_postgres_connection, attempt_count=1, max_retries=3)
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    reap_stuck_jobs(jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=50)

    assert "dead_letter" not in fake_postgres_connection.last_query


# -- attempt_count is never incremented ---------------------------------------


def test_reap_never_increments_attempt_count(fake_postgres_connection) -> None:
    """The LOCKED decision: claim_jobs is the only operation that ever
    increments attempt_count. The reaper must pass the stuck job's
    already-claimed attempt_count straight through, unchanged."""
    _queue_stuck_job(fake_postgres_connection, attempt_count=2, max_retries=3)
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    reap_stuck_jobs(jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=50)

    assert fake_postgres_connection.last_params["claimed_attempt_count"] == 2


# -- exhausted retry budget: dead_letter --------------------------------------


def test_stuck_job_at_max_retries_boundary_calls_mark_dead_letter(fake_postgres_connection) -> None:
    """max_retries is a TOTAL attempt cap: attempt_count == max_retries is
    already the last allowed attempt (Phase 3E semantics, unchanged)."""
    _queue_stuck_job(fake_postgres_connection, attempt_count=3, max_retries=3)
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=50
    )

    query = fake_postgres_connection.last_query
    assert "status = 'dead_letter'" in query
    assert "next_attempt_at = NULL" in query
    assert outcomes == [
        ReapedJobOutcome(
            job_id=JOB_ID, new_status="dead_letter", next_attempt_at=None, recorded=True
        )
    ]


def test_max_retries_one_dead_letters_directly_with_no_retry_offered(
    fake_postgres_connection,
) -> None:
    """max_retries=1 boundary case: one crashed attempt is already the only
    attempt allowed -- straight to dead_letter, never back to 'failed'."""
    _queue_stuck_job(fake_postgres_connection, attempt_count=1, max_retries=1)
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=50
    )

    assert outcomes[0].new_status == "dead_letter"
    assert "status = 'failed'" not in fake_postgres_connection.last_query


# -- batch with mixed outcomes -------------------------------------------------


def test_batch_mixes_retryable_and_exhausted_jobs_independently(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(
        rows=[
            (JOB_ID, 1, 3, "worker-a", CLAIMED_AT),
            (JOB_ID_2, 3, 3, "worker-b", CLAIMED_AT),
        ]
    )
    fake_postgres_connection.queue_result(rowcount=1)  # JOB_ID's mark_failed
    fake_postgres_connection.queue_result(rowcount=1)  # JOB_ID_2's mark_dead_letter
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=50
    )

    assert len(outcomes) == 2
    assert outcomes[0].job_id == JOB_ID
    assert outcomes[0].new_status == "failed"
    assert outcomes[1].job_id == JOB_ID_2
    assert outcomes[1].new_status == "dead_letter"


# -- lost race to the original worker: not an error ---------------------------


def test_stale_reclaim_reports_recorded_false_and_is_not_raised(fake_postgres_connection) -> None:
    """The original worker turned out to be alive (merely slow) and
    resolved the job itself first -- mark_failed matches zero rows. This
    must never raise; it's reported via recorded=False, same convention
    FailureHandlingOutcome already establishes."""
    _queue_stuck_job(fake_postgres_connection, attempt_count=1, max_retries=3)
    fake_postgres_connection.queue_result(rowcount=0)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=50
    )

    assert len(outcomes) == 1
    assert outcomes[0].recorded is False


# -- one job's repository failure does not abort the batch -------------------


class _SelectivelyFailingRepository:
    """Minimal EvaluationJobsRepository double: mark_failed raises for one
    specific job_id, succeeds for any other. fake_postgres_connection's
    single-response-queue design can't express "fail on job A, succeed on
    job B" within one batch, so this test needs its own double.
    """

    def __init__(self, stuck_jobs: list[StuckJob], *, failing_job_id: uuid.UUID) -> None:
        self._stuck_jobs = stuck_jobs
        self._failing_job_id = failing_job_id
        self.mark_failed_calls: list[uuid.UUID] = []

    def select_stuck_jobs(
        self, *, stuck_threshold_seconds: float, batch_size: int
    ) -> list[StuckJob]:
        return self._stuck_jobs

    def mark_failed(self, *, job_id, claimed_attempt_count, next_attempt_at, last_error) -> bool:
        if job_id == self._failing_job_id:
            raise RuntimeError("PostgreSQL unreachable")
        self.mark_failed_calls.append(job_id)
        return True

    def mark_dead_letter(self, *, job_id, claimed_attempt_count, last_error) -> bool:
        raise AssertionError("not expected in this test")


def test_one_jobs_repository_failure_does_not_abort_the_batch() -> None:
    failing_job_id = JOB_ID
    ok_job_id = JOB_ID_2
    stuck_jobs = [
        StuckJob(
            id=failing_job_id,
            attempt_count=1,
            max_retries=3,
            claimed_by="worker-a",
            claimed_at=CLAIMED_AT,
        ),
        StuckJob(
            id=ok_job_id,
            attempt_count=1,
            max_retries=3,
            claimed_by="worker-b",
            claimed_at=CLAIMED_AT,
        ),
    ]
    repository = _SelectivelyFailingRepository(stuck_jobs, failing_job_id=failing_job_id)

    outcomes = reap_stuck_jobs(
        jobs_repository=repository, stuck_threshold_seconds=900.0, batch_size=50
    )

    assert len(outcomes) == 1
    assert outcomes[0].job_id == ok_job_id
    assert repository.mark_failed_calls == [ok_job_id]
