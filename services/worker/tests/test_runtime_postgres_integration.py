"""Real-PostgreSQL integration tests for worker.runtime.WorkerRuntime.

Mirrors test_evaluation_jobs_postgres_integration.py / test_reaper_postgres_integration.py's
conventions (fixed test database, truncate-before/after, skip if unreachable).

Scope, deliberately narrow: prove WorkerRuntime's own tick methods work
against a real connection (claims an actual pending row, reaps an actual
backdated stuck row, closes its connection). Uses a fake Dispatcher, not a
real one -- claim_jobs' and reap_stuck_jobs' own correctness (SKIP LOCKED,
attempt_count fencing, retry/dead-letter boundary) are already proven by
test_evaluation_jobs_postgres_integration.py and
test_reaper_postgres_integration.py; re-proving them here would duplicate
coverage rather than add any. Dispatcher's own correctness (real ClickHouse
span fetch, real evaluate() call, real result persistence) is proven by
test_dispatcher_integration.py -- also not re-proven here.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from worker.dispatcher import DispatchOutcome
from worker.postgres.repository import ClaimedJob, EvaluationJobsRepository
from worker.runtime import WorkerRuntime

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
    with psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True) as c:
        c.execute(f"TRUNCATE TABLE {_PG_TABLES} RESTART IDENTITY CASCADE")


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


def _real_connection_factory() -> psycopg.Connection:
    """Mirrors worker.postgres.client.get_connection's exact shape (a fresh
    autocommit=True connection per call) without depending on
    worker.config.settings.database_url, which points at the dev database,
    not vigil_test."""
    return psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True)


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


def _fetch_status(connection, job_id: uuid.UUID) -> str:
    row = connection.execute(
        "SELECT status FROM evaluation_jobs WHERE id = %(id)s", {"id": job_id}
    ).fetchone()
    return row[0]


class FakeDispatcher:
    """No real evaluation happens here -- see module docstring for why a
    real one is unnecessary for what this file proves."""

    def __init__(self) -> None:
        self.dispatch_calls: list[list[ClaimedJob]] = []

    def dispatch(self, jobs: list[ClaimedJob]) -> list[DispatchOutcome]:
        self.dispatch_calls.append(list(jobs))
        return [DispatchOutcome(job=job, evaluation_outcome=None, error=None) for job in jobs]


def _make_runtime(*, dispatcher, worker_id: str = "runtime-integration-test") -> WorkerRuntime:
    return WorkerRuntime(
        dispatcher=dispatcher,
        jobs_connection_factory=_real_connection_factory,
        worker_id=worker_id,
        claim_batch_size=10,
        poll_interval_seconds=1.0,
        reaper_interval_seconds=1000.0,
        stuck_job_threshold_seconds=900.0,
        reaper_batch_size=10,
    )


def test_runtime_claims_an_actual_pending_job(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id)

    dispatcher = FakeDispatcher()
    runtime = _make_runtime(dispatcher=dispatcher)

    did_work = runtime._claim_and_dispatch()

    assert did_work is True
    assert len(dispatcher.dispatch_calls) == 1
    [claimed_job] = dispatcher.dispatch_calls[0]
    assert claimed_job.id == job_id
    assert claimed_job.attempt_count == 1
    assert _fetch_status(real_pg_connection, job_id) == "running"


def test_runtime_reaper_reclaims_an_actual_backdated_stuck_job(real_pg_connection) -> None:
    project_id = _insert_project(real_pg_connection)
    job_id = _insert_job(real_pg_connection, project_id=project_id, max_retries=3)

    repo = EvaluationJobsRepository(real_pg_connection)
    [claimed] = repo.claim_jobs(worker_id="crashed-worker", batch_size=1)
    assert claimed.id == job_id
    real_pg_connection.execute(
        "UPDATE evaluation_jobs SET claimed_at = now() - interval '1000 seconds' WHERE id = %(id)s",
        {"id": job_id},
    )

    runtime = _make_runtime(dispatcher=FakeDispatcher())

    runtime._reap()

    assert _fetch_status(real_pg_connection, job_id) == "failed"
