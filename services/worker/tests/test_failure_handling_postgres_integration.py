"""Real-PostgreSQL integration tests for worker.failure_handling.

Mirrors test_evaluation_jobs_postgres_integration.py's conventions
(fixed test database, truncate-before/after, skip if unreachable) --
proves the retryable/dead-letter transitions this phase adds actually take
effect against a real row, and that attempt_count fencing (already proven
for mark_succeeded/mark_failed/mark_dead_letter directly in that file)
holds when reached through handle_execution_failure specifically.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from worker.execution import SourceSpanNotFoundError
from worker.failure_handling import handle_execution_failure
from worker.postgres.repository import EvaluationJobsRepository

PG_TEST_DATABASE_URL = os.environ.get(
    "VIGIL_WORKER_TEST_DATABASE_URL",
    "postgresql://vigil:vigil@localhost:5434/vigil_test",
)
if not PG_TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith("test"):
    raise RuntimeError(
        "VIGIL_WORKER_TEST_DATABASE_URL does not point at a database named "
        "'*test' -- refusing to run destructive tests against it."
    )

_PG_TABLES = "evaluation_jobs, projects, organizations"


def _truncate_all() -> None:
    with psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True) as connection:
        connection.execute(f"TRUNCATE TABLE {_PG_TABLES} RESTART IDENTITY CASCADE")


@pytest.fixture
def real_pg_connection():
    try:
        probe = psycopg.connect(PG_TEST_DATABASE_URL, connect_timeout=2)
        probe.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"PostgreSQL not reachable at {PG_TEST_DATABASE_URL} ({exc}).")

    _truncate_all()
    connection = psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True)
    try:
        yield connection
    finally:
        connection.close()
        _truncate_all()


def _insert_project(connection) -> uuid.UUID:
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    unique = uuid.uuid4().hex[:8]
    connection.execute(
        "INSERT INTO organizations (id, name, slug) VALUES (%(id)s, %(name)s, %(slug)s)",
        {"id": org_id, "name": f"Org {unique}", "slug": f"org-{unique}"},
    )
    connection.execute(
        "INSERT INTO projects (id, organization_id, name, slug) "
        "VALUES (%(id)s, %(org_id)s, %(name)s, %(slug)s)",
        {
            "id": project_id,
            "org_id": org_id,
            "name": f"Project {unique}",
            "slug": f"project-{unique}",
        },
    )
    return project_id


def _insert_job(connection, *, project_id: uuid.UUID, max_retries: int = 3) -> uuid.UUID:
    job_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO evaluation_jobs
            (id, project_id, trace_id, span_id, evaluator_name, evaluator_version, max_retries)
        VALUES
            (%(id)s, %(project_id)s, %(trace_id)s, %(span_id)s, %(evaluator_name)s,
             %(evaluator_version)s, %(max_retries)s)
        """,
        {
            "id": job_id,
            "project_id": project_id,
            "trace_id": uuid.uuid4().hex,
            "span_id": uuid.uuid4().hex[:16],
            "evaluator_name": "relevance",
            "evaluator_version": "0.1.0",
            "max_retries": max_retries,
        },
    )
    return job_id


def _fetch_job(connection, job_id: uuid.UUID) -> dict:
    cursor = connection.execute(
        "SELECT status, attempt_count, next_attempt_at, last_error "
        "FROM evaluation_jobs WHERE id = %(id)s",
        {"id": job_id},
    )
    row = cursor.fetchone()
    return dict(zip(("status", "attempt_count", "next_attempt_at", "last_error"), row, strict=True))


def test_retryable_failure_leaves_job_failed_and_reclaimable(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, max_retries=3)

    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    [claimed] = jobs_repository.claim_jobs(worker_id="failure-handling-test", batch_size=1)
    assert claimed.attempt_count == 1

    before = datetime.now(UTC)
    outcome = handle_execution_failure(
        claimed, RuntimeError("transient ClickHouse hiccup"), jobs_repository=jobs_repository
    )

    assert outcome.new_status == "failed"
    assert outcome.recorded is True

    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "failed"
    assert row["attempt_count"] == 1
    assert row["next_attempt_at"] is not None
    assert row["next_attempt_at"] > before
    assert "transient ClickHouse hiccup" in row["last_error"]

    # Not yet due -> not claimable.
    assert jobs_repository.claim_jobs(worker_id="failure-handling-test", batch_size=10) == []


