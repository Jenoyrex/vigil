"""Orchestration tests for worker.execution.execute_job.

Every dependency is a fake or a wrapped-real-repository-around-a-fake-client
(fake_clickhouse_client/fake_postgres_connection from tests/conftest.py),
and the evaluator is a lightweight `FakeEvaluator` test double -- so these
tests are fast, deterministic, and independent of either registered
evaluator's actual scoring behavior. Call-ordering assertions (persist
before mark_succeeded) use `unittest.mock.Mock.attach_mock` to get one
ordered call list across two different repository objects' methods.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.types import EvaluationResult
from clickhouse_connect.driver.exceptions import ClickHouseError

from worker.clickhouse.repository import ClickHouseInsertError, EvaluationResultsRepository
from worker.clickhouse.span_repository import SourceSpanRepository
from worker.execution import EvaluationOutcome, SourceSpanNotFoundError, execute_job
from worker.postgres.evaluator_config_repository import EvaluatorConfigRepository
from worker.postgres.repository import ClaimedJob, EvaluationJobsRepository
from worker.registry import EvaluatorRegistry, UnknownEvaluatorError

JOB_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
CREATED_AT = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

VALID_SPAN_ROW = ("hello", False, "world", False)
SPAN_COLUMNS = ("input", "input_truncated", "output", "output_truncated")


class FakeEvaluator:
    """Minimal `Evaluator`-shaped test double: records every `evaluate()`
    call's input/threshold and returns a scripted, deterministic result."""

    name = "fake_evaluator"
    version = "0.1.0"

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []

    def evaluate(self, evaluator_input, *, threshold=None) -> EvaluationResult:
        self.calls.append(SimpleNamespace(evaluator_input=evaluator_input, threshold=threshold))
        return EvaluationResult(
            evaluator_name=self.name,
            evaluator_version=self.version,
            score=0.9,
            label="relevant",
            explanation="fake",
            evaluation_latency_ms=1.0,
            evaluation_cost_usd=None,
            evaluator_model="fake-model",
            evaluator_provider=None,
        )


def _job(**overrides) -> ClaimedJob:
    defaults = {
        "id": JOB_ID,
        "project_id": PROJECT_ID,
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "evaluator_name": "fake_evaluator",
        "evaluator_version": "0.1.0",
        "attempt_count": 1,
        "max_retries": 3,
        "created_at": CREATED_AT,
    }
    defaults.update(overrides)
    return ClaimedJob(**defaults)


@pytest.fixture
def fake_evaluator() -> FakeEvaluator:
    return FakeEvaluator()


@pytest.fixture
def registry(fake_evaluator: FakeEvaluator) -> EvaluatorRegistry:
    return EvaluatorRegistry(evaluators=[fake_evaluator])


@pytest.fixture
def span_repository(fake_clickhouse_client) -> SourceSpanRepository:
    return SourceSpanRepository(fake_clickhouse_client)


@pytest.fixture
def results_repository(fake_clickhouse_client) -> EvaluationResultsRepository:
    return EvaluationResultsRepository(fake_clickhouse_client)


@pytest.fixture
def evaluator_config_repository(fake_postgres_connection) -> EvaluatorConfigRepository:
    return EvaluatorConfigRepository(fake_postgres_connection)


@pytest.fixture
def jobs_repository(fake_postgres_connection) -> EvaluationJobsRepository:
    return EvaluationJobsRepository(fake_postgres_connection)


def _queue_span(fake_clickhouse_client, row=VALID_SPAN_ROW) -> None:
    fake_clickhouse_client.queue_result(SPAN_COLUMNS, [row])


def _queue_config(fake_postgres_connection, *, threshold: float | None) -> None:
    fake_postgres_connection.queue_result(rows=[(True, 0.1, threshold)])


# -- happy path / ordering ---------------------------------------------------


def test_execute_job_persists_result_before_marking_succeeded(
    fake_clickhouse_client,
    fake_postgres_connection,
    registry,
    span_repository,
    evaluator_config_repository,
    results_repository,
    jobs_repository,
) -> None:
    _queue_span(fake_clickhouse_client)
    _queue_config(fake_postgres_connection, threshold=None)
    fake_postgres_connection.queue_result(rowcount=1)  # mark_succeeded response

    manager = Mock()
    manager.attach_mock(Mock(wraps=results_repository.insert_results), "insert_results")
    manager.attach_mock(Mock(wraps=jobs_repository.mark_succeeded), "mark_succeeded")
    results_repository.insert_results = manager.insert_results
    jobs_repository.mark_succeeded = manager.mark_succeeded

    execute_job(
        _job(),
        registry=registry,
        span_repository=span_repository,
        evaluator_config_repository=evaluator_config_repository,
        results_repository=results_repository,
        jobs_repository=jobs_repository,
    )

    assert [call[0] for call in manager.mock_calls] == ["insert_results", "mark_succeeded"]


def test_execute_job_returns_the_result_and_marked_succeeded_flag(
    fake_clickhouse_client,
    fake_postgres_connection,
    registry,
    span_repository,
    evaluator_config_repository,
    results_repository,
    jobs_repository,
) -> None:
    _queue_span(fake_clickhouse_client)
    _queue_config(fake_postgres_connection, threshold=None)
    fake_postgres_connection.queue_result(rowcount=1)

    outcome = execute_job(
        _job(),
        registry=registry,
        span_repository=span_repository,
        evaluator_config_repository=evaluator_config_repository,
        results_repository=results_repository,
        jobs_repository=jobs_repository,
    )

    assert isinstance(outcome, EvaluationOutcome)
    assert outcome.result.label == "relevant"
    assert outcome.marked_succeeded is True


