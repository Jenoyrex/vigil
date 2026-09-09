"""Real-store integration tests for worker.dispatcher.Dispatcher, including
the resource-ownership fix's core claim: multiple dispatcher tasks can run
concurrently against real ClickHouse and real PostgreSQL without sharing a
thread-unsafe client/connection.

Not a full worker end-to-end (no poller, no main loop) --
test_execution_integration.py remains the primary real-store execution proof
for the single-job path this dispatcher wraps. Runs with
`max_concurrent_evaluations >= 2` and `worker.resources.real_execution_resources`
as the resource provider -- the same provider real (not-yet-built) worker
code would use -- specifically to prove the "Attempt to execute concurrent
queries within the same session" failure this fix addresses does not
recur, and that PostgreSQL access is likewise safe under real concurrency.

Skipped automatically if either real store is unreachable, mirroring every
other integration test in this package.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from app.relevance import EVALUATOR_NAME, EVALUATOR_VERSION, RelevanceEvaluator

import worker.config
from worker.clickhouse.repository import EvaluationResultsRepository
from worker.dispatcher import Dispatcher
from worker.postgres.repository import EvaluationJobsRepository
from worker.registry import EvaluatorRegistry
from worker.resources import real_execution_resources

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

_SPAN_COLUMNS = (
    "project_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "span_type",
    "resource",
    "start_time",
    "end_time",
    "status",
    "status_message",
    "input",
    "input_size_bytes",
    "input_truncated",
    "output",
    "output_size_bytes",
    "output_truncated",
    "attributes",
    "attributes_truncated",
    "events.time",
    "events.name",
    "events.attributes",
    "events_truncated",
    "llm_provider",
    "llm_model",
    "llm_input_tokens",
    "llm_output_tokens",
    "llm_total_tokens",
    "llm_cost_usd",
    "environment",
    "release",
)


def _truncate_pg() -> None:
    with psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True) as connection:
        connection.execute(f"TRUNCATE TABLE {_PG_TABLES} RESTART IDENTITY CASCADE")


@pytest.fixture
def real_pg_connection():
    try:
        probe = psycopg.connect(PG_TEST_DATABASE_URL, connect_timeout=2)
        probe.close()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"PostgreSQL not reachable at {PG_TEST_DATABASE_URL} ({exc}).")

    _truncate_pg()
    connection = psycopg.connect(PG_TEST_DATABASE_URL, autocommit=True)
    try:
        yield connection
    finally:
        connection.close()
        _truncate_pg()


@pytest.fixture
def real_execution_resources_against_test_db(real_pg_connection, monkeypatch):
    """`worker.resources.real_execution_resources` opens its own PostgreSQL
    connections via `worker.postgres.client.get_connection`, which reads
    `worker.config.settings.database_url` -- the *development* database by
    default (`vigil`), not this package's dedicated `vigil_test` database
    `real_pg_connection` truncates/uses. Point it at the same test database
    for the duration of one test, exactly like every other setting override
    in this file, so `real_execution_resources`'s own connections see the
    rows this test just inserted.
    """
    monkeypatch.setattr(worker.config.settings, "database_url", PG_TEST_DATABASE_URL)
    return real_execution_resources


@pytest.fixture
def real_clickhouse_client():
    from worker.clickhouse.client import get_clickhouse_client
    from worker.config import settings

    try:
        client = get_clickhouse_client()
        client.ping()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(
            f"ClickHouse not reachable at {settings.clickhouse_host}:"
            f"{settings.clickhouse_port} ({exc})."
        )
    return client


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


def _insert_pending_job(
    connection, *, project_id: uuid.UUID, trace_id: str, span_id: str
) -> uuid.UUID:
    job_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO evaluation_jobs
            (id, project_id, trace_id, span_id, evaluator_name, evaluator_version)
        VALUES
            (%(id)s, %(project_id)s, %(trace_id)s, %(span_id)s, %(evaluator_name)s,
             %(evaluator_version)s)
        """,
        {
            "id": job_id,
            "project_id": project_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "evaluator_name": EVALUATOR_NAME,
            "evaluator_version": EVALUATOR_VERSION,
        },
    )
    return job_id


def _insert_span(
    client, *, project_id: uuid.UUID, trace_id: str, span_id: str, input_text: str, output_text: str
) -> None:
    now = datetime.now(UTC)
    row = [
        project_id,
        trace_id,
        span_id,
        None,
        "integration-test-span",
        "llm",
        "vigil-worker-test",
        now,
        now,
        "ok",
        None,
        input_text,
        len(input_text.encode("utf-8")),
        False,
        output_text,
        len(output_text.encode("utf-8")),
        False,
        {},
        False,
        [],
        [],
        [],
        False,
        None,
        None,
        None,
        None,
        None,
        None,
        "development",
        None,
    ]
    client.insert("spans", [row], column_names=list(_SPAN_COLUMNS))


