"""Unit tests for worker.failure_handling.

`is_retryable`/`compute_next_attempt_at` are pure functions, tested directly.
`handle_execution_failure` is tested against the real `EvaluationJobsRepository`
wrapped around `fake_postgres_connection` (see tests/conftest.py) -- the same
pattern test_evaluation_jobs_repository.py already uses -- so these tests
assert the exact SQL/parameters `mark_failed`/`mark_dead_letter` actually
receive, not just that *some* method was called.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from app.types import InvalidEvaluatorInputError
from clickhouse_connect.driver.exceptions import OperationalError

from worker.clickhouse.repository import ClickHouseInsertError, ClickHouseUnavailableError
from worker.execution import SourceSpanNotFoundError
from worker.failure_handling import (
    FailureHandlingOutcome,
    compute_next_attempt_at,
    handle_execution_failure,
    is_retryable,
)
from worker.postgres.repository import ClaimedJob, EvaluationJobsRepository
from worker.registry import UnknownEvaluatorError

JOB_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
CREATED_AT = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


def _job(*, attempt_count: int, max_retries: int = 3) -> ClaimedJob:
    return ClaimedJob(
        id=JOB_ID,
        project_id=PROJECT_ID,
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        evaluator_name="relevance",
        evaluator_version="0.1.0",
        attempt_count=attempt_count,
        max_retries=max_retries,
        created_at=CREATED_AT,
    )


def _missing_span_error() -> SourceSpanNotFoundError:
    return SourceSpanNotFoundError(
        project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID, job_id=JOB_ID
    )


# -- is_retryable -------------------------------------------------------


def test_source_span_not_found_is_permanent() -> None:
    assert is_retryable(_missing_span_error()) is False


def test_invalid_evaluator_input_is_permanent() -> None:
    assert is_retryable(InvalidEvaluatorInputError("bad shape")) is False


def test_unknown_evaluator_is_retryable() -> None:
    assert is_retryable(UnknownEvaluatorError("no such evaluator")) is True


@pytest.mark.parametrize(
    "exc",
    [
        ClickHouseUnavailableError("connection refused"),
        ClickHouseInsertError("type mismatch"),
        OperationalError("timeout"),
    ],
)
def test_infrastructure_failures_are_retryable(exc: Exception) -> None:
    assert is_retryable(exc) is True


def test_generic_evaluator_exception_is_retryable() -> None:
    assert is_retryable(RuntimeError("evaluator blew up")) is True


def test_unexpected_exception_type_defaults_to_retryable() -> None:
    class SomeFutureExceptionNoOneHasClassifiedYet(Exception):
        pass

    assert is_retryable(SomeFutureExceptionNoOneHasClassifiedYet("???")) is True


# -- compute_next_attempt_at ----------------------------------------------


@pytest.mark.parametrize(
    ("attempt_count", "expected_base_delay"),
    [(1, 5.0), (2, 10.0), (3, 20.0), (4, 40.0)],
)
def test_backoff_doubles_per_attempt_starting_from_base(
    attempt_count: int, expected_base_delay: float
) -> None:
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    with patch("worker.failure_handling.random.uniform", return_value=0.0):
        next_attempt_at = compute_next_attempt_at(attempt_count, now=now)
    assert next_attempt_at == now + timedelta(seconds=expected_base_delay)


def test_backoff_adds_random_jitter() -> None:
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    with patch("worker.failure_handling.random.uniform", return_value=1.5) as mock_uniform:
        next_attempt_at = compute_next_attempt_at(1, now=now)
    mock_uniform.assert_called_once_with(0, 2.0)
    assert next_attempt_at == now + timedelta(seconds=5.0 + 1.5)


def test_backoff_is_capped_at_max_delay_before_jitter() -> None:
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    with patch("worker.failure_handling.random.uniform", return_value=0.0):
        next_attempt_at = compute_next_attempt_at(20, now=now)  # 5 * 2**19 is enormous
    assert next_attempt_at == now + timedelta(seconds=300.0)


def test_backoff_jitter_is_never_negative_or_unbounded() -> None:
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    next_attempt_at = compute_next_attempt_at(1, now=now)
    delay = (next_attempt_at - now).total_seconds()
    assert 5.0 <= delay <= 5.0 + 2.0


# -- handle_execution_failure: retryable, budget remains -------------------


def test_retryable_failure_with_budget_remaining_calls_mark_failed(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)
    job = _job(attempt_count=1, max_retries=3)

    outcome = handle_execution_failure(
        job, RuntimeError("transient"), jobs_repository=jobs_repository
    )

    query = fake_postgres_connection.last_query
    assert "status = 'failed'" in query
    params = fake_postgres_connection.last_params
    assert params["job_id"] == JOB_ID
    assert params["claimed_attempt_count"] == 1
    assert "RuntimeError" in params["last_error"]
    assert "transient" in params["last_error"]

    assert outcome == FailureHandlingOutcome(
        new_status="failed", next_attempt_at=params["next_attempt_at"], recorded=True
    )


def test_retryable_failure_never_calls_mark_dead_letter_while_budget_remains(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)
    handle_execution_failure(
        _job(attempt_count=1, max_retries=3), RuntimeError("x"), jobs_repository=jobs_repository
    )
    assert "dead_letter" not in fake_postgres_connection.last_query


# -- handle_execution_failure: retry budget exhausted -----------------------


def test_retryable_failure_with_budget_exhausted_calls_mark_dead_letter(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)
    job = _job(attempt_count=3, max_retries=3)

    outcome = handle_execution_failure(job, RuntimeError("x"), jobs_repository=jobs_repository)

    query = fake_postgres_connection.last_query
    assert "status = 'dead_letter'" in query
    assert "next_attempt_at = NULL" in query
    params = fake_postgres_connection.last_params
    assert params["job_id"] == JOB_ID
    assert params["claimed_attempt_count"] == 3

    assert outcome == FailureHandlingOutcome(
        new_status="dead_letter", next_attempt_at=None, recorded=True
    )


def test_total_attempt_cap_is_inclusive_not_exclusive(fake_postgres_connection) -> None:
    """max_retries is a TOTAL attempt cap: attempt_count == max_retries is
    already the last allowed attempt, per evaluation_job.py's own docstring."""
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    # One below the cap: still retryable.
    handle_execution_failure(
        _job(attempt_count=2, max_retries=3), RuntimeError("x"), jobs_repository=jobs_repository
    )
    assert "status = 'failed'" in fake_postgres_connection.last_query

    # At the cap: dead-lettered.
    fake_postgres_connection.queue_result(rowcount=1)
    handle_execution_failure(
        _job(attempt_count=3, max_retries=3), RuntimeError("x"), jobs_repository=jobs_repository
    )
    assert "status = 'dead_letter'" in fake_postgres_connection.last_query