def test_execute_job_inserts_a_row_with_evaluation_id_equal_to_job_id(
    fake_clickhouse_client,
    fake_postgres_connection,
    registry,
    span_repository,
    evaluator_config_repository,
    results_repository,
    jobs_repository,
) -> None:
    _queue_span(fake_clickhouse_client)
    _queue_config(fake_postgres_connection, threshold=None)
    fake_postgres_connection.queue_result(rowcount=1)

    job = _job()
    execute_job(
        job,
        registry=registry,
        span_repository=span_repository,
        evaluator_config_repository=evaluator_config_repository,
        results_repository=results_repository,
        jobs_repository=jobs_repository,
    )

    from worker.clickhouse.repository import RESULT_COLUMNS

    inserted_row = dict(
        zip(RESULT_COLUMNS, fake_clickhouse_client.last_insert.data[0], strict=True)
    )
    assert inserted_row["evaluation_id"] == job.id


# -- threshold resolution -----------------------------------------------------


def test_execute_job_passes_configured_threshold_to_the_evaluator(
    fake_clickhouse_client,
    fake_postgres_connection,
    registry,
    fake_evaluator,
    span_repository,
    evaluator_config_repository,
    results_repository,
    jobs_repository,
) -> None:
    _queue_span(fake_clickhouse_client)
    _queue_config(fake_postgres_connection, threshold=0.9)
    fake_postgres_connection.queue_result(rowcount=1)

    execute_job(
        _job(),
        registry=registry,
        span_repository=span_repository,
        evaluator_config_repository=evaluator_config_repository,
        results_repository=results_repository,
        jobs_repository=jobs_repository,
    )

    assert fake_evaluator.calls[-1].threshold == 0.9


def test_execute_job_passes_none_threshold_when_no_config_row_exists(
    fake_clickhouse_client,
    fake_postgres_connection,
    registry,
    fake_evaluator,
    span_repository,
    evaluator_config_repository,
    results_repository,
    jobs_repository,
) -> None:
    _queue_span(fake_clickhouse_client)
    # No queued postgres result at all -> the config lookup's `.execute()`
    # call gets the fake's default empty response -> get_config returns
    # None. mark_succeeded's own (also unqueued, also default) response is
    # irrelevant to this test.

    execute_job(
        _job(),
        registry=registry,
        span_repository=span_repository,
        evaluator_config_repository=evaluator_config_repository,
        results_repository=results_repository,
        jobs_repository=jobs_repository,
    )

    assert fake_evaluator.calls[-1].threshold is None


# -- ClickHouse failure prevents mark_succeeded -------------------------------


def test_clickhouse_insert_failure_prevents_mark_succeeded(
    fake_clickhouse_client,
    fake_postgres_connection,
    registry,
    span_repository,
    evaluator_config_repository,
    results_repository,
    jobs_repository,
) -> None:
    _queue_span(fake_clickhouse_client)
    _queue_config(fake_postgres_connection, threshold=None)

    # Fail only the insert, not the (already-succeeded) span query --
    # `fail_with` applies to every call the fake makes, which would also
    # break the span fetch this test needs to succeed first. A raw
    # ClickHouseError from the driver is mapped, inside
    # EvaluationResultsRepository.insert_results, to ClickHouseInsertError
    # -- see worker/clickhouse/repository.py and its own
    # test_insert_results_maps_clickhouse_error_to_insert_error.
    def _raise_on_insert(*_args, **_kwargs):
        raise ClickHouseError("insert rejected")

    fake_clickhouse_client.insert = _raise_on_insert

    with pytest.raises(ClickHouseInsertError):
        execute_job(
            _job(),
            registry=registry,
            span_repository=span_repository,
            evaluator_config_repository=evaluator_config_repository,
            results_repository=results_repository,
            jobs_repository=jobs_repository,
        )

    # Only the config lookup happened on PostgreSQL -- mark_succeeded was
    # never reached.
    assert len(fake_postgres_connection.calls) == 1


# -- missing span --------------------------------------------------------------


def test_missing_span_raises_structured_error_and_touches_no_persistence(
    fake_clickhouse_client,
    fake_postgres_connection,
    registry,
    span_repository,
    evaluator_config_repository,
    results_repository,
    jobs_repository,
) -> None:
    # No queued span result -> get_span returns None.
    job = _job()

    with pytest.raises(SourceSpanNotFoundError) as exc_info:
        execute_job(
            job,
            registry=registry,
            span_repository=span_repository,
            evaluator_config_repository=evaluator_config_repository,
            results_repository=results_repository,
            jobs_repository=jobs_repository,
        )

    assert exc_info.value.job_id == job.id
    assert fake_clickhouse_client.insert_calls == []
    assert fake_postgres_connection.calls == []


# -- missing evaluator ----------------------------------------------------------


def test_missing_evaluator_raises_structured_error_and_touches_no_persistence(
    fake_clickhouse_client,
    fake_postgres_connection,
    span_repository,
    evaluator_config_repository,
    results_repository,
    jobs_repository,
) -> None:
    empty_registry = EvaluatorRegistry(evaluators=[])
    job = _job(evaluator_name="relevance", evaluator_version="9.9.9")

    with pytest.raises(UnknownEvaluatorError):
        execute_job(
            job,
            registry=empty_registry,
            span_repository=span_repository,
            evaluator_config_repository=evaluator_config_repository,
            results_repository=results_repository,
            jobs_repository=jobs_repository,
        )

    assert fake_clickhouse_client.insert_calls == []
    assert fake_clickhouse_client.query_calls == []
    assert fake_postgres_connection.calls == []
