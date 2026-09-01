"""Repository-level tests for app.clickhouse.query_repository.TracesQueryRepository.

Uses `fake_ch_query_client` (see tests/conftest.py) instead of a real
ClickHouse server -- these tests assert the exact generated SQL and bound
parameters, so they catch a regression in tenant isolation or FINAL usage
even if a fake's canned response would otherwise mask it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from app.clickhouse.query_repository import TracesQueryRepository

PROJECT_ID = uuid.uuid4()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _repo(fake_ch_query_client) -> TracesQueryRepository:
    return TracesQueryRepository(fake_ch_query_client)


# -- list_traces --------------------------------------------------------


def test_list_traces_scopes_by_project_id(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=None,
        limit=20,
        cursor=None,
    )
    assert fake_ch_query_client.last_parameters["project_id"] == PROJECT_ID
    assert "project_id = {project_id:UUID}" in fake_ch_query_client.last_query


def test_list_traces_never_uses_final(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=None,
        limit=20,
        cursor=None,
    )
    assert "FINAL" not in fake_ch_query_client.last_query


def test_list_traces_orders_by_start_time_desc_trace_id_desc(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=None,
        limit=20,
        cursor=None,
    )
    assert "ORDER BY trace_start_time DESC, trace_id DESC" in fake_ch_query_client.last_query


def test_list_traces_applies_environment_and_resource_filters(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment="production",
        resource="checkout-service",
        has_error=None,
        limit=20,
        cursor=None,
    )
    query = fake_ch_query_client.last_query
    params = fake_ch_query_client.last_parameters
    assert "environment = {environment:String}" in query
    assert "resource = {resource:String}" in query
    assert params["environment"] == "production"
    assert params["resource"] == "checkout-service"


def test_list_traces_select_aliases_never_collide_with_filtered_column_names(
    fake_ch_query_client,
) -> None:
    """`any(environment) AS environment` combined with a `WHERE environment
    = ...` filter is rejected by real ClickHouse with "Aggregate function
    ... is found in WHERE" (ILLEGAL_AGGREGATION) -- ClickHouse resolves a
    SELECT alias against every reference to that name in the same query,
    including WHERE, when the alias shares a name with a real column.
    Verified directly against ClickHouse 24.8. The fake client here can't
    catch the ClickHouse-side error itself (see
    test_query_clickhouse_integration.py for the real-server regression
    test), but this locks in the alias names the fix depends on.
    """
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment="production",
        resource="checkout-service",
        has_error=None,
        limit=20,
        cursor=None,
    )
    query = fake_ch_query_client.last_query
    assert "AS trace_environment" in query
    assert "AS trace_resource" in query
    assert "AS environment" not in query
    assert "AS resource" not in query


def test_list_traces_has_error_true_filters_error_span_count(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=True,
        limit=20,
        cursor=None,
    )
    assert "HAVING" in fake_ch_query_client.last_query
    assert "error_span_count > 0" in fake_ch_query_client.last_query


def test_list_traces_has_error_false_filters_zero_errors(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=False,
        limit=20,
        cursor=None,
    )
    assert "error_span_count = 0" in fake_ch_query_client.last_query


def test_list_traces_cursor_adds_keyset_having_clause(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    cursor_time = datetime(2026, 8, 31, 0, 0, 0, tzinfo=UTC)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=None,
        limit=20,
        cursor=(cursor_time, TRACE_ID),
    )
    query = fake_ch_query_client.last_query
    params = fake_ch_query_client.last_parameters
    assert "(trace_start_time, trace_id) <" in query
    assert "OFFSET" not in query.upper()
    assert params["cursor_start_time"] == cursor_time
    assert params["cursor_trace_id"] == TRACE_ID


def test_list_traces_never_uses_offset(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=None,
        limit=20,
        cursor=None,
    )
    assert "OFFSET" not in fake_ch_query_client.last_query.upper()


def test_list_traces_binds_limit_as_parameter(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=None,
        limit=21,
        cursor=None,
    )
    assert fake_ch_query_client.last_parameters["limit"] == 21
    assert "LIMIT {limit:UInt32}" in fake_ch_query_client.last_query


def test_list_traces_casts_trace_id_to_string(fake_ch_query_client) -> None:
    """trace_id is a FixedString column -- without an explicit cast,
    clickhouse_connect would hand back raw bytes instead of str."""
    repo = _repo(fake_ch_query_client)
    repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=None,
        limit=20,
        cursor=None,
    )
    assert "toString(trace_id) AS trace_id" in fake_ch_query_client.last_query


def test_list_traces_returns_named_rows(fake_ch_query_client) -> None:
    fake_ch_query_client.queue_result(
        ("trace_id", "trace_start_time", "trace_end_time", "span_count"),
        [(TRACE_ID, NOW, NOW, 3)],
    )
    repo = _repo(fake_ch_query_client)
    rows = repo.list_traces(
        project_id=PROJECT_ID,
        start_time_from=NOW,
        start_time_to=NOW,
        environment=None,
        resource=None,
        has_error=None,
        limit=20,
        cursor=None,
    )
    assert rows == [
        {"trace_id": TRACE_ID, "trace_start_time": NOW, "trace_end_time": NOW, "span_count": 3}
    ]


# -- summarize_trace ------------------------------------------------------


def test_summarize_trace_scopes_by_project_and_trace_id(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.summarize_trace(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=None)
    query = fake_ch_query_client.last_query
    params = fake_ch_query_client.last_parameters
    assert "project_id = {project_id:UUID}" in query
    assert "trace_id = {trace_id:String}" in query
    assert params["project_id"] == PROJECT_ID
    assert params["trace_id"] == TRACE_ID


def test_summarize_trace_never_uses_final(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.summarize_trace(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=None)
    assert "FINAL" not in fake_ch_query_client.last_query


def test_summarize_trace_optional_start_date_prunes_partition(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.summarize_trace(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=date(2026, 8, 31))
    query = fake_ch_query_client.last_query
    params = fake_ch_query_client.last_parameters
    assert "toDate(start_time) = {start_date:Date}" in query
    assert params["start_date"] == date(2026, 8, 31)


def test_summarize_trace_returns_none_when_zero_spans(fake_ch_query_client) -> None:
    fake_ch_query_client.queue_result(
        (
            "total_span_count",
            "error_span_count",
            "root_span_count",
            "trace_start_time",
            "trace_end_time",
        ),
        [(0, 0, 0, None, None)],
    )
    repo = _repo(fake_ch_query_client)
    result = repo.summarize_trace(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=None)
    assert result is None


def test_summarize_trace_returns_row_when_spans_exist(fake_ch_query_client) -> None:
    fake_ch_query_client.queue_result(
        (
            "total_span_count",
            "error_span_count",
            "root_span_count",
            "trace_start_time",
            "trace_end_time",
        ),
        [(3, 1, 1, NOW, NOW)],
    )
    repo = _repo(fake_ch_query_client)
    result = repo.summarize_trace(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=None)
    assert result == {
        "total_span_count": 3,
        "error_span_count": 1,
        "root_span_count": 1,
        "trace_start_time": NOW,
        "trace_end_time": NOW,
    }


# -- get_trace_spans --------------------------------------------------------


def test_get_trace_spans_uses_final(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_trace_spans(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=None, limit=2001)
    assert "FROM spans FINAL" in fake_ch_query_client.last_query


def test_get_trace_spans_scopes_by_project_and_trace_id(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_trace_spans(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=None, limit=2001)
    params = fake_ch_query_client.last_parameters
    assert params["project_id"] == PROJECT_ID
    assert params["trace_id"] == TRACE_ID


def test_get_trace_spans_orders_start_time_asc(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_trace_spans(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=None, limit=2001)
    assert "ORDER BY start_time ASC" in fake_ch_query_client.last_query


def test_get_trace_spans_casts_span_and_parent_span_id_to_string(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_trace_spans(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=None, limit=2001)
    query = fake_ch_query_client.last_query
    assert "toString(span_id) AS span_id" in query
    assert "toString(parent_span_id) AS parent_span_id" in query


def test_get_trace_spans_never_selects_project_id(fake_ch_query_client) -> None:
    """project_id must never be echoed back to the client."""
    repo = _repo(fake_ch_query_client)
    repo.get_trace_spans(project_id=PROJECT_ID, trace_id=TRACE_ID, start_date=None, limit=2001)
    select_clause = fake_ch_query_client.last_query.split("FROM spans")[0]
    assert "project_id" not in select_clause


# -- get_span --------------------------------------------------------------


def test_get_span_uses_final(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID, start_date=None)
    assert "FROM spans FINAL" in fake_ch_query_client.last_query


def test_get_span_scopes_by_project_trace_and_span_id(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID, start_date=None)
    params = fake_ch_query_client.last_parameters
    assert params["project_id"] == PROJECT_ID
    assert params["trace_id"] == TRACE_ID
    assert params["span_id"] == SPAN_ID


def test_get_span_limits_to_one_row(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.get_span(project_id=PROJECT_ID, trace_id=TRACE_ID, span_id=SPAN_ID, start_date=None)
    assert "LIMIT 1" in fake_ch_query_client.last_query
