"""One end-to-end test against a real local ClickHouse (see
infrastructure/clickhouse/), exercising EvaluationResultsRepository directly
against the real `evaluation_results` table -- no mocking of the storage
layer. Mirrors
apps/api/tests/test_traces_clickhouse_integration.py's structure and
skip-if-unreachable convention.

Skipped automatically if ClickHouse isn't reachable, so the rest of the
suite (and CI environments without ClickHouse running) aren't blocked by it.
tests/test_evaluation_results_repository.py (fake client) should be
preferred for anything not specifically about real ClickHouse behavior --
this file is deliberately small.

Uses a fixed, obviously-fake `evaluation_id`/`project_id` (same convention
`infrastructure/clickhouse/verify_evaluation_results.sh` and
apps/api's own real-ClickHouse test use): re-running this test relies on
ReplacingMergeTree's dedup to keep assertions valid across repeated runs,
never on a clean database.
"""

from __future__ import annotations

import uuid

import pytest

from app.clickhouse.repository import EvaluationResultsRepository

TEST_EVALUATION_ID = uuid.UUID("00000000-0000-4000-8000-0000000000e2")
TEST_PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
TEST_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TEST_SPAN_ID = "00f067aa0ba902b7"


@pytest.fixture
def real_clickhouse_client():
    from app.clickhouse.client import get_clickhouse_client
    from app.config import settings

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


def _row_count(ch_client, *, final: bool) -> int:
    query = "SELECT count() FROM evaluation_results" + (" FINAL" if final else "")
    query += " WHERE project_id = {project_id:UUID} AND evaluation_id = {evaluation_id:UUID}"
    result = ch_client.query(
        query,
        parameters={"project_id": TEST_PROJECT_ID, "evaluation_id": TEST_EVALUATION_ID},
    )
    return result.result_rows[0][0]


def _result_row(**overrides) -> dict:
    row = {
        "evaluation_id": TEST_EVALUATION_ID,
        "project_id": TEST_PROJECT_ID,
        "trace_id": TEST_TRACE_ID,
        "span_id": TEST_SPAN_ID,
        "evaluator_name": "relevance_embedding",
        "evaluator_version": "0.1.0",
        "score": 0.87,
        "label": "relevant",
        "explanation": "integration test row",
        "evaluator_model": "BAAI/bge-small-en-v1.5",
        "evaluator_provider": None,
        "evaluation_latency_ms": 345.2,
        "evaluation_cost_usd": None,
        "job_created_at": "2026-09-08 12:00:00.000",
    }
    row.update(overrides)
    return row


def test_insert_query_and_duplicate_behavior_against_real_clickhouse(
    real_clickhouse_client,
) -> None:
    repo = EvaluationResultsRepository(real_clickhouse_client)

    repo.insert_results([_result_row()])

    result = repo.get_by_evaluation_id(project_id=TEST_PROJECT_ID, evaluation_id=TEST_EVALUATION_ID)
    assert result is not None
    assert result["evaluation_id"] == TEST_EVALUATION_ID
    assert result["project_id"] == TEST_PROJECT_ID
    assert result["trace_id"] == TEST_TRACE_ID
    assert result["span_id"] == TEST_SPAN_ID
    assert result["evaluator_name"] == "relevance_embedding"
    assert result["label"] == "relevant"

    # Re-insert the identical evaluation_id (simulates a retried write) --
    # ReplacingMergeTree provides eventual, not immediate, physical
    # deduplication.
    repo.insert_results([_result_row()])

    assert _row_count(real_clickhouse_client, final=False) >= 1
    assert _row_count(real_clickhouse_client, final=True) == 1

    real_clickhouse_client.command("OPTIMIZE TABLE evaluation_results FINAL")
    assert _row_count(real_clickhouse_client, final=False) == 1
