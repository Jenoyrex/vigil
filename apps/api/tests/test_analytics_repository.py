"""Repository-level tests for app.clickhouse.analytics_repository.AnalyticsRepository.

Uses `fake_ch_query_client` (see tests/conftest.py) -- asserts exact SQL
and bound parameters, so a regression in tenant isolation, the "no FINAL on
analytics" rule, or the llm_provider IS NOT NULL filter is caught even
though a fake's canned response would otherwise mask it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.clickhouse.analytics_repository import AnalyticsRepository, _safe_identifier

PROJECT_ID = uuid.uuid4()
FROM = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
TO = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)


def _repo(fake_ch_query_client) -> AnalyticsRepository:
    return AnalyticsRepository(fake_ch_query_client)


# -- span_analytics -----------------------------------------------------


def test_span_analytics_scopes_by_project_id(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.span_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by=None,
        bucket=None,
    )
    assert fake_ch_query_client.last_parameters["project_id"] == PROJECT_ID
    assert "project_id = {project_id:UUID}" in fake_ch_query_client.last_query


def test_span_analytics_never_uses_final(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.span_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by=None,
        bucket=None,
    )
    assert "FINAL" not in fake_ch_query_client.last_query


def test_span_analytics_flat_uses_quantile_not_quantile_exact(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.span_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by=None,
        bucket=None,
    )
    query = fake_ch_query_client.last_query
    assert "quantile(0.50)(duration_ms)" in query
    assert "quantileExact" not in query


def test_span_analytics_group_by_environment_selects_column(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.span_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by="environment",
        bucket=None,
    )
    query = fake_ch_query_client.last_query
    assert "environment AS group_value" in query
    assert "GROUP BY group_value" in query
    assert "ORDER BY span_count DESC" in query


def test_span_analytics_group_by_caps_at_50(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.span_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by="span_type",
        bucket=None,
    )
    assert "LIMIT 50" in fake_ch_query_client.last_query


def test_span_analytics_bucket_hour_uses_start_of_hour(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.span_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by=None,
        bucket="hour",
    )
    query = fake_ch_query_client.last_query
    assert "toStartOfHour(start_time) AS bucket_start" in query
    assert "ORDER BY bucket_start ASC" in query


def test_span_analytics_bucket_day_uses_start_of_day(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.span_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by=None,
        bucket="day",
    )
    assert "toStartOfDay(start_time) AS bucket_start" in fake_ch_query_client.last_query


def test_span_analytics_applies_environment_resource_span_type_filters(
    fake_ch_query_client,
) -> None:
    repo = _repo(fake_ch_query_client)
    repo.span_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment="production",
        resource="checkout-service",
        span_type="llm",
        group_by=None,
        bucket=None,
    )
    query = fake_ch_query_client.last_query
    params = fake_ch_query_client.last_parameters
    assert "environment = {environment:String}" in query
    assert "resource = {resource:String}" in query
    assert "span_type = {span_type:String}" in query
    assert params["environment"] == "production"
    assert params["resource"] == "checkout-service"
    assert params["span_type"] == "llm"


# -- llm_usage_analytics ----------------------------------------------------


def test_llm_usage_scopes_by_project_id(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.llm_usage_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        group_by=None,
    )
    assert fake_ch_query_client.last_parameters["project_id"] == PROJECT_ID


def test_llm_usage_never_uses_final(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.llm_usage_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        group_by=None,
    )
    assert "FINAL" not in fake_ch_query_client.last_query


def test_llm_usage_filters_llm_provider_is_not_null(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.llm_usage_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        group_by=None,
    )
    assert "llm_provider IS NOT NULL" in fake_ch_query_client.last_query


def test_llm_usage_flat_does_not_filter_by_span_type(fake_ch_query_client) -> None:
    """The 'is this an LLM span' signal is llm_provider, never span_type."""
    repo = _repo(fake_ch_query_client)
    repo.llm_usage_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        group_by=None,
    )
    assert "span_type" not in fake_ch_query_client.last_query


def test_llm_usage_group_by_llm_model(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.llm_usage_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        group_by="llm_model",
    )
    query = fake_ch_query_client.last_query
    assert "llm_model AS group_value" in query
    assert "ORDER BY total_cost_usd DESC" in query
    assert "LIMIT 50" in query


def test_llm_usage_sums_are_coalesced_to_zero(fake_ch_query_client) -> None:
    repo = _repo(fake_ch_query_client)
    repo.llm_usage_analytics(
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        group_by=None,
    )
    query = fake_ch_query_client.last_query
    assert "coalesce(sum(llm_input_tokens), 0)" in query
    assert "coalesce(sum(llm_cost_usd), toDecimal64(0, 6))" in query


# -- _safe_identifier: the group_by/bucket allow-list guard ------------------


def test_safe_identifier_returns_mapped_value() -> None:
    assert _safe_identifier({"a": "col_a"}, "a") == "col_a"


def test_safe_identifier_rejects_unmapped_key() -> None:
    with pytest.raises(ValueError, match="Unsupported grouping/bucket key"):
        _safe_identifier({"a": "col_a"}, "'; DROP TABLE spans; --")
