"""Real-ClickHouse integration tests for
app.clickhouse.evaluations_query_repository.EvaluationsQueryRepository and
`GET /v1/traces/{trace_id}/spans/{span_id}/evaluations`.

Mirrors test_traces_clickhouse_integration.py's structure and
skip-if-unreachable convention exactly. There is no ingest endpoint for
`evaluation_results` (only services/worker ever writes it) -- these tests
insert directly into the table, the same approach
services/worker/tests/test_eligible_span_repository_clickhouse_integration.py
already established for its own real-ClickHouse tests against `spans`.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.clickhouse.client import get_clickhouse_client
from app.config import settings

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"

_RESULT_COLUMNS = (
    "evaluation_id",
    "project_id",
    "trace_id",
    "span_id",
    "evaluator_name",
    "evaluator_version",
    "score",
    "label",
    "explanation",
    "evaluator_model",
    "evaluator_provider",
    "evaluation_latency_ms",
    "evaluation_cost_usd",
    "job_created_at",
)


@pytest.fixture
def real_clickhouse_client():
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
def real_client(db_session: Session, real_clickhouse_client) -> Generator[TestClient, None, None]:
    from app.api.v1.evaluations import get_evaluations_query_repository
    from app.clickhouse.evaluations_query_repository import EvaluationsQueryRepository
    from app.db.session import get_db
    from app.main import app

    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_evaluations_query_repository] = lambda: EvaluationsQueryRepository(
        real_clickhouse_client
    )
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def _insert_result(
    ch_client, *, project_id: uuid.UUID, trace_id: str, span_id: str, **overrides: Any
) -> uuid.UUID:
    evaluation_id = overrides.pop("evaluation_id", uuid.uuid4())
    row = {
        "evaluation_id": evaluation_id,
        "project_id": project_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "evaluator_name": "relevance",
        "evaluator_version": "0.1.0",
        "score": 0.87,
        "label": "relevant",
        "explanation": "cosine similarity above threshold",
        "evaluator_model": "tfidf",
        "evaluator_provider": None,
        "evaluation_latency_ms": 3.35,
        "evaluation_cost_usd": Decimal("0.000000"),
        "job_created_at": datetime.now(UTC),
    }
    row.update(overrides)
    ch_client.insert(
        "evaluation_results",
        [[row[column] for column in _RESULT_COLUMNS]],
        column_names=list(_RESULT_COLUMNS),
    )
    return evaluation_id


def test_repository_returns_inserted_result(real_clickhouse_client) -> None:
    from app.clickhouse.evaluations_query_repository import EvaluationsQueryRepository

    project_id = uuid.uuid4()
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    evaluation_id = _insert_result(
        real_clickhouse_client, project_id=project_id, trace_id=trace_id, span_id=span_id
    )

    repo = EvaluationsQueryRepository(real_clickhouse_client)
    rows = repo.get_span_evaluations(project_id=project_id, trace_id=trace_id, span_id=span_id)

    assert len(rows) == 1
    assert rows[0]["evaluation_id"] == evaluation_id
    assert rows[0]["evaluator_name"] == "relevance"
    assert rows[0]["trace_id"] == trace_id
    assert rows[0]["span_id"] == span_id


def test_repository_scopes_by_project_id(real_clickhouse_client) -> None:
    """Two projects that happen to share a (trace_id, span_id) -- vanishingly
    unlikely with real OTel-generated ids, constructed deliberately here --
    must never see each other's evaluation results."""
    from app.clickhouse.evaluations_query_repository import EvaluationsQueryRepository

    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    project_a = uuid.uuid4()
    project_b = uuid.uuid4()
    _insert_result(real_clickhouse_client, project_id=project_a, trace_id=trace_id, span_id=span_id)

    repo = EvaluationsQueryRepository(real_clickhouse_client)
    rows_b = repo.get_span_evaluations(project_id=project_b, trace_id=trace_id, span_id=span_id)

    assert rows_b == []


def test_repository_returns_final_row_after_duplicate_write(real_clickhouse_client) -> None:
    """Simulates a retried worker insert for the same evaluation_id (the
    ClickHouse-write-succeeded-but-confirmation-lost scenario
    app/clickhouse/evaluations_query_repository.py's module docstring
    documents) -- FINAL must collapse the two physical rows into one."""
    from app.clickhouse.evaluations_query_repository import EvaluationsQueryRepository

    project_id = uuid.uuid4()
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    evaluation_id = uuid.uuid4()
    _insert_result(
        real_clickhouse_client,
        project_id=project_id,
        trace_id=trace_id,
        span_id=span_id,
        evaluation_id=evaluation_id,
        score=0.5,
    )
    _insert_result(
        real_clickhouse_client,
        project_id=project_id,
        trace_id=trace_id,
        span_id=span_id,
        evaluation_id=evaluation_id,
        score=0.9,
    )

    repo = EvaluationsQueryRepository(real_clickhouse_client)
    rows = repo.get_span_evaluations(project_id=project_id, trace_id=trace_id, span_id=span_id)

    assert len(rows) == 1  # FINAL deduplicated the two physical rows


def test_repository_returns_multiple_evaluators_for_one_span(real_clickhouse_client) -> None:
    from app.clickhouse.evaluations_query_repository import EvaluationsQueryRepository

    project_id = uuid.uuid4()
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    _insert_result(
        real_clickhouse_client,
        project_id=project_id,
        trace_id=trace_id,
        span_id=span_id,
        evaluator_name="relevance",
    )
    _insert_result(
        real_clickhouse_client,
        project_id=project_id,
        trace_id=trace_id,
        span_id=span_id,
        evaluator_name="relevance_embedding",
        score=0.91,
    )

    repo = EvaluationsQueryRepository(real_clickhouse_client)
    rows = repo.get_span_evaluations(project_id=project_id, trace_id=trace_id, span_id=span_id)

    assert {row["evaluator_name"] for row in rows} == {"relevance", "relevance_embedding"}


def test_endpoint_returns_inserted_result_end_to_end(
    real_client: TestClient, real_clickhouse_client, active_api_key
) -> None:
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    _insert_result(
        real_clickhouse_client,
        project_id=active_api_key.project.id,
        trace_id=trace_id,
        span_id=span_id,
    )

    response = real_client.get(
        f"/v1/traces/{trace_id}/spans/{span_id}/evaluations",
        headers={"Authorization": f"Bearer {active_api_key.raw_key}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["trace_id"] == trace_id
    assert body["results"][0]["span_id"] == span_id


def test_endpoint_does_not_leak_another_projects_result(
    real_client: TestClient, real_clickhouse_client, active_api_key
) -> None:
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    _insert_result(
        real_clickhouse_client,
        project_id=uuid.uuid4(),  # a different project entirely
        trace_id=trace_id,
        span_id=span_id,
    )

    response = real_client.get(
        f"/v1/traces/{trace_id}/spans/{span_id}/evaluations",
        headers={"Authorization": f"Bearer {active_api_key.raw_key}"},
    )
    assert response.status_code == 200
    assert response.json() == {"results": []}
