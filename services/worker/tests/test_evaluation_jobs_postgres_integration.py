"""Real-PostgreSQL integration tests for
app.postgres.repository.EvaluationJobsRepository.

These are the tests that actually matter for this milestone's acceptance
criterion: SKIP LOCKED concurrency correctness and the attempt_count
fencing invariant are properties of real row-locking behavior that no fake
connection can demonstrate (test_evaluation_jobs_repository.py's fake-based
tests only prove the SQL text/parameters are correct, never that PostgreSQL
actually enforces what that SQL claims to enforce).

Runs against the same dedicated `vigil_test` database
apps/api/tests/conftest.py already uses (same schema, migrated by
apps/api's Alembic setup) -- skipped automatically if unreachable, mirroring
test_evaluation_results_clickhouse_integration.py's convention for the
analogous ClickHouse case. Never runs against the development database; the
same "database name must end in 'test'" guard apps/api/tests/conftest.py
uses is repeated here independently (duplicate rather than centralize).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.postgres.repository import EvaluationJobsRepository

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

EVALUATOR_NAME = "relevance_embedding"
EVALUATOR_VERSION = "0.1.0"


def _truncate_all() -> None:
    with psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True) as connection:
        connection.execute(f"TRUNCATE TABLE {_PG_TABLES} RESTART IDENTITY CASCADE")


@pytest.fixture
def real_pg_connection():
    """One autocommit connection against the dedicated test database, with
    all relevant tables truncated before and after -- same isolation
    convention apps/api/tests/conftest.py's `db_session` fixture uses.
    """
    try:
        probe = psycopg.connect(PG_TEST_DATABASE_URL, connect_timeout=2)
        probe.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(
            f"PostgreSQL not reachable at {PG_TEST_DATABASE_URL} ({exc}); "
            "start it via infrastructure/docker-compose.yml to run this test."
        )

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


def _insert_job(
    connection,
    *,
    project_id: uuid.UUID,
    status: str = "pending",
    attempt_count: int = 0,
    max_retries: int = 3,
    next_attempt_at: datetime | None = None,
    created_at: datetime | None = None,
    evaluator_name: str = EVALUATOR_NAME,
    evaluator_version: str = EVALUATOR_VERSION,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    span_id = uuid.uuid4().hex[:16]
    params = {
        "id": job_id,
        "project_id": project_id,
        "trace_id": uuid.uuid4().hex,
        "span_id": span_id,
        "evaluator_name": evaluator_name,
        "evaluator_version": evaluator_version,
        "status": status,
        "attempt_count": attempt_count,
        "max_retries": max_retries,
        "next_attempt_at": next_attempt_at,
    }
    if created_at is not None:
        params["created_at"] = created_at
        connection.execute(
            """
            INSERT INTO evaluation_jobs
                (id, project_id, trace_id, span_id, evaluator_name, evaluator_version,
                 status, attempt_count, max_retries, next_attempt_at, created_at)
            VALUES
                (%(id)s, %(project_id)s, %(trace_id)s, %(span_id)s, %(evaluator_name)s,
                 %(evaluator_version)s, %(status)s, %(attempt_count)s, %(max_retries)s,
                 %(next_attempt_at)s, %(created_at)s)
            """,
            params,
        )
    else:
        connection.execute(
            """
            INSERT INTO evaluation_jobs
                (id, project_id, trace_id, span_id, evaluator_name, evaluator_version,
                 status, attempt_count, max_retries, next_attempt_at)
            VALUES
                (%(id)s, %(project_id)s, %(trace_id)s, %(span_id)s, %(evaluator_name)s,
                 %(evaluator_version)s, %(status)s, %(attempt_count)s, %(max_retries)s,
                 %(next_attempt_at)s)
            """,
            params,
        )
    return job_id


def _fetch_job(connection, job_id: uuid.UUID) -> dict:
    cursor = connection.execute(
        "SELECT id, project_id, trace_id, span_id, evaluator_name, evaluator_version, status, "
        "attempt_count, max_retries, next_attempt_at, claimed_at, claimed_by, last_error, "
        "created_at, updated_at "
        "FROM evaluation_jobs WHERE id = %(id)s",
        {"id": job_id},
    )
    columns = [
        "id",
        "project_id",
        "trace_id",
        "span_id",
        "evaluator_name",
        "evaluator_version",
        "status",
        "attempt_count",
        "max_retries",
        "next_attempt_at",
        "claimed_at",
        "claimed_by",
        "last_error",
        "created_at",
        "updated_at",
    ]
    row = cursor.fetchone()
    return dict(zip(columns, row, strict=True))


# -- claimability by status/next_attempt_at ---------------------------------


def test_pending_and_due_failed_jobs_are_claimable(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    pending_id = _insert_job(real_pg_connection, project_id=project_id, status="pending")
    failed_no_backoff_id = _insert_job(
        real_pg_connection, project_id=project_id, status="failed", next_attempt_at=None
    )
    failed_due_id = _insert_job(
        real_pg_connection,
        project_id=project_id,
        status="failed",
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    repo = EvaluationJobsRepository(real_pg_connection)
    claimed_ids = {job.id for job in repo.claim_jobs(worker_id="w1", batch_size=10)}

    assert claimed_ids == {pending_id, failed_no_backoff_id, failed_due_id}


def test_future_next_attempt_at_is_not_claimable(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    not_due_id = _insert_job(
        real_pg_connection,
        project_id=project_id,
        status="failed",
        next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
    )

    repo = EvaluationJobsRepository(real_pg_connection)
    claimed_ids = {job.id for job in repo.claim_jobs(worker_id="w1", batch_size=10)}

    assert not_due_id not in claimed_ids
    assert claimed_ids == set()


@pytest.mark.parametrize("status", ["running", "succeeded", "dead_letter"])
def test_non_pending_failed_statuses_are_not_claimable(real_pg_connection, status: str) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, status=status)

    repo = EvaluationJobsRepository(real_pg_connection)
    claimed_ids = {job.id for job in repo.claim_jobs(worker_id="w1", batch_size=10)}

    assert job_id not in claimed_ids


# -- attempt_count / RETURNING data ------------------------------------------


def test_claim_increments_attempt_count(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, attempt_count=0)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="w1", batch_size=10)

    assert claimed.attempt_count == 1
    assert _fetch_job(real_pg_connection, job_id)["attempt_count"] == 1


def test_returning_data_matches_inserted_job(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(
        real_pg_connection,
        project_id=project_id,
        max_retries=7,
        evaluator_name="relevance",
        evaluator_version="0.2.0",
    )
    inserted = _fetch_job(real_pg_connection, job_id)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="w1", batch_size=10)

    assert claimed.id == job_id
    assert claimed.project_id == project_id
    assert claimed.trace_id == inserted["trace_id"]
    assert claimed.span_id == inserted["span_id"]
    assert claimed.evaluator_name == "relevance"
    assert claimed.evaluator_version == "0.2.0"
    assert claimed.max_retries == 7
    assert claimed.created_at == inserted["created_at"]


def test_claim_sets_claimed_at_and_claimed_by(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)

    repo = EvaluationJobsRepository(real_pg_connection)
    repo.claim_jobs(worker_id="worker-xyz", batch_size=10)

    row = _fetch_job(real_pg_connection, job_id)
    assert row["claimed_by"] == "worker-xyz"
    assert row["claimed_at"] is not None
    assert row["status"] == "running"


# -- concurrency: two claimers can never claim the same job ------------------


def test_concurrent_claim_never_returns_overlapping_jobs_under_a_held_lock(
    real_pg_connection,
) -> None:
    """Deterministic mechanism proof: while one connection holds the claim
    statement's row lock open (uncommitted), a second, fully concurrent
    claim must skip that row entirely and only claim what remains -- this
    is SKIP LOCKED actually being exercised, not merely two claims that
    happened to run one after the other.
    """
    project_id = _insert_project(real_pg_connection)
    earlier_id = _insert_job(
        real_pg_connection,
        project_id=project_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    later_id = _insert_job(
        real_pg_connection,
        project_id=project_id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    connection_a = psycopg.connect(PG_TEST_DATABASE_URL, autocommit=False)
    try:
        # Connection A claims (ORDER BY created_at picks `earlier_id`
        # first) but does not commit -- its row lock is still held.
        repo_a = EvaluationJobsRepository(connection_a)
        [claimed_by_a] = repo_a.claim_jobs(worker_id="worker-a", batch_size=1)
        assert claimed_by_a.id == earlier_id

        # Connection B, fully concurrent (A's transaction is still open),
        # must skip the locked row and claim only what remains.
        connection_b = psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True)
        try:
            repo_b = EvaluationJobsRepository(connection_b)
            claimed_by_b = repo_b.claim_jobs(worker_id="worker-b", batch_size=10)
        finally:
            connection_b.close()

        assert {job.id for job in claimed_by_b} == {later_id}
        connection_a.commit()
    finally:
        connection_a.close()

    row_a = _fetch_job(real_pg_connection, earlier_id)
    row_b = _fetch_job(real_pg_connection, later_id)
    assert row_a["status"] == "running" and row_a["claimed_by"] == "worker-a"
    assert row_b["status"] == "running" and row_b["claimed_by"] == "worker-b"


def test_concurrent_claim_stress_never_double_claims_under_real_thread_concurrency(
    real_pg_connection,
) -> None:
    """Complementary stress proof under genuine OS-thread concurrency (not
    a deterministically-held lock): many pending jobs, two threads racing
    to claim them via a barrier to maximize overlap -- the union of what
    each thread claims must be disjoint and must exactly cover every job.
    """
    project_id = _insert_project(real_pg_connection)
    job_ids = {_insert_job(real_pg_connection, project_id=project_id) for _ in range(20)}

    results: dict[str, set[uuid.UUID]] = {}
    barrier = threading.Barrier(2)

    def _claim(worker_id: str) -> None:
        connection = psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True)
        try:
            repo = EvaluationJobsRepository(connection)
            barrier.wait()
            claimed = repo.claim_jobs(worker_id=worker_id, batch_size=20)
            results[worker_id] = {job.id for job in claimed}
        finally:
            connection.close()

    threads = [
        threading.Thread(target=_claim, args=("worker-a",)),
        threading.Thread(target=_claim, args=("worker-b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed_a, claimed_b = results["worker-a"], results["worker-b"]
    assert claimed_a.isdisjoint(claimed_b)
    assert claimed_a | claimed_b == job_ids


# -- completion transitions: attempt_count fencing ---------------------------


def test_mark_succeeded_stale_attempt_cannot_overwrite_a_reaped_newer_attempt(
    real_pg_connection,
) -> None:
    """Simulates the exact race the fencing token exists to prevent: a
    worker claims a job (attempt_count -> 1), a reaper (not yet built)
    later resets it -- incrementing attempt_count and moving it back to
    'failed' -- and only then does the original, now-stale attempt finish
    and try to mark it succeeded. That must be rejected, not silently
    accepted.
    """
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="worker-a", batch_size=10)
    assert claimed.attempt_count == 1

    # Simulate a reaper resetting this stuck-looking job directly (the
    # reaper itself is out of scope for this phase).
    real_pg_connection.execute(
        "UPDATE evaluation_jobs SET status = 'failed', attempt_count = 2 WHERE id = %(id)s",
        {"id": job_id},
    )

    accepted = repo.mark_succeeded(job_id=job_id, claimed_attempt_count=claimed.attempt_count)
    assert accepted is False

    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "failed"
    assert row["attempt_count"] == 2


def test_mark_succeeded_accepts_a_current_attempt(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="worker-a", batch_size=10)

    accepted = repo.mark_succeeded(job_id=job_id, claimed_attempt_count=claimed.attempt_count)
    assert accepted is True
    assert _fetch_job(real_pg_connection, job_id)["status"] == "succeeded"


def test_mark_failed_stale_attempt_cannot_overwrite_a_reaped_newer_attempt(
    real_pg_connection,
) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="worker-a", batch_size=10)

    real_pg_connection.execute(
        "UPDATE evaluation_jobs SET status = 'dead_letter', attempt_count = 4 WHERE id = %(id)s",
        {"id": job_id},
    )

    accepted = repo.mark_failed(
        job_id=job_id,
        claimed_attempt_count=claimed.attempt_count,
        next_attempt_at=datetime.now(UTC) + timedelta(seconds=30),
        last_error="stale attempt's own error",
    )
    assert accepted is False

    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "dead_letter"
    assert row["attempt_count"] == 4


def test_mark_dead_letter_stale_attempt_cannot_overwrite_a_reaped_newer_attempt(
    real_pg_connection,
) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="worker-a", batch_size=10)

    real_pg_connection.execute(
        "UPDATE evaluation_jobs SET status = 'succeeded', attempt_count = 1 WHERE id = %(id)s",
        {"id": job_id},
    )

    accepted = repo.mark_dead_letter(
        job_id=job_id, claimed_attempt_count=claimed.attempt_count, last_error="stale"
    )
    assert accepted is False

    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "succeeded"


# -- updated_at freshness -----------------------------------------------------


def test_mark_succeeded_updates_updated_at(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="worker-a", batch_size=10)
    updated_at_after_claim = _fetch_job(real_pg_connection, job_id)["updated_at"]

    time.sleep(0.01)
    repo.mark_succeeded(job_id=job_id, claimed_attempt_count=claimed.attempt_count)

    updated_at_after_success = _fetch_job(real_pg_connection, job_id)["updated_at"]
    assert updated_at_after_success > updated_at_after_claim


def test_mark_failed_updates_updated_at(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="worker-a", batch_size=10)
    updated_at_after_claim = _fetch_job(real_pg_connection, job_id)["updated_at"]

    time.sleep(0.01)
    repo.mark_failed(
        job_id=job_id,
        claimed_attempt_count=claimed.attempt_count,
        next_attempt_at=datetime.now(UTC) + timedelta(seconds=30),
        last_error="boom",
    )

    updated_at_after_failure = _fetch_job(real_pg_connection, job_id)["updated_at"]
    assert updated_at_after_failure > updated_at_after_claim


def test_mark_dead_letter_updates_updated_at(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="worker-a", batch_size=10)
    updated_at_after_claim = _fetch_job(real_pg_connection, job_id)["updated_at"]

    time.sleep(0.01)
    repo.mark_dead_letter(
        job_id=job_id, claimed_attempt_count=claimed.attempt_count, last_error="retries exhausted"
    )

    updated_at_after_dead_letter = _fetch_job(real_pg_connection, job_id)["updated_at"]
    assert updated_at_after_dead_letter > updated_at_after_claim
