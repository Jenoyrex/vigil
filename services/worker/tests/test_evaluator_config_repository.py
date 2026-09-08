"""Repository-level tests for
worker.postgres.evaluator_config_repository.EvaluatorConfigRepository.

Uses `fake_postgres_connection` (see tests/conftest.py) instead of a real
PostgreSQL server, mirroring test_evaluation_jobs_repository.py's style.
"""

from __future__ import annotations

import uuid

from worker.postgres.evaluator_config_repository import EvaluatorConfig, EvaluatorConfigRepository

PROJECT_ID = uuid.uuid4()


def test_get_config_scopes_by_project_id_and_evaluator_name(fake_postgres_connection) -> None:
    repo = EvaluatorConfigRepository(fake_postgres_connection)
    repo.get_config(project_id=PROJECT_ID, evaluator_name="relevance_embedding")

    assert fake_postgres_connection.last_params == {
        "project_id": PROJECT_ID,
        "evaluator_name": "relevance_embedding",
    }
    query = fake_postgres_connection.last_query
    assert "project_id = %(project_id)s" in query
    assert "evaluator_name = %(evaluator_name)s" in query


def test_get_config_selects_only_the_required_fields(fake_postgres_connection) -> None:
    repo = EvaluatorConfigRepository(fake_postgres_connection)
    repo.get_config(project_id=PROJECT_ID, evaluator_name="relevance_embedding")

    query = fake_postgres_connection.last_query
    assert "SELECT enabled, sampling_rate, threshold" in query
    assert "SELECT *" not in query


def test_get_config_returns_none_when_no_row_exists(fake_postgres_connection) -> None:
    repo = EvaluatorConfigRepository(fake_postgres_connection)
    assert repo.get_config(project_id=PROJECT_ID, evaluator_name="relevance") is None


def test_get_config_returns_matching_row(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rows=[(True, 0.1, 0.75)])
    repo = EvaluatorConfigRepository(fake_postgres_connection)
    config = repo.get_config(project_id=PROJECT_ID, evaluator_name="relevance_embedding")

    assert config == EvaluatorConfig(enabled=True, sampling_rate=0.1, threshold=0.75)


def test_get_config_threshold_may_be_null(fake_postgres_connection) -> None:
    fake_postgres_connection.queue_result(rows=[(True, 1.0, None)])
    repo = EvaluatorConfigRepository(fake_postgres_connection)
    config = repo.get_config(project_id=PROJECT_ID, evaluator_name="relevance")

    assert config is not None
    assert config.threshold is None