# -- handle_execution_failure: permanent failures ---------------------------


def test_permanent_failure_dead_letters_immediately_on_first_attempt(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)
    job = _job(attempt_count=1, max_retries=3)  # budget nowhere near exhausted

    outcome = handle_execution_failure(job, _missing_span_error(), jobs_repository=jobs_repository)

    assert "status = 'dead_letter'" in fake_postgres_connection.last_query
    assert outcome.new_status == "dead_letter"
    assert outcome.next_attempt_at is None


def test_invalid_evaluator_input_dead_letters_immediately(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)
    job = _job(attempt_count=1, max_retries=3)

    outcome = handle_execution_failure(
        job, InvalidEvaluatorInputError("wrong shape"), jobs_repository=jobs_repository
    )

    assert "status = 'dead_letter'" in fake_postgres_connection.last_query
    assert outcome.new_status == "dead_letter"


# -- last_error bounding -----------------------------------------------------


def test_last_error_is_bounded_length(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=1)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)
    huge_message = "x" * 10_000

    handle_execution_failure(
        _job(attempt_count=1), RuntimeError(huge_message), jobs_repository=jobs_repository
    )

    last_error = fake_postgres_connection.last_params["last_error"]
    assert len(last_error) <= 2000


# -- stale fenced update -----------------------------------------------------


def test_stale_fenced_update_is_not_raised_and_reports_recorded_false(
    fake_postgres_connection,
) -> None:
    fake_postgres_connection.queue_result(rowcount=0)  # simulates a reaped/newer attempt
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    outcome = handle_execution_failure(
        _job(attempt_count=1), RuntimeError("x"), jobs_repository=jobs_repository
    )

    assert outcome.recorded is False


def test_stale_fenced_dead_letter_update_is_not_raised(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rowcount=0)
    jobs_repository = EvaluationJobsRepository(fake_postgres_connection)

    outcome = handle_execution_failure(
        _job(attempt_count=1), _missing_span_error(), jobs_repository=jobs_repository
    )

    assert outcome.recorded is False
