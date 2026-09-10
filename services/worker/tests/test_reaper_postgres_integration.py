"""Real-PostgreSQL integration tests for worker.reaper.reap_stuck_jobs.

Mirrors test_evaluation_jobs_postgres_integration.py's conventions (fixed
test database, truncate-before/after, skip if unreachable). These are the
tests that actually matter for Phase 3F's acceptance criterion: SKIP LOCKED
batch-partitioning across concurrent reapers, and the attempt_count fencing
invariant genuinely rejecting a stale worker's post-reclaim completion, are
properties of real row-locking behavior no fake connection can demonstrate.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from worker.postgres.repository import EvaluationJobsRepository
from worker.reaper import reap_stuck_jobs

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
            "evaluator_name": EVALUATOR_NAME,
            "evaluator_version": EVALUATOR_VERSION,
            "max_retries": max_retries,
        },
    )
    return job_id


def _fetch_job(connection, job_id: uuid.UUID) -> dict:
    cursor = connection.execute(
        "SELECT status, attempt_count, max_retries, next_attempt_at, claimed_at, claimed_by, "
        "last_error FROM evaluation_jobs WHERE id = %(id)s",
        {"id": job_id},
    )
    columns = (
        "status",
        "attempt_count",
        "max_retries",
        "next_attempt_at",
        "claimed_at",
        "claimed_by",
        "last_error",
    )
    row = cursor.fetchone()
    return dict(zip(columns, row, strict=True))


def _backdate_claimed_at(connection, job_id: uuid.UUID, *, seconds_ago: float) -> None:
    connection.execute(
        "UPDATE evaluation_jobs SET claimed_at = now() - make_interval(secs => %(seconds_ago)s) "
        "WHERE id = %(id)s",
        {"id": job_id, "seconds_ago": seconds_ago},
    )


def _claim_and_backdate(
    connection, *, project_id: uuid.UUID, max_retries: int = 3, seconds_ago: float = 1000.0
) -> uuid.UUID:
    """Insert a job, claim it (a real claim -- attempt_count, claimed_by,
    claimed_at all set by claim_jobs exactly as production does), then push
    claimed_at back past the stuck threshold to simulate a worker that
    claimed it and then went silent."""
    job_id = _insert_job(connection, project_id=project_id, max_retries=max_retries)
    repo = EvaluationJobsRepository(connection)
    [claimed] = repo.claim_jobs(worker_id="crashed-worker", batch_size=1)
    assert claimed.id == job_id
    _backdate_claimed_at(connection, job_id, seconds_ago=seconds_ago)
    return job_id


# -- select_stuck_jobs: which rows are stuck ----------------------------------


def test_running_job_past_threshold_is_selected(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _claim_and_backdate(real_pg_connection, project_id=project_id, seconds_ago=1000.0)

    repo = EvaluationJobsRepository(real_pg_connection)
    stuck = repo.select_stuck_jobs(stuck_threshold_seconds=900.0, batch_size=10)

    assert {job.id for job in stuck} == {job_id}


def test_running_job_not_past_threshold_is_not_selected(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    _claim_and_backdate(real_pg_connection, project_id=project_id, seconds_ago=10.0)

    repo = EvaluationJobsRepository(real_pg_connection)
    stuck = repo.select_stuck_jobs(stuck_threshold_seconds=900.0, batch_size=10)

    assert stuck == []


@pytest.mark.parametrize("status", ["pending", "failed", "succeeded", "dead_letter"])
def test_non_running_statuses_are_never_selected_regardless_of_claimed_at(
    real_pg_connection, status: str
) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)
    real_pg_connection.execute(
        "UPDATE evaluation_jobs SET status = %(status)s, "
        "claimed_at = now() - interval '1 hour' WHERE id = %(id)s",
        {"status": status, "id": job_id},
    )

    repo = EvaluationJobsRepository(real_pg_connection)
    stuck = repo.select_stuck_jobs(stuck_threshold_seconds=900.0, batch_size=10)

    assert stuck == []


def test_concurrent_select_stuck_jobs_never_returns_overlapping_rows_under_a_held_lock(
    real_pg_connection,
) -> None:
    """Mirrors test_concurrent_claim_never_returns_overlapping_jobs_under_a_held_lock:
    while one connection holds select_stuck_jobs' row lock open (uncommitted),
    a second, fully concurrent call must skip that row and only select what
    remains -- SKIP LOCKED actually being exercised."""
    project_id = _insert_project(real_pg_connection)
    job_a = _claim_and_backdate(real_pg_connection, project_id=project_id, seconds_ago=1000.0)
    job_b = _claim_and_backdate(real_pg_connection, project_id=project_id, seconds_ago=1000.0)

    connection_a = psycopg.connect(PG_TEST_DATABASE_URL, autocommit=False)
    try:
        repo_a = EvaluationJobsRepository(connection_a)
        stuck_by_a = repo_a.select_stuck_jobs(stuck_threshold_seconds=900.0, batch_size=1)
        assert {job.id for job in stuck_by_a} <= {job_a, job_b}

        connection_b = psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True)
        try:
            repo_b = EvaluationJobsRepository(connection_b)
            stuck_by_b = repo_b.select_stuck_jobs(stuck_threshold_seconds=900.0, batch_size=10)
        finally:
            connection_b.close()

        claimed_by_a_ids = {job.id for job in stuck_by_a}
        claimed_by_b_ids = {job.id for job in stuck_by_b}
        assert claimed_by_a_ids.isdisjoint(claimed_by_b_ids)
        assert claimed_by_a_ids | claimed_by_b_ids == {job_a, job_b}
        connection_a.commit()
    finally:
        connection_a.close()


# -- end-to-end reclaim --------------------------------------------------------


def test_reclaim_retryable_stuck_job_end_to_end(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _claim_and_backdate(
        real_pg_connection, project_id=project_id, max_retries=3, seconds_ago=1000.0
    )
    before = datetime.now(UTC)

    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=10
    )

    assert len(outcomes) == 1
    assert outcomes[0].recorded is True
    assert outcomes[0].new_status == "failed"

    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "failed"
    assert row["attempt_count"] == 1  # unchanged -- only claim_jobs ever increments it
    assert row["next_attempt_at"] is not None
    assert row["next_attempt_at"] > before
    assert "crashed-worker" in row["last_error"]

    # Re-claimable once its backoff elapses.
    real_pg_connection.execute(
        "UPDATE evaluation_jobs SET next_attempt_at = now() - interval '1 second' "
        "WHERE id = %(id)s",
        {"id": job_id},
    )
    [reclaimed] = jobs_repository.claim_jobs(worker_id="w2", batch_size=1)
    assert reclaimed.id == job_id
    assert reclaimed.attempt_count == 2  # this real re-claim is what advances it


def test_reclaim_at_max_retries_boundary_dead_letters(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _claim_and_backdate(
        real_pg_connection, project_id=project_id, max_retries=1, seconds_ago=1000.0
    )

    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=10
    )

    assert outcomes[0].new_status == "dead_letter"
    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "dead_letter"
    assert row["attempt_count"] == 1
    assert row["next_attempt_at"] is None

    # Dead-lettered jobs are never re-claimed.
    assert jobs_repository.claim_jobs(worker_id="w2", batch_size=10) == []


def test_claimed_at_and_claimed_by_preserved_across_reclaim(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _claim_and_backdate(
        real_pg_connection, project_id=project_id, max_retries=1, seconds_ago=1000.0
    )
    before_reclaim = _fetch_job(real_pg_connection, job_id)

    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    reap_stuck_jobs(jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=10)

    after_reclaim = _fetch_job(real_pg_connection, job_id)
    assert after_reclaim["claimed_at"] == before_reclaim["claimed_at"]
    assert after_reclaim["claimed_by"] == "crashed-worker"


def test_batch_size_bounds_reclaim_count(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_ids = {
        _claim_and_backdate(real_pg_connection, project_id=project_id, seconds_ago=1000.0)
        for _ in range(5)
    }

    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=2
    )

    assert len(outcomes) == 2
    reclaimed_ids = {outcome.job_id for outcome in outcomes}
    still_running_ids = job_ids - reclaimed_ids
    assert len(still_running_ids) == 3
    for job_id in still_running_ids:
        assert _fetch_job(real_pg_connection, job_id)["status"] == "running"


def test_freshly_claimed_job_is_never_reclaimed_even_if_it_would_dead_letter(
    real_pg_connection,
) -> None:
    """The threshold check is purely about staleness, independent of
    max_retries -- a job that would dead-letter if stuck must still be left
    alone while genuinely within its claim window."""
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, max_retries=1)
    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    [claimed] = jobs_repository.claim_jobs(worker_id="live-worker", batch_size=1)
    assert claimed.attempt_count == 1  # already == max_retries=1, but NOT stuck

    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=10
    )

    assert outcomes == []
    assert _fetch_job(real_pg_connection, job_id)["status"] == "running"


# -- fencing: first-committer-wins races --------------------------------------


def test_stale_worker_cannot_overwrite_after_reaper_reclaims_first(real_pg_connection) -> None:
    """The critical fencing scenario this whole feature exists for: the
    reaper reclaims a stuck job (status -> 'failed'); the original crashed
    worker's own completion call, which was never actually lost -- it was
    just very late -- arrives afterward and must be rejected."""
    project_id = _insert_project(real_pg_connection)
    job_id = _claim_and_backdate(
        real_pg_connection, project_id=project_id, max_retries=3, seconds_ago=1000.0
    )
    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    # Capture the original attempt's claimed_attempt_count directly, since
    # claim_jobs already ran once inside _claim_and_backdate.
    original_attempt_count = _fetch_job(real_pg_connection, job_id)["attempt_count"]
    assert original_attempt_count == 1

    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=10
    )
    assert outcomes[0].new_status == "failed"
    assert _fetch_job(real_pg_connection, job_id)["status"] == "failed"

    # The stale worker's own success call, arriving after the reclaim.
    accepted = jobs_repository.mark_succeeded(
        job_id=job_id, claimed_attempt_count=original_attempt_count
    )
    assert accepted is False

    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "failed"  # not overwritten to 'succeeded'


def test_reaper_does_not_overwrite_a_worker_that_completed_first(real_pg_connection) -> None:
    """Reverse ordering: the worker was alive and finished successfully
    just before the reaper's threshold check ran. The reaper's reclaim must
    be a safe no-op, never overwriting the already-succeeded row."""
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, max_retries=3)
    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    [claimed] = jobs_repository.claim_jobs(worker_id="slow-but-alive-worker", batch_size=1)

    # Backdate claimed_at so this row LOOKS stuck to the reaper's threshold
    # check, then let the "worker" finish successfully right before the
    # reaper runs.
    _backdate_claimed_at(real_pg_connection, job_id, seconds_ago=1000.0)
    accepted = jobs_repository.mark_succeeded(
        job_id=job_id, claimed_attempt_count=claimed.attempt_count
    )
    assert accepted is True

    outcomes = reap_stuck_jobs(
        jobs_repository=jobs_repository, stuck_threshold_seconds=900.0, batch_size=10
    )

    # status != 'running' anymore, so select_stuck_jobs never even selects it.
    assert outcomes == []
    row = _fetch_job(real_pg_connection, job_id)
    assert row["status"] == "succeeded"
