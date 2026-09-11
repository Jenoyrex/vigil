"""Unit tests for worker.clickhouse.eligible_span_repository.EligibleSpanRepository.

Uses `fake_clickhouse_client` (tests/conftest.py) -- these tests assert the
exact SQL text/parameters passed to `.query(...)`, in particular that
`start_time >= :start_date_hint` and `ingested_at > :lower_bound` are each
included only when their corresponding argument is not None, and that
`ORDER BY ... LIMIT` never uses OFFSET. Real ClickHouse scan/ordering
behavior is proven separately in
test_eligible_span_repository_clickhouse_integration.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from worker.clickhouse.eligible_span_repository import EligibleSpanRepository

PROJECT_ID = uuid.uuid4()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
INGESTED_AT = datetime(2026, 9, 10, 12, 0, 0, 123000, tzinfo=UTC)


def test_query_filters_span_type_llm(fake_clickhouse_client) -> None:
    repo = EligibleSpanRepository(fake_clickhouse_client)
    repo.select_eligible_spans(lower_bound=None, start_date_hint=None, batch_size=10)

    assert "span_type = 'llm'" in fake_clickhouse_client.last_query


def test_lower_bound_predicate_included_when_given(fake_clickhouse_client) -> None:
    lower_bound = datetime(2026, 9, 10, 11, 0, 0, tzinfo=UTC)
    repo = EligibleSpanRepository(fake_clickhouse_client)
    repo.select_eligible_spans(lower_bound=lower_bound, start_date_hint=None, batch_size=10)

    query = fake_clickhouse_client.last_query
    assert "ingested_at > {lower_bound:DateTime64(3)}" in query
    assert fake_clickhouse_client.last_parameters["lower_bound"] == lower_bound


def test_lower_bound_predicate_omitted_when_none(fake_clickhouse_client) -> None:
    """lower_bound=None means "no checkpoint yet" -- the predicate must be
    omitted entirely, never compared against NULL (which would match
    nothing in SQL and silently scan zero rows)."""
    repo = EligibleSpanRepository(fake_clickhouse_client)
    repo.select_eligible_spans(lower_bound=None, start_date_hint=None, batch_size=10)

    query = fake_clickhouse_client.last_query
    assert "ingested_at >" not in query
    assert "lower_bound" not in fake_clickhouse_client.last_parameters


def test_start_date_hint_predicate_included_when_given(fake_clickhouse_client) -> None:
    hint = date(2026, 9, 7)
    repo = EligibleSpanRepository(fake_clickhouse_client)
    repo.select_eligible_spans(lower_bound=None, start_date_hint=hint, batch_size=10)

    query = fake_clickhouse_client.last_query
    assert "start_time >= {start_date_hint:Date}" in query
    assert fake_clickhouse_client.last_parameters["start_date_hint"] == hint


def test_start_date_hint_predicate_omitted_when_none(fake_clickhouse_client) -> None:
    repo = EligibleSpanRepository(fake_clickhouse_client)
    repo.select_eligible_spans(lower_bound=None, start_date_hint=None, batch_size=10)

    query = fake_clickhouse_client.last_query
    assert "start_time >=" not in query
    assert "start_date_hint" not in fake_clickhouse_client.last_parameters


def test_deterministic_ordering_no_offset(fake_clickhouse_client) -> None:
    repo = EligibleSpanRepository(fake_clickhouse_client)
    repo.select_eligible_spans(lower_bound=None, start_date_hint=None, batch_size=10)

    query = fake_clickhouse_client.last_query
    assert "ORDER BY ingested_at, span_id" in query
    assert "OFFSET" not in query.upper()
    assert "LIMIT {batch_size:UInt32}" in query


def test_batch_size_bound_passed_through(fake_clickhouse_client) -> None:
    repo = EligibleSpanRepository(fake_clickhouse_client)
    repo.select_eligible_spans(lower_bound=None, start_date_hint=None, batch_size=42)

    assert fake_clickhouse_client.last_parameters["batch_size"] == 42


def test_trace_id_and_span_id_wrapped_in_tostring_not_project_id(fake_clickhouse_client) -> None:
    """FixedString columns (trace_id, span_id) need toString(...) to decode
    as str; project_id is a native UUID column and must not be wrapped."""
    repo = EligibleSpanRepository(fake_clickhouse_client)
    repo.select_eligible_spans(lower_bound=None, start_date_hint=None, batch_size=10)

    query = fake_clickhouse_client.last_query
    assert "toString(trace_id) AS trace_id" in query
    assert "toString(span_id) AS span_id" in query
    assert "toString(project_id)" not in query


def test_maps_rows_to_eligible_span(fake_clickhouse_client) -> None:
    fake_clickhouse_client.queue_result(
        column_names=("project_id", "trace_id", "span_id", "ingested_at"),
        rows=[(PROJECT_ID, TRACE_ID, SPAN_ID, INGESTED_AT)],
    )
    repo = EligibleSpanRepository(fake_clickhouse_client)
    spans = repo.select_eligible_spans(lower_bound=None, start_date_hint=None, batch_size=10)

    assert len(spans) == 1
    span = spans[0]
    assert span.project_id == PROJECT_ID
    assert span.trace_id == TRACE_ID
    assert span.span_id == SPAN_ID
    assert span.ingested_at == INGESTED_AT


def test_naive_ingested_at_normalized_to_utc(fake_clickhouse_client) -> None:
    naive_ingested_at = datetime(2026, 9, 10, 12, 0, 0, 123000)  # no tzinfo, as ClickHouse returns
    fake_clickhouse_client.queue_result(
        column_names=("project_id", "trace_id", "span_id", "ingested_at"),
        rows=[(PROJECT_ID, TRACE_ID, SPAN_ID, naive_ingested_at)],
    )
    repo = EligibleSpanRepository(fake_clickhouse_client)
    [span] = repo.select_eligible_spans(lower_bound=None, start_date_hint=None, batch_size=10)

    assert span.ingested_at.tzinfo is not None
    assert span.ingested_at == naive_ingested_at.replace(tzinfo=UTC)


def test_returns_empty_list_when_nothing_eligible(fake_clickhouse_client) -> None:
    repo = EligibleSpanRepository(fake_clickhouse_client)
    assert repo.select_eligible_spans(lower_bound=None, start_date_hint=None, batch_size=10) == []
