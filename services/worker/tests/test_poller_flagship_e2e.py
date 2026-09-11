"""Flagship end-to-end test for Phase 3H: a real ClickHouse span, discovered
by a real Poller tick, turned into a real evaluation_jobs row via a real,
running apps/api (HTTP, real internal-token auth, real ground-truth
ClickHouse verification, real idempotent Postgres insert) -- and then
claimable by Phase 3G's existing, unmodified claim path.

Skipped automatically if PostgreSQL, ClickHouse, or a running apps/api
instance is unreachable -- this is the one test in this phase that needs
all three, by design (it exists specifically to prove the full chain no
other single test proves in combination). Every individual link in the
chain is already proven in isolation elsewhere:
test_eligible_span_repository_clickhouse_integration.py (scan),
apps/api/tests/test_evaluations_api.py (the endpoint's own logic),
test_evaluation_jobs_postgres_integration.py (claim_jobs). This test does
not re-prove any of those in depth.

Uses a fake EvaluatorRegistry (name/version only, no real evaluate() call
ever happens) -- the poller never calls evaluate(); paying
EmbeddingRelevanceEvaluator's ONNX load cost here would be pure overhead
unrelated to what this test proves.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from worker.clickhouse.eligible_span_repository import EligibleSpanRepository
from worker.poller import HttpJobCreationClient, Poller
from worker.postgres.poller_checkpoint_repository import PollerCheckpointRepository
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

API_BASE_URL = os.environ.get("VIGIL_WORKER_TEST_API_BASE_URL", "http://localhost:8000")
INTERNAL_TOKEN = os.environ.get(
    "VIGIL_WORKER_INTERNAL_SERVICE_TOKEN", "test-internal-service-token"
)

_PG_TABLES = (
    "evaluation_jobs, evaluator_configs, evaluation_poller_checkpoint, projects, organizations"
)


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


@pytest.fixture
def real_clickhouse_client():
    from worker.clickhouse.client import get_clickhouse_client
    from worker.config import settings

    try:
        ch_client = get_clickhouse_client()
        ch_client.ping()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(
            f"ClickHouse not reachable at {settings.clickhouse_host}:"
            f"{settings.clickhouse_port} ({exc}); start it via "
            "infrastructure/docker-compose.yml to run this test."
        )
    return ch_client


@pytest.fixture
def require_real_api():
    try:
        urllib.request.urlopen(f"{API_BASE_URL}/health", timeout=2)
    except (urllib.error.URLError, OSError) as exc:  # pragma: no cover - environment-dependent
        pytest.skip(
            f"apps/api not reachable at {API_BASE_URL} ({exc}); start it (e.g. "
            "`uvicorn app.main:app` from apps/api, pointed at the vigil_test "
            "database) to run this test."
        )


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


def _enable_evaluator(connection, *, project_id: uuid.UUID, evaluator_name: str) -> None:
    connection.execute(
        "INSERT INTO evaluator_configs (id, project_id, evaluator_name, enabled, sampling_rate) "
        "VALUES (%(id)s, %(project_id)s, %(evaluator_name)s, true, 1.0)",
        {"id": uuid.uuid4(), "project_id": project_id, "evaluator_name": evaluator_name},
    )


def _insert_span(ch_client, *, project_id: uuid.UUID, ingested_at: datetime) -> tuple[str, str]:
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    ch_client.insert(
        "spans",
        [
            [
                project_id,
                trace_id,
                span_id,
                "flagship-e2e-span",
                "llm",
                "flagship-e2e",
                ingested_at,
                ingested_at,
                "test",
                ingested_at,
            ]
        ],
        column_names=[
            "project_id",
            "trace_id",
            "span_id",
            "name",
            "span_type",
            "resource",
            "start_time",
            "end_time",
            "environment",
            "ingested_at",
        ],
    )
    return trace_id, span_id


class _FakeEvaluator:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version

    def evaluate(self, *a, **k):
        raise AssertionError("the poller must never call evaluate()")


def test_span_to_poller_to_api_to_evaluation_jobs_to_claimable(
    real_pg_connection, real_clickhouse_client, require_real_api
) -> None:
    project_id = _insert_project(real_pg_connection)
    _enable_evaluator(real_pg_connection, project_id=project_id, evaluator_name="relevance")

    ingested_at = datetime.now(UTC)
    trace_id, span_id = _insert_span(
        real_clickhouse_client, project_id=project_id, ingested_at=ingested_at
    )

    # Seed the checkpoint to just before this span's ingested_at, rather than
    # relying on the natural "no checkpoint yet -> scan the whole retention
    # window" first-run behavior: this ClickHouse test container accumulates
    # `spans` rows across every test in this suite run (spans is never
    # truncated between tests, unlike evaluation_jobs), so an unbounded scan
    # would compete with however much older llm-typed test data already
    # exists for a small poller_batch_size -- exactly the scenario
    # poller_overlap_seconds/checkpoint seeding exists to avoid in
    # production too. This is a narrow, realistic scan window, not a
    # special-cased test shortcut.
    checkpoint_repo = PollerCheckpointRepository(real_pg_connection)
    checkpoint_repo.get_or_create_checkpoint()
    checkpoint_repo.advance_checkpoint(
        observed_watermark=None, new_watermark=ingested_at - timedelta(seconds=5)
    )

    poller = Poller(
        eligible_span_repository=EligibleSpanRepository(real_clickhouse_client),
        checkpoint_connection_factory=lambda: psycopg.connect(
            PG_TEST_DATABASE_URL, autocommit=True
        ),
        job_creation_client=HttpJobCreationClient(
            base_url=API_BASE_URL, internal_service_token=INTERNAL_TOKEN, timeout_seconds=10.0
        ),
        registry=EvaluatorRegistry([_FakeEvaluator("relevance", "0.1.0")]),
        poller_batch_size=100,
        poller_overlap_seconds=60.0,
        poller_start_time_lookback_days=3,
        poller_interval_seconds=30.0,
    )

    poller.run_one_tick()

    row = real_pg_connection.execute(
        "SELECT status, evaluator_name, evaluator_version, max_retries "
        "FROM evaluation_jobs WHERE project_id = %(project_id)s AND trace_id = %(trace_id)s "
        "AND span_id = %(span_id)s",
        {"project_id": project_id, "trace_id": trace_id, "span_id": span_id},
    ).fetchone()
    assert row is not None, "poller did not create an evaluation_jobs row via the real API"
    status, evaluator_name, evaluator_version, max_retries = row
    assert status == "pending"
    assert evaluator_name == "relevance"
    assert evaluator_version == "0.1.0"
    assert max_retries == 3  # evaluator_configs' default, snapshotted

    # Phase 3G's existing, unmodified claim path can claim it.
    jobs_repository = EvaluationJobsRepository(real_pg_connection)
    [claimed] = jobs_repository.claim_jobs(worker_id="flagship-e2e", batch_size=10)
    assert claimed.trace_id == trace_id
    assert claimed.span_id == span_id
    assert claimed.attempt_count == 1

    # Checkpoint advanced too, past this span's ingested_at -- within 1ms,
    # since ClickHouse's DateTime64(3) truncates to millisecond precision
    # (the Python-side ingested_at we inserted carries microseconds).
    checkpoint_row = real_pg_connection.execute(
        "SELECT last_ingested_at FROM evaluation_poller_checkpoint WHERE id = 'global'"
    ).fetchone()
    assert checkpoint_row is not None
    assert checkpoint_row[0] >= ingested_at - timedelta(milliseconds=1)
