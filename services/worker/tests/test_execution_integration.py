"""End-to-end integration test: claimed job + real span -> evaluator
execution -> a real `evaluation_results` row exists, and the owning
PostgreSQL job is marked `succeeded`.

Runs against both real local stores this repository already has
integration-test conventions for: ClickHouse (see
test_evaluation_results_clickhouse_integration.py) and PostgreSQL (see
test_evaluation_jobs_postgres_integration.py) -- skipped automatically if
either is unreachable. Deliberately uses only the TF-IDF `RelevanceEvaluator`
(cheap to construct, no model load) rather than the full production
`EvaluatorRegistry` -- this test's purpose is proving the wiring across
`worker/execution.py`'s claim -> span -> evaluate -> persist path against
real stores, not re-exercising either evaluator's own scoring behavior
(already covered in services/evaluator's own test suite and in
tests/test_registry.py here).

Does not build a full retry/reaper/poller end-to-end -- the job row is
claimed manually via `EvaluationJobsRepository`, exactly as
test_evaluation_jobs_postgres_integration.py already does, not via a poller
(which does not exist yet).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from app.relevance import EVALUATOR_NAME, EVALUATOR_VERSION, RelevanceEvaluator

from worker.clickhouse.repository import EvaluationResultsRepository
from worker.clickhouse.span_repository import SourceSpanRepository
from worker.execution import execute_job
from worker.postgres.evaluator_config_repository import EvaluatorConfigRepository
from worker.postgres.repository import EvaluationJobsRepository
from worker.registry import EvaluatorRegistry

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


def test_claimed_job_and_span_produce_a_persisted_evaluation_result(
    real_pg_connection, real_clickhouse_client
) -> None:
    project_id = _insert_project(real_pg_connection)
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]

    job_id = _insert_pending_job(
        real_pg_connection, project_id=project_id, trace_id=trace_id, span_id=span_id
    )
    _insert_span(
        real_clickhouse_client,
        project_id=project_id,
        trace_id=trace_id,
        span_id=span_id,
        input_text="What is the capital of France?",
        output_text="The capital of France is Paris.",
    )

    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    [claimed] = jobs_repository.claim_jobs(worker_id="integration-test", batch_size=1)
    assert claimed.id == job_id

    registry = EvaluatorRegistry(evaluators=[RelevanceEvaluator()])
    span_repository = SourceSpanRepository(real_clickhouse_client)
    evaluator_config_repository = EvaluatorConfigRepository(real_pg_connection)
    results_repository = EvaluationResultsRepository(real_clickhouse_client)

    outcome = execute_job(
        claimed,
        registry=registry,
        span_repository=span_repository,
        evaluator_config_repository=evaluator_config_repository,
        results_repository=results_repository,
        jobs_repository=jobs_repository,
    )

    assert outcome.marked_succeeded is True
    assert outcome.result.label == "relevant"

    persisted = results_repository.get_by_evaluation_id(project_id=project_id, evaluation_id=job_id)
    assert persisted is not None
    assert persisted["project_id"] == project_id
    assert persisted["trace_id"] == trace_id
    assert persisted["span_id"] == span_id
    assert persisted["evaluator_name"] == EVALUATOR_NAME
    assert persisted["label"] == "relevant"

    job_row = real_pg_connection.execute(
        "SELECT status FROM evaluation_jobs WHERE id = %(id)s", {"id": job_id}
    ).fetchone()
    assert job_row[0] == "succeeded"
