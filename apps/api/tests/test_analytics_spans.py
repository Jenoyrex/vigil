"""Route-level tests for GET /v1/analytics/spans. Uses
`fake_analytics_repository` (see tests/conftest.py) -- no real ClickHouse
involved.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient


def _flat_row(**overrides):
    row = {
        "span_count": 100,
        "error_span_count": 2,
        "p50_latency_ms": 50.0,
        "p90_latency_ms": 150.0,
        "p99_latency_ms": 300.0,
    }
    row.update(overrides)
    return row


def _auth_headers(active_api_key: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {active_api_key.raw_key}"}


def test_missing_auth_is_401(client: TestClient) -> None:
    response = client.get("/v1/analytics/spans")
    assert response.status_code == 401


def test_project_id_comes_from_auth(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.span_analytics_result = [_flat_row()]
    client.get("/v1/analytics/spans", headers=_auth_headers(active_api_key))
    [call] = fake_analytics_repository.span_analytics_calls
    assert call["project_id"] == active_api_key.project.id


def test_flat_response_shape(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.span_analytics_result = [_flat_row()]
    response = client.get("/v1/analytics/spans", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    body = response.json()
    assert body["span_count"] == 100
    assert body["error_span_count"] == 2
    assert body["error_rate"] == 0.02
    assert body["latency_ms"] == {"p50": 50.0, "p90": 150.0, "p99": 300.0}
    assert body["groups"] is None
    assert body["buckets"] is None


def test_group_by_environment_returns_groups(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.span_analytics_result = [
        {"group_value": "production", **_flat_row()}
    ]
    response = client.get(
        "/v1/analytics/spans",
        params={"group_by": "environment"},
        headers=_auth_headers(active_api_key),
    )
    body = response.json()
    assert body["groups"][0]["value"] == "production"
    assert body["span_count"] is None


def test_bucket_hour_returns_buckets(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.span_analytics_result = [
        {"bucket_start": datetime(2026, 8, 31, 0, 0, 0), **_flat_row()}
    ]
    response = client.get(
        "/v1/analytics/spans", params={"bucket": "hour"}, headers=_auth_headers(active_api_key)
    )
    body = response.json()
    assert body["buckets"][0]["span_count"] == 100


def test_group_by_and_bucket_together_is_422(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    response = client.get(
        "/v1/analytics/spans",
        params={"group_by": "environment", "bucket": "hour"},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_invalid_group_by_value_is_422(
    client: TestClient, active_api_key: SimpleNamespace
) -> None:
    response = client.get(
        "/v1/analytics/spans",
        params={"group_by": "not-a-real-dimension"},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_invalid_bucket_value_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get(
        "/v1/analytics/spans", params={"bucket": "minute"}, headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 422


def test_group_by_result_capped_at_50(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    """The cap itself is enforced in SQL (see test_analytics_repository.py);
    this just confirms the route/service pass through whatever the
    repository returns without adding its own limit."""
    fake_analytics_repository.span_analytics_result = [
        {"group_value": f"env-{i}", **_flat_row()} for i in range(50)
    ]
    response = client.get(
        "/v1/analytics/spans",
        params={"group_by": "environment"},
        headers=_auth_headers(active_api_key),
    )
    assert len(response.json()["groups"]) == 50


def test_window_wider_than_max_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get(
        "/v1/analytics/spans",
        params={"start_time_from": "2026-08-01T00:00:00Z", "start_time_to": "2026-08-31T00:00:00Z"},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_filters_forwarded_to_repository(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.span_analytics_result = [_flat_row()]
    client.get(
        "/v1/analytics/spans",
        params={"environment": "production", "resource": "checkout-service", "span_type": "llm"},
        headers=_auth_headers(active_api_key),
    )
    [call] = fake_analytics_repository.span_analytics_calls
    assert call["environment"] == "production"
    assert call["resource"] == "checkout-service"
    assert call["span_type"] == "llm"


def test_clickhouse_unavailable_is_503(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    from app.clickhouse.repository import ClickHouseUnavailableError

    fake_analytics_repository.fail_with = ClickHouseUnavailableError("down")
    response = client.get("/v1/analytics/spans", headers=_auth_headers(active_api_key))
    assert response.status_code == 503