def test_dispatcher_processes_multiple_real_claimed_jobs_concurrently(
    real_pg_connection, real_clickhouse_client, real_execution_resources_against_test_db
) -> None:
    """The core proof this fix exists for: `max_concurrent_evaluations >= 2`
    against real ClickHouse and real PostgreSQL, with enough jobs relative
    to the concurrency limit that genuine overlap is expected, and no
    "Attempt to execute concurrent queries within the same session" (or any
    other) failure occurs.
    """
    project_id = _insert_project(real_pg_connection)

    job_specs = [
        ("What is the capital of France?", "The capital of France is Paris."),
        ("What is the capital of Japan?", "The capital of Japan is Tokyo."),
        ("What is the capital of Italy?", "The capital of Italy is Rome."),
        ("What is the capital of Spain?", "The capital of Spain is Madrid."),
        ("What is the capital of Germany?", "The capital of Germany is Berlin."),
        ("What is the capital of Portugal?", "The capital of Portugal is Lisbon."),
    ]
    job_ids = []
    for input_text, output_text in job_specs:
        trace_id = uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        job_ids.append(
            _insert_pending_job(
                real_pg_connection, project_id=project_id, trace_id=trace_id, span_id=span_id
            )
        )
        _insert_span(
            real_clickhouse_client,
            project_id=project_id,
            trace_id=trace_id,
            span_id=span_id,
            input_text=input_text,
            output_text=output_text,
        )

    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    claimed = jobs_repository.claim_jobs(worker_id="dispatcher-integration-test", batch_size=10)
    assert {job.id for job in claimed} == set(job_ids)

    # max_concurrent_evaluations=3 (not 1): resource_provider resolves a
    # fresh/thread-cached client+connection *inside* each task (see
    # worker/resources.py), so this no longer shares one real ClickHouse
    # client or one real PostgreSQL connection across concurrent dispatcher
    # threads -- the fix this test exists to prove.
    dispatcher = Dispatcher(
        max_concurrent_evaluations=3,
        registry=EvaluatorRegistry(evaluators=[RelevanceEvaluator()]),
        resource_provider=real_execution_resources_against_test_db,
    )

    outcomes = dispatcher.dispatch(claimed)

    assert len(outcomes) == 6
    assert all(outcome.succeeded for outcome in outcomes), [
        outcome.error for outcome in outcomes if not outcome.succeeded
    ]
    assert all(outcome.evaluation_outcome.marked_succeeded for outcome in outcomes)

    for job_id in job_ids:
        row = real_pg_connection.execute(
            "SELECT status FROM evaluation_jobs WHERE id = %(id)s", {"id": job_id}
        ).fetchone()
        assert row[0] == "succeeded"

        result = EvaluationResultsRepository(real_clickhouse_client).get_by_evaluation_id(
            project_id=project_id, evaluation_id=job_id
        )
        assert result is not None
        assert result["label"] == "relevant"


def test_real_execution_resources_gives_each_call_its_own_postgres_connection(
    real_pg_connection, real_execution_resources_against_test_db
) -> None:
    """More surgical, deterministic proof for PostgreSQL specifically:
    every call to the resource provider yields a distinct connection object
    (never shared across calls, let alone across threads), and that
    connection is closed once its `with` block exits -- so per-task
    connections never leak, without needing a pool.
    """
    with real_execution_resources_against_test_db() as first:
        first_connection = first.jobs_repository._connection
        assert not first_connection.closed

    assert first_connection.closed

    with real_execution_resources_against_test_db() as second:
        second_connection = second.jobs_repository._connection
        assert second_connection is not first_connection


def test_real_execution_resources_supports_genuinely_concurrent_clickhouse_access(
    real_pg_connection, real_clickhouse_client, real_execution_resources_against_test_db
) -> None:
    """Surgical proof for ClickHouse specifically, independent of full job
    execution: several threads each resolve their own resources and issue a
    real point-lookup query at (as close to) the same time as Python's
    GIL/thread scheduling allows, via a barrier -- reproducing the exact
    shape of access that previously raised "Attempt to execute concurrent
    queries within the same session."
    """
    project_id = _insert_project(real_pg_connection)
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    _insert_span(
        real_clickhouse_client,
        project_id=project_id,
        trace_id=trace_id,
        span_id=span_id,
        input_text="What is the capital of France?",
        output_text="The capital of France is Paris.",
    )

    thread_count = 4
    barrier = threading.Barrier(thread_count, timeout=10)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _query() -> None:
        try:
            with real_execution_resources_against_test_db() as resources:
                barrier.wait()
                span = resources.span_repository.get_span(
                    project_id=project_id, trace_id=trace_id, span_id=span_id
                )
                assert span is not None
        except BaseException as exc:  # noqa: BLE001 -- captured for the main
            # thread to re-raise/report; a bare `raise` here would only ever
            # surface as an unhandled exception in a background thread.
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_query) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