def test_dead_letter_when_retry_budget_exhausted(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, max_retries=1)

    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    [claimed] = jobs_repository.claim_jobs(worker_id="failure-handling-test", batch_size=1)
    assert claimed.attempt_count == 1  # already == max_retries=1

    outcome = handle_execution_failure(
        claimed, RuntimeError("still failing"), jobs_repository=jobs_repository
    )

    assert outcome.new_status == "dead_letter"

    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "dead_letter"
    assert row["next_attempt_at"] is None

    # Dead-lettered jobs are never re-claimed.
    assert jobs_repository.claim_jobs(worker_id="failure-handling-test", batch_size=10) == []


def test_permanent_failure_dead_letters_even_with_full_budget_remaining(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, max_retries=5)

    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    [claimed] = jobs_repository.claim_jobs(worker_id="failure-handling-test", batch_size=1)

    error = SourceSpanNotFoundError(
        project_id=project_id, trace_id=claimed.trace_id, span_id=claimed.span_id, job_id=job_id
    )
    outcome = handle_execution_failure(claimed, error, jobs_repository=jobs_repository)

    assert outcome.new_status == "dead_letter"
    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "dead_letter"
    assert (
        row["attempt_count"] == 1
    )  # far below max_retries=5 -- irrelevant for a permanent failure


def test_reclaimed_job_can_eventually_be_dead_lettered_across_retries(real_pg_connection) -> None:
    """Fencing under handle_execution_failure specifically, across two real
    claim cycles: fail once (-> failed, re-claimable), wait out the (tiny,
    test-only) backoff by claiming with next_attempt_at already in the
    past, fail again at the budget limit (-> dead_letter)."""
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, max_retries=2)
    jobs_repository = EvaluationJobsRepository(real_pg_connection)

    [first_claim] = jobs_repository.claim_jobs(worker_id="w1", batch_size=1)
    assert first_claim.attempt_count == 1
    handle_execution_failure(
        first_claim, RuntimeError("first failure"), jobs_repository=jobs_repository
    )
    assert _fetch_job(real_pg_connection, job_id)["status"] == "failed"

    # Force the backoff window open for the test instead of waiting.
    real_pg_connection.execute(
        "UPDATE evaluation_jobs SET next_attempt_at = now() - interval '1 second' "
        "WHERE id = %(id)s",
        {"id": job_id},
    )

    [second_claim] = jobs_repository.claim_jobs(worker_id="w1", batch_size=1)
    assert second_claim.attempt_count == 2  # incremented again by this second claim

    outcome = handle_execution_failure(
        second_claim, RuntimeError("second failure"), jobs_repository=jobs_repository
    )
    assert outcome.new_status == "dead_letter"
    assert _fetch_job(real_pg_connection, job_id)["status"] == "dead_letter"


def test_stale_attempt_cannot_overwrite_a_reaped_newer_attempt(real_pg_connection) -> None:
    """The exact race the fencing token exists to prevent, reached through
    handle_execution_failure this time (Phase 3B already proved this
    directly against mark_failed/mark_dead_letter)."""
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, max_retries=3)
    jobs_repository = EvaluationJobsRepository(real_pg_connection)

    [claimed] = jobs_repository.claim_jobs(worker_id="w1", batch_size=1)
    assert claimed.attempt_count == 1

    # Simulate a reaper resetting this job (not yet built) between claim
    # and this (now-stale) worker's failure-handling call.
    real_pg_connection.execute(
        "UPDATE evaluation_jobs SET status = 'dead_letter', attempt_count = 4 WHERE id = %(id)s",
        {"id": job_id},
    )

    outcome = handle_execution_failure(
        claimed, RuntimeError("stale worker's own failure"), jobs_repository=jobs_repository
    )

    assert outcome.recorded is False
    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "dead_letter"
    assert row["attempt_count"] == 4  # untouched by the stale attempt
