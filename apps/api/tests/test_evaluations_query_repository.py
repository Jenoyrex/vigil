"""Repository-level tests for
app.clickhouse.evaluations_query_repository.EvaluationsQueryRepository.

Uses `fake_ch_query_client` (see tests/conftest.py) instead of a real
ClickHouse server -- these tests assert the exact generated SQL and bound
parameters, so they catch a regression in tenant isolation or FINAL usage
even if a fake's canned response would otherwise mask it. Mirrors
test_query_repository.py's conventions exactly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.clickhouse.evaluations_query_repository import EvaluationsQueryRepository

PROJECT_ID = uuid.uuid4()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
EVALUATION_ID = uuid.uuid4()
JOB_CREATED_AT = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
WRITTEN_AT = datetime(2026, 9, 11, 12, 0, 1, tzinfo=UTC)

_COLUMNS = (
    "evaluation_id",
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
    "written_at",
)


def _repo(fake_ch_query_client) -> EvaluationsQueryRepository:
    return EvaluationsQueryRepository(fake_ch_query_client)


def _row(**overrides):
    row = {
        "evaluation_id": EVALUATION_ID,
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "evaluator_name": "relevance",
        "evaluator_version": "0.1.0",
        "score": 0.87,
        "label": "relevant",
        "explanation": "cosine similarity above threshold",
        "evaluator_model": None,
        "evaluator_provider": None,
        "evaluation_latency_ms": 3.35,
        "evaluation_cost_usd": Decimal("0.000000"),
        "job_created_at": JOB_CREATED_AT,
        "written_at": WRITTEN_AT,
    }
    row.update(overrides)
    return row


def test_get_span_evaluations_scopes_by_project_id(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert fake_ch_query_client.last_parameters["project_id"] == PROJECT_ID
    assert "project_id = {project_id:UUID}" in fake_ch_query_client.last_query


def test_get_span_evaluations_scopes_by_trace_id_and_span_id(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert fake_ch_query_client.last_parameters["trace_id"] == TRACE_ID
    assert fake_ch_query_client.last_parameters["span_id"] == SPAN_ID
    assert "trace_id = {trace_id:String}" in fake_ch_query_client.last_query
    assert "span_id = {span_id:String}" in fake_ch_query_client.last_query


def test_get_span_evaluations_uses_final(fake_ch_query_client) -> None:
    """Unlike list_traces (never FINAL), this is a narrow, single-span-scoped
    detail lookup -- the same "immediate dedup is cheap and correct here"
    reasoning app/clickhouse/query_repository.py's get_span already
    documents."""
    repo = _repo(fake_ch_query_client)
    repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert "FINAL" in fake_ch_query_client.last_query


def test_get_span_evaluations_wraps_fixedstring_columns_in_tostring(
    fake_ch_query_client,
) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    query = fake_ch_query_client.last_query
    assert "toString(trace_id) AS trace_id" in query
    assert "toString(span_id) AS span_id" in query
    assert "toString(project_id)" not in query  # native UUID column, no cast


def test_get_span_evaluations_never_selects_star(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert "SELECT *" not in fake_ch_query_client.last_query


def test_get_span_evaluations_orders_deterministically(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert "ORDER BY evaluator_name, evaluator_version" in fake_ch_query_client.last_query


def test_get_span_evaluations_never_uses_offset(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert "OFFSET" not in fake_ch_query_client.last_query.upper()


def test_get_span_evaluations_maps_rows(fake_ch_query_client) -> None:
    fake_ch_query_client.queue_result(column_names=_COLUMNS, rows=[tuple(_row().values())])
    repo = _repo(fake_ch_query_client)

    rows = repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert len(rows) == 1
    assert rows[0]["evaluation_id"] == EVALUATION_ID
    assert rows[0]["evaluator_name"] == "relevance"
    assert rows[0]["score"] == 0.87


def test_get_span_evaluations_returns_empty_list_when_nothing_evaluated(
    fake_ch_query_client,
) -> None:
    repo = _repo(fake_ch_query_client)
    rows = repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)
    assert rows == []


def test_get_span_evaluations_returns_multiple_evaluators(fake_ch_query_client) -> None:
    fake_ch_query_client.queue_result(
        column_names=_COLUMNS,
        rows=[
            tuple(_row(evaluator_name="relevance").values()),
            tuple(_row(evaluator_name="relevance_embedding", score=0.91).values()),
        ],
    )
    repo = _repo(fake_ch_query_client)

    rows = repo.get_span_evaluations(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert len(rows) == 2
    assert {row["evaluator_name"] for row in rows} == {"relevance", "relevance_embedding"}
