"""Route-level tests for GET /v1/traces/{trace_id}/spans/{span_id}. Uses
`fake_traces_query_repository` (see tests/conftest.py) -- no real
ClickHouse involved.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


def _span_row(**overrides):
    row = {
        "span_id": SPAN_ID,
        "parent_span_id": None,
        "name": "openai.chat.completion",
        "span_type": "llm",
        "resource": "checkout-service",
        "start_time": datetime(2026, 8, 31, 12, 0, 0),
        "end_time": datetime(2026, 8, 31, 12, 0, 1, 250000),
        "duration_ms": 1250,
        "status": "ok",
        "status_message": None,
        "input": '{"messages":[]}',
        "input_size_bytes": 16,
        "input_truncated": False,
        "output": None,
        "output_size_bytes": 0,
        "output_truncated": False,
        "attributes": {"llm.request.temperature": "0.7"},
        "attributes_truncated": False,
        "events.time": [],
        "events.name": [],
        "events.attributes": [],
        "events_truncated": False,
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "llm_input_tokens": 12,
        "llm_output_tokens": 8,
        "llm_total_tokens": 20,
        "llm_cost_usd": None,
        "environment": "production",
        "release": "v1.0.0",
    }
    row.update(overrides)
    return row


def _auth_headers(active_api_key: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {active_api_key.raw_key}"}


def test_missing_auth_is_401(client: TestClient) -> None:
    response = client.get(f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}")
    assert response.status_code == 401


def test_malformed_trace_id_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get(
        f"/v1/traces/not-hex/spans/{SPAN_ID}", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 422


def test_malformed_span_id_is_422(client: TestClient, active_api_key: SimpleNamespace) -> None:
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/too-short", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 422


def test_not_found_returns_404(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.get_span_result = []
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 404


def test_project_id_comes_from_auth(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.get_span_result = [_span_row()]
    client.get(f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}", headers=_auth_headers(active_api_key))
    [call] = fake_traces_query_repository.get_span_calls
    assert call["project_id"] == active_api_key.project.id
    assert call["trace_id"] == TRACE_ID
    assert call["span_id"] == SPAN_ID


def test_response_includes_content_attributes_and_llm_fields(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.get_span_result = [_span_row()]
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["span_id"] == SPAN_ID
    assert body["input"] == '{"messages":[]}'
    assert body["attributes"] == {"llm.request.temperature": "0.7"}
    assert body["llm_provider"] == "openai"
    assert body["llm_input_tokens"] == 12


def test_events_included_in_response(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.get_span_result = [
        _span_row(
            **{
                "events.time": [datetime(2026, 8, 31, 12, 0, 0, 500000)],
                "events.name": ["first_token"],
                "events.attributes": [{"k": "v"}],
            }
        )
    ]
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}", headers=_auth_headers(active_api_key)
    )
    events = response.json()["events"]
    assert events == [
        {"time": "2026-08-31T12:00:00.500000Z", "name": "first_token", "attributes": {"k": "v"}}
    ]


def test_llm_cost_usd_serialized_as_decimal_precise_string(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    from decimal import Decimal

    fake_traces_query_repository.get_span_result = [_span_row(llm_cost_usd=Decimal("0.000123"))]
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}", headers=_auth_headers(active_api_key)
    )
    assert response.json()["llm_cost_usd"] == "0.000123"


def test_start_date_hint_is_forwarded(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    fake_traces_query_repository.get_span_result = [_span_row()]
    client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}",
        params={"start_date": "2026-08-31"},
        headers=_auth_headers(active_api_key),
    )
    [call] = fake_traces_query_repository.get_span_calls
    assert str(call["start_date"]) == "2026-08-31"


def test_clickhouse_unavailable_is_503(
    client: TestClient, active_api_key: SimpleNamespace, fake_traces_query_repository
) -> None:
    from app.clickhouse.repository import ClickHouseUnavailableError

    fake_traces_query_repository.fail_with = ClickHouseUnavailableError("down")
    response = client.get(
        f"/v1/traces/{TRACE_ID}/spans/{SPAN_ID}", headers=_auth_headers(active_api_key)
    )
    assert response.status_code == 503
