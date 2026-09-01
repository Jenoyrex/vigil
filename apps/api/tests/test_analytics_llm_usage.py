"""Route-level tests for GET /v1/analytics/llm-usage. Uses
`fake_analytics_repository` (see tests/conftest.py) -- no real ClickHouse
involved.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient


def _flat_row(**overrides):
    row = {
        "llm_span_count": 10,
        "total_input_tokens": 1000,
        "total_output_tokens": 400,
        "total_tokens": 1400,
        "total_cost_usd": Decimal("0.034500"),
    }
    row.update(overrides)
    return row


def _auth_headers(active_api_key: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {active_api_key.raw_key}"}


def test_missing_auth_is_401(client: TestClient) -> None:
    response = client.get("/v1/analytics/llm-usage")
    assert response.status_code == 401


def test_project_id_comes_from_auth(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.llm_usage_analytics_result = [_flat_row()]
    client.get("/v1/analytics/llm-usage", headers=_auth_headers(active_api_key))
    [call] = fake_analytics_repository.llm_usage_analytics_calls
    assert call["project_id"] == active_api_key.project.id


def test_flat_response_shape_and_cost_as_string(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.llm_usage_analytics_result = [_flat_row()]
    response = client.get("/v1/analytics/llm-usage", headers=_auth_headers(active_api_key))
    assert response.status_code == 200
    body = response.json()
    assert body["llm_span_count"] == 10
    assert body["total_input_tokens"] == 1000
    assert body["total_output_tokens"] == 400
    assert body["total_tokens"] == 1400
    assert body["total_cost_usd"] == "0.034500"
    assert isinstance(body["total_cost_usd"], str)
    assert body["groups"] is None


def test_group_by_llm_model_returns_groups(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.llm_usage_analytics_result = [
        {"group_value": "gpt-4o-mini", **_flat_row()}
    ]
    response = client.get(
        "/v1/analytics/llm-usage",
        params={"group_by": "llm_model"},
        headers=_auth_headers(active_api_key),
    )
    body = response.json()
    assert body["groups"][0]["value"] == "gpt-4o-mini"
    assert body["groups"][0]["total_cost_usd"] == "0.034500"
    assert body["llm_span_count"] is None


def test_group_by_llm_provider_is_accepted(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.llm_usage_analytics_result = [
        {"group_value": "openai", **_flat_row()}
    ]
    response = client.get(
        "/v1/analytics/llm-usage",
        params={"group_by": "llm_provider"},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 200


def test_invalid_group_by_value_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    """span_type is not a valid group_by for LLM usage (unlike span analytics)."""
    response = client.get(
        "/v1/analytics/llm-usage",
        params={"group_by": "span_type"},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_window_wider_than_max_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get(
        "/v1/analytics/llm-usage",
        params={"start_time_from": "2026-08-01T00:00:00Z", "start_time_to": "2026-08-31T00:00:00Z"},
        headers=_auth_headers(active_api_key),
    )
    assert response.status_code == 422


def test_no_llm_spans_returns_zeroed_flat_response(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.llm_usage_analytics_result = []
    response = client.get("/v1/analytics/llm-usage", headers=_auth_headers(active_api_key))
    body = response.json()
    assert body["llm_span_count"] == 0
    assert body["total_cost_usd"] == "0"


def test_environment_filter_forwarded(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    fake_analytics_repository.llm_usage_analytics_result = [_flat_row()]
    client.get(
        "/v1/analytics/llm-usage",
        params={"environment": "production"},
        headers=_auth_headers(active_api_key),
    )
    [call] = fake_analytics_repository.llm_usage_analytics_calls
    assert call["environment"] == "production"


def test_clickhouse_unavailable_is_503(
    client: TestClient, active_api_key: SimpleNamespace, fake_analytics_repository
) -> None:
    from app.clickhouse.repository import ClickHouseUnavailableError

    fake_analytics_repository.fail_with = ClickHouseUnavailableError("down")
    response = client.get("/v1/analytics/llm-usage", headers=_auth_headers(active_api_key))
    assert response.status_code == 503
