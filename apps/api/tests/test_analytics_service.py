"""Pure unit tests for app.services.analytics: NaN handling, error-rate
calculation, group_by/bucket mutual exclusion, empty-result defaults, and
Decimal cost formatting. No FastAPI, no ClickHouse (uses FakeAnalyticsRepository
from tests/conftest.py for the two response-building entry points).
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.analytics import (
    QueryValidationError,
    _error_rate,
    _safe_float,
    llm_usage_analytics_response,
    span_analytics_response,
)

PROJECT_ID = uuid.uuid4()
FROM = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
TO = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)


def test_safe_float_converts_nan_to_zero() -> None:
    assert _safe_float(float("nan")) == 0.0


def test_safe_float_passes_through_normal_values() -> None:
    assert _safe_float(123.4) == 123.4


def test_error_rate_is_zero_when_no_spans() -> None:
    assert _error_rate(span_count=0, error_span_count=0) == 0.0


def test_error_rate_divides_errors_by_total() -> None:
    assert _error_rate(span_count=200, error_span_count=4) == 0.02


def test_group_by_and_bucket_together_raise_validation_error(fake_analytics_repository) -> None:
    with pytest.raises(QueryValidationError, match="mutually exclusive"):
        span_analytics_response(
            fake_analytics_repository,
            project_id=PROJECT_ID,
            start_time_from=FROM,
            start_time_to=TO,
            environment=None,
            resource=None,
            span_type=None,
            group_by="environment",
            bucket="hour",
            default_window_hours=24,
            max_window_days=7,
        )


def test_span_analytics_flat_response_when_no_rows(fake_analytics_repository) -> None:
    fake_analytics_repository.span_analytics_result = []
    response = span_analytics_response(
        fake_analytics_repository,
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by=None,
        bucket=None,
        default_window_hours=24,
        max_window_days=7,
    )
    assert response.span_count == 0
    assert response.error_rate == 0.0
    assert response.latency_ms.p50 == 0.0
    assert response.groups is None
    assert response.buckets is None


def test_span_analytics_flat_response_converts_nan_latency(fake_analytics_repository) -> None:
    fake_analytics_repository.span_analytics_result = [
        {
            "span_count": 0,
            "error_span_count": 0,
            "p50_latency_ms": float("nan"),
            "p90_latency_ms": float("nan"),
            "p99_latency_ms": float("nan"),
        }
    ]
    response = span_analytics_response(
        fake_analytics_repository,
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by=None,
        bucket=None,
        default_window_hours=24,
        max_window_days=7,
    )
    assert not math.isnan(response.latency_ms.p50)
    assert response.latency_ms.p50 == 0.0


def test_span_analytics_grouped_response_builds_groups(fake_analytics_repository) -> None:
    fake_analytics_repository.span_analytics_result = [
        {
            "group_value": "production",
            "span_count": 100,
            "error_span_count": 2,
            "p50_latency_ms": 10.0,
            "p90_latency_ms": 20.0,
            "p99_latency_ms": 30.0,
        }
    ]
    response = span_analytics_response(
        fake_analytics_repository,
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by="environment",
        bucket=None,
        default_window_hours=24,
        max_window_days=7,
    )
    assert response.groups[0].value == "production"
    assert response.groups[0].error_rate == 0.02
    assert response.span_count is None  # flat fields unset in grouped mode


def test_span_analytics_bucketed_response_builds_buckets(fake_analytics_repository) -> None:
    fake_analytics_repository.span_analytics_result = [
        {
            "bucket_start": datetime(2026, 8, 31, 0, 0, 0),
            "span_count": 10,
            "error_span_count": 0,
            "p50_latency_ms": 1.0,
            "p90_latency_ms": 2.0,
            "p99_latency_ms": 3.0,
        }
    ]
    response = span_analytics_response(
        fake_analytics_repository,
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        resource=None,
        span_type=None,
        group_by=None,
        bucket="day",
        default_window_hours=24,
        max_window_days=7,
    )
    assert response.buckets[0].span_count == 10
    assert response.buckets[0].bucket_start.tzinfo is not None  # naive CH read normalized


def test_llm_usage_flat_response_when_no_rows_defaults_to_zero(fake_analytics_repository) -> None:
    fake_analytics_repository.llm_usage_analytics_result = []
    response = llm_usage_analytics_response(
        fake_analytics_repository,
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        group_by=None,
        default_window_hours=24,
        max_window_days=7,
    )
    assert response.llm_span_count == 0
    assert response.total_cost_usd == "0"


def test_llm_usage_cost_is_serialized_as_decimal_precise_string(fake_analytics_repository) -> None:
    fake_analytics_repository.llm_usage_analytics_result = [
        {
            "llm_span_count": 2,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "total_tokens": 150,
            "total_cost_usd": Decimal("0.123456"),
        }
    ]
    response = llm_usage_analytics_response(
        fake_analytics_repository,
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        group_by=None,
        default_window_hours=24,
        max_window_days=7,
    )
    assert response.total_cost_usd == "0.123456"
    assert isinstance(response.total_cost_usd, str)


def test_llm_usage_grouped_response_builds_groups(fake_analytics_repository) -> None:
    fake_analytics_repository.llm_usage_analytics_result = [
        {
            "group_value": "gpt-4o-mini",
            "llm_span_count": 5,
            "total_input_tokens": 500,
            "total_output_tokens": 200,
            "total_tokens": 700,
            "total_cost_usd": Decimal("1.500000"),
        }
    ]
    response = llm_usage_analytics_response(
        fake_analytics_repository,
        project_id=PROJECT_ID,
        start_time_from=FROM,
        start_time_to=TO,
        environment=None,
        group_by="llm_model",
        default_window_hours=24,
        max_window_days=7,
    )
    assert response.groups[0].value == "gpt-4o-mini"
    assert response.groups[0].total_cost_usd == "1.500000"
    assert response.llm_span_count is None  # flat fields unset in grouped mode
