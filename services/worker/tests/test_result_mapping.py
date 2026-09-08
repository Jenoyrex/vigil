"""Tests for worker.result_mapping.evaluation_result_to_row -- pure
function, no fakes needed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.types import EvaluationResult

from worker.postgres.repository import ClaimedJob
from worker.result_mapping import evaluation_result_to_row

JOB_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
CREATED_AT = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


def _job(**overrides) -> ClaimedJob:
    defaults = {
        "id": JOB_ID,
        "project_id": PROJECT_ID,
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": "00f067aa0ba902b7",
        "evaluator_name": "relevance_embedding",
        "evaluator_version": "0.1.0",
        "attempt_count": 1,
        "max_retries": 3,
        "created_at": CREATED_AT,
    }
    defaults.update(overrides)
    return ClaimedJob(**defaults)


def _result(**overrides) -> EvaluationResult:
    defaults = {
        "evaluator_name": "relevance_embedding",
        "evaluator_version": "0.1.0",
        "score": 0.87,
        "label": "relevant",
        "explanation": "cosine similarity 0.8700; threshold=0.5000 -> label='relevant'.",
        "evaluation_latency_ms": 345.2,
        "evaluation_cost_usd": None,
        "evaluator_model": "BAAI/bge-small-en-v1.5",
        "evaluator_provider": None,
    }
    defaults.update(overrides)
    return EvaluationResult(**defaults)


def test_evaluation_id_comes_from_the_job_id_not_generated() -> None:
    job = _job()
    row = evaluation_result_to_row(_result(), job)
    assert row["evaluation_id"] == JOB_ID
    assert row["evaluation_id"] == job.id


def test_job_created_at_carries_through_verbatim() -> None:
    row = evaluation_result_to_row(_result(), _job(created_at=CREATED_AT))
    assert row["job_created_at"] == CREATED_AT


def test_row_does_not_include_written_at() -> None:
    row = evaluation_result_to_row(_result(), _job())
    assert "written_at" not in row


def test_row_maps_every_required_field() -> None:
    from worker.clickhouse.repository import RESULT_COLUMNS

    job = _job()
    result = _result()
    row = evaluation_result_to_row(result, job)

    assert set(row) == set(RESULT_COLUMNS)
    assert row["project_id"] == job.project_id
    assert row["trace_id"] == job.trace_id
    assert row["span_id"] == job.span_id
    assert row["evaluator_name"] == result.evaluator_name
    assert row["evaluator_version"] == result.evaluator_version
    assert row["score"] == result.score
    assert row["label"] == result.label
    assert row["explanation"] == result.explanation
    assert row["evaluator_model"] == result.evaluator_model
    assert row["evaluator_provider"] == result.evaluator_provider
    assert row["evaluation_latency_ms"] == result.evaluation_latency_ms
    assert row["evaluation_cost_usd"] == result.evaluation_cost_usd


def test_row_is_insertable_via_the_real_repository(fake_clickhouse_client) -> None:
    """End-to-end within the fake: the row this function produces must be
    exactly what EvaluationResultsRepository.insert_results expects --
    catches a field-name drift between the two modules immediately."""
    from worker.clickhouse.repository import EvaluationResultsRepository

    row = evaluation_result_to_row(_result(), _job())
    EvaluationResultsRepository(fake_clickhouse_client).insert_results([row])

    assert fake_clickhouse_client.last_insert.table == "evaluation_results"
