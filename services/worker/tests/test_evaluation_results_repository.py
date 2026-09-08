"""Repository-level tests for app.clickhouse.repository.EvaluationResultsRepository.

Uses `fake_clickhouse_client` (see tests/conftest.py) instead of a real
ClickHouse server -- these tests assert the exact column order passed to
`.insert(...)` and the exact generated SQL/bound parameters passed to
`.query(...)`, mirroring apps/api/tests/test_query_repository.py's style for
TracesQueryRepository.
"""

from __future__ import annotations

import uuid

import pytest
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

from app.clickhouse.repository import (
    RESULT_COLUMNS,
    ClickHouseInsertError,
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    EvaluationResultsRepository,
)

EVALUATION_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


def _result_row(**overrides) -> dict:
    row = {
        "evaluation_id": EVALUATION_ID,
        "project_id": PROJECT_ID,
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "evaluator_name": "relevance_embedding",
        "evaluator_version": "0.1.0",
        "score": 0.87,
        "label": "relevant",
        "explanation": "cosine similarity 0.8700; threshold=0.5000 -> label='relevant'.",
        "evaluator_model": "BAAI/bge-small-en-v1.5",
        "evaluator_provider": None,
        "evaluation_latency_ms": 345.2,
        "evaluation_cost_usd": None,
        "job_created_at": "2026-09-08T12:00:00.000",
    }
    row.update(overrides)
    return row


# -- insert_results -------------------------------------------------------


def test_insert_results_no_op_on_empty_list(fake_clickhouse_client) -> None:
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    repo.insert_results([])
    assert fake_clickhouse_client.insert_calls == []


def test_insert_results_uses_exact_column_order(fake_clickhouse_client) -> None:
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    repo.insert_results([_result_row()])

    call = fake_clickhouse_client.last_insert
    assert call.table == "evaluation_results"
    assert tuple(call.column_names) == RESULT_COLUMNS
    assert "written_at" not in call.column_names


def test_insert_results_row_values_match_column_order(fake_clickhouse_client) -> None:
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    row = _result_row()
    repo.insert_results([row])

    call = fake_clickhouse_client.last_insert
    inserted_row = call.data[0]
    assert inserted_row == [row[column] for column in RESULT_COLUMNS]


def test_insert_results_batches_multiple_rows_in_one_call(fake_clickhouse_client) -> None:
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    repo.insert_results([_result_row(), _result_row(evaluation_id=uuid.uuid4())])

    assert len(fake_clickhouse_client.insert_calls) == 1
    assert len(fake_clickhouse_client.last_insert.data) == 2


def test_insert_results_maps_operational_error_to_unavailable(fake_clickhouse_client) -> None:
    fake_clickhouse_client.fail_with = OperationalError("connection refused")
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    with pytest.raises(ClickHouseUnavailableError):
        repo.insert_results([_result_row()])


def test_insert_results_maps_clickhouse_error_to_insert_error(fake_clickhouse_client) -> None:
    fake_clickhouse_client.fail_with = ClickHouseError("type mismatch")
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    with pytest.raises(ClickHouseInsertError):
        repo.insert_results([_result_row()])


# -- get_by_evaluation_id --------------------------------------------------


def test_get_by_evaluation_id_scopes_by_project_and_evaluation_id(fake_clickhouse_client) -> None:
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    repo.get_by_evaluation_id(project_id=PROJECT_ID, evaluation_id=EVALUATION_ID)

    assert fake_clickhouse_client.last_parameters == {
        "project_id": PROJECT_ID,
        "evaluation_id": EVALUATION_ID,
    }
    assert "project_id = {project_id:UUID}" in fake_clickhouse_client.last_query
    assert "evaluation_id = {evaluation_id:UUID}" in fake_clickhouse_client.last_query


def test_get_by_evaluation_id_uses_final(fake_clickhouse_client) -> None:
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    repo.get_by_evaluation_id(project_id=PROJECT_ID, evaluation_id=EVALUATION_ID)

    assert "FINAL" in fake_clickhouse_client.last_query


def test_get_by_evaluation_id_returns_none_when_not_found(fake_clickhouse_client) -> None:
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    result = repo.get_by_evaluation_id(project_id=PROJECT_ID, evaluation_id=EVALUATION_ID)
    assert result is None


def test_get_by_evaluation_id_returns_matching_row(fake_clickhouse_client) -> None:
    fake_clickhouse_client.queue_result(
        ("evaluation_id", "project_id", "label", "score"),
        [(EVALUATION_ID, PROJECT_ID, "relevant", 0.87)],
    )
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    result = repo.get_by_evaluation_id(project_id=PROJECT_ID, evaluation_id=EVALUATION_ID)

    assert result == {
        "evaluation_id": EVALUATION_ID,
        "project_id": PROJECT_ID,
        "label": "relevant",
        "score": 0.87,
    }


def test_get_by_evaluation_id_maps_operational_error_to_unavailable(fake_clickhouse_client) -> None:
    fake_clickhouse_client.fail_with = OperationalError("connection refused")
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    with pytest.raises(ClickHouseUnavailableError):
        repo.get_by_evaluation_id(project_id=PROJECT_ID, evaluation_id=EVALUATION_ID)


def test_get_by_evaluation_id_maps_clickhouse_error_to_query_error(fake_clickhouse_client) -> None:
    fake_clickhouse_client.fail_with = ClickHouseError("bad query")
    repo = EvaluationResultsRepository(fake_clickhouse_client)
    with pytest.raises(ClickHouseQueryError):
        repo.get_by_evaluation_id(project_id=PROJECT_ID, evaluation_id=EVALUATION_ID)
