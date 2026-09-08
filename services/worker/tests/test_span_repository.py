"""Repository-level tests for
worker.clickhouse.span_repository.SourceSpanRepository.

Uses `fake_clickhouse_client` (see tests/conftest.py) instead of a real
ClickHouse server -- asserts the exact generated SQL/bound parameters and
that only the four evaluator-relevant columns are selected (never
`SELECT *`), mirroring test_evaluation_results_repository.py's style for
the sibling ClickHouse repository.
"""

from __future__ import annotations

import uuid

import pytest
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError

from worker.clickhouse.repository import ClickHouseQueryError, ClickHouseUnavailableError
from worker.clickhouse.span_repository import SourceSpanRepository

PROJECT_ID = uuid.uuid4()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


def test_get_span_scopes_by_project_trace_and_span_id(fake_clickhouse_client) -> None:
    repo = SourceSpanRepository(fake_clickhouse_client)
    repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert fake_clickhouse_client.last_parameters == {
        "project_id": PROJECT_ID,
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
    }
    query = fake_clickhouse_client.last_query
    assert "project_id = {project_id:UUID}" in query
    assert "trace_id = {trace_id:String}" in query
    assert "span_id = {span_id:String}" in query


def test_get_span_selects_only_the_four_required_columns(fake_clickhouse_client) -> None:
    repo = SourceSpanRepository(fake_clickhouse_client)
    repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    query = fake_clickhouse_client.last_query
    assert "SELECT *" not in query
    for column in ("input", "input_truncated", "output", "output_truncated"):
        assert column in query


def test_get_span_uses_final(fake_clickhouse_client) -> None:
    repo = SourceSpanRepository(fake_clickhouse_client)
    repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)
    assert "FINAL" in fake_clickhouse_client.last_query


def test_get_span_returns_none_when_not_found(fake_clickhouse_client) -> None:
    repo = SourceSpanRepository(fake_clickhouse_client)
    assert repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID) is None


def test_get_span_returns_matching_row(fake_clickhouse_client) -> None:
    fake_clickhouse_client.queue_result(
        ("input", "input_truncated", "output", "output_truncated"),
        [("hello", False, "world", False)],
    )
    repo = SourceSpanRepository(fake_clickhouse_client)
    span = repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)

    assert span == {
        "input": "hello",
        "input_truncated": False,
        "output": "world",
        "output_truncated": False,
    }


def test_get_span_maps_operational_error_to_unavailable(fake_clickhouse_client) -> None:
    fake_clickhouse_client.fail_with = OperationalError("connection refused")
    repo = SourceSpanRepository(fake_clickhouse_client)
    with pytest.raises(ClickHouseUnavailableError):
        repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)


def test_get_span_maps_clickhouse_error_to_query_error(fake_clickhouse_client) -> None:
    fake_clickhouse_client.fail_with = ClickHouseError("bad query")
    repo = SourceSpanRepository(fake_clickhouse_client)
    with pytest.raises(ClickHouseQueryError):
        repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID)
