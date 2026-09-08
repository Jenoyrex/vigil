"""Repository-level tests for app.postgres.repository.EvaluationJobsRepository.

Uses `fake_postgres_connection` (see tests/conftest.py) instead of a real
PostgreSQL server -- these tests assert the exact SQL text and bound
parameters passed to `.execute(...)`, in particular that every completion
transition's WHERE clause includes the attempt_count fencing predicate
(docs/decisions/005-evaluation-job-storage-worker.md's Phase 3 amendment,
section 3) -- catching a regression in the SQL itself even without a live
server. Real-PostgreSQL concurrency/locking behavior (SKIP LOCKED, the
fencing invariant actually preventing a stale overwrite, updated_at
freshness) is proven separately in
test_evaluation_jobs_postgres_integration.py, which this module
deliberately does not attempt to substitute for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.postgres.repository import EvaluationJobsRepository

JOB_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
CREATED_AT = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


# -- claim_jobs -------------------------------------------------------------


def test_claim_jobs_passes_worker_id_and_batch_size(fake_postgres_connection) -> None:
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.claim_jobs(worker_id="worker-1", batch_size=5)

    assert fake_postgres_connection.last_params == {"worker_id": "worker-1", "batch_size": 5}


def test_claim_jobs_query_only_claims_pending_or_failed(fake_postgres_connection) -> None:
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.claim_jobs(worker_id="worker-1", batch_size=5)

    query = fake_postgres_connection.last_query
    assert "status IN ('pending', 'failed')" in query
    assert "next_attempt_at IS NULL OR next_attempt_at <= now()" in query


def test_claim_jobs_uses_for_update_skip_locked(fake_postgres_connection) -> None:
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.claim_jobs(worker_id="worker-1", batch_size=5)

    assert "FOR UPDATE SKIP LOCKED" in fake_postgres_connection.last_query


def test_claim_jobs_transitions_to_running_and_increments_attempt_count(
    fake_postgres_connection,
) -> None:
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.claim_jobs(worker_id="worker-1", batch_size=5)

    query = fake_postgres_connection.last_query
    assert "status = 'running'" in query
    assert "attempt_count = attempt_count + 1" in query
    assert "claimed_at = now()" in query
    assert "claimed_by = %(worker_id)s" in query


def test_claim_jobs_updates_updated_at(fake_postgres_connection) -> None:
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.claim_jobs(worker_id="worker-1", batch_size=5)
    assert "updated_at = now()" in fake_postgres_connection.last_query


def test_claim_jobs_maps_returned_rows_to_claimed_job(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(
        rows=[
            (
                JOB_ID,
                PROJECT_ID,
                TRACE_ID,
                SPAN_ID,
                "relevance_embedding",
                "0.1.0",
                1,
                3,
                CREATED_AT,
            )
        ],
    )
    repo = EvaluationJobsRepository(fake_postgres_connection)
    claimed = repo.claim_jobs(worker_id="worker-1", batch_size=5)

    assert len(claimed) == 1
    job = claimed[0]
    assert job.id == JOB_ID
    assert job.project_id == PROJECT_ID
    assert job.trace_id == TRACE_ID
    assert job.span_id == SPAN_ID
    assert job.evaluator_name == "relevance_embedding"
    assert job.evaluator_version == "0.1.0"
    assert job.attempt_count == 1
    assert job.max_retries == 3
    assert job.created_at == CREATED_AT


def test_claim_jobs_returns_empty_list_when_nothing_claimable(fake_postgres_connection) -> None:
    repo = EvaluationJobsRepository(fake_postgres_connection)
    assert repo.claim_jobs(worker_id="worker-1", batch_size=5) == []


# -- mark_succeeded / mark_failed / mark_dead_letter: fencing clause --------


def test_mark_succeeded_requires_running_status_and_matching_attempt_count(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.mark_succeeded(job_id=JOB_ID, claimed_attempt_count=1)

    query = fake_postgres_connection.last_query
    assert "status = 'running'" in query
    assert "attempt_count = %(claimed_attempt_count)s" in query
    assert "id = %(job_id)s" in query
    assert fake_postgres_connection.last_params == {
        "job_id": JOB_ID,
        "claimed_attempt_count": 1,
    }


def test_mark_succeeded_sets_terminal_status_and_updated_at(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.mark_succeeded(job_id=JOB_ID, claimed_attempt_count=1)

    query = fake_postgres_connection.last_query
    assert "status = 'succeeded'" in query
    assert "updated_at = now()" in query


def test_mark_succeeded_returns_true_when_one_row_affected(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    assert repo.mark_succeeded(job_id=JOB_ID, claimed_attempt_count=1) is True


def test_mark_succeeded_returns_false_when_stale(fake_postgres_connection) -> None:
    """A zero-row update means this attempt is stale -- the caller must
    treat this as 'drop the result', never raise or retry the statement
    itself."""
    fake_postgres_connection.queue_result(rowcount=0)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    assert repo.mark_succeeded(job_id=JOB_ID, claimed_attempt_count=1) is False


def test_mark_failed_requires_running_status_and_matching_attempt_count(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    next_attempt_at = datetime(2026, 9, 8, 12, 5, 0, tzinfo=UTC)
    repo.mark_failed(
        job_id=JOB_ID,
        claimed_attempt_count=2,
        next_attempt_at=next_attempt_at,
        last_error="evaluator timed out",
    )

    query = fake_postgres_connection.last_query
    assert "status = 'running'" in query
    assert "attempt_count = %(claimed_attempt_count)s" in query
    assert fake_postgres_connection.last_params == {
        "job_id": JOB_ID,
        "claimed_attempt_count": 2,
        "next_attempt_at": next_attempt_at,
        "last_error": "evaluator timed out",
    }


def test_mark_failed_sets_failed_status_with_next_attempt_at_and_error(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.mark_failed(
        job_id=JOB_ID,
        claimed_attempt_count=1,
        next_attempt_at=CREATED_AT,
        last_error="boom",
    )

    query = fake_postgres_connection.last_query
    assert "status = 'failed'" in query
    assert "next_attempt_at = %(next_attempt_at)s" in query
    assert "last_error = %(last_error)s" in query
    assert "updated_at = now()" in query


def test_mark_failed_returns_false_when_stale(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=0)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    result = repo.mark_failed(
        job_id=JOB_ID, claimed_attempt_count=1, next_attempt_at=CREATED_AT, last_error="boom"
    )
    assert result is False


def test_mark_dead_letter_requires_running_status_and_matching_attempt_count(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.mark_dead_letter(job_id=JOB_ID, claimed_attempt_count=3, last_error="retries exhausted")

    query = fake_postgres_connection.last_query
    assert "status = 'running'" in query
    assert "attempt_count = %(claimed_attempt_count)s" in query
    assert fake_postgres_connection.last_params == {
        "job_id": JOB_ID,
        "claimed_attempt_count": 3,
        "last_error": "retries exhausted",
    }


def test_mark_dead_letter_clears_next_attempt_at_and_sets_terminal_status(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    repo.mark_dead_letter(job_id=JOB_ID, claimed_attempt_count=3, last_error="retries exhausted")

    query = fake_postgres_connection.last_query
    assert "status = 'dead_letter'" in query
    assert "next_attempt_at = NULL" in query
    assert "updated_at = now()" in query


def test_mark_dead_letter_returns_false_when_stale(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=0)
    repo = EvaluationJobsRepository(fake_postgres_connection)
    assert repo.mark_dead_letter(job_id=JOB_ID, claimed_attempt_count=1, last_error="x") is False
